# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Materialize the remote image references an authored document points at.

A markdown chapter written by an upstream parser usually references its figures
by URL — the host that produced the document served the bytes at parse time, and
that host is frequently gone by ingestion time. OpenViking publishes documents
whose image references resolve to bytes the system stores itself, so each remote
reference is fetched once, run through the same acceptance policy as a local
file, and rewritten to a content-addressed sibling path.

Fetching is best-effort by design. A dead link, a non-public destination, or an
oversized body must not abort a document that has hundreds of working figures,
so every failure becomes a typed :class:`RemoteImageFailure` the caller reports
in its ingestion result instead of an exception.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Sequence, Tuple
from urllib.parse import unquote, urlsplit

from openviking.parse.image_validation import is_valid_image
from openviking.utils.network_guard import ensure_public_remote_target
from openviking_cli.exceptions import PermissionDeniedError
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

HTTP_SCHEMES = ("http", "https")
# ``image_validation`` refuses anything above this size, so an over-long body is
# never worth buffering.
MAX_FETCH_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_CONCURRENT_FETCHES = 8
USER_AGENT = "OpenViking/1.0 remote-chapter-image"

#: Failure reasons surfaced in an ingestion result. Keep this set closed so the
#: report stays machine-readable for the corpus owner.
FAILURE_REASONS = frozenset(
    {"blocked", "dns", "timeout", "http_status", "too_large", "not_image", "not_found"}
)

_FORMAT_SUFFIXES = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",
    "APNG": ".png",
    "MPO": ".jpg",
}
_TRUSTED_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
)


class RemoteImageFailure(Exception):
    """A remote reference that could not be turned into stored bytes."""

    def __init__(self, reason: str, url: str, detail: str = "") -> None:
        if reason not in FAILURE_REASONS:
            raise ValueError(f"Unknown remote image failure reason: {reason}")
        super().__init__(f"{reason}: {url}{f' ({detail})' if detail else ''}")
        self.reason = reason
        self.url = url
        self.detail = detail

    def describe(self) -> dict:
        return {"url": self.url, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RemoteImage:
    """Validated bytes plus the content-addressed name they should be stored under."""

    url: str
    data: bytes
    suffix: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def filename(self) -> str:
        """Bare stored name: the digest makes re-ingestion of the same image a no-op."""
        return f"{self.digest}{self.suffix}"


def normalize_reference(reference: str) -> str:
    """Return the bare URL an authored markdown reference denotes."""
    value = (reference or "").strip()
    if len(value) >= 2 and value.startswith("<") and value.endswith(">"):
        value = value[1:-1].strip()
    return value


def scheme_of(reference: str) -> str:
    try:
        return urlsplit(normalize_reference(reference)).scheme.lower()
    except ValueError:
        return ""


def is_fetchable(reference: str) -> bool:
    """True when a fetch (or an inline decode) can produce the image bytes."""
    if scheme_of(reference) in HTTP_SCHEMES:
        return True
    return normalize_reference(reference).lower().startswith("data:")


def sniff_suffix(data: bytes, reference: str) -> str:
    """Pick the stored extension: the actual format first, the authored one second."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            suffix = _FORMAT_SUFFIXES.get((image.format or "").upper())
        if suffix:
            return suffix
    except Exception:
        pass
    path_suffix = Path(unquote(urlsplit(normalize_reference(reference)).path)).suffix.lower()
    if path_suffix in {".jfif", ".jpe"}:
        return ".jpg"
    if path_suffix in _TRUSTED_SUFFIXES:
        return path_suffix
    return ".png"


def _require_acceptable(data: bytes, url: str) -> None:
    """Apply the shared image acceptance policy, reporting refusal as a failure."""
    name = normalize_reference(url).rsplit("/", 1)[-1] or "image"
    if not is_valid_image(data, Path(name)):
        raise RemoteImageFailure("not_image", url, "rejected by the image quality gate")


def decode_data_uri(reference: str) -> RemoteImage:
    """Materialize an inline ``data:image/...;base64,`` occurrence (no network)."""
    url = normalize_reference(reference)
    head, _, payload = url.partition(",")
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise RemoteImageFailure(
            "not_image", url, f"undecodable data URI ({exc.__class__.__name__})"
        ) from exc
    if len(data) > MAX_FETCH_BYTES:
        raise RemoteImageFailure("too_large", url, f"{len(data)} bytes")
    if "image/" not in head and not _looks_like_image(data):
        raise RemoteImageFailure("not_image", url, f"media type {head or 'unknown'!r}")
    _require_acceptable(data, url)
    return RemoteImage(url=url, data=data, suffix=sniff_suffix(data, url))


def _looks_like_image(data: bytes) -> bool:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:
        return False


async def _stream(client, url: str, max_bytes: int) -> bytes:
    import httpx

    try:
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
            if response.status_code >= 400:
                raise RemoteImageFailure("http_status", url, f"HTTP {response.status_code}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise RemoteImageFailure("too_large", url, f"{declared} bytes declared")
            chunks: List[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise RemoteImageFailure("too_large", url, f"over {max_bytes} bytes")
                chunks.append(chunk)
    except RemoteImageFailure:
        raise
    except httpx.TimeoutException as exc:
        raise RemoteImageFailure("timeout", url, exc.__class__.__name__) from exc
    except httpx.TransportError as exc:
        reason = "dns" if isinstance(exc, httpx.NetworkError) else "timeout"
        raise RemoteImageFailure(reason, url, exc.__class__.__name__) from exc
    data = b"".join(chunks)
    if not data:
        raise RemoteImageFailure("not_found", url, "empty body")
    if len(data) > max_bytes:
        raise RemoteImageFailure("too_large", url, f"{len(data)} bytes")
    return data


async def resolve_remote_image(
    reference: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_FETCH_BYTES,
    client=None,
) -> RemoteImage:
    """Fetch (or decode) *reference* and return validated, storable bytes."""
    url = normalize_reference(reference)
    if url.lower().startswith("data:"):
        return await asyncio.to_thread(decode_data_uri, url)
    if scheme_of(url) not in HTTP_SCHEMES:
        raise RemoteImageFailure("not_found", url, "not a fetchable image reference")
    try:
        ensure_public_remote_target(url)
    except PermissionDeniedError as exc:
        raise RemoteImageFailure("blocked", url, str(exc)) from exc
    import httpx

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
    try:
        data = await _stream(client, url, max_bytes)
    finally:
        if owns_client:
            await client.aclose()
    await asyncio.to_thread(_require_acceptable, data, url)
    return RemoteImage(url=url, data=data, suffix=sniff_suffix(data, url))


async def resolve_remote_images(
    references: Iterable[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_FETCH_BYTES,
    concurrency: int = MAX_CONCURRENT_FETCHES,
    client_factory=None,
) -> Mapping[str, object]:
    """Resolve every distinct remote reference, deduplicated and bounded.

    Keys are normalized references so a caller can look up the string it found in
    the document. Values are :class:`RemoteImage` or :class:`RemoteImageFailure`:
    failures are data, never exceptions, because one dead host must not cost a
    document its other figures. *client_factory* lets a caller (or a test) supply
    the transport; production uses the shared httpx defaults.
    """
    urls = list(dict.fromkeys(normalize_reference(ref) for ref in references if ref.strip()))
    if not urls:
        return {}
    import httpx

    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: dict = {}
    if client_factory is None:
        client_factory = lambda: httpx.AsyncClient(  # noqa: E731 - single-use default
            timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    async with client_factory() as client:

        async def _one(url: str):
            async with semaphore:
                try:
                    return url, await resolve_remote_image(
                        url, timeout=timeout, max_bytes=max_bytes, client=client
                    )
                except RemoteImageFailure as failure:
                    logger.warning(
                        f"[RemoteImages] Could not materialize {url}: {failure.reason} "
                        f"{failure.detail}"
                    )
                    return url, failure

        results.update(await asyncio.gather(*(_one(url) for url in urls)))
    return dict(results)


def collect_remote_spans(content: str) -> Sequence[Tuple[int, int, str]]:
    """Locate every remote image reference in *content*, skipping protected code.

    Covers both authored forms — ``![alt](https://…)`` and ``<img src="https://…">``
    — and returns ``(start, end, reference)`` spans for the URL group, so the
    caller replaces only the reference and keeps the surrounding syntax.
    """
    from openviking.parse.image_rewrite import (
        HTML_IMG_PATTERN,
        _IMAGE_PATTERN,
        _protected_ranges,
    )

    protected = _protected_ranges(content)
    candidates = [(match.start(2), match.end(2), match.group(2)) for match in _IMAGE_PATTERN.finditer(content)]
    for match in HTML_IMG_PATTERN.finditer(content):
        candidates.append((match.start(2), match.end(2), match.group(2)))
    spans = []
    for start, end, reference in candidates:
        if any(begin <= start < stop for begin, stop in protected):
            continue
        if is_fetchable(reference):
            spans.append((start, end, normalize_reference(reference)))
    return spans


def apply_replacements(content: str, replacements: Iterable[Tuple[int, int, str]]) -> str:
    """Apply non-overlapping ``(start, end, text)`` edits back-to-front."""
    rewritten = content
    for start, end, text in sorted(replacements, key=lambda item: item[0], reverse=True):
        rewritten = rewritten[:start] + text + rewritten[end:]
    return rewritten
