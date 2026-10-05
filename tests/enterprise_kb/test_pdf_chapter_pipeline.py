# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""PDF -> chapter tree integration using real PDF metadata and stored assets."""

import hashlib
import io
import json
from unittest.mock import AsyncMock

import pymupdf
import pytest
from PIL import Image

from openviking.parse.parsers.markdown import MarkdownParser
from openviking.parse.parsers.pdf import PDFParser
from openviking_cli.utils.config.parser_config import PDFConfig


class MemoryFS:
    def __init__(self):
        self.files = {}
        self.dirs = set()

    def create_temp_uri(self):
        return "viking://temp/chapter-test"

    async def mkdir(self, uri, **kwargs):
        self.dirs.add(uri)

    async def write_file(self, uri, content):
        self.files[uri] = content

    async def write_file_bytes(self, uri, content):
        self.files[uri] = content

    async def delete_temp(self, uri, **kwargs):
        self.files = {k: v for k, v in self.files.items() if not k.startswith(uri + "/")}


def make_pdf(path, *, bookmarks=True):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "1 Intro\nFirst body")
        doc.new_page().insert_text((72, 72), "1.1 Child\nLast body")
        if bookmarks:
            doc.set_toc([[1, "1 Intro", 1], [2, "1.1 Child", 2]])
        doc.save(path)
    return path


@pytest.fixture
def fs(monkeypatch):
    fs = MemoryFS()
    monkeypatch.setattr(MarkdownParser, "_get_viking_fs", lambda _: fs)
    return fs


async def test_real_local_pdf_creates_short_chapters_not_page_injected_headings(tmp_path, fs):
    pdf = make_pdf(tmp_path / "manual.pdf")
    parser = PDFParser(PDFConfig(strategy="local"))
    result = await parser.parse(pdf, structure_policy="bookmarks_preferred")
    assert result.temp_dir_path
    assert result.meta["alignment_status"] == "matched"
    assert result.meta["structure_source"] == "pdf_bookmarks"
    assert parser.config.heading_detection == "auto"  # request did not mutate shared config
    root = result.temp_dir_path + "/manual"
    manifest = json.loads(fs.files[root + "/_manifest.json"])
    assert [c["page_start"] for c in manifest["chapters"]] == [1, 2]
    assert fs.files[root + "/1_Intro/index.md"].count("1 Intro") == 1
    assert "Last body" in fs.files[root + "/1_Intro/1.1_Child.md"]
    assert "[1.1 Child](1_Intro/1.1_Child.md)" in fs.files[root + "/toc.md"]


async def test_structural_container_has_nonempty_heading_without_copying_child_body(fs):
    from openviking.parse.chapter_splitter import Chapter, ChapterPlan

    body = '# Child\nRepair evidence.\n'
    plan = ChapterPlan([
        Chapter('container', 'Container', 1, '', None, None, match_status='structural_only'),
        Chapter('child', 'Child', 2, body, 0, len(body), parent_id='container'),
    ], 'pdf_bookmarks', 'matched', [])
    parser = MarkdownParser()
    root = 'viking://temp/chapter-test/manual'
    layout = parser._compute_chapter_layout(plan, 'viking://temp/chapter-test', root, 'manual')
    await parser._apply_layout(layout)
    assert fs.files[root + '/Container/index.md'] == '# Container\n'
    assert fs.files[root + '/Container/Child.md'] == body
    manifest = json.loads(fs.files[root + '/_manifest.json'])
    assert manifest['chapters'][0]['match_status'] == 'structural_only'


async def test_external_markdown_without_page_anchors_auto_falls_back_and_copies_images(
    tmp_path, fs, monkeypatch
):
    pdf = make_pdf(tmp_path / "upload_x.pdf")
    data = io.BytesIO()
    Image.new("RGB", (100, 100), "red").save(data, format="PNG")
    image = tmp_path / "diagram.png"
    image.write_bytes(data.getvalue())
    markdown = "# Actual\n正文\n![diagram](diagram.png)\n\n## End\n尾部\n"
    convert = AsyncMock(return_value=(markdown, {"strategy": "mineru"}))
    monkeypatch.setattr(PDFParser, "_convert_to_markdown", convert)
    parser = PDFParser()
    result = await parser.parse(
        pdf, source_name="维修手册.pdf", structure_policy="bookmarks_preferred"
    )
    assert result.temp_dir_path
    assert result.meta["alignment_status"] == "failed"
    assert result.meta["info"][0]["code"] == "BOOKMARK_ALIGNMENT_FAILED"
    root = result.temp_dir_path + "/维修手册"
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    assert fs.files[root + f"/Actual/images/{digest}.png"] == data.getvalue()
    assert f"images/{digest}.png" in fs.files[root + "/Actual/index.md"]
    assert "尾部" in fs.files[root + "/Actual/End.md"]
    assert "<!-- Page" not in fs.files[root + "/Actual/index.md"]
    assert json.loads(fs.files[root + "/_toc.json"])["bookmarks"][0]["title"] == "1 Intro"
    assert not any(".image_mappings" in uri for uri in fs.files)


async def test_no_bookmark_pdf_rule_fallback_is_successful(tmp_path, fs, monkeypatch):
    pdf = make_pdf(tmp_path / "manual.pdf", bookmarks=False)
    monkeypatch.setattr(
        PDFParser, "_convert_to_markdown", AsyncMock(return_value=("短正文\n\n尾部", {}))
    )
    result = await PDFParser().parse(pdf, structure_policy="bookmarks_preferred")
    assert result.temp_dir_path
    assert result.meta["structure_source"] == "rule_blocks"
    assert result.meta["info"][0]["code"] == "BOOKMARKS_UNAVAILABLE"
    assert any(uri.endswith("/section_0001.md") for uri in fs.files)


async def test_missing_image_is_a_real_failure_not_alignment_info(tmp_path, fs, monkeypatch):
    pdf = make_pdf(tmp_path / "manual.pdf")
    monkeypatch.setattr(
        PDFParser,
        "_convert_to_markdown",
        AsyncMock(return_value=("# Actual\n![x](missing.png)", {})),
    )
    result = await PDFParser().parse(pdf, structure_policy="bookmarks_preferred")
    assert not result.temp_dir_path
    assert any("Missing chapter image" in warning for warning in result.warnings)


async def test_undersized_declared_image_is_skipped_not_fatal(tmp_path, fs, monkeypatch):
    pdf = make_pdf(tmp_path / "manual.pdf")
    data = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(data, format="PNG")  # below the 14px gate
    (tmp_path / "tiny.png").write_bytes(data.getvalue())
    markdown = "# Actual\n正文\n![tiny](tiny.png)\n\n## End\n尾部\n"
    monkeypatch.setattr(
        PDFParser,
        "_convert_to_markdown",
        AsyncMock(return_value=(markdown, {"strategy": "mineru"})),
    )
    result = await PDFParser(PDFConfig(strategy="local")).parse(
        pdf, source_name="维修手册.pdf", structure_policy="bookmarks_preferred"
    )
    assert result.temp_dir_path
    codes = [entry["code"] for entry in result.meta["info"]]
    assert "CHAPTER_IMAGE_QUALITY_SKIPPED" in codes
    skipped = next(entry for entry in result.meta["info"] if entry["code"] == "CHAPTER_IMAGE_QUALITY_SKIPPED")
    assert skipped["count"] == 1 and skipped["images"] == ["tiny.png"]
    root = result.temp_dir_path + "/维修手册"
    index = fs.files[root + "/Actual/index.md"]
    assert "正文" in index and "尾部" in fs.files[root + "/Actual/End.md"]
    assert "tiny.png" not in index and "![" not in index


async def test_valid_and_invalid_images_coexist_in_one_chapter(tmp_path, fs, monkeypatch):
    pdf = make_pdf(tmp_path / "manual.pdf")
    good = io.BytesIO()
    Image.new("RGB", (100, 100), "blue").save(good, format="PNG")
    tiny = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(tiny, format="PNG")  # below the 14px gate
    (tmp_path / "good.png").write_bytes(good.getvalue())
    (tmp_path / "tiny.png").write_bytes(tiny.getvalue())
    markdown = "# Actual\n正文\n![good](good.png)\n![tiny](tiny.png)\n"
    monkeypatch.setattr(
        PDFParser,
        "_convert_to_markdown",
        AsyncMock(return_value=(markdown, {"strategy": "mineru"})),
    )
    result = await PDFParser(PDFConfig(strategy="local")).parse(
        pdf, source_name="维修手册.pdf", structure_policy="bookmarks_preferred"
    )
    assert result.temp_dir_path
    root = result.temp_dir_path + "/维修手册"
    index = fs.files[root + "/Actual.md"]
    good_digest = hashlib.sha256(good.getvalue()).hexdigest()
    assert f"images/{good_digest}.png" in index and "tiny.png" not in index
    assert fs.files[root + f"/images/{good_digest}.png"] == good.getvalue()
    assert "![" not in index.replace(f"![good](images/{good_digest}.png)", "")
    skipped = [entry for entry in result.meta["info"] if entry["code"] == "CHAPTER_IMAGE_QUALITY_SKIPPED"]
    assert len(skipped) == 1 and skipped[0]["count"] == 1


async def test_filename_collision_does_not_overwrite_toc_or_repeated_chapter(
    tmp_path, fs, monkeypatch
):
    pdf = make_pdf(tmp_path / "manual.pdf")
    markdown = "# toc\nfirst\n\n# toc\nsecond\n"
    monkeypatch.setattr(PDFParser, "_convert_to_markdown", AsyncMock(return_value=(markdown, {})))
    result = await PDFParser().parse(pdf, structure_policy="bookmarks_preferred")
    root = result.temp_dir_path + "/manual"
    manifest = json.loads(fs.files[root + "/_manifest.json"])
    paths = [c["path"] for c in manifest["chapters"]]
    assert len(set(paths)) == 2
    assert "toc.md" not in paths
    assert "first" in fs.files[root + "/" + paths[0]]
    assert "second" in fs.files[root + "/" + paths[1]]


@pytest.fixture
def agfs_missing_file_contract(monkeypatch):
    # Match production RagFS's typed missing-file error in this local test double.
    from openviking.pyagfs.exceptions import AGFSNotFoundError
    from tests.utils.mock_agfs import MockLocalAGFS

    original = MockLocalAGFS.read_file

    def read(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except FileNotFoundError as exc:
            raise AGFSNotFoundError(str(exc)) from exc

    monkeypatch.setattr(MockLocalAGFS, "read_file", read)
    original_write = MockLocalAGFS.writeto

    def write(self, path, content, *args, **kwargs):
        if not isinstance(content, (str, bytes, bytearray, memoryview)):
            content = b"".join(content)
        return original_write(self, path, content, *args, **kwargs)

    monkeypatch.setattr(MockLocalAGFS, "writeto", write)


@pytest.mark.usefixtures("agfs_missing_file_contract")
async def test_service_publishes_chapters_under_requested_parent(
    service, request_context, tmp_path
):
    pdf = make_pdf(tmp_path / "manual.pdf")
    result = await service.resources._execute_resource_ingestion(
        path=str(pdf),
        ctx=request_context,
        parent="viking://resources/manuals",
        create_parent=True,
        defer_post_processing=False,
        build_index=False,
        processing_mode="vectors_only",
        structure_policy="bookmarks_preferred",
        parse_backend="local",
    )
    assert result.get("status") != "error", result
    root = result["root_uri"]
    assert root.startswith("viking://resources/manuals/manual")
    from openviking.storage.viking_fs import get_viking_fs

    fs = get_viking_fs()
    assert "First body" in await fs.read_file(root + "/1_Intro/index.md", ctx=request_context)
    assert "Last body" in await fs.read_file(root + "/1_Intro/1.1_Child.md", ctx=request_context)
    assert (
        json.loads(await fs.read_file(root + "/_manifest.json", ctx=request_context))[
            "structure_source"
        ]
        == "pdf_bookmarks"
    )
