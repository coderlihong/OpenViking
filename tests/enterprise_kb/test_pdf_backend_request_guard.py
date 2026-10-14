# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""A PDF ingestion request names a backend this deployment can actually serve.

``parse_backend`` selects the model that turns a PDF into the chapter tree the
knowledge base is built from.  Naming a backend whose endpoint was never
configured is a request error, and it used to look like a success: the config
check fired while the request-derived parser was constructed, the parse-level
handler caught it, and the import committed a zero-chapter tree with no
``toc.md`` -- silently, for every file in the batch.

The two outcomes must stay distinct.  An unserviceable *request* is rejected with
the configuration key to set.  A *conversion* failure -- the model was reached and
let this document down -- keeps degrading softly, because one broken PDF inside a
directory import must not fail the other two hundred.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.parse.model_backend import ParseModelBackend, ParsedDocument
from openviking.parse.parsers.markdown import MarkdownParser
from openviking.parse.parsers.pdf import PDFParser
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils.config.parser_config import PDFConfig
from tests.enterprise_kb.test_pdf_chapter_pipeline import MemoryFS, make_pdf

TELEOCR_ENDPOINT = "http://parser.invalid:8000"


def registered_config(**overrides) -> PDFConfig:
    """A deployment that really serves the artifact protocol for teleocr."""

    return PDFConfig(
        strategy="local",
        model_endpoints={"teleocr": TELEOCR_ENDPOINT},
        default_model_backend="teleocr",
        **overrides,
    )


MODEL_MARKDOWN = "# 第 1 章 安全须知\n正文一\n\n# 第 2 章 拆装\n正文二\n"


def _document(markdown: str, assets_dir: Path) -> ParsedDocument:
    """The shape one completed artifact job hands back, minus the HTTP round trip."""

    return ParsedDocument(
        markdown=markdown,
        assets_dir=assets_dir,
        metadata={"backend": "teleocr", "mode": "auto"},
        temporary=SimpleNamespace(cleanup=lambda: None),
    )


@pytest.fixture
def fs(monkeypatch):
    fs = MemoryFS()
    monkeypatch.setattr(MarkdownParser, "_get_viking_fs", lambda _: fs)
    return fs


@pytest.mark.parametrize(
    "config,backend,expected",
    [
        (PDFConfig(strategy="local"), "teleocr", r"pdf\.model_endpoints\.teleocr"),
        (PDFConfig(strategy="local"), "logics", r"pdf\.model_endpoints\.logics"),
        (PDFConfig(strategy="local"), "mineru", r"pdf\.model_endpoints\.mineru"),
        (registered_config(), "mineru", r"pdf\.model_endpoints\.mineru"),
        (PDFConfig(strategy="local"), "gpt-visual", "Unsupported PDF parse_backend"),
    ],
)
def test_a_backend_the_deployment_cannot_serve_is_a_request_error(config, backend, expected):
    with pytest.raises(InvalidArgumentError, match=expected) as caught:
        PDFParser(config)._require_serviceable_backend(backend)

    assert caught.value.details["parse_backend"] == backend


async def test_an_unregistered_backend_never_reaches_an_empty_ingestion(tmp_path, fs):
    pdf = make_pdf(tmp_path / "manual.pdf")
    parser = PDFParser(PDFConfig(strategy="local"))

    # Raised out to the caller, not returned as a warning-only empty result: a
    # committed flat tree would look like a successful import of a manual that
    # lost every chapter and its navigation index.
    with pytest.raises(InvalidArgumentError, match="teleocr"):
        await parser.parse(pdf, parse_backend="teleocr")


@pytest.mark.parametrize(
    "config,backend",
    [
        (PDFConfig(strategy="local"), None),
        (PDFConfig(strategy="local"), "local"),
        (PDFConfig(strategy="local"), "auto"),
        (PDFConfig(strategy="local", mineru_endpoint="http://mineru.invalid"), "mineru"),
        (registered_config(), "teleocr"),
        (registered_config(), "auto"),
    ],
)
def test_a_serviceable_backend_request_is_allowed_through(config, backend):
    PDFParser(config)._require_serviceable_backend(backend)


async def test_a_registered_backend_reaches_the_model_and_fails_softly(tmp_path, fs, monkeypatch):
    pdf = make_pdf(tmp_path / "manual.pdf")
    calls = []

    async def submit(self, path, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("model died")

    monkeypatch.setattr(ParseModelBackend, "parse", submit)

    result = await PDFParser(registered_config()).parse(pdf, parse_backend="teleocr")

    assert calls and calls[0]["backend"] == "teleocr"
    assert calls[0]["expected_pages"] == 2
    assert result.warnings == ["Failed to parse PDF: model died"]
    assert not result.temp_dir_path


async def test_a_pdf_pymupdf_refuses_is_still_counted_and_still_submitted(
    tmp_path, fs, monkeypatch
):
    """The page expectation falls back to the other PDF stack before the file is lost.

    ``expected_pages`` is what the artifact contract checks returned page coverage
    against, so it has to be measured locally -- but measuring it is not pymupdf's
    exclusive job.  A manual whose repaired cross-reference table pymupdf rejects is
    still countable by the pdfminer stack the local strategy already depends on, and
    dropping it from the corpus would turn a reader quirk into missing coverage.
    """

    pdf = make_pdf(tmp_path / "manual.pdf")
    calls = []
    assets = tmp_path / "model-document"
    assets.mkdir()

    async def submit(self, path, **kwargs):
        calls.append(kwargs)
        return _document(MODEL_MARKDOWN, assets)

    def refuse(path, *args, **kwargs):
        raise RuntimeError("Failed to open file as type pdf")

    monkeypatch.setattr(ParseModelBackend, "parse", submit)
    monkeypatch.setattr("pymupdf.open", refuse)

    result = await PDFParser(registered_config()).parse(pdf, parse_backend="teleocr")

    assert calls[0]["expected_pages"] == 2
    assert result.meta["page_count_source"] == "pdfplumber"
    assert result.temp_dir_path


async def test_a_pdf_neither_local_stack_opens_is_not_submitted_and_costs_one_file(
    tmp_path, fs, monkeypatch
):
    """An uncountable document is the only thing the failure is allowed to take.

    The page expectation cannot be fabricated, so the model is not called at all --
    what matters is that the damage stops at this file: the directory import commits
    every other document and records this one as failed.
    """

    truncated = tmp_path / "broken.pdf"
    truncated.write_bytes(b"%PDF-1.7")
    submitted = AsyncMock(side_effect=AssertionError("the model must not be reached"))
    monkeypatch.setattr(ParseModelBackend, "parse", submitted)

    result = await PDFParser(registered_config()).parse(truncated, parse_backend="teleocr")

    submitted.assert_not_awaited()
    assert not result.temp_dir_path
    assert result.warnings[0].startswith("Failed to parse PDF:")


async def test_a_document_without_a_readable_outline_is_split_by_its_own_headings(
    tmp_path, fs, monkeypatch
):
    """A failed outline read is a degraded structure, not a lost document.

    The bookmark path exists because a manual's own table of contents is the best
    chapter anchor.  When the local reader cannot supply it -- after the model has
    already spent minutes on the pages -- ``split_chapters`` is designed to group by
    the document's own headings, so the chapter tree survives and the degradation is
    recorded on the result for whoever audits structure.
    """

    pdf = make_pdf(tmp_path / "manual.pdf")
    assets = tmp_path / "model-document"
    assets.mkdir()

    async def submit(self, path, **kwargs):
        return _document(MODEL_MARKDOWN, assets)

    def refuse_outline(path):
        raise RuntimeError("outline reader died")

    monkeypatch.setattr(ParseModelBackend, "parse", submit)
    monkeypatch.setattr(PDFParser, "_read_pdf_bookmarks", staticmethod(refuse_outline))

    result = await PDFParser(registered_config()).parse(pdf, parse_backend="teleocr")

    assert result.temp_dir_path
    assert result.meta["structure_note"] == "bookmarks_unavailable: outline reader died"
    assert result.warnings == ["bookmarks_unavailable: outline reader died"]
    assert [uri for uri in fs.files if uri.endswith("/toc.md")]
