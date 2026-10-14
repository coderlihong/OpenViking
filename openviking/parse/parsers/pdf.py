# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
PDF parser for OpenViking.

Unified parser that converts PDF to Markdown then parses the result.
Supports dual strategy:
- Local: pdfplumber for direct conversion
- Remote: MinerU API for advanced conversion

This design simplifies PDF handling by delegating structure analysis
to the MarkdownParser after conversion.
"""

import asyncio
import base64
import hashlib
import io
import re
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from openviking.parse.base import (
    NodeType,
    ParseResult,
    ResourceNode,
    create_parse_result,
    lazy_import,
)
from openviking.parse.parsers.base_parser import BaseParser
from openviking.utils.time_utils import parse_iso_datetime
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils import get_logger
from openviking_cli.utils.config.parser_config import PDFConfig

logger = get_logger(__name__)

#: Backends a request may name through ``parse_backend``.  ``local`` runs in
#: process; the rest are served by the versioned artifact protocol and need a
#: registered endpoint before any request can name one.
LOCAL_BACKEND = "local"
ARTIFACT_BACKENDS = ("mineru", "teleocr", "logics")
REQUESTABLE_BACKENDS = (LOCAL_BACKEND, *ARTIFACT_BACKENDS, "auto")


class PDFParser(BaseParser):
    """
    PDF parser with dual conversion strategy.

    Converts PDF → Markdown → ParseResult using MarkdownParser.
    When available, extracts PDF bookmarks/outlines and injects them as
    markdown headings so MarkdownParser can build a hierarchical directory
    structure instead of flat numbered files.

    Strategies:
    - "local": Use pdfplumber for text and table extraction
    - "mineru": Use MinerU API for advanced PDF processing
    - "auto": Try local first, fallback to MinerU if configured

    Examples:
        >>> # Local parsing
        >>> parser = PDFParser(PDFConfig(strategy="local"))
        >>> result = await parser.parse("document.pdf")

        >>> # Remote API parsing
        >>> config = PDFConfig(
        ...     strategy="mineru",
        ...     mineru_endpoint="http://127.0.0.1:8000"
        ... )
        >>> parser = PDFParser(config)
        >>> result = await parser.parse("document.pdf")
    """

    def __init__(self, config: Optional[PDFConfig] = None):
        """
        Initialize PDF parser.

        Args:
            config: PDFConfig instance (defaults to auto strategy)
        """
        self.config = config or PDFConfig()
        self.config.validate()

        # Lazy import MarkdownParser to avoid circular imports
        self._markdown_parser = None

    def _get_markdown_parser(self):
        """Lazy import and create MarkdownParser."""
        if self._markdown_parser is None:
            from openviking.parse.parsers.markdown import MarkdownParser

            self._markdown_parser = MarkdownParser(config=self.config)
        return self._markdown_parser

    def _require_serviceable_backend(self, backend: Optional[str]) -> None:
        """Reject a ``parse_backend`` this deployment cannot serve, before parsing.

        Doing this here instead of letting ``PDFConfig.validate()`` fire while the
        request-derived parser is built matters: that raise lands inside
        :meth:`parse`'s conversion handler, which turns any failure into an empty
        parse result.  A backend name with no registered endpoint therefore
        committed a zero-chapter tree with no ``toc.md`` and no error -- the one
        outcome a knowledge base must not report as success.  Artifact backends
        are all-or-nothing for a deployment, so the honest answer is a rejected
        request that names the configuration key to set.
        """

        if backend in (None, "auto"):
            return
        if backend not in REQUESTABLE_BACKENDS:
            raise InvalidArgumentError(
                f"Unsupported PDF parse_backend: {backend}",
                details={"parse_backend": backend, "supported": list(REQUESTABLE_BACKENDS)},
            )
        endpoints = self.config.model_endpoints or {}
        if backend in endpoints or backend == LOCAL_BACKEND:
            return
        if backend == "mineru" and self.config.mineru_endpoint:
            # Legacy single-endpoint MinerU API: served in-process by the
            # conversion path rather than by the artifact protocol.
            return
        raise InvalidArgumentError(
            f"PDF parse_backend '{backend}' has no configured model endpoint; "
            f"set pdf.model_endpoints.{backend} "
            f"(configured: {', '.join(sorted(endpoints)) or 'none'})",
            details={"parse_backend": backend, "configured_endpoints": sorted(endpoints)},
        )

    @property
    def supported_extensions(self) -> List[str]:
        """List of supported file extensions."""
        return [".pdf"]

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        """
        Parse PDF file.

        Args:
            source: Path to PDF file
            **kwargs: Additional options (resource_name/source_name for original filename)

        Returns:
            ParseResult with document tree

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ValueError: If conversion fails with all strategies
        """
        start_time = time.time()
        pdf_path = Path(source)

        # Get resource name from kwargs, prefer original filename from upload
        resource_name = kwargs.get("resource_name") or kwargs.get("source_name")

        if not pdf_path.exists():
            return create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_path=str(pdf_path),
                source_format="pdf",
                parser_name="PDFParser",
                parse_time=time.time() - start_time,
                warnings=[f"File not found: {pdf_path}"],
            )

        model_document = None
        try:
            policy = kwargs.get("structure_policy")
            backend = kwargs.get("parse_backend")
            if policy is None and kwargs.get("split_content") is not False:
                # The chapter tree is the uniform shape every ingested PDF comes
                # back in, whichever backend produced its Markdown: the document's
                # own outline forms it, and the tree is what makes the corpus
                # navigable.  A caller that explicitly asks for one unsplit
                # document is the only opt-out.
                policy = "bookmarks_preferred"
            if policy not in (None, "bookmarks_preferred"):
                raise InvalidArgumentError(f"Unsupported structure_policy: {policy}")
            self._require_serviceable_backend(backend)
            if kwargs.get("structure_policy") and kwargs.get("split_content") is False:
                raise InvalidArgumentError("bookmarks_preferred cannot be combined with no_split")
            # Step 1: registered Docker backends use the validated artifact protocol.
            endpoints = self.config.model_endpoints or {}
            effective_backend = backend or (
                self.config.default_model_backend if endpoints else self.config.strategy
            )
            if effective_backend == "auto" and endpoints:
                effective_backend = self.config.default_model_backend
            uses_model_artifacts = effective_backend in endpoints
            # The outline lives in the file, so it is read before the conversion is
            # configured: a PDF that has bookmarks is split by them, and one that has
            # none keeps the converter's own heading detection, so ``split_chapters``
            # splits on real headings instead of falling back to token-budget blocks.
            outline: list[dict] = []
            outline_error: Optional[str] = None
            if policy:
                try:
                    outline = await asyncio.to_thread(self._read_pdf_bookmarks, pdf_path)
                except ValueError:
                    raise  # an unsupported input (e.g. password-protected) stays unsupported
                except Exception as error:
                    # The outline is a convenience the splitter can do without; a
                    # reader that cannot open the file does not make the document
                    # unparsable, and the conversion has not spent model time yet.
                    outline_error = str(error)
                    logger.warning(
                        f"PDF bookmarks unavailable for {pdf_path.name}: {error}"
                    )
            # Each request owns its conversion config. Never mutate the registry's
            # shared parser or let page-derived injected headings prove alignment.
            # Injected headings stay disabled whenever the tree has something better
            # to split on: the model's own Markdown or a real document outline.
            split_on_declared_structure = bool(policy) and (
                uses_model_artifacts or bool(outline)
            )
            converter = self
            if policy or backend:
                converter = PDFParser(
                    replace(
                        self.config,
                        strategy=backend or self.config.strategy,
                        heading_detection=(
                            "none" if split_on_declared_structure else self.config.heading_detection
                        ),
                    )
                )
            if uses_model_artifacts:
                from openviking.parse.model_backend import ParseModelBackend

                expected_pages, page_count_source = await asyncio.to_thread(
                    self._count_pdf_pages, pdf_path
                )
                model_document = await ParseModelBackend(
                    endpoints[effective_backend],
                    api_key=self.config.model_api_key,
                    timeout=self.config.model_timeout,
                    max_artifact_bytes=self.config.max_model_artifact_bytes,
                ).parse(
                    pdf_path,
                    backend=effective_backend,
                    mode=kwargs.get("model_mode", "auto"),
                    options=kwargs.get("backend_options"),
                    expected_pages=expected_pages,
                )
                markdown_content = model_document.markdown
                conversion_meta = {
                    **model_document.metadata,
                    "strategy": effective_backend,
                    "effective_backend": effective_backend,
                    "library": "parser-artifact-v1",
                    "page_count_source": page_count_source,
                }
            else:
                if kwargs.get("model_mode", "auto") != "auto" or kwargs.get("backend_options"):
                    raise InvalidArgumentError(
                        "model_mode/backend_options require a registered Docker parser backend"
                    )
                markdown_content, conversion_meta = await converter._convert_to_markdown(
                    pdf_path,
                    resource_name=resource_name,
                )

            chapter_kwargs = {}
            structure_note: Optional[str] = None
            if policy:
                from openviking.parse.chapter_splitter import split_chapters

                if conversion_meta.get("library") == "pdfplumber":
                    # Local page comments are synthetic conversion annotations,
                    # never chapter anchors or meaningful preface text.
                    markdown_content = re.sub(
                        r"<!-- Page \d+(?: (?:Table|Image) \d+)? -->\s*",
                        "",
                        markdown_content,
                    ).strip()
                bookmarks = outline
                if outline_error:
                    # ``split_chapters`` treats an empty outline as "use the
                    # document's own headings" -- a designed input -- so the corpus
                    # keeps a chapter tree instead of losing the file to a reader
                    # that only ever supplied a convenience.
                    structure_note = f"bookmarks_unavailable: {outline_error}"
                plan = await asyncio.to_thread(
                    split_chapters,
                    markdown_content,
                    bookmarks,
                    section_target_tokens=kwargs.get("section_target_tokens", 4096),
                )
                chapter_kwargs["_chapter_plan"] = plan

            # Step 2: Parse Markdown using MarkdownParser, pass through resource name
            md_parser = self._get_markdown_parser()
            from openviking_cli.utils.storage import get_storage

            storage = get_storage()
            result = await md_parser.parse_content(
                markdown_content,
                source_path=str(pdf_path),
                resource_name=(
                    Path(resource_name).stem
                    if resource_name and Path(resource_name).suffix.lower() == ".pdf"
                    else resource_name
                ),
                source_name=resource_name,
                base_dir=model_document.assets_dir if model_document else pdf_path.parent,
                allowed_media_dirs=[storage.media_dir]
                + ([model_document.assets_dir] if model_document else []),
                split_content=kwargs.get("split_content", True),
                **chapter_kwargs,
            )

            # Step 3: Update metadata for PDF origin
            result.source_format = "pdf"  # Override markdown format
            result.parser_name = "PDFParser"
            result.parser_version = "2.0"
            result.parse_time = time.time() - start_time
            result.meta.update(conversion_meta)
            result.meta["pdf_strategy"] = conversion_meta.get(
                "effective_backend", converter.config.strategy
            )
            result.meta["intermediate_markdown_length"] = len(markdown_content)
            result.meta["intermediate_markdown_preview"] = markdown_content[:500]
            if structure_note:
                # A tree built from document headings rather than the outline is a
                # degraded structure, not a failed one: it has to reach the ingestion
                # ledger so the operator can tell which chapters to re-check.
                result.meta["structure_note"] = structure_note
                result.warnings.append(structure_note)

            logger.info(
                f"PDF parsed successfully: {pdf_path.name} "
                f"({len(markdown_content)} chars markdown, "
                f"{result.parse_time:.2f}s)"
            )

            return result

        except InvalidArgumentError:
            # A rejected request is not a conversion failure: it applies to every
            # file of the batch, so it must reach the caller instead of becoming
            # an empty tree that commits as a successful, structureless import.
            raise
        except Exception as e:
            # The string alone cannot say which step failed (conversion, bookmarks,
            # or the markdown handoff), and a degraded ingestion is only debuggable
            # from the frame that raised.
            logger.error(f"Failed to parse PDF {pdf_path}: {e}", exc_info=True)
            return create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_path=str(pdf_path),
                source_format="pdf",
                parser_name="PDFParser",
                parse_time=time.time() - start_time,
                warnings=[f"Failed to parse PDF: {e}"],
            )

        finally:
            if model_document is not None:
                model_document.close()

    @staticmethod
    def _count_pdf_pages(pdf_path: Path) -> tuple[int, str]:
        """Count pages for the artifact protocol's coverage expectation, and say
        which library measured them.

        ``expected_pages`` is what the returned document's page ids are checked
        against, so a backend that quietly drops a page fails the job instead of
        committing a chapter tree with a hole in it.  pymupdf measures first
        because it is also the library the outline comes from; a file it refuses
        to open -- a repaired xref, a generator that wrote a truncated header --
        is still countable by the pdfminer stack the local strategy already
        depends on, and losing that manual from the corpus is not the fallback.
        """

        pymupdf = lazy_import("pymupdf")
        try:
            with pymupdf.open(str(pdf_path)) as document:
                return len(document), "pymupdf"
        except Exception as pymupdf_error:
            pdfplumber = lazy_import("pdfplumber")
            try:
                with pdfplumber.open(str(pdf_path)) as document:
                    return len(document.pages), "pdfplumber"
            except Exception:
                # The first error is the one an operator can act on; pymupdf
                # names the file and the type it tried to open.
                raise pymupdf_error

    @staticmethod
    def _read_pdf_bookmarks(pdf_path: Path) -> List[Dict[str, Any]]:
        """Read outline hierarchy with resolved destinations; pages remain metadata."""
        pymupdf = lazy_import("pymupdf")
        with pymupdf.open(str(pdf_path)) as document:
            if document.needs_pass:
                raise ValueError("Password-protected PDF is not supported")
            return [
                {
                    "level": level,
                    "title": title.strip(),
                    "page_num": page if 1 <= page <= len(document) else None,
                }
                for level, title, page in document.get_toc()
                if title and title.strip()
            ]

    async def _convert_to_markdown(
        self,
        pdf_path: Path,
        resource_name: Optional[str] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """
        Convert PDF to Markdown using configured strategy.

        Args:
            pdf_path: Path to PDF file
            resource_name: Optional resource name for organizing saved images

        Returns:
            Tuple of (markdown_content, metadata_dict)

        Raises:
            ValueError: If all conversion strategies fail
        """
        if self.config.strategy == "local":
            return await self._convert_local(pdf_path, resource_name=resource_name)

        elif self.config.strategy == "mineru":
            return await self._convert_mineru(pdf_path, resource_name=resource_name)

        elif self.config.strategy == "auto":
            # Try local first
            try:
                return await self._convert_local(pdf_path, resource_name=resource_name)
            except Exception as e:
                logger.warning(f"Local conversion failed: {e}")

                # Fallback to MinerU if configured
                if self.config.mineru_endpoint:
                    logger.info("Falling back to MinerU API")
                    return await self._convert_mineru(pdf_path, resource_name=resource_name)
                else:
                    raise ValueError(
                        f"Local conversion failed and no MinerU endpoint configured: {e}"
                    )

        else:
            raise ValueError(f"Unknown strategy: {self.config.strategy}")

    async def _convert_local(
        self, pdf_path: Path, storage=None, resource_name: Optional[str] = None
    ) -> tuple[str, Dict[str, Any]]:
        # pdfplumber / pdfminer 的解析与图片/表格提取通常是 CPU/IO 密集且为同步实现，
        # 放到线程池中执行，避免阻塞事件循环。
        return await asyncio.to_thread(self._convert_local_sync, pdf_path, storage, resource_name)

    def _convert_local_sync(
        self, pdf_path: Path, storage=None, resource_name: Optional[str] = None
    ) -> tuple[str, Dict[str, Any]]:
        """同步版：用 pdfplumber 将 PDF 转 Markdown。

        该方法会在 :meth:`_convert_local` 中通过 asyncio.to_thread 调用。
        """
        pdfplumber = lazy_import("pdfplumber")

        # Import storage utilities
        if storage is None:
            from openviking_cli.utils.storage import get_storage

            storage = get_storage()

        if resource_name is None:
            resource_name = pdf_path.stem

        parts = []
        meta = {
            "strategy": "local",
            "library": "pdfplumber",
            "pages_processed": 0,
            "images_extracted": 0,
            "images_deduplicated": 0,
            "tables_extracted": 0,
            "bookmarks_found": 0,
            "bookmarks_resolved": 0,
            "bookmarks_unresolved": 0,
            "headings_found": 0,
            "heading_source": "none",
        }

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                meta["total_pages"] = len(pdf.pages)

                # Extract structure (bookmarks → font fallback)
                detection_mode = self.config.heading_detection
                bookmarks = []
                raw_bookmarks = []
                heading_source = "none"

                if detection_mode in ("bookmarks", "auto"):
                    raw_bookmarks = self._extract_bookmarks(pdf)
                    meta["bookmarks_found"] = len(raw_bookmarks)
                    bookmarks = [bm for bm in raw_bookmarks if bm["page_num"] is not None]
                    meta["bookmarks_resolved"] = len(bookmarks)
                    meta["bookmarks_unresolved"] = len(raw_bookmarks) - len(bookmarks)

                    if bookmarks:
                        heading_source = "bookmarks"
                    elif raw_bookmarks:
                        logger.info(
                            "Bookmark detection found %d entries but none resolved to pages; "
                            "ignoring bookmark headings",
                            len(raw_bookmarks),
                        )

                if not bookmarks and detection_mode in ("font", "auto"):
                    bookmarks = self._detect_headings_by_font(pdf)
                    if bookmarks:
                        heading_source = "font_analysis"

                meta["headings_found"] = len(bookmarks)
                meta["heading_source"] = heading_source
                logger.info(
                    "Heading detection: source=%s, headings=%d, bookmarks=%d, resolved=%d, "
                    "unresolved=%d",
                    heading_source,
                    len(bookmarks),
                    meta["bookmarks_found"],
                    meta["bookmarks_resolved"],
                    meta["bookmarks_unresolved"],
                )

                # Group bookmarks by page_num
                bookmarks_by_page = defaultdict(list)
                for bm in bookmarks:
                    page = bm["page_num"]
                    if page is None:
                        continue
                    bookmarks_by_page[page].append(bm)

                for page_num, page in enumerate(pdf.pages, 1):
                    try:
                        # Inject headings before page text
                        page_bookmarks = bookmarks_by_page.get(page_num, [])
                        for bm in page_bookmarks:
                            heading_prefix = "#" * bm["level"]
                            parts.append(f"\n{heading_prefix} {bm['title']}\n")

                        # Extract text
                        text = page.extract_text()
                        if text and text.strip():
                            # Add page marker as HTML comment
                            parts.append(f"<!-- Page {page_num} -->\n{text.strip()}")
                            meta["pages_processed"] += 1

                        # Extract tables
                        tables = page.extract_tables()
                        for table_idx, table in enumerate(tables or []):
                            if table and len(table) > 0:
                                md_table = self._format_table_markdown(table)
                                if md_table:
                                    parts.append(
                                        f"<!-- Page {page_num} Table {table_idx + 1} -->\n{md_table}"
                                    )
                                    meta["tables_extracted"] += 1

                        # Extract images.
                        #
                        # A page can stack several image XObjects on the exact same
                        # spot — print-to-PDF producers routinely emit a background
                        # layer plus a content layer. Since extraction rasterises the
                        # page *region* rather than the XObject itself, every one of
                        # them renders to identical bytes. Skip the repeats: bbox
                        # first, which avoids the (expensive) render entirely, then a
                        # content hash as a backstop. Both sets are per-page, so a
                        # header logo repeated across pages is still kept once per
                        # page.
                        images = page.images
                        seen_boxes = set()
                        seen_digests = set()
                        for img_idx, img in enumerate(images or []):
                            try:
                                bbox_key = (
                                    round(img["x0"], 1),
                                    round(img["top"], 1),
                                    round(img["x1"], 1),
                                    round(img["bottom"], 1),
                                )
                                if bbox_key in seen_boxes:
                                    meta["images_deduplicated"] += 1
                                    continue
                                seen_boxes.add(bbox_key)

                                # Extract image using underlying PDF object
                                image_obj = self._extract_image_from_page(page, img)
                                if image_obj:
                                    # Dedup only — md5 keeps this cheap, and the
                                    # flag keeps it working on FIPS-locked hosts.
                                    digest = hashlib.md5(image_obj, usedforsecurity=False).digest()
                                    if digest in seen_digests:
                                        meta["images_deduplicated"] += 1
                                        continue
                                    seen_digests.add(digest)

                                    # Save image
                                    filename = f"page{page_num}_img{img_idx + 1}"
                                    image_path = storage.save_image(
                                        resource_name, image_obj, filename=filename
                                    )

                                    # Generate path relative to the media root.
                                    rel_path = image_path.relative_to(storage.media_dir)
                                    parts.append(
                                        f"<!-- Page {page_num} Image {img_idx + 1} -->\n"
                                        f"![Page {page_num} Image {img_idx + 1}]({rel_path})"
                                    )
                                    meta["images_extracted"] += 1
                            except Exception as img_err:
                                logger.warning(
                                    f"Failed to extract image {img_idx + 1} on page {page_num}: {img_err}"
                                )
                    finally:
                        self._release_page_cache(page)

            if not parts:
                logger.warning(f"No content extracted from {pdf_path}")
                return "", meta

            markdown_content = "\n\n".join(parts)
            logger.info(
                f"Local conversion: {meta['pages_processed']}/{meta['total_pages']} pages, "
                f"{meta['headings_found']} headings ({meta['heading_source']}, "
                f"bookmarks={meta['bookmarks_found']}, "
                f"resolved={meta['bookmarks_resolved']}), "
                f"{meta['images_extracted']} images "
                f"({meta['images_deduplicated']} duplicates skipped), "
                f"{meta['tables_extracted']} tables → "
                f"{len(markdown_content)} chars"
            )

            return markdown_content, meta

        except Exception as e:
            logger.error(f"pdfplumber conversion failed: {e}")
            raise

    @staticmethod
    def _release_page_cache(page: Any) -> None:
        """Release pdfplumber/pdfminer per-page caches when available."""
        close = getattr(page, "close", None)
        if callable(close):
            try:
                close()
                return
            except Exception:
                pass

        flush_cache = getattr(page, "flush_cache", None)
        if callable(flush_cache):
            try:
                flush_cache()
            except Exception:
                pass

    def _extract_bookmarks(self, pdf) -> List[Dict[str, Any]]:
        """Extract bookmark structure from PDF outlines.

        Returns: [{level: int, title: str, page_num: int(1-based)}]
        """
        try:
            if not hasattr(pdf, "doc") or not hasattr(pdf.doc, "get_outlines"):
                return []

            outlines = list(pdf.doc.get_outlines())
            if not outlines:
                return []

            page_ref_to_num = self._build_page_number_map(pdf)

            bookmarks = []
            for level, title, dest, action, _se in outlines:
                if not title or not title.strip():
                    continue

                page_num = None
                try:
                    if dest is None and isinstance(action, dict):
                        dest = action.get("D")
                    # Resolve the destination before indexing: bytes[0] is an
                    # ASCII integer, not a PDF page reference.
                    if isinstance(dest, (str, bytes)):
                        dest = pdf.doc.get_dest(dest)
                    if hasattr(dest, "resolve"):
                        dest = dest.resolve()
                    if isinstance(dest, dict):
                        dest = dest.get("D")
                    if isinstance(dest, (list, tuple)) and dest:
                        page_num = self._resolve_bookmark_page(
                            dest[0], page_ref_to_num, len(pdf.pages)
                        )
                except Exception:
                    pass

                bookmarks.append(
                    {
                        "level": min(max(level, 1), 6),
                        "title": title.strip(),
                        "page_num": page_num,
                    }
                )

            return bookmarks

        except Exception as e:
            logger.warning(f"Failed to extract bookmarks: {e}")
            return []

    def _build_page_number_map(self, pdf) -> Dict[int, int]:
        """Build a lookup from PDF page object ids to 1-based page numbers.

        pdfminer outlines and link annotations reference page objects by object id.
        In pdfplumber these ids are exposed as ``page.page_obj.pageid``; some mocks
        or alternate inputs may still expose ``objid``, so we keep both.
        """
        page_ref_to_num: Dict[int, int] = {}
        for page_num, page in enumerate(pdf.pages, 1):
            page_obj = getattr(page, "page_obj", None)
            if page_obj is None:
                continue

            for attr_name in ("pageid", "objid"):
                ref_id = getattr(page_obj, attr_name, None)
                if isinstance(ref_id, int):
                    page_ref_to_num.setdefault(ref_id, page_num)

        return page_ref_to_num

    def _resolve_bookmark_page(
        self, page_ref: Any, page_ref_to_num: Dict[int, int], total_pages: int
    ) -> Optional[int]:
        """Resolve a bookmark destination to a 1-based page number."""
        ref_id = getattr(page_ref, "objid", None)
        if isinstance(ref_id, int):
            return page_ref_to_num.get(ref_id)

        if isinstance(page_ref, int):
            # 0-based integer page index (common in many PDF producers)
            candidate = page_ref + 1
            if 1 <= candidate <= total_pages:
                return candidate
            return None

        if hasattr(page_ref, "resolve"):
            resolved = page_ref.resolve()
            for attr_name in ("pageid", "objid"):
                resolved_id = getattr(resolved, attr_name, None)
                if isinstance(resolved_id, int):
                    return page_ref_to_num.get(resolved_id)

        return None

    def _detect_headings_by_font(self, pdf) -> List[Dict[str, Any]]:
        """Detect headings by font size analysis.

        Returns: [{level: int, title: str, page_num: int(1-based)}]
        """
        try:
            # Step 1: Sample font size distribution (every 5th page)
            size_counter: Counter = Counter()
            sample_pages = pdf.pages[::5]
            for page in sample_pages:
                try:
                    for char in page.chars:
                        if char["text"].strip():
                            rounded = round(char["size"] * 2) / 2
                            size_counter[rounded] += 1
                finally:
                    self._release_page_cache(page)

            if not size_counter:
                return []

            # Step 2: Determine body font size and heading font sizes
            body_size = size_counter.most_common(1)[0][0]
            min_delta = self.config.font_heading_min_delta

            heading_sizes = sorted(
                [
                    s
                    for s, count in size_counter.items()
                    if s >= body_size + min_delta and count < size_counter[body_size] * 0.5
                ],
                reverse=True,
            )

            max_levels = self.config.max_heading_levels
            heading_sizes = heading_sizes[:max_levels]

            if not heading_sizes:
                logger.debug(f"Font analysis: body_size={body_size}pt, no heading sizes found")
                return []

            size_to_level = {s: i + 1 for i, s in enumerate(heading_sizes)}
            logger.debug(
                f"Font analysis: body_size={body_size}pt, "
                f"heading_sizes={heading_sizes}, size_to_level={size_to_level}"
            )

            # Step 3: Extract heading text page by page
            headings: List[Dict[str, Any]] = []

            def flush_line(chars_to_flush: list, page_num: int) -> None:
                if not chars_to_flush:
                    return
                title = "".join(c["text"] for c in chars_to_flush).strip()
                size = round(chars_to_flush[0]["size"] * 2) / 2

                if len(title) < 2:
                    return
                if len(title) > 100:
                    return
                if title.isdigit():
                    return
                if re.match(r"^[\d\s.·…]+$", title):
                    return

                headings.append(
                    {
                        "level": size_to_level[size],
                        "title": title,
                        "page_num": page_num,
                    }
                )

            for page in pdf.pages:
                try:
                    page_num = page.page_number + 1
                    chars = sorted(page.chars, key=lambda c: (c["top"], c["x0"]))

                    current_line_chars: list = []
                    current_top = None

                    for char in chars:
                        # Performance: headings won't appear in bottom 70% of page
                        if char["top"] > page.height * 0.3:
                            flush_line(current_line_chars, page_num)
                            current_line_chars = []
                            break

                        rounded_size = round(char["size"] * 2) / 2
                        if rounded_size not in size_to_level:
                            flush_line(current_line_chars, page_num)
                            current_line_chars = []
                            current_top = None
                            continue

                        # Same line check (top offset < 2pt)
                        if current_top is not None and abs(char["top"] - current_top) > 2:
                            flush_line(current_line_chars, page_num)
                            current_line_chars = []

                        current_line_chars.append(char)
                        current_top = char["top"]

                    flush_line(current_line_chars, page_num)
                finally:
                    self._release_page_cache(page)

            # Step 4: Deduplicate - filter headers appearing on >30% of pages
            title_page_count: Counter = Counter(h["title"] for h in headings)
            total_pages = len(pdf.pages)
            header_titles = {t for t, c in title_page_count.items() if c > total_pages * 0.3}
            headings = [h for h in headings if h["title"] not in header_titles]

            logger.debug(
                f"Font heading detection: {len(headings)} headings found "
                f"(filtered {len(header_titles)} header titles)"
            )
            return headings

        except Exception as e:
            logger.warning(f"Failed to detect headings by font: {e}")
            return []

    def _extract_image_from_page(self, page, img_info: dict) -> Optional[bytes]:
        """
        Extract a PDF image as valid PNG bytes.

        Renders the image's bounding box on the page to a raster PNG via
        pdfplumber's ``crop().to_image()`` instead of returning the raw decoded
        XObject stream (which is not a valid image file and cannot be opened).

        Args:
            page: pdfplumber page object
            img_info: Image metadata from page.images

        Returns:
            PNG-encoded image bytes or None if extraction fails
        """
        try:
            # pdfplumber coordinates: ``top`` is measured from the top of the page.
            bbox = (
                max(0, img_info["x0"]),
                max(0, img_info["top"]),
                min(page.width, img_info["x1"]),
                min(page.height, img_info["bottom"]),
            )

            # Skip degenerate / zero-area boxes that cannot be cropped.
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                return None

            cropped = page.crop(bbox)
            page_image = cropped.to_image(resolution=self.config.image_resolution)

            buffer = io.BytesIO()
            page_image.save(buffer, format="PNG")
            return buffer.getvalue()

        except Exception as e:
            logger.debug(f"Image extraction error: {e}")
            return None

    async def _convert_mineru(
        self,
        pdf_path: Path,
        storage=None,
        resource_name: Optional[str] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """
        Convert PDF to Markdown using the MinerU /file_parse API.

        Args:
            pdf_path: Path to PDF file
            storage: Media storage used to persist extracted images; defaults to
                the configured storage if None
            resource_name: Resource name under which extracted images are saved;
                defaults to the PDF stem

        Returns:
            Tuple of (markdown_content, metadata) where metadata includes
            strategy, endpoint, api_version, backend, task_id, processing_time
            (seconds) and images_saved

        Raises:
            ValueError: If MinerU endpoint is not configured, or the task does
                not complete
            Exception: If the API call fails
        """
        httpx = lazy_import("httpx")

        if not self.config.mineru_endpoint:
            raise ValueError("MinerU endpoint not configured")

        # mineru_endpoint is a base URL; append /file_parse unless it already ends with it
        base = self.config.mineru_endpoint.rstrip("/")
        url = base if base.endswith("/file_parse") else f"{base}/file_parse"

        meta = {
            "strategy": "mineru",
            "endpoint": self.config.mineru_endpoint,
            "api_version": None,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.mineru_timeout) as client:
                # Prepare file upload
                with open(pdf_path, "rb") as f:
                    files = {"files": (pdf_path.name, f, "application/pdf")}

                    # Prepare Form fields
                    data: Dict[str, Any] = dict(self.config.mineru_bodys or {})

                    # MinerU must return extracted images for the markdown refs.
                    data["return_images"] = True

                    # Make API request
                    logger.info(f"Calling MinerU API: {url}")
                    response = await client.post(
                        url,
                        files=files,
                        data=data,
                    )
                    response.raise_for_status()

            # Parse response
            result = response.json()
            if result.get("status") != "completed":
                raise ValueError(f"MinerU task not completed: {result.get('status')}")

            results = result.get("results") or {}
            file_result = results.get(pdf_path.name) or next(iter(results.values()), {})
            markdown_content = file_result.get("md_content") or ""

            # Extract metadata from response
            meta["api_version"] = result.get("version")
            meta["backend"] = result.get("backend")
            meta["task_id"] = result.get("task_id")
            started_at, completed_at = result.get("started_at"), result.get("completed_at")
            if started_at and completed_at:
                meta["processing_time"] = (
                    parse_iso_datetime(completed_at) - parse_iso_datetime(started_at)
                ).total_seconds()

            if not markdown_content:
                logger.warning(f"MinerU returned empty content for {pdf_path}")
                return markdown_content, meta

            if storage is None:
                from openviking_cli.utils.storage import get_storage

                storage = get_storage()

            if resource_name is None:
                resource_name = pdf_path.stem

            # MinerU embeds images as base64 data-URLs, referenced from markdown
            # as `images/<filename>`; save them into the media store and rewrite
            # the references to the stored relative paths.
            repl: Dict[str, str] = {}
            media_dir = storage.media_dir
            for img_name, data_url in (file_result.get("images") or {}).items():
                try:
                    # data URL form: "data:image/jpeg;base64,<b64>"
                    image_bytes = base64.b64decode(data_url.split(",", 1)[-1])
                    img_path = Path(img_name)
                    image_path = storage.save_image(
                        resource_name,
                        image_bytes,
                        filename=img_path.stem,
                        extension=img_path.suffix or ".png",
                    )
                    repl[f"images/{img_name}"] = image_path.relative_to(media_dir).as_posix()
                except Exception as img_err:
                    logger.warning(f"Failed to save MinerU image {img_name}: {img_err}")

            if repl:
                # Single pass over the markdown replaces every known reference;
                # unknown ``images/...`` text is left untouched.
                ref_pattern = re.compile(
                    "|".join(re.escape(ref) for ref in sorted(repl, key=len, reverse=True))
                )
                markdown_content = ref_pattern.sub(lambda m: repl[m.group(0)], markdown_content)
                meta["images_saved"] = len(repl)

            logger.info(f"MinerU conversion: {len(markdown_content)} chars")

            return markdown_content, meta

        except Exception as e:
            logger.error(f"MinerU API call failed: {e}")
            raise

    def _format_table_markdown(self, table: List[List[Optional[str]]]) -> str:
        """
        Convert table data to Markdown table format.

        Args:
            table: 2D array of table cells

        Returns:
            Markdown table string

        Examples:
            >>> table = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
            >>> print(parser._format_table_markdown(table))
            | Name | Age |
            | --- | --- |
            | Alice | 30 |
            | Bob | 25 |
        """
        if not table or not table[0]:
            return ""

        # Clean cells and handle None values
        def clean_cell(cell):
            if cell is None:
                return ""
            return str(cell).strip().replace("|", "\\|")  # Escape pipe characters

        lines = []

        # Header row
        header = table[0]
        header_cells = [clean_cell(cell) for cell in header]
        lines.append("| " + " | ".join(header_cells) + " |")

        # Separator row
        separator = ["---"] * len(header)
        lines.append("| " + " | ".join(separator) + " |")

        # Data rows
        for row in table[1:]:
            # Pad row to match header length
            padded_row = row + [None] * (len(header) - len(row))
            cells = [clean_cell(cell) for cell in padded_row[: len(header)]]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, instruction: str = "", **kwargs
    ) -> ParseResult:
        """
        Parse PDF content string.

        Note: This method is not recommended for PDFParser as it requires
        file path for conversion tools. Use parse() with file path instead.

        Args:
            content: PDF content (not supported)
            source_path: Optional source path
            **kwargs: Additional options

        Raises:
            NotImplementedError: PDFParser requires file path
        """
        raise NotImplementedError(
            "PDFParser does not support parsing content strings. "
            "Use parse() with a file path instead."
        )
