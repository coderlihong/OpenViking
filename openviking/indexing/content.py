# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Private derived-content revisions and published URI reads through AGFS.

Raw upload originals are not retained. These blobs contain parsed/source text or images,
use the existing account-scoped AGFS encryption stack, and have no public URI.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager, contextmanager
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
    # ``observe_draft`` takes the publication write lock; keep it off the event
    # loop so a busy writer cannot stall every other coroutine (measured as
    # multi-second loop stalls on this call during fleet reindex).
    epoch = (
        await asyncio.to_thread(registry.observe_draft, ctx.account_id, uri)
        if registry
        else None
    )
    with draft_sources():
        raw = await fs.read_file_bytes(uri, ctx=ctx)
    reference = await archive_bytes(fs, raw, ctx)
    if epoch is not None:
        reference["draft_epoch"] = epoch
    return reference, raw


def _is_lock_conflict(exc: Exception) -> bool:
    # RAGFS auto-pathlock runs with a fixed 0s wait, so a concurrent writer on
    # the same blob path surfaces as an immediate lock-timeout internal error.
    # "file exists" covers the legacy CreateNew EEXIST leak before it maps to
    # a native AlreadyExists conflict.
    message = str(exc).lower()
    return "lock" in message and (
        "timed out" in message
        or "conflict" in message
        or "busy" in message
        or "file exists" in message
    )


async def archive_bytes(fs, raw, ctx):
    """Archive private derived metadata through the same account AGFS stack."""
    sha = hashlib.sha256(raw).hexdigest()
    path = blob_path(fs, ctx, sha)
    for directory in (path.rsplit("/", 2)[0], path.rsplit("/", 1)[0]):
        try:
            await fs._async_agfs.mkdir(directory)
        except Exception as exc:
            if "already exists" not in str(exc).lower() and not isinstance(exc, FileExistsError):
                if not _is_lock_conflict(exc):
                    raise
                # A sibling mkdir holds the directory lock; the blob write below
                # re-verifies the final state, so proceed without the mkdir.
    try:
        await fs._async_agfs.stat(path)
    except Exception as exc:
        if not is_not_found_error(exc) and not isinstance(exc, FileNotFoundError):
            raise
        try:
            await fs._async_agfs.write(path, raw)
        except Exception as exc:
            # Duplicate-content sources (e.g. repeated PDF placeholder images)
            # race here: the winner's write creates the blob while this caller
            # is between stat and write. The read-back below decides whether
            # this caller still needs its own write.
            if not _is_lock_conflict(exc) and not isinstance(exc, FileExistsError):
                raise
    # Read the blob back and verify it byte-for-byte. Two live races make a
    # single attempt unreliable: the caller that lost the directory lock skips
    # its write expecting a winner that has not landed yet (or a store cleaned
    # out from under a running build keeps stale visibility), which surfaces as
    # not-found; and two writers of the *same* content (same sha256) can be
    # observed mid-write, which surfaces as a byte mismatch. Both were reported
    # as file-level failures -- 15 "Failed to vectorize" plus "Immutable source
    # revision failed verification" on a case image.
    #
    # Not-found is repaired (the bytes are in hand); a mismatch is only re-read,
    # never overwritten: a blob whose content really is wrong is corruption, and
    # rewriting it would hide exactly the failure this verification exists to
    # report.
    stored = None
    for attempt in range(3):
        try:
            stored = fs._handle_agfs_read(await fs._async_agfs.read(path))
        except Exception as exc:
            if not is_not_found_error(exc) and not isinstance(exc, FileNotFoundError):
                raise
            stored = None
            try:
                await fs._async_agfs.write(path, raw)
            except Exception as exc:
                if not _is_lock_conflict(exc) and not isinstance(exc, FileExistsError):
                    raise
        if stored == raw:
            break
        if attempt < 2:
            await asyncio.sleep(0.2 * (attempt + 1))
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


@asynccontextmanager
async def source_write(uri, ctx, backend=None):
    """Hold a durable fence until the storage await confirms completion.

    Cancellation/transport failure cannot prove that the backend stopped.
    Keep an uncertain fence for verified recovery instead of expiring it.

    Each transition is a sqlite write transaction. Running them on the event
    loop put every concurrent request behind the single writer: the loop
    watchdog caught /health stalls of 30-45 s with the main thread parked in
    ``begin_write`` -> ``db`` (BEGIN IMMEDIATE) from this context manager, once
    per file write. They are offloaded to a worker thread; the wrapped operation
    still runs on the caller's loop.
    """
    registry = configured_store()
    if registry is None:
        yield
        return
    reference = backend.prepare_source_write(ctx.account_id, uri) if backend is not None and hasattr(backend, 'prepare_source_write') else None
    token = await asyncio.to_thread(registry.begin_write, ctx.account_id, uri, reference)
    try:
        if reference is not None:
            with backend.activate_source_write(reference):
                yield
        else:
            yield
    except BaseException:
        await asyncio.to_thread(registry.uncertain_write, token)
        raise
    else:
        await asyncio.to_thread(registry.finish_write, token)
