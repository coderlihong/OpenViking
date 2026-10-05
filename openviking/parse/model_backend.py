# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Streaming client for the Docker parser worker artifact protocol."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from openviking.parse.model_artifact import safe_extract, validate_document, validate_options


def _stage_input(source: Path, target: Path) -> str:
    """Freeze the bytes sent to the worker and fingerprint that exact copy."""
    digest = hashlib.sha256()
    with source.open("rb") as original, target.open("xb") as staged:
        while chunk := original.read(1024 * 1024):
            staged.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ParsedDocument:
    markdown: str
    assets_dir: Path
    metadata: dict[str, Any]
    temporary: tempfile.TemporaryDirectory

    def close(self) -> None:
        self.temporary.cleanup()


class ParseModelBackend:
    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "",
        timeout: float = 7200,
        max_artifact_bytes: int = 10 * 1024**3,
        transport=None,
    ):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout <= 0 or isinstance(max_artifact_bytes, bool) or
                not isinstance(max_artifact_bytes, int) or max_artifact_bytes < 1):
            raise ValueError("Parser timeout and artifact limit must be positive")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_artifact_bytes = max_artifact_bytes
        self.transport = transport

    async def parse(
        self,
        source: Path,
        *,
        backend: str,
        mode: str = "auto",
        options: dict[str, Any] | None = None,
        expected_pages: int,
    ) -> ParsedDocument:
        import json

        if isinstance(expected_pages, bool) or not isinstance(expected_pages, int) or expected_pages < 1:
            raise ValueError("Expected parser page count must be positive")
        normalized = validate_options(backend, mode, {} if options is None else options)
        temporary = tempfile.TemporaryDirectory(prefix="openviking-parser-")
        root = Path(temporary.name)
        deadline = time.monotonic() + self.timeout
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        job_id = None
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            headers=headers,
            timeout=min(self.timeout, 300),
            transport=self.transport,
        ) as client:
            try:
                staged_source = root / "input.pdf"
                source_sha256 = await asyncio.to_thread(_stage_input, source, staged_source)
                with staged_source.open("rb") as stream:
                    response = await client.post(
                        "/v1/jobs",
                        files={"file": (source.name, stream, "application/pdf")},
                        data={"backend": backend, "mode": mode, "options": json.dumps(normalized)},
                    )
                response.raise_for_status()
                job_id = response.json()["job_id"]
                if (
                    not isinstance(job_id, str)
                    or not job_id
                    or re.fullmatch(r"[0-9a-f]+(?:-[0-9a-f]+)*", job_id) is None
                ):
                    raise ValueError("Invalid parser job identifier")
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Parser job deadline exceeded")
                    status = await client.get(f"/v1/jobs/{job_id}")
                    status.raise_for_status()
                    body = status.json()
                    if not isinstance(body, dict) or "status" not in body:
                        raise ValueError("Invalid parser job status response")
                    if body["status"] == "completed":
                        break
                    if body["status"] in {"failed", "cancelled"}:
                        raise RuntimeError(
                            f"Parser {backend} {body['status']}: {body.get('error', '')}"
                        )
                    if body["status"] not in {"queued", "running"}:
                        raise ValueError("Unknown parser job state")
                    await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
                archive = root / "artifact.zip"
                received = 0
                async with client.stream("GET", f"/v1/jobs/{job_id}/artifact") as result:
                    result.raise_for_status()
                    with archive.open("wb") as output:
                        async for data in result.aiter_bytes():
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Parser artifact download deadline exceeded")
                            received += len(data)
                            if received > self.max_artifact_bytes:
                                raise ValueError(
                                    "Parser artifact download exceeds configured limit"
                                )
                            output.write(data)
                await asyncio.to_thread(
                    safe_extract, archive, root / "document", self.max_artifact_bytes
                )
                record = await asyncio.to_thread(
                    validate_document,
                    root / "document",
                    backend=backend,
                    mode=mode,
                    options=normalized,
                    expected_pages=expected_pages,
                )
                preservation = record.get("source_image_preservation")
                if preservation is not None and preservation.get("source_pdf_sha256") != source_sha256:
                    raise ValueError("Parser image provenance does not match submitted PDF bytes")
                metadata = {
                    k: v for k, v in record.items() if k not in {"markdown", "blocks", "assets"}
                }
                metadata["effective_backend"] = record["backend"]
                metadata["effective_mode"] = record["mode"]
                metadata["page_identity_verified"] = record["coverage"].get("page_ids") == list(range(expected_pages))
                metadata["asset_count"] = len(record["assets"])
                metadata["block_count"] = len(record["blocks"])
                metadata["submitted_pdf_sha256"] = source_sha256
                return ParsedDocument(record["markdown"], root / "document", metadata, temporary)
            except BaseException:
                if job_id:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(client.delete(f"/v1/jobs/{job_id}")), 5
                        )
                    except (Exception, asyncio.CancelledError):
                        pass
                temporary.cleanup()
                raise
