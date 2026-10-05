# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Portable, validated document artifacts shared by parser workers and OpenViking."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import stat
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from markdown_it import MarkdownIt

_HTML_IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_HTML_IMAGE_SRC = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")

BACKEND_MODES = {
    # Only modes actually passed to the selected engine are advertised. Native
    # TeleOCR/Logics pipelines currently have no request-level mode switch.
    "mineru": {"auto", "text", "ocr"},
    "teleocr": {"auto"},
    "logics": {"auto"},
}


def validate_options(backend: str, mode: str, options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(backend, str) or not isinstance(mode, str) or backend not in BACKEND_MODES or mode not in BACKEND_MODES[backend]:
        raise ValueError(f"UNSUPPORTED_PARSE_MODE: {backend}/{mode}")
    if not isinstance(options, dict):
        raise ValueError("Parser options must be an object")
    allowed = {
        "mineru": {"quality_tier"},
        "teleocr": {"layout_mode"},
        "logics": {"window_size", "max_model_len", "enforce_eager"},
    }[backend]
    if options.keys() - allowed:
        raise ValueError(f"Unsupported {backend} options: {sorted(options.keys() - allowed)}")
    result = dict(options)
    if backend == "mineru":
        result.setdefault("quality_tier", "standard")
        if not isinstance(result["quality_tier"], str) or result["quality_tier"] not in {"standard", "advanced"}:
            raise ValueError("Unsupported MinerU quality_tier: only standard/advanced use the pinned model revision")
    if backend == "teleocr":
        result.setdefault("layout_mode", "Detection")
        if not isinstance(result["layout_mode"], str) or result["layout_mode"] not in {"Detection", "Segmentation"}:
            raise ValueError("Invalid TeleOCR layout_mode")
    if backend == "logics":
        result.setdefault("window_size", 2)
        result.setdefault("max_model_len", 32768)
        result.setdefault("enforce_eager", True)
        for key, low, high in [("window_size", 1, 8), ("max_model_len", 16385, 32768)]:
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Invalid Logics {key}")
        if not isinstance(result["enforce_eager"], bool):
            raise ValueError("Invalid Logics enforce_eager")
    return result


def options_digest(backend: str, mode: str, options: dict[str, Any]) -> str:
    value = json.dumps([backend, mode, options], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def safe_extract(archive_path: Path, destination: Path, max_bytes: int = 10 * 1024**3) -> None:
    """Extract bounded regular files only; reject collisions, links and traversal."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("Parser artifact limit must be positive")
    if destination.is_symlink():
        raise ValueError("Unsafe parser extraction destination")
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    total = 0
    seen = set()
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (not entry.filename or "\x00" in entry.filename or name.is_absolute() or ".." in name.parts or "\\" in entry.filename
                    or name.as_posix() != entry.filename.rstrip("/")):
                raise ValueError("Unsafe parser artifact path")
            if entry.is_dir():
                continue
            key = name.as_posix().casefold()
            file_type = stat.S_IFMT(entry.external_attr >> 16)
            if key in seen or file_type not in {0, stat.S_IFREG}:
                raise ValueError("Duplicate or linked parser artifact")
            seen.add(key)
            total += entry.file_size
            if total > max_bytes or len(seen) > 100000:
                raise ValueError("Parser artifact exceeds configured limit")
            target = destination / name
            if not target.resolve().is_relative_to(root) or target.is_symlink() or target.exists():
                raise ValueError("Unsafe or existing parser artifact target")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def ordered_blocks(markdown: str) -> list[dict[str, Any]]:
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    offsets = [0]
    for line in markdown.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    blocks = []
    cursor = 0
    for token in tokens:
        if not token.map or token.level != 0 or token.nesting == -1:
            continue
        end = offsets[token.map[1]]
        if end <= cursor:
            continue
        text = markdown[cursor:end]
        blocks.append(
            {
                "block_id": f"block_{len(blocks):06d}",
                "type": token.type.removesuffix("_open"),
                "text_or_markdown": text,
                "heading_level": int(token.tag[1:]) if token.type == "heading_open" else None,
            }
        )
        cursor = end
    if cursor < len(markdown):
        blocks.append(
            {
                "block_id": f"block_{len(blocks):06d}",
                "type": "text",
                "text_or_markdown": markdown[cursor:],
            }
        )
    if "".join(block["text_or_markdown"] for block in blocks) != markdown:
        raise ValueError("Normalized block coverage mismatch")
    return blocks


def package_document(
    markdown: str,
    assets_dir: Path,
    destination: Path,
    *,
    backend: str,
    version: str,
    model_revision: str,
    mode: str,
    options: dict[str, Any],
    input_pages: int,
    processed_pages: int,
    page_ids: list[int] | None = None,
) -> dict[str, Any]:
    if (not isinstance(markdown, str) or not markdown.strip() or
            any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in (input_pages, processed_pages)) or
            input_pages != processed_pages):
        raise ValueError("Empty or incomplete parser output")
    if page_ids is not None and (not isinstance(page_ids, list) or
            any(isinstance(index, bool) or not isinstance(index, int) for index in page_ids) or
            page_ids != list(range(input_pages))):
        raise ValueError("Incomplete parser page identity coverage")
    options = validate_options(backend, mode, options)
    assets = []
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for image in sorted(assets_dir.rglob("*")):
            if not image.is_file() or image.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
                ".bmp",
                ".tif",
                ".tiff",
                ".svg",
            }:
                continue
            if image.is_symlink():
                raise ValueError("Parser assets cannot be symlinks")
            relative = image.relative_to(assets_dir).as_posix()
            with image.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            assets.append(
                {
                    "asset_id": digest,
                    "path": relative,
                    "size": image.stat().st_size,
                    "sha256": digest,
                }
            )
            archive.write(image, relative)
        record = {
            "artifact_version": 1,
            "backend": backend,
            "version": version,
            "model_revision": model_revision,
            "mode": mode,
            "options": options,
            "options_digest": options_digest(backend, mode, options),
            "coverage": {
                "input_pages": input_pages,
                "processed_pages": processed_pages,
                "complete": True,
                **({"page_ids": page_ids} if page_ids is not None else {}),
            },
            "markdown": markdown,
            "blocks": ordered_blocks(markdown),
            "assets": assets,
            "warnings": [],
            "resume_granularity": "document",
        }
        archive.writestr("parsed.json", json.dumps(record, ensure_ascii=False))
    return record


def _strip_unavailable_images(markdown: str, declared: set[str]) -> tuple[str, list[str]]:
    """Drop only image references that resolve to no declared asset.

    The logics backend emits synthetic ``imgs/img_in_image_box_*.jpg`` placeholders
    for regions it never crops to files, so those assets cannot exist. Real figures
    are always shipped, so a declared reference is never dropped. Fenced code and
    ``srcset`` violations are left for validation to keep rejecting.
    """
    stripped: list[str] = []

    def replace_html(match: re.Match[str]) -> str:
        tag = match.group(0)
        if "srcset" in tag.casefold():
            return tag
        source = _HTML_IMAGE_SRC.search(tag)
        src = next((group for group in source.groups() if group is not None), "") if source else ""
        if not src or _image_path(src) in declared:
            return tag
        stripped.append(src)
        return ""

    def replace_markdown(match: re.Match[str]) -> str:
        src = match.group(1)
        if src.startswith("<") and src.endswith(">"):
            src = src[1:-1]
        if _image_path(src) in declared:
            return match.group(0)
        stripped.append(src)
        return ""

    lines, fenced = [], False
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            line = _MARKDOWN_IMAGE.sub(replace_markdown, _HTML_IMAGE_TAG.sub(replace_html, line))
        lines.append(line)
    return "".join(lines), sorted(set(stripped))


def validate_document(
    root: Path, *, backend: str, mode: str, options: dict[str, Any], expected_pages: int
) -> dict[str, Any]:
    if isinstance(expected_pages, bool) or not isinstance(expected_pages, int) or expected_pages < 1:
        raise ValueError("Expected parser page count must be positive")
    if root.is_symlink() or (root / "parsed.json").is_symlink():
        raise ValueError("Unsafe parser document root")
    record = json.loads((root / "parsed.json").read_text())
    if not isinstance(record, dict):
        raise ValueError("Parser artifact must be an object")
    if (
        record.get("artifact_version") != 1
        or record.get("backend") != backend
        or record.get("mode") != mode
    ):
        raise ValueError("Parser artifact identity mismatch")
    if not record.get("version") or not record.get("model_revision"):
        raise ValueError("Parser artifact has no reproducible version")
    expected_digest = options_digest(backend, mode, validate_options(backend, mode, options))
    if (record.get("options_digest") != expected_digest or
            record.get("options") != validate_options(backend, mode, options)):
        raise ValueError("Parser artifact options mismatch")
    coverage = record.get("coverage", {})
    if (
        not isinstance(coverage, dict)
        or any(isinstance(coverage.get(key), bool) or not isinstance(coverage.get(key), int)
               for key in ("input_pages", "processed_pages"))
        or coverage.get("complete") is not True
        or coverage.get("input_pages") != expected_pages
        or coverage.get("processed_pages") != expected_pages
    ):
        raise ValueError("Incomplete parser page coverage")
    page_ids = coverage.get("page_ids")
    if page_ids is not None and (not isinstance(page_ids, list) or
            any(isinstance(index, bool) or not isinstance(index, int) for index in page_ids) or
            page_ids != list(range(expected_pages))):
        raise ValueError("Incomplete parser page identity coverage")
    declared, declared_keys = set(), set()
    assets = record.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("Parser assets must be an array")
    for asset in assets:
        if (not isinstance(asset, dict) or not isinstance(asset.get("path"), str) or
                not asset["path"] or not isinstance(asset.get("size"), int) or isinstance(asset["size"], bool) or
                asset["size"] < 1 or not isinstance(asset.get("sha256"), str)):
            raise ValueError("Invalid parser asset record")
        path = PurePosixPath(asset["path"])
        if (path.is_absolute() or ".." in path.parts or "\\" in str(path) or "\x00" in asset["path"] or
                path.as_posix() != asset["path"] or path.as_posix().casefold() in declared_keys):
            raise ValueError("Unsafe parser asset reference")
        image = root / path
        if (image.is_symlink() or not image.resolve().is_relative_to(root.resolve()) or
                not image.is_file() or image.stat().st_size != asset["size"]):
            raise ValueError("Missing parser asset")
        with image.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != asset["sha256"]:
                raise ValueError("Parser asset hash mismatch")
        declared.add(path.as_posix())
        declared_keys.add(path.as_posix().casefold())
    markdown = record.get("markdown", "")
    if backend == "logics" and isinstance(markdown, str):
        markdown, unavailable = _strip_unavailable_images(markdown, declared)
        if unavailable:
            record["markdown"] = markdown
            rebuilt = ordered_blocks(markdown)
            placements = []
            preservation = record.get("source_image_preservation")
            if isinstance(preservation, dict):
                placements = [
                    item for item in preservation.get("placements", [])
                    if isinstance(item, dict) and item.get("path") in declared
                ]
            for block in rebuilt:
                occurrences = [item for item in placements if item["path"] in block["text_or_markdown"]]
                if occurrences:
                    block["source_pdf_image_occurrences"] = occurrences
            record["blocks"] = rebuilt
            record["warnings"] = [
                *(record.get("warnings") if isinstance(record.get("warnings"), list) else []),
                {
                    "code": "LOGICS_IMAGE_UNAVAILABLE",
                    "message": "解析结果引用了未随产物下发的图片，已移除引用并保留周边文本",
                    "reason": "worker did not ship the referenced image assets",
                    "unavailable_count": len(unavailable),
                    "unavailable_images": unavailable,
                },
            ]
    blocks = record.get("blocks", [])
    if (not isinstance(markdown, str) or not markdown.strip() or not isinstance(blocks, list) or not blocks or
            any(not isinstance(b, dict) or not isinstance(b.get("text_or_markdown"), str) for b in blocks) or
            "".join(b["text_or_markdown"] for b in blocks) != markdown or
            len({b.get("block_id") for b in blocks if isinstance(b.get("block_id"), str)}) != len(blocks)):
        raise ValueError("Incomplete parser block coverage")
    # Validate references resolved by the Markdown AST, including reference-style images.
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)
    for token in tokens:
        for child in token.children or []:
            if child.type == "image":
                src = child.attrGet("src") or ""
                if _image_path(src) not in declared:
                    raise ValueError(f"Missing or external parser image: {src}")
    html = [token.content for token in tokens if token.type == "html_block"]
    html.extend(
        child.content
        for token in tokens
        for child in token.children or []
        if child.type == "html_inline"
    )
    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag.casefold() == "img":
                attrs = dict(attrs)
                src = attrs.get("src") or ""
                if attrs.get("srcset") or _image_path(src) not in declared:
                    raise ValueError(f"Missing or external parser image: {src}")

    parser = Images(convert_charrefs=True)
    for fragment in html:
        parser.feed(fragment)
    parser.close()
    _validate_source_image_preservation(record, expected_pages)
    return record


def _image_path(src: str) -> str:
    """Resolve URL-encoded relative image references without allowing remote URLs."""
    parsed = urlsplit(src)
    path = unquote(parsed.path)
    candidate = PurePosixPath(path)
    if (parsed.scheme or parsed.netloc or not path or candidate.is_absolute() or ".." in candidate.parts or
            "\\" in path or "\x00" in path or candidate.as_posix() != path):
        raise ValueError(f"Missing or external parser image: {src}")
    return path


def _validate_source_image_preservation(record: dict[str, Any], expected_pages: int) -> None:
    """Validate provenance shape and binding; client also binds submitted PDF bytes."""
    if "source_image_preservation" not in record:
        if (any(any(key in asset for key in ("source_pdf_occurrences", "restored", "model_extracted")) for asset in record["assets"]) or
                any("source_pdf_image_occurrences" in block for block in record["blocks"])):
            raise ValueError("Invalid source PDF image preservation provenance")
        return
    preservation = record["source_image_preservation"]
    def invalid():
        raise ValueError("Invalid source PDF image preservation provenance")
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum
    def bbox(value):
        if (not isinstance(value, list) or len(value) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value) or
                value[2] <= value[0] or value[3] <= value[1]):
            invalid()
        return value
    def same_bbox(left, right):
        return all(abs(a-b) <= 0.0001 for a, b in zip(left, right))
    if not isinstance(preservation, dict):
        invalid()
    digest = preservation.get("source_pdf_sha256")
    placements = preservation.get("placements")
    if (preservation.get("version") != 1 or type(preservation.get("version")) is not int or
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None or
            preservation.get("model_quality_accepted") is not False or
            preservation.get("scope") != "visible embedded/inline PDF image placements; vector-only figures are not identified" or
            not isinstance(placements, list) or not integer(preservation.get("visible_placements")) or
            preservation["visible_placements"] != len(placements) or
            not integer(preservation.get("restored_placements"))):
        invalid()
    assets = {asset["path"]: asset for asset in record["assets"]}
    seen, by_path = set(), {}
    restored_count = 0
    for item in placements:
        if not isinstance(item, dict):
            invalid()
        page = item.get("source_page_index")
        draw = item.get("source_draw_index")
        path = item.get("path")
        if (not integer(page) or page >= expected_pages or
                type(item.get("source_page_number")) is not int or item["source_page_number"] != page + 1 or
                not integer(draw) or (page, draw) in seen or not integer(item.get("source_xref")) or
                not isinstance(path, str) or path not in assets or item.get("asset_sha256") != assets[path]["sha256"] or
                item.get("source_pdf_sha256") != digest):
            invalid()
        seen.add((page, draw))
        original = bbox(item.get("source_bbox_pt"))
        visible = bbox(item.get("visible_bbox_pt"))
        bounds = bbox(item.get("page_bounds_pt"))
        rendered = bbox(item.get("render_bbox_pt"))
        if not same_bbox(bounds[:2], [0, 0]):
            invalid()
        intersection = [max(original[0], bounds[0]), max(original[1], bounds[1]),
                        min(original[2], bounds[2]), min(original[3], bounds[3])]
        if intersection[2] <= intersection[0] or intersection[3] <= intersection[1] or not same_bbox(visible, intersection):
            invalid()
        rotation = item.get("page_rotation")
        if type(rotation) is not int or rotation not in {0, 90, 180, 270}:
            invalid()
        x0, y0, x1, y1 = visible
        width, height = bounds[2], bounds[3]
        expected = {0: visible, 90: [height-y1, x0, height-y0, x1],
                    180: [width-x1, height-y1, width-x0, height-y0],
                    270: [y0, width-x1, y1, width-x0]}[rotation]
        restored = item.get("restored")
        if (not same_bbox(rendered, expected) or type(restored) is not bool or
                item.get("model_extracted") is not (not restored) or
                item.get("exact_model_pixel_match") is not (not restored) or
                item.get("chapter_assignment") != "unassigned" or item.get("interpretation") != "not inferred" or
                item.get("source_coordinates") != "unrotated page points" or
                item.get("render_coordinates") != "rotated page points" or
                item.get("extraction_method") != ("source_pdf_visible_region_render" if restored else "model_asset_exact_pixels")):
            invalid()
        restored_count += restored
        by_path.setdefault(path, []).append(item)
    if preservation["restored_placements"] != restored_count:
        invalid()
    for asset in record["assets"]:
        if asset["path"] not in by_path and any(key in asset for key in ("source_pdf_occurrences", "restored", "model_extracted")):
            invalid()
    for path, occurrences in by_path.items():
        asset = assets[path]
        restored = any(item["restored"] for item in occurrences)
        if (asset.get("source_pdf_occurrences") != occurrences or
                asset.get("restored") is not restored or asset.get("model_extracted") is not (not restored)):
            invalid()
    for block in record["blocks"]:
        occurrences = [item for path, items in by_path.items() if path in block["text_or_markdown"] for item in items]
        if ((occurrences and block.get("source_pdf_image_occurrences") != occurrences) or
                (not occurrences and "source_pdf_image_occurrences" in block)):
            invalid()
