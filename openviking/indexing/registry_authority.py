# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Authenticated, single-host authority for publication repository commands.

No SQL or arbitrary Python method is exposed. Lost replies are uncertain writes:
clients fail closed and must reconcile rather than automatically replay commands.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import socket
import socketserver
import stat

if os.name == "posix":
    import fcntl
import sqlite3
from functools import lru_cache
from contextlib import closing

from openviking.indexing.publication import PublicationStore
from openviking.indexing.registry_lexical import LEXICAL_COMMANDS
from openviking.indexing.registry_gc import GC_COMMANDS
from openviking.indexing.registry_metadata import METADATA_COMMANDS
from openviking.indexing.registry_domains import DOMAIN_COMMANDS
from openviking.indexing.write_recovery import RECOVERY_COMMANDS
from openviking_cli.exceptions import (OpenVikingError, ConflictError, NotFoundError,
    PermissionDeniedError, FailedPreconditionError, InvalidArgumentError)

MAX_FRAME = 4 * 1024 * 1024
COMMANDS = (
    "begin", "observe_draft", "begin_write", "finish_write", "uncertain_write",
    "pending_writes", "invalidate_draft", "begin_batch", "fail_batch", "seal_batch",
    "batch_state", "can_write", "seal", "acknowledge", "fail", "state", "capture",
    "release", "renew", "protected_content_revisions", "retired_candidates",
    "tombstone", "clear_tombstone", "tombstones", "is_tombstoned", "sources",
) + DOMAIN_COMMANDS + RECOVERY_COMMANDS + LEXICAL_COMMANDS + METADATA_COMMANDS + GC_COMMANDS + (
    "job_create", "job_get", "job_list", "job_claim", "job_heartbeat", "job_checkpoint",
    "job_parse_acquire", "job_parse_release", "job_parse_quarantine_resolve",
    "job_complete", "job_fail", "job_cancel", "job_recover",
    "job_publication_status", "job_publication_ready",
    "job_publication_seal",
)


class AuthorityUnavailable(RuntimeError):
    """No local fallback or automatic retry is safe after a lost response."""


def _token(path):
    source = Path(path)
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError("Authority token must be a regular file owned by the current user")
    if info.st_mode & 0o077:
        raise ValueError("Authority token file must be private (mode 0600)")
    value = source.read_text().strip()
    if len(value) < 32:
        raise ValueError("Authority token must contain at least 32 characters")
    return value


def _encode(value):
    if isinstance(value, (set, tuple)):
        return {"__registry_type__": type(value).__name__, "items": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def _decode(value):
    if isinstance(value, dict):
        if value.get("__registry_type__") == "set":
            return set(_decode(v) for v in value["items"])
        if value.get("__registry_type__") == "tuple":
            return tuple(_decode(v) for v in value["items"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


class AuthorityRepository:
    """PublicationStore-compatible command boundary, without database access."""
    def __init__(self, endpoint, token_file, timeout=5):
        self.endpoint = str(Path(endpoint).resolve())
        self.path = "authority+unix://" + self.endpoint
        self._secret = _token(token_file)
        self.timeout = timeout

    def db(self):
        raise RuntimeError("Direct SQL is unavailable on authority workers; migrate to repository commands")

    visibility = PublicationStore.visibility
    source_reference = PublicationStore.source_reference

    def _call(self, command, args, kwargs):
        request = json.dumps({"token": self._secret, "command": command, "args": _encode(args), "kwargs": _encode(kwargs)}).encode() + b"\n"
        if len(request) > MAX_FRAME:
            raise ValueError("Registry request exceeds frame limit")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(self.timeout)
                channel.connect(self.endpoint)
                channel.sendall(request)
                with channel.makefile("rb") as stream:
                    raw = stream.readline(MAX_FRAME + 1)
                if not raw.endswith(b"\n") or len(raw) > MAX_FRAME:
                    raise ValueError("Invalid authority response frame")
                response = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise AuthorityUnavailable("Registry authority unavailable; outcome may be uncertain; no fallback") from exc
        if response.get("error"):
            code, details = response.get("code"), response.get("details", {})
            if code == "NOT_FOUND":
                raise NotFoundError(details.get("resource", ""), details.get("type", "resource"), details.get("reason"))
            error_types = {"CONFLICT": ConflictError, "PERMISSION_DENIED": PermissionDeniedError,
                           "FAILED_PRECONDITION": FailedPreconditionError, "INVALID_ARGUMENT": InvalidArgumentError}
            if code in error_types:
                raise error_types[code](response["error"])
            if code:
                raise OpenVikingError(response["error"], code=code, details=details)
            kind = response.get("kind")
            if kind == "ValueError":
                raise ValueError(response["error"])
            raise RuntimeError(response["error"])
        return _decode(response["result"])


def _proxy(command):
    def invoke(self, *args, **kwargs):
        return self._call(command, args, kwargs)
    invoke.__name__ = command
    return invoke


for _command in COMMANDS:
    setattr(AuthorityRepository, _command, _proxy(_command))


@lru_cache(maxsize=8)
def authority_repository(endpoint, token_file):
    return AuthorityRepository(endpoint, token_file)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(MAX_FRAME + 1)
            if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                raise ValueError("Invalid registry request frame")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("Registry request must be an object")
            if not hmac.compare_digest(str(request.get("token", "")), self.server.secret):
                raise ValueError("Registry authentication failed")
            command = request.get("command")
            if command not in COMMANDS:
                raise ValueError("Unsupported registry command")
            # dispatch contains only bound, explicitly enumerated repository commands.
            args, kwargs = _decode(request.get("args", [])), _decode(request.get("kwargs", {}))
            if not isinstance(args, (list, tuple)) or not isinstance(kwargs, dict):
                raise ValueError("Invalid registry command arguments")
            result = self.server.dispatch[command](*args, **kwargs)
            response = {"result": _encode(result)}
        except OpenVikingError as exc:
            response = {"error": exc.message, "code": exc.code, "details": exc.details}
        except (ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as exc:
            response = {"kind": type(exc).__name__, "error": str(exc)}
        payload = json.dumps(response).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            payload = b'{"kind":"RuntimeError","error":"Registry response exceeds frame limit"}\n'
        self.wfile.write(payload)


class RegistryAuthority(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, endpoint, database, token_file, *, recovery_backends=None, recovery_authorizer=None):
        if os.name != "posix":
            raise RuntimeError("Registry Unix socket authority requires a POSIX host")
        self.secret = _token(token_file)
        database = str(Path(database).resolve())
        endpoint = str(Path(endpoint).resolve())
        if len(os.fsencode(endpoint)) > 103:
            raise ValueError("Authority Unix socket path must be at most 103 bytes")
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        parent = Path(endpoint).parent
        if not parent.is_dir() or parent.stat().st_mode & 0o077:
            raise ValueError("Authority socket requires an existing private directory (mode 0700)")
        self._owner_lock = open(database + ".authority.lock", "a+b")
        try:
            fcntl.flock(self._owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.repository = PublicationStore(database, authority_owned=True)
            self.repository.configure_write_recovery(backends=recovery_backends or {}, authorizer=recovery_authorizer)
            with closing(sqlite3.connect(database)) as db:
                db.execute("PRAGMA journal_mode=WAL")
            self.dispatch = {name: getattr(self.repository, name) for name in COMMANDS}
            # A stale endpoint may only be reclaimed after the database ownership lock.
            if os.path.lexists(endpoint):
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(endpoint)
                    except ConnectionRefusedError:
                        os.unlink(endpoint)
                    else:
                        raise RuntimeError("Authority socket already has a live owner")
            super().__init__(endpoint, _Handler)
            os.chmod(endpoint, 0o600)
        except BaseException:
            self._owner_lock.close()
            raise

    def backup(self, destination):
        """Operator-local consistent SQLite snapshot; never exposed to workers."""
        if Path(destination).resolve() == Path(self.repository.path):
            raise ValueError("Backup destination must differ from authority database")
        with closing(sqlite3.connect(self.repository.path)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        self._owner_lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    with RegistryAuthority(args.socket, args.database, args.token_file) as authority:
        try:
            authority.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
