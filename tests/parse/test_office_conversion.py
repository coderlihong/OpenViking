# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for KB-012 Office -> PDF -> unified parse.

Unit tests cover extension routing, conversion error mapping, TTL cleanup and
the router wiring. Real end-to-end LibreOffice conversion lives behind
``@pytest.mark.integration`` and skips when ``soffice``/``libreoffice`` is
absent, so a missing binary never produces a fake green.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.parse.base import NodeType, ParseResult, ResourceNode, create_parse_result
from openviking.parse.office_converter import (
    OFFICE_EXTENSIONS,
    OfficeConversionError,
    OfficeConversionResult,
    OfficeConverter,
    OfficeConverterConfig,
)
from openviking.parse.parser_router import ParserRouter
from openviking_cli.exceptions import InvalidArgumentError


def _soffice_available() -> bool:
    return OfficeConverter().is_available()


requires_soffice = pytest.mark.skipif(
    not _soffice_available(),
    reason="LibreOffice (soffice/libreoffice) is not installed",
)


# --------------------------------------------------------------------- fixtures
def _minimal_docx(path: Path, *, macro: bool = False) -> Path:
    """Write a structurally valid minimal OOXML Word document."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
        )
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>'
            "</w:body></w:document>",
        )
        if macro:
            archive.writestr("word/vbaProject.bin", b"\x00\x01\x02")
    return path


def _minimal_xlsx(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook/>')
    return path


# ----------------------------------------------------------- extension routing
@pytest.mark.parametrize("ext", sorted(OFFICE_EXTENSIONS))
def test_office_extension_routing(ext):
    converter = OfficeConverter()
    assert converter.is_office_extension(ext)
    assert converter.is_office_extension(ext.lstrip("."))
    assert converter.is_office_extension(ext.upper())
    assert converter.kind_for_extension(ext) == OFFICE_EXTENSIONS[ext]


@pytest.mark.parametrize("ext", [".pdf", ".txt", ".md", ".odt", ".csv", ""])
def test_non_office_extensions_are_not_routed(ext):
    converter = OfficeConverter()
    assert not converter.is_office_extension(ext)
    assert converter.kind_for_extension(ext) is None


def test_supported_extensions_match_support_matrix():
    assert set(OfficeConverter().supported_extensions) == {
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }


# --------------------------------------------------------------- error mapping
def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        OfficeConverter().convert_sync(tmp_path / "nope.docx")


def test_unsupported_extension_raises(tmp_path):
    src = tmp_path / "file.odt"
    src.write_bytes(b"PK\x03\x04")
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "unsupported_extension"


def test_empty_file_is_corrupt(tmp_path):
    src = tmp_path / "empty.docx"
    src.write_bytes(b"")
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "corrupt"


def test_corrupt_ooxml_raises(tmp_path):
    src = tmp_path / "broken.docx"
    src.write_bytes(b"not a zip container at all")
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "corrupt"


def test_password_protected_ooxml_detected(tmp_path):
    # Encrypted OOXML is an OLE/CFB container, not a zip.
    src = tmp_path / "secret.docx"
    src.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "password_protected"


def test_password_protected_legacy_detected(tmp_path):
    src = tmp_path / "secret.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"EncryptedPackage" + b"\x00" * 64)
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "password_protected"


def test_macro_document_blocked(tmp_path):
    src = _minimal_docx(tmp_path / "macro.docx", macro=True)
    with pytest.raises(OfficeConversionError) as exc:
        OfficeConverter().convert_sync(src)
    assert exc.value.reason == "macro_blocked"


def test_size_limit_enforced(tmp_path):
    src = _minimal_docx(tmp_path / "big.docx")
    converter = OfficeConverter(OfficeConverterConfig(max_input_bytes=10))
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "resource_limit"


def test_converter_unavailable_when_binary_missing(tmp_path):
    src = _minimal_docx(tmp_path / "doc.docx")
    converter = OfficeConverter(OfficeConverterConfig(binary=str(tmp_path / "no-soffice")))
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "converter_unavailable"


def test_missing_fonts_reported(tmp_path, monkeypatch):
    src = tmp_path / "fonts.docx"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:rPr><w:rFonts w:ascii="Definitely Missing Font"/>'
            "</w:rPr>",
        )
    converter = OfficeConverter()
    monkeypatch.setattr(converter, "_installed_fonts", lambda: {"Arial"})
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "missing_fonts"
    assert "Definitely Missing Font" in exc.value.details["missing_fonts"]


def test_fonts_guaranteed_by_config_do_not_fail(tmp_path, monkeypatch):
    src = tmp_path / "fonts_ok.docx"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:rFonts w:ascii="Pinned Font"/>',
        )
    converter = OfficeConverter(OfficeConverterConfig(required_fonts=("Pinned Font",)))
    monkeypatch.setattr(converter, "_installed_fonts", lambda: {"Arial"})
    monkeypatch.setattr(
        converter, "_run_libreoffice", lambda *a, **k: SimpleNamespace(returncode=0, stderr="")
    )

    def _fake_pdf(work_dir: Path, source: Path) -> Path:
        out = work_dir / f"{source.stem}.pdf"
        out.write_bytes(b"%PDF-1.7")
        return out

    monkeypatch.setattr(converter, "_locate_output_pdf", _fake_pdf)
    monkeypatch.setattr(converter, "_read_pdf_structure", lambda _p: {"page_count": 1})
    result = converter.convert_sync(src)
    assert result.pdf_path.exists()
    converter.cleanup(result)


def test_timeout_maps_to_clear_error(tmp_path, monkeypatch):
    src = _minimal_docx(tmp_path / "slow.docx")
    converter = OfficeConverter(OfficeConverterConfig(binary=sys.executable))

    def _timeout(*args, **kwargs):
        raise OfficeConversionError("Office to PDF conversion timed out", "timeout")

    monkeypatch.setattr(converter, "_run_libreoffice", _timeout)
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "timeout"


def test_nonzero_returncode_is_conversion_failed(tmp_path, monkeypatch):
    src = _minimal_docx(tmp_path / "fail.docx")
    converter = OfficeConverter(OfficeConverterConfig(binary=sys.executable))
    monkeypatch.setattr(
        converter, "_run_libreoffice",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "conversion_failed"
    assert "boom" in str(exc.value)


def test_no_pdf_output_is_conversion_failed(tmp_path, monkeypatch):
    src = _minimal_docx(tmp_path / "nopdf.docx")
    converter = OfficeConverter(OfficeConverterConfig(binary=sys.executable))
    monkeypatch.setattr(
        converter, "_run_libreoffice",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    with pytest.raises(OfficeConversionError) as exc:
        converter.convert_sync(src)
    assert exc.value.reason == "conversion_failed"


def test_failed_conversion_removes_work_dir(tmp_path, monkeypatch):
    src = _minimal_docx(tmp_path / "fail.docx")
    temp_root = tmp_path / "tmp"
    converter = OfficeConverter(
        OfficeConverterConfig(binary=sys.executable, temp_root=temp_root)
    )
    monkeypatch.setattr(
        converter, "_run_libreoffice",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(OfficeConversionError):
        converter.convert_sync(src)
    leftovers = [p for p in temp_root.iterdir()] if temp_root.exists() else []
    assert leftovers == []


def test_config_rejects_macro_execution_and_bad_timeout():
    with pytest.raises(ValueError):
        OfficeConverterConfig(macro_execution="enabled").validate()
    with pytest.raises(ValueError):
        OfficeConverterConfig(timeout=0).validate()
    with pytest.raises(ValueError):
        OfficeConverterConfig(ttl_seconds=-1).validate()


# --------------------------------------------------------------- TTL / cleanup
def _fake_result(work_dir: Path, tmp_path: Path) -> OfficeConversionResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    pdf = work_dir / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    return OfficeConversionResult(
        pdf_path=pdf,
        kind="word",
        extension=".docx",
        source_path=tmp_path / "doc.docx",
        work_dir=work_dir,
        profile_dir=work_dir / "profile",
        created_at=time.time(),
        ttl_seconds=100.0,
    )


def test_cleanup_removes_intermediate_pdf(tmp_path):
    converter = OfficeConverter(OfficeConverterConfig(temp_root=tmp_path / "root"))
    work_dir = tmp_path / "root" / "office_word_abc"
    result = _fake_result(work_dir, tmp_path)
    assert result.pdf_path.exists()
    converter.cleanup(result)
    assert not work_dir.exists()


def test_cleanup_expired_respects_ttl(tmp_path):
    temp_root = tmp_path / "root"
    converter = OfficeConverter(OfficeConverterConfig(temp_root=temp_root, ttl_seconds=100))
    fresh = temp_root / "office_word_fresh"
    stale = temp_root / "office_word_stale"
    fresh.mkdir(parents=True)
    stale.mkdir(parents=True)
    old = time.time() - 1000
    import os

    os.utime(stale, (old, old))

    removed = converter.cleanup_expired()
    assert stale in removed
    assert not stale.exists()
    assert fresh.exists()


def test_cleanup_source_only_when_configured(tmp_path):
    source = tmp_path / "keep.docx"
    source.write_bytes(b"x")
    work_dir = tmp_path / "root" / "w"
    result = _fake_result(work_dir, tmp_path)
    result.source_path = source

    converter = OfficeConverter(OfficeConverterConfig(temp_root=tmp_path / "root"))
    converter.cleanup_source(result)
    assert source.exists()

    deleting = OfficeConverter(
        OfficeConverterConfig(temp_root=tmp_path / "root", delete_source_after_parse=True)
    )
    deleting.cleanup_source(result)
    assert source.exists()  # Source ownership belongs to the ingestion lifecycle.


# ---------------------------------------------------------------- router wiring
class _FakeConverter:
    """Minimal converter stub recording the router's convert/cleanup calls."""

    def __init__(self, tmp_path: Path):
        self.converted: list[Path] = []
        self.cleaned: list[OfficeConversionResult] = []
        self.cleanup_source_calls = 0
        self._tmp = tmp_path

    def is_office_extension(self, extension: str) -> bool:
        return extension.lower().lstrip(".") in {"doc", "docx", "xls", "xlsx", "ppt", "pptx"}

    async def convert(self, source: Path, *, resource_name=None, resolved_extension=None) -> OfficeConversionResult:
        self.converted.append(Path(source))
        work_dir = self._tmp / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        pdf = work_dir / "converted.pdf"
        pdf.write_bytes(b"%PDF-1.7")
        return OfficeConversionResult(
            pdf_path=pdf,
            kind="word",
            extension=Path(source).suffix,
            source_path=Path(source),
            work_dir=work_dir,
            profile_dir=work_dir / "profile",
            created_at=time.time(),
            ttl_seconds=100.0,
            structure={"page_count": 3, "bookmark_count": 2},
            warnings=["Excel note"],
        )

    def cleanup(self, result: OfficeConversionResult) -> None:
        self.cleaned.append(result)

    def cleanup_source(self, result: OfficeConversionResult) -> None:
        self.cleanup_source_calls += 1


async def test_router_routes_office_through_converter_then_pdf(tmp_path):
    docx = tmp_path / "report.docx"
    docx.write_bytes(b"stub")
    registry = SimpleNamespace(
        parse=AsyncMock(
            return_value=create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_format="pdf",
                parser_name="PDFParser",
            )
        )
    )
    router = ParserRouter(registry)
    fake = _FakeConverter(tmp_path)
    router.set_office_converter(fake)

    result = await router.parse(str(docx))

    assert fake.converted == [docx]
    # Re-parsed through the internal registry with the converted PDF.
    registry.parse.assert_awaited_once()
    parsed_arg = registry.parse.await_args.args[0]
    assert str(parsed_arg).endswith(".pdf")
    assert registry.parse.await_args.kwargs["resolved_extension"] == ".pdf"
    # Intermediate artifacts cleaned; provenance recorded on the result.
    assert len(fake.cleaned) == 1
    assert fake.cleanup_source_calls == 0
    assert result.source_format == "docx"
    assert result.meta["office_document_kind"] == "word"
    assert result.meta["office_pdf_page_count"] == 3
    assert "Excel note" in result.warnings


async def test_router_uses_resolved_extension_for_extensionless_download(tmp_path):
    downloaded = tmp_path / "download"
    downloaded.write_bytes(b"stub")
    registry = SimpleNamespace(
        parse=AsyncMock(
            return_value=create_parse_result(root=ResourceNode(type=NodeType.ROOT))
        )
    )
    router = ParserRouter(registry)
    fake = _FakeConverter(tmp_path)
    router.set_office_converter(fake)

    await router.parse(str(downloaded), resolved_extension=".pptx")

    assert fake.converted == [downloaded]


async def test_router_structure_policy_allowed_for_office(tmp_path):
    """Office + structure_policy no longer raises the PDF-only rejection."""
    xlsx = tmp_path / "sheet.xlsx"
    xlsx.write_bytes(b"stub")
    registry = SimpleNamespace(
        parse=AsyncMock(
            return_value=create_parse_result(root=ResourceNode(type=NodeType.ROOT))
        )
    )
    router = ParserRouter(registry)
    router.set_office_converter(_FakeConverter(tmp_path))

    result = await router.parse(str(xlsx), structure_policy="bookmarks_preferred")

    assert isinstance(result, ParseResult)
    # structure_policy is forwarded to the PDF parser which owns fallback semantics.
    assert registry.parse.await_args.kwargs["structure_policy"] == "bookmarks_preferred"


async def test_router_still_rejects_non_pdf_structure_request(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# hi")
    router = ParserRouter(SimpleNamespace(parse=AsyncMock()))
    with pytest.raises(InvalidArgumentError):
        await router.parse(str(md), structure_policy="bookmarks_preferred")


async def test_router_propagates_conversion_error(tmp_path):
    docx = tmp_path / "broken.docx"
    docx.write_bytes(b"stub")
    registry = SimpleNamespace(parse=AsyncMock())
    router = ParserRouter(registry)

    class _Failing(_FakeConverter):
        async def convert(self, source: Path, *, resource_name=None, resolved_extension=None):
            raise OfficeConversionError("Corrupt Office document", "corrupt", source=source)

    router.set_office_converter(_Failing(tmp_path))
    with pytest.raises(OfficeConversionError) as exc:
        await router.parse(str(docx))
    assert exc.value.reason == "corrupt"
    registry.parse.assert_not_awaited()


async def test_router_does_not_fall_back_to_anydoc_on_failure(tmp_path):
    """A conversion failure must not silently re-route to the AnyDoc parser."""
    docx = tmp_path / "macro.docx"
    docx.write_bytes(b"stub")
    anydoc_parser = SimpleNamespace(parse=AsyncMock())
    registry = SimpleNamespace(parse=AsyncMock(), get_parser_for_file=lambda p: anydoc_parser)
    router = ParserRouter(registry)

    class _Failing(_FakeConverter):
        async def convert(self, source: Path, *, resource_name=None, resolved_extension=None):
            raise OfficeConversionError("macro blocked", "macro_blocked", source=source)

    router.set_office_converter(_Failing(tmp_path))
    with pytest.raises(OfficeConversionError):
        await router.parse(str(docx))
    registry.parse.assert_not_awaited()
    anydoc_parser.parse.assert_not_awaited()


# ------------------------------------------------- real LibreOffice conversion
@pytest.mark.integration
@requires_soffice
def test_real_docx_conversion_produces_parseable_pdf(tmp_path):
    docx = tmp_path / "hello.docx"
    _minimal_docx(docx)
    converter = OfficeConverter(OfficeConverterConfig(temp_root=tmp_path / "tmp"))
    result = converter.convert_sync(docx)
    try:
        assert result.pdf_path.is_file()
        assert result.pdf_path.read_bytes().startswith(b"%PDF")
        assert result.structure.get("page_count", 0) >= 1
    finally:
        converter.cleanup(result)


@pytest.mark.integration
@requires_soffice
async def test_real_office_conversion_is_routed_end_to_end(tmp_path):
    docx = tmp_path / "hello.docx"
    _minimal_docx(docx)
    from openviking.parse.registry import ParserRegistry

    router = ParserRouter(ParserRegistry())
    router.set_office_converter(
        OfficeConverter(OfficeConverterConfig(temp_root=tmp_path / "tmp"))
    )
    result = await router.parse(str(docx))
    assert result.source_format == "docx"
    assert result.meta.get("office_converter") == "libreoffice-headless"


@pytest.mark.integration
@requires_soffice
def test_soffice_binary_is_headless_capable():
    binary = OfficeConverter()._locate_binary()
    completed = subprocess.run(
        [binary, "--headless", "--version"], capture_output=True, text=True, timeout=60
    )
    assert shutil.which(binary) or Path(binary).is_file()
    assert completed.returncode == 0
