# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
ParserRouter: Route parsing requests between ParserRegistry and UnderstandingAPI.

Routing is controlled by ov.conf (OpenVikingConfig.parser_api).
"""

from pathlib import Path
from typing import Union
from urllib.parse import urlparse

from openviking.parse.accessors.base import LocalResource, SourceType
from openviking.parse.backend import ParserBackend, normalize_parser_backend
from openviking.parse.base import ParseResult
from openviking.parse.office_converter import OfficeConverter
from openviking.parse.registry import ParserRegistry
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class ParserRouter:
    """
    ParserRouter: Route parsing to internal ParserRegistry or third-party UnderstandingAPI.

    Routing logic:
    1. Check feature flag and extension whitelist
    2. Default: ParserRegistry
    3. Matched extensions: UnderstandingAPI
    """

    def __init__(self, parser_registry: ParserRegistry):
        self._parser_registry = parser_registry
        self._understanding_api = None
        self._office_converter_instance = None

    def understanding_api_enabled(self) -> bool:
        """Return whether the external parser is enabled."""
        try:
            from openviking_cli.utils.config.open_viking_config import get_openviking_config

            parser_api = getattr(get_openviking_config(), "parser_api", None)
        except Exception:
            return False
        return bool(parser_api and getattr(parser_api, "enable", False))

    def should_use_understanding_api(
        self,
        source: Union[str, Path, LocalResource],
        resolved_extension: str = "",
    ) -> bool:
        """Decide whether to use UnderstandingAPI."""
        # FeishuAccessor has already normalized proprietary content to Markdown.
        if (
            isinstance(source, LocalResource)
            and source.source_type == SourceType.FEISHU
            and source.meta.get("feishu_content_kind") != "file"
        ):
            return False

        try:
            from openviking_cli.utils.config.open_viking_config import get_openviking_config

            ov_config = get_openviking_config()
        except Exception:
            return False

        parser_api = getattr(ov_config, "parser_api", None)
        if not parser_api or not getattr(parser_api, "enable", False):
            return False

        source_path = self._extract_source_path(source)

        try:
            from openviking.parse.accessors.feishu_accessor import FeishuAccessor

            if FeishuAccessor._is_feishu_url(str(source_path)):
                return bool(getattr(parser_api, "enable_feishu_url", False))
        except Exception:
            pass

        ext = self._normalize_extension(resolved_extension) or self._extract_extension(source_path)
        # A configured PDF model is the default ingestion route; UnderstandingAPI
        # must not silently bypass its artifact/chapter contract.
        if ext == "pdf" and getattr(getattr(ov_config, "pdf", None), "model_endpoints", None):
            return False
        extensions = getattr(parser_api, "extensions", None) or []
        return ext in extensions

    def should_use_understanding_directly(self, source: str, **kwargs) -> bool:
        if self.chapter_options_requested(kwargs):
            return False
        parser_backend = normalize_parser_backend(kwargs.get("parser_backend"))
        if parser_backend is ParserBackend.INTERNAL:
            return False
        forced = parser_backend is ParserBackend.UNDERSTANDING
        return bool(
            (forced or self.should_use_understanding_api(source))
            and self._get_understanding_api().can_submit_url_directly(source, **kwargs)
        )

    @staticmethod
    def _normalize_extension(extension: str) -> str:
        return str(extension or "").lower().lstrip(".")

    def _extract_extension(self, source_path: Union[str, Path]) -> str:
        source = str(source_path)
        parsed = urlparse(source)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            source = parsed.path
        return Path(source).suffix.lower().lstrip(".")

    @staticmethod
    def _is_directory_source(source_path: Union[str, Path]) -> bool:
        """Whether the source is a local folder whose files route per extension."""
        try:
            return Path(str(source_path)).is_dir()
        except (OSError, ValueError):
            return False

    @staticmethod
    def _display_name(source_path: Union[str, Path]) -> str:
        if isinstance(source_path, str) and source_path.startswith(("http://", "https://")):
            return "<url>"
        try:
            return Path(str(source_path)).name or "<path>"
        except (OSError, ValueError):
            return "<path>"

    #: Options that only the PDF chapter pipeline can honour.
    CHAPTER_OPTION_KEYS = ("structure_policy", "parse_backend", "model_mode", "backend_options")

    def chapter_options_requested(self, kwargs: dict) -> bool:
        return any(kwargs.get(key) is not None for key in self.CHAPTER_OPTION_KEYS)

    async def parse(self, source: Union[str, Path, "LocalResource"], **kwargs) -> ParseResult:
        """
        Parse with ParserRegistry or UnderstandingAPI based on the routing decision.
        """
        source_path = self._extract_source_path(source)
        extension = self._normalize_extension(
            kwargs.get("resolved_extension", "")
        ) or self._extract_extension(source_path)

        # Office sources are converted to PDF and re-parsed through the same PDF
        # path. AnyDoc->markdown is never used as a substitute completion path.
        if self._get_office_converter().is_office_extension(extension):
            return await self._parse_office(
                source,
                source_path=source_path,
                extension=extension,
                **kwargs,
            )

        chapter_requested = self.chapter_options_requested(kwargs)
        if chapter_requested and extension != "pdf" and not self._is_directory_source(source_path):
            # A folder is not one format: naming a PDF model there means "use it for
            # the PDFs inside", and DirectoryParser forwards it to exactly those
            # children.  A single markdown file has no such children -- its chapter
            # tree comes from its own headings -- so the option has nothing to act
            # on.  Say so on the log and drop it rather than refusing the ingest:
            # rejecting an authored-markdown upload would cost the whole chapter set.
            dropped = [key for key in self.CHAPTER_OPTION_KEYS if kwargs.get(key) is not None]
            for key in dropped:
                kwargs.pop(key, None)
            logger.info(
                f"[ParserRouter] Ignored {', '.join(dropped)} for {self._display_name(source_path)}: "
                f"chapter options act on PDF sources only "
                f"(source extension '{extension or 'none'}')"
            )
            # `chapter_requested` stays True on purpose.  The caller asked for a
            # chapter tree, and that request is what keeps the file on the internal
            # registry instead of the external parser's extension whitelist -- an
            # option the caller never asked for.

        parser_backend = normalize_parser_backend(kwargs.pop("parser_backend", None))

        normalized_feishu = (
            isinstance(source, LocalResource)
            and source.source_type == SourceType.FEISHU
            and source.meta.get("feishu_content_kind") != "file"
        )
        use_understanding = (
            not chapter_requested
            and not normalized_feishu
            and (
                parser_backend is ParserBackend.UNDERSTANDING
                or (
                    parser_backend is None
                    and self.should_use_understanding_api(
                        source,
                        resolved_extension=str(kwargs.get("resolved_extension") or ""),
                    )
                )
            )
        )

        if use_understanding and kwargs.get("split_content") is False:
            raise InvalidArgumentError(
                "parse_mode='no_split' is not supported by the configured Understanding parser."
            )

        if use_understanding:
            if isinstance(source, LocalResource):
                kwargs["source_name"] = source.meta.get("resolved_name") or Path(source_path).name
                kwargs["resolved_extension"] = (
                    kwargs.get("resolved_extension")
                    or source.meta.get("resolved_extension")
                    or extension
                )
            display = source_path
            if isinstance(source_path, str) and source_path.startswith(("http://", "https://")):
                display = "<url>"
            else:
                try:
                    display = Path(source_path).name
                except Exception:
                    display = "<path>"
            logger.info(f"[ParserRouter] Using UnderstandingAPI for {display}")
            return await self._get_understanding_api().parse(str(source_path), **kwargs)
        else:
            try:
                display = Path(source_path).name
            except Exception:
                display = "<path>"
            logger.info(f"[ParserRouter] Using internal ParserRegistry for {display}")
            return await self._parser_registry.parse(source_path, **kwargs)

    def _get_office_converter(self) -> OfficeConverter:
        """Lazily build the shared Office -> PDF converter."""
        if self._office_converter_instance is None:
            from openviking.parse.office_converter import OfficeConverter

            self._office_converter_instance = OfficeConverter()
        return self._office_converter_instance

    def set_office_converter(self, converter: OfficeConverter) -> None:
        """Inject a converter (used by tests and by callers pinning config)."""
        self._office_converter_instance = converter

    async def _parse_office(
        self,
        source: Union[str, Path, LocalResource],
        *,
        source_path: Union[str, Path],
        extension: str,
        **kwargs,
    ) -> ParseResult:
        """Convert an Office source to PDF, then parse it via the PDF parser.

        The converted PDF re-enters the *same* internal PDF parser path, so
        chapter/structure semantics are identical to a native PDF upload.
        Conversion errors (missing fonts / password / corrupt / timeout / macro)
        propagate as clear :class:`OfficeConversionError`s; there is no AnyDoc
        fallback.
        """
        converter = self._get_office_converter()
        result = await converter.convert(Path(source_path), resolved_extension=extension)
        try:
            pdf_kwargs = dict(kwargs)
            # Preserve the original filename for the resource, but parse the PDF.
            resource_name = kwargs.get("resource_name") or kwargs.get("source_name")
            if resource_name is None and isinstance(source, LocalResource):
                resource_name = source.meta.get("resolved_name")
            if resource_name is None:
                resource_name = Path(source_path).name
            pdf_kwargs["source_name"] = resource_name
            pdf_kwargs["resource_name"] = resource_name
            pdf_kwargs["resolved_extension"] = ".pdf"
            parse_result = await self._parser_registry.parse(
                result.pdf_path,
                **pdf_kwargs,
            )
        finally:
            # Intermediate PDF/profile are TTL artifacts, never persisted inputs.
            converter.cleanup(result)
            # The converter never owns the user original. Its caller owns the
            # temporary upload/download lifecycle separately from PDF cleanup.

        parse_result.source_format = extension.lstrip(".") or parse_result.source_format
        parse_result.meta.update(result.metadata())
        parse_result.meta["office_pdf_page_count"] = result.structure.get("page_count")
        parse_result.meta["office_pdf_bookmark_count"] = result.structure.get("bookmark_count")
        for warning in result.warnings:
            if warning not in parse_result.warnings:
                parse_result.warnings.append(warning)
        return parse_result

    async def submit(self, source: Union[str, Path, LocalResource], **kwargs) -> str:
        source_path = self._extract_source_path(source)
        if Path(source_path).is_file():
            source_name = (
                source.meta["resolved_name"]
                if isinstance(source, LocalResource)
                else kwargs.get("source_name")
            )
            return await self._get_understanding_api().submit_file(
                source_path,
                source_name=source_name,
                resolved_extension=(
                    source.meta["resolved_extension"]
                    if isinstance(source, LocalResource)
                    else kwargs.get("resolved_extension", "")
                ),
            )
        if not self.should_use_understanding_api(str(source_path)):
            raise ValueError("source is not routed to UnderstandingAPI")
        return await self._get_understanding_api().submit_url(str(source_path), **kwargs)

    async def upload_file(self, source: Union[str, Path, LocalResource]) -> str:
        """Upload a local source file and return only the external Files API file_id."""
        source_path = self._extract_source_path(source)
        source_name = source.meta["resolved_name"] if isinstance(source, LocalResource) else None
        return await self._get_understanding_api().upload_file(
            source_path,
            source_name=source_name,
            resolved_extension=(
                source.meta["resolved_extension"] if isinstance(source, LocalResource) else ""
            ),
        )

    def _extract_source_path(self, source: Union[str, Path, LocalResource]) -> Union[str, Path]:
        """Extract a filesystem path from the source."""
        if hasattr(source, "path"):
            return source.path
        return source

    def _get_understanding_api(self):
        if self._understanding_api is None:
            from openviking.parse.understanding_api import UnderstandingAPI

            self._understanding_api = UnderstandingAPI()
        return self._understanding_api
