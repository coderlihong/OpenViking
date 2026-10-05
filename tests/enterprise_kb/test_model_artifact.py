# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Portable artifacts cannot hide incomplete model output or lose image assets."""

import hashlib
import json
import zipfile

import httpx
import pytest
from PIL import Image

from openviking.parse.model_artifact import (
    package_document,
    safe_extract,
    validate_document,
    validate_options,
)
from openviking.parse.model_backend import ParseModelBackend


def make_artifact(tmp_path, backend="teleocr"):
    assets = tmp_path / "assets"
    (assets / "images").mkdir(parents=True)
    Image.new("RGB", (100, 100), "red").save(assets / "images/a.png")
    markdown = (
        "# Title\n正文\n\n![figure][ref]\n\n[ref]: images/a.png\n\n```md\n# not a title\n```\n尾部"
    )
    artifact = tmp_path / "artifact.zip"
    record = package_document(
        markdown,
        assets,
        artifact,
        backend=backend,
        version="test-commit",
        model_revision="test-revision",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    return artifact, record


@pytest.mark.parametrize("backend", ["mineru", "teleocr", "logics"])
def test_roundtrip_preserves_all_blocks_reference_images_and_backend_identity(tmp_path, backend):
    archive, expected = make_artifact(tmp_path, backend)
    root = tmp_path / "unpacked"
    safe_extract(archive, root)
    actual = validate_document(root, backend=backend, mode="auto", options={}, expected_pages=2)
    assert actual == expected
    assert "".join(b["text_or_markdown"] for b in actual["blocks"]) == expected["markdown"]
    assert actual["markdown"].endswith("尾部")


@pytest.mark.parametrize("name", ["../escape.png", "/escape.png", "a\\escape.png"])
def test_archive_rejects_traversal(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(name, b"x")
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract(archive, tmp_path / "out")


def test_archive_rejects_bombs_and_case_collisions(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("a.png", b"xxx")
    with pytest.raises(ValueError, match="limit"):
        safe_extract(archive, tmp_path / "out", max_bytes=2)
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("a.png", b"x")
        out.writestr("A.png", b"y")
    with pytest.raises(ValueError, match="Duplicate"):
        safe_extract(archive, tmp_path / "out")


@pytest.mark.parametrize(
    "defect", ["missing_image", "truncated_pages", "hash", "wrong_options", "blocks"]
)
def test_artifact_defects_fail_instead_of_pretending_success(tmp_path, defect):
    archive, record = make_artifact(tmp_path)
    root = tmp_path / "out"
    safe_extract(archive, root)
    if defect == "missing_image":
        (root / "images/a.png").unlink()
    elif defect == "truncated_pages":
        record["coverage"]["processed_pages"] = 1
    elif defect == "hash":
        (root / "images/a.png").write_bytes(b"corrupt")
    elif defect == "wrong_options":
        record["options_digest"] = "incorrect"
    elif defect == "blocks":
        record["blocks"].pop()
    (root / "parsed.json").write_text(json.dumps(record))
    with pytest.raises(ValueError):
        validate_document(root, backend="teleocr", mode="auto", options={}, expected_pages=2)


@pytest.mark.parametrize(
    "backend,mode,options",
    [
        ("teleocr", "text", {}),
        ("logics", "ocr", {}),
        ("logics", "layout", {"window_size": True}),
        ("teleocr", "auto", {"model_path": "/etc/passwd"}),
    ],
)
def test_backend_capability_options_rejected(backend, mode, options):
    with pytest.raises(ValueError):
        validate_options(backend, mode, options)


async def test_http_model_client_downloads_verified_portable_artifact(tmp_path):
    archive, record = make_artifact(tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-fixture")
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "1234abcd"})
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=archive.read_bytes())
        return httpx.Response(200, json={"status": "completed"})

    result = await ParseModelBackend("http://parser", transport=httpx.MockTransport(handle)).parse(
        source, backend="teleocr", expected_pages=2
    )
    assert result.markdown == record["markdown"]
    assert result.metadata["asset_count"] == 1
    assert requests == [
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs/1234abcd"),
        ("GET", "/v1/jobs/1234abcd/artifact"),
    ]
    path = result.assets_dir
    result.close()
    assert not path.exists()


async def test_model_upload_uses_frozen_source_bytes_after_original_changes(tmp_path):
    archive, _ = make_artifact(tmp_path)
    source = tmp_path / "source.pdf"
    submitted = b"%PDF-immutable-upload-fixture"
    source.write_bytes(submitted)
    uploads = []

    def handle(request):
        if request.method == "POST":
            source.write_bytes(b"different caller file after staging")
            uploads.append(request.read())
            return httpx.Response(202, json={"job_id": "1234abcd"})
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=archive.read_bytes())
        return httpx.Response(200, json={"status": "completed"})

    result = await ParseModelBackend("http://parser", transport=httpx.MockTransport(handle)).parse(
        source, backend="teleocr", expected_pages=2
    )
    try:
        assert submitted in uploads[0]
        assert source.read_bytes() not in uploads[0]
        assert result.metadata["submitted_pdf_sha256"] == hashlib.sha256(submitted).hexdigest()
    finally:
        result.close()


@pytest.mark.parametrize("matching", [True, False])
async def test_model_image_provenance_must_match_submitted_pdf(tmp_path, matching):
    archive, record = make_artifact(tmp_path, "mineru")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-provenance-fixture")
    expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    record["source_image_preservation"] = {
        "version": 1,
        "scope": "visible embedded/inline PDF image placements; vector-only figures are not identified",
        "model_quality_accepted": False,
        "source_pdf_sha256": expected_digest if matching else "0" * 64,
        "visible_placements": 0,
        "restored_placements": 0,
        "placements": [],
    }
    # Preserve a genuine validated portable artifact; alter only its claimed
    # source fingerprint, which the artifact alone cannot independently know.
    with zipfile.ZipFile(archive) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    entries["parsed.json"] = json.dumps(record).encode()
    with zipfile.ZipFile(archive, "w") as rewritten:
        for name, content in entries.items():
            rewritten.writestr(name, content)
    requests = []

    def handle(request):
        requests.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"job_id": "1234abcd"})
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=archive.read_bytes())
        return httpx.Response(200, json={"status": "completed"})

    client = ParseModelBackend("http://parser", transport=httpx.MockTransport(handle))
    if matching:
        result = await client.parse(source, backend="mineru", expected_pages=2)
        try:
            assert result.metadata["source_image_preservation"]["source_pdf_sha256"] == expected_digest
        finally:
            result.close()
    else:
        with pytest.raises(ValueError, match="does not match submitted PDF"):
            await client.parse(source, backend="mineru", expected_pages=2)
        assert requests[-1] == "DELETE"


async def test_deadline_cancels_remote_job(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-fixture")
    requests = []

    def handle(request):
        requests.append(request.method)
        return httpx.Response(
            200, json={"job_id": "1234abcd"} if request.method == "POST" else {"status": "queued"}
        )

    with pytest.raises(TimeoutError):
        await ParseModelBackend(
            "http://parser", timeout=0.01, transport=httpx.MockTransport(handle)
        ).parse(source, backend="teleocr", expected_pages=2)
    assert requests[-1] == "DELETE"


def test_fenced_html_example_is_not_an_asset_reference(tmp_path):
    archive, record = make_artifact(tmp_path)
    markdown = record["markdown"] + '\n```html\n<img src="example.png">\n```\n'
    assets = tmp_path / "assets"
    package_document(
        markdown,
        assets,
        archive,
        backend="teleocr",
        version="test",
        model_revision="test",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    root = tmp_path / "out"
    safe_extract(archive, root)
    validate_document(root, backend="teleocr", mode="auto", options={}, expected_pages=2)


def test_real_html_missing_asset_is_rejected(tmp_path):
    archive, record = make_artifact(tmp_path)
    package_document(
        record["markdown"] + '\n<img src="missing.png">\n',
        tmp_path / "assets",
        archive,
        backend="teleocr",
        version="test",
        model_revision="test",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    root = tmp_path / "out"
    safe_extract(archive, root)
    with pytest.raises(ValueError, match="image"):
        validate_document(root, backend="teleocr", mode="auto", options={}, expected_pages=2)


def test_logics_synthetic_box_images_are_stripped_with_warning(tmp_path):
    archive, _ = make_artifact(tmp_path, "logics")
    assets = tmp_path / "assets"
    markdown = (
        "# 符号\n\n"
        "<table><tr><td><img src=\"imgs/img_in_image_box_328_184_402_240.jpg\" alt=\"Image\"\" />"
        "</td><td>安全</td><td>进行作业时需要的特别安全注意事项。</td></tr>"
        "<tr><td><img src=\"imgs/img_in_image_box_329_254_402_310.jpg\" /></td><td>标注</td></tr></table>\n\n"
        "![figure](images/a.png)\n"
    )
    package_document(
        markdown,
        assets,
        archive,
        backend="logics",
        version="test",
        model_revision="test",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    root = tmp_path / "out"
    safe_extract(archive, root)
    record = validate_document(root, backend="logics", mode="auto", options={}, expected_pages=2)
    assert "img_in_image_box" not in record["markdown"]
    assert "images/a.png" in record["markdown"]
    assert "进行作业时需要的特别安全注意事项。" in record["markdown"]
    assert "安全" in record["markdown"] and "标注" in record["markdown"]
    assert "".join(b["text_or_markdown"] for b in record["blocks"]) == record["markdown"]
    warning = [w for w in record["warnings"] if w["code"] == "LOGICS_IMAGE_UNAVAILABLE"]
    assert len(warning) == 1
    assert warning[0]["unavailable_count"] == 2
    assert warning[0]["unavailable_images"] == [
        "imgs/img_in_image_box_328_184_402_240.jpg",
        "imgs/img_in_image_box_329_254_402_310.jpg",
    ]


def test_logics_declared_image_is_never_stripped(tmp_path):
    archive, record = make_artifact(tmp_path, "logics")
    root = tmp_path / "out"
    safe_extract(archive, root)
    validated = validate_document(root, backend="logics", mode="auto", options={}, expected_pages=2)
    assert validated["markdown"] == record["markdown"]
    assert validated["warnings"] == []


@pytest.mark.parametrize("backend", ["mineru", "teleocr"])
def test_unavailable_image_stripping_is_logics_only(tmp_path, backend):
    archive, _ = make_artifact(tmp_path, backend)
    package_document(
        '# Title\n正文\n\n![figure][ref]\n\n[ref]: images/a.png\n\n<img src="imgs/img_in_image_box_1_2_3_4.jpg">\n尾部',
        tmp_path / "assets",
        archive,
        backend=backend,
        version="test",
        model_revision="test",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    root = tmp_path / "out"
    safe_extract(archive, root)
    with pytest.raises(ValueError, match="image"):
        validate_document(root, backend=backend, mode="auto", options={}, expected_pages=2)


def test_logics_external_and_traversal_images_still_rejected(tmp_path):
    archive, _ = make_artifact(tmp_path, "logics")
    package_document(
        '# Title\n正文\n\n![x](https://evil.example/x.png)\n',
        tmp_path / "assets",
        archive,
        backend="logics",
        version="test",
        model_revision="test",
        mode="auto",
        options={},
        input_pages=2,
        processed_pages=2,
    )
    root = tmp_path / "out"
    safe_extract(archive, root)
    with pytest.raises(ValueError, match="image"):
        validate_document(root, backend="logics", mode="auto", options={}, expected_pages=2)
