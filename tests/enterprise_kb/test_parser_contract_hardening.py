"""Adversarial parser identities, resident readiness, assets and own-body contracts."""
import asyncio
import importlib.util
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from openviking.parse.chapter_splitter import Chapter, ChapterPlan, split_chapters
from openviking.parse.model_artifact import BACKEND_MODES, safe_extract, validate_document, validate_options
from openviking.parse.model_backend import ParseModelBackend
from tests.enterprise_kb.test_model_artifact import make_artifact


@pytest.fixture
def artifact(tmp_path):
    archive, record = make_artifact(tmp_path)
    root = tmp_path / "document"
    safe_extract(archive, root)
    return root, record


def check(root, record, pages=2):
    (root / "parsed.json").write_text(json.dumps(record))
    return validate_document(root, backend="teleocr", mode="auto", options={}, expected_pages=pages)


@pytest.mark.parametrize("backend,mode", [("mineru", "layout"), ("teleocr", "ocr"),
                                          ("teleocr", "layout"), ("logics", "layout")])
def test_unimplemented_modes_are_not_advertised_or_silently_aliased(backend, mode):
    assert mode not in BACKEND_MODES[backend]
    with pytest.raises(ValueError, match="UNSUPPORTED_PARSE_MODE"):
        validate_options(backend, mode, {})


@pytest.mark.parametrize("options", [[], False, "ocr"])
def test_nonobject_options_are_rejected(options):
    with pytest.raises(ValueError): validate_options("mineru", "auto", options)


def test_artifact_options_must_match_digest_and_request(artifact):
    root, record = artifact
    record["options"]["layout_mode"] = "Segmentation"
    with pytest.raises(ValueError, match="options"):
        check(root, record)


def test_boolean_page_count_cannot_impersonate_one_page(artifact):
    root, record = artifact
    record["coverage"].update(input_pages=True, processed_pages=True)
    with pytest.raises(ValueError, match="coverage"):
        check(root, record, 1)


@pytest.mark.parametrize("pages", [True, 0, -1, 2.0])
def test_expected_page_count_requires_positive_integer(artifact, pages):
    root, record = artifact
    with pytest.raises(ValueError, match="page count"):
        check(root, record, pages)


def test_duplicate_block_ids_are_not_complete_coverage(artifact):
    root, record = artifact
    record["blocks"][1]["block_id"] = record["blocks"][0]["block_id"]
    with pytest.raises(ValueError, match="block coverage"):
        check(root, record)


def test_duplicate_assets_are_not_distinct_provenance(artifact):
    root, record = artifact
    record["assets"].append(record["assets"][0].copy())
    with pytest.raises(ValueError, match="Unsafe"):
        check(root, record)


def test_noncanonical_asset_path_is_rejected(artifact):
    root, record = artifact
    record["assets"][0]["path"] = "images//a.png"
    with pytest.raises(ValueError, match="Unsafe"):
        check(root, record)


def test_asset_symlink_is_rejected_even_when_pointing_inside_root(artifact):
    root, record = artifact
    image = root / "images/a.png"
    image.rename(root / "original.png")
    image.symlink_to(root / "original.png")
    with pytest.raises(ValueError, match="Missing"):
        check(root, record)


@pytest.mark.parametrize("tag", ['<img src=missing.png>', '<img SRC="missing.png">',
                                 '<img src="images/a.png" srcset="https://outside/private.png 2x">'])
def test_html_unquoted_and_srcset_references_cannot_hide_missing_or_external_images(artifact, tag):
    from openviking.parse.model_artifact import ordered_blocks
    root, record = artifact
    record["markdown"] += "\n" + tag + "\n"
    record["blocks"] = ordered_blocks(record["markdown"])
    with pytest.raises(ValueError, match="image"):
        check(root, record)


def test_url_encoded_legitimate_image_reference_resolves_safely(artifact):
    from openviking.parse.model_artifact import ordered_blocks
    root, record = artifact
    (root / "images/a.png").rename(root / "images/a b.png")
    record["assets"][0]["path"] = "images/a b.png"
    record["markdown"] = record["markdown"].replace("images/a.png", "images/a%20b.png")
    record["blocks"] = ordered_blocks(record["markdown"])
    assert check(root, record)["assets"][0]["path"] == "images/a b.png"


def test_wrong_case_image_reference_does_not_pass_on_case_sensitive_storage(artifact):
    from openviking.parse.model_artifact import ordered_blocks
    root, record = artifact
    record["markdown"] = record["markdown"].replace("images/a.png", "images/A.png")
    record["blocks"] = ordered_blocks(record["markdown"])
    with pytest.raises(ValueError, match="image"):
        check(root, record)


def test_extract_cannot_write_through_existing_symlink_parent(tmp_path):
    destination, outside = tmp_path / "output", tmp_path / "outside"
    destination.mkdir(); outside.mkdir()
    (destination / "images").symlink_to(outside, target_is_directory=True)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle: handle.writestr("images/a.png", b"private")
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract(archive, destination)
    assert not (outside / "a.png").exists()


def test_extract_cannot_overwrite_existing_file(tmp_path):
    destination = tmp_path / "output"; destination.mkdir()
    (destination / "a.png").write_bytes(b"existing")
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle: handle.writestr("a.png", b"new")
    with pytest.raises(ValueError): safe_extract(archive, destination)
    assert (destination / "a.png").read_bytes() == b"existing"


@pytest.mark.parametrize("name", ["./a.png", "images//a.png", "images/./a.png"])
def test_extract_rejects_path_aliases(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle: handle.writestr(name, b"new")
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract(archive, tmp_path / "output")


def test_empty_heading_falls_back_to_whole_rule_blocks_without_invented_title():
    content = "#\n\nbody and tail\n"
    plan = split_chapters(content, [])
    assert plan.structure_source == "rule_blocks"
    plan.validate_coverage(content)


def test_structural_only_chapter_cannot_hide_duplicate_body():
    chapters = [Chapter("branch", "branch", 1, "duplicated", None, None),
                Chapter("leaf", "leaf", 2, "body", 0, 4, parent_id="branch")]
    with pytest.raises(ValueError, match="Structural"):
        ChapterPlan(chapters, "pdf_bookmarks", "matched", []).validate_coverage("body")


def test_duplicate_chapter_ids_cannot_collide_at_public_uri():
    chapters = [Chapter("same", "one", 1, "ab", 0, 2), Chapter("same", "two", 1, "cd", 2, 4)]
    with pytest.raises(ValueError, match="unique"):
        ChapterPlan(chapters, "markdown_headings", "not_available", []).validate_coverage("abcd")


@pytest.mark.parametrize("changes", [{"section_target_tokens": True}, {"alignment_timeout": float("nan")},
                                     {"max_alignment_candidates": 0}])
def test_invalid_split_budgets_are_rejected(changes):
    with pytest.raises(ValueError, match="budgets"):
        split_chapters("body", [], **changes)


async def test_client_invalid_page_count_rejected_before_upload(tmp_path):
    called = []
    transport = httpx.MockTransport(lambda request: called.append(request))
    with pytest.raises(ValueError, match="page count"):
        await ParseModelBackend("http://worker", transport=transport).parse(tmp_path / "missing.pdf", backend="mineru", expected_pages=True)
    assert not called


@pytest.fixture
def worker(tmp_path, monkeypatch):
    directory = Path(__file__).parents[2] / "deploy/parser-models"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("parser_contract_worker", directory / "worker.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "STATE", tmp_path / "state")
    monkeypatch.setattr(module, "JOBS", tmp_path / "state/jobs")
    monkeypatch.setattr(module, "ENV", tmp_path / "env")
    monkeypatch.setattr(module, "BACKEND", "mineru")
    (module.ENV / "bin").mkdir(parents=True)
    (module.ENV / "bin/python").touch()
    (tmp_path / "locks").mkdir()
    (tmp_path / "locks/mineru.requirements.txt").touch()
    (tmp_path / "locks/mineru-models.json").touch()
    module.app.state.worker_task = SimpleNamespace(cancelled=lambda: False, done=lambda: False)
    module.app.state.recovery_complete = True
    return module


def mock_engine(worker, monkeypatch, handler):
    cls = httpx.AsyncClient
    monkeypatch.setattr(worker.httpx, "AsyncClient", lambda **kwargs: cls(transport=httpx.MockTransport(handler), **kwargs))


async def test_resident_engine_disconnect_overrides_ready_dependencies(worker, monkeypatch):
    def disconnected(request): raise httpx.ConnectError("private engine address and secret", request=request)
    mock_engine(worker, monkeypatch, disconnected)
    report = await worker.live_health()
    assert report["dependencies_ready"] and report["worker_state"] == "running"
    assert not report["engine_ready"] and not report["ready_for_jobs"]
    assert "private" not in str(report)


async def test_resident_tier_probe_and_expiry_control_readiness(worker, monkeypatch):
    mock_engine(worker, monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "standard", "current_model": "fixture-model"}]}))
    assert (await worker.live_health())["ready_for_jobs"]
    worker.app.state.engine_checked_at = time.monotonic() - 6
    assert not worker.health()["ready_for_jobs"]


@pytest.mark.parametrize("body", [{}, {"data": []}, {"data": [{"id": "standard", "current_model": ""}]}])
async def test_http_200_without_usable_tier_is_not_engine_ready(worker, monkeypatch, body):
    mock_engine(worker, monkeypatch, lambda request: httpx.Response(200, json=body))
    assert not (await worker.live_health())["ready_for_jobs"]


async def test_submit_rejects_disconnected_engine_before_reading_upload(worker, monkeypatch):
    from fastapi import HTTPException
    mock_engine(worker, monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(HTTPException) as exc:
        await worker.submit(file=None, backend="mineru", mode="auto", options="{}")
    assert exc.value.status_code == 503
    assert not worker.JOBS.exists()


async def test_partial_resident_output_is_cancelled_and_never_packaged(worker, monkeypatch):
    job_id = "a" * 32
    with worker.database() as connection:
        connection.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)", (job_id, "mineru", "auto", "{}", "running", 1, 1, ""))
    seen = []
    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/tiers": return httpx.Response(200, json={})
        if request.method == "POST": return httpx.Response(200, json={"job_id": "engine-id"})
        return httpx.Response(200, json={"status": "partial", "files": []})
    mock_engine(worker, monkeypatch, handler)
    with pytest.raises(ValueError, match="partial"):
        await worker.mineru_engine({"id": job_id, "mode": "auto", "options": "{}"}, worker.JOBS / job_id)
    assert ("DELETE", "/v1/parse/jobs/engine-id") in seen


def test_client_and_deployed_artifact_validator_are_byte_identical():
    root = Path(__file__).parents[2]
    assert (root / "openviking/parse/model_artifact.py").read_bytes() == (root / "deploy/parser-models/model_artifact.py").read_bytes()


@pytest.mark.parametrize("records", [[{"page_idx": 0}, {"page_idx": 0}], [{"page_idx": 1}, {"page_idx": 0}],
                                     [{"page_idx": False}, {"page_idx": 1}], [{}, {}]])
def test_equal_page_counts_cannot_hide_duplicate_or_unproven_page_identities(worker, records):
    with pytest.raises(ValueError, match="identities"):
        worker.validate_page_records(records, 2)


def test_native_page_identity_contract_accepts_exact_zero_based_order(worker):
    assert worker.validate_page_records([{"page_idx": 0}, {"page_idx": 1}], 2) == [0, 1]


def test_artifact_declared_page_identities_are_validated(artifact):
    root, record = artifact
    record["coverage"]["page_ids"] = [0, 0]
    with pytest.raises(ValueError, match="identity"):
        check(root, record)


async def test_reference_images_defined_in_later_chapter_are_copied_next_to_each_use(tmp_path, monkeypatch):
    from PIL import Image
    from openviking.parse.parsers.markdown import MarkdownParser
    from tests.enterprise_kb.test_pdf_chapter_pipeline import MemoryFS
    fs = MemoryFS()
    monkeypatch.setattr(MarkdownParser, "_get_viking_fs", lambda _: fs)
    image = tmp_path / "a b.png"
    Image.new("RGB", (100, 100), "red").save(image)
    source = '# A\n![figure][ref]\n\n# B\n![ref][]\n\n[ref]: a%20b.png\n'
    plan = split_chapters(source, [])
    parser = MarkdownParser()
    layout = parser._compute_chapter_layout(plan, 'viking://temp/test', 'viking://temp/test/manual', 'manual')
    await parser._apply_chapter_layout(layout, tmp_path, None)
    chapter_files = {key: value for key, value in fs.files.items() if key.endswith('/A.md') or key.endswith('/B.md')}
    assert len(chapter_files) == 2
    assert all('images/' in content for content in chapter_files.values())
    assert any(key.endswith('.png') for key in fs.files)
    assert all('![figure][ref]' not in content and '![ref][]' not in content for content in chapter_files.values())
    plan.validate_coverage(source)


async def test_unquoted_html_images_are_materialized_as_stable_content_hash_paths(tmp_path, monkeypatch):
    from PIL import Image
    from openviking.parse.parsers.markdown import MarkdownParser
    from tests.enterprise_kb.test_pdf_chapter_pipeline import MemoryFS
    fs = MemoryFS(); monkeypatch.setattr(MarkdownParser, '_get_viking_fs', lambda _: fs)
    Image.new('RGB', (100, 100), 'red').save(tmp_path / 'a.png')
    source = '# A\n<img src=a.png>\n'
    parser = MarkdownParser(); plan = split_chapters(source, [])
    layout = parser._compute_chapter_layout(plan, 'viking://temp/test', 'viking://temp/test/manual', 'manual')
    await parser._apply_chapter_layout(layout, tmp_path, None)
    stored = fs.files['viking://temp/test/manual/A.md']
    assert '<img src=images/' in stored
    assert len([key for key in fs.files if key.endswith('.png')]) == 1


def test_reference_expansion_preserves_code_examples_and_inline_images():
    from openviking.parse.parsers.markdown import MarkdownParser
    source = '`![figure][ref]`\n\n![figure](original.png)\n\n![figure][ref]\n'
    result = MarkdownParser._expand_chapter_image_references(source, {'REF': 'resolved.png', 'FIGURE': 'wrong.png'})
    assert '`![figure][ref]`' in result
    assert '![figure](original.png)' in result
    assert '![figure](<resolved.png>)' in result


def test_child_named_index_cannot_overwrite_parent_own_body():
    from openviking.parse.parsers.markdown import MarkdownParser
    source = '# Parent\nunique parent body\n\n## index\nunique child body\n'
    plan = split_chapters(source, [])
    parser = MarkdownParser()
    layout = parser._compute_chapter_layout(plan, 'viking://temp/test', 'viking://temp/test/manual', 'manual')
    writes = [op for op in layout.ops if op.kind == 'write']
    assert len({op.uri.casefold() for op in writes}) == len(writes)
    assert next(op.content for op in writes if op.uri.endswith('/Parent/index.md')) == plan.chapters[0].content
    assert len([op for op in writes if 'unique child body' in (op.content or '')]) == 1
    second = parser._compute_chapter_layout(plan, 'viking://temp/test', 'viking://temp/test/manual', 'manual')
    assert [op.uri for op in writes] == [op.uri for op in second.ops if op.kind == 'write']


@pytest.mark.parametrize('tier', ['flash', 'basic'])
def test_tier_using_different_models_cannot_claim_the_standard_pinned_revision(tier):
    with pytest.raises(ValueError, match='pinned model revision'):
        validate_options('mineru', 'auto', {'quality_tier': tier})
