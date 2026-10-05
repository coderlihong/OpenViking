# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Private derived-content revisions and published URI reads through AGFS.

Raw upload originals are not retained. These blobs contain parsed/source text or images,
use the existing account-scoped AGFS encryption stack, and have no public URI.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from openviking.indexing.publication import configured_store
from openviking.server.error_mapping import is_not_found_error
from openviking_cli.exceptions import InvalidArgumentError, NotFoundError

_draft = ContextVar("index_draft_source_reads", default=False)


@contextmanager
def draft_sources():
    token = _draft.set(True)
    try:
        yield
    finally:
        _draft.reset(token)


def draft_source_job(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with draft_sources():
            return await function(*args, **kwargs)

    return wrapped


def blob_path(fs, ctx, sha):
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Invalid source revision digest")
    return (
        fs._uri_to_path("viking://", ctx=ctx).rstrip("/") + "/_system/index_blobs/" + sha + ".bin"
    )


async def archive_source(fs, uri, ctx):
    # Authorized read of the latest working source; private data never lives in
    # the SQLite metadata database or a queue message.
    registry = configured_store()
    epoch = registry.observe_draft(ctx.account_id, uri) if registry else None
    with draft_sources():
        raw = await fs.read_file_bytes(uri, ctx=ctx)
    reference = await archive_bytes(fs, raw, ctx)
    if epoch is not None:
        reference["draft_epoch"] = epoch
    return reference, raw


async def archive_bytes(fs, raw, ctx):
    """Archive private derived metadata through the same account AGFS stack."""
    sha = hashlib.sha256(raw).hexdigest()
    path = blob_path(fs, ctx, sha)
    for directory in (path.rsplit("/", 2)[0], path.rsplit("/", 1)[0]):
        try:
            await fs._async_agfs.mkdir(directory)
        except Exception as exc:
            if "already exists" not in str(exc).lower() and not isinstance(exc, FileExistsError):
                raise
    try:
        await fs._async_agfs.stat(path)
    except Exception as exc:
        if not is_not_found_error(exc) and not isinstance(exc, FileNotFoundError):
            raise
        await fs._async_agfs.write(path, raw)
    stored = fs._handle_agfs_read(await fs._async_agfs.read(path))
    if stored != raw:
        raise RuntimeError("Immutable source revision failed verification")
    reference = {"sha256": sha, "size": len(raw)}
    return reference


async def projected_content(fs, uri, ctx):
    if _draft.get():
        return None
    registry = configured_store()
    if registry is None:
        return None
    from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton

    if not OpenVikingConfigSingleton._instance.embedding.chunking.publish_content:
        return None
    with registry.source_reference(ctx.account_id, uri) as reference:
        if reference is None:
            return None
        # A retained revision must not resurrect a deleted source. The caller
        # has checked current source ACL before this helper reads private bytes.
        try:
            stat = await fs._async_agfs.stat(fs._uri_to_path(uri, ctx=ctx))
        except Exception as exc:
            if is_not_found_error(exc) or isinstance(exc, FileNotFoundError):
                raise NotFoundError(uri, "file") from exc
            raise
        if isinstance(stat, dict) and stat.get("isDir", False):
            raise InvalidArgumentError("Published source URI is now a directory")
        raw = fs._handle_agfs_read(
            await fs._async_agfs.read(blob_path(fs, ctx, reference["sha256"]))
        )
        if len(raw) != reference["size"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise RuntimeError("Published source revision is missing or corrupted")
        return raw


def invalidate_draft(uri, ctx):
    registry = configured_store()
    if registry is not None:
        registry.invalidate_draft(ctx.account_id, uri)


@contextmanager
def source_write(uri, ctx, backend=None):
    """Hold a durable fence until the storage await confirms completion.

    Cancellation/transport failure cannot prove that the backend stopped.
    Keep an uncertain fence for verified recovery instead of expiring it.
    """
    registry = configured_store()
    if registry is None:
        yield
        return
    reference = backend.prepare_source_write(ctx.account_id, uri) if backend is not None and hasattr(backend, 'prepare_source_write') else None
    token = registry.begin_write(ctx.account_id, uri, backend_ref=reference)
    try:
        if reference is not None:
            with backend.activate_source_write(reference):
                yield
        else:
            yield
    except BaseException:
        registry.uncertain_write(token)
        raise
    else:
        registry.finish_write(token)
