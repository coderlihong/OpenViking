# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Authored markdown that references remote images ends up with stored bytes."""

import base64
import hashlib
from io import BytesIO
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from openviking.parse import remote_images
from openviking.parse.parsers.markdown import MarkdownParser
from openviking.parse.remote_images import (
    RemoteImage,
    RemoteImageFailure,
    collect_remote_spans,
    decode_data_uri,
    resolve_remote_images,
)
from openviking_cli.utils.config.parser_config import ParserConfig

URL = "https://images.example.org/manual/a.jpg"


def png_bytes(size=(64, 48), color="red") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


PAYLOAD = png_bytes()
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Pin the SSRF guard to a public address so tests never depend on real DNS."""
    from openviking.utils import network_guard

    monkeypatch.setattr(network_guard, "_resolve_host_addresses", lambda host: {"93.184.216.34"})


def fake_client(payload: bytes, *, status: int = 200, headers=None, error: Exception = None):
    def handler(request):
        if error is not None:
            raise error
        return httpx.Response(
            status, headers=headers or {"content-type": "image/png"}, content=payload
        )

    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_collect_remote_spans_covers_both_authored_forms_and_skips_code():
    content = (
        "![示意图](https://host/a.jpg)\n\n"
        '<figure>\n    <img src="https://host/b.png" width="300"/>\n</figure>\n\n'
        "![本地](images/c.png)\n"
        "![已入库](viking://resources/x/images/d.png)\n"
        "```\n![代码示例](https://host/not-an-image.jpg)\n```\n"
    )

    assert [span[2] for span in collect_remote_spans(content)] == [
        "https://host/a.jpg",
        "https://host/b.png",
    ]


def test_inline_data_uri_is_materialized_without_a_request():
    image = decode_data_uri(f"data:image/png;base64,{base64.b64encode(PAYLOAD).decode()}")

    assert image.data == PAYLOAD
    # Content-addressed names make re-ingestion of the same figure a no-op.
    assert image.filename == f"{DIGEST}.png"


def test_data_uri_is_refused_before_it_reaches_storage():
    with pytest.raises(RemoteImageFailure) as refused:
        decode_data_uri(f"data:image/png;base64,{base64.b64encode(png_bytes((8, 8))).decode()}")

    assert refused.value.reason == "not_image"


async def test_resolve_returns_validated_bytes_named_by_the_actual_format():
    resolutions = await resolve_remote_images([URL], client_factory=fake_client(PAYLOAD))

    asset = resolutions[URL]
    assert isinstance(asset, RemoteImage)
    # The authored reference claims .jpg; the bytes are PNG, and the stored name
    # follows the bytes so no consumer is told a format the file denies.
    assert asset.filename == f"{DIGEST}.png"


async def test_a_missing_host_is_a_reported_failure_not_an_exception():
    resolutions = await resolve_remote_images(
        [URL], client_factory=fake_client(b"", error=httpx.ConnectError("NXDOMAIN"))
    )

    failure = resolutions[URL]
    assert isinstance(failure, RemoteImageFailure)
    assert failure.reason in {"dns", "timeout"}


async def test_http_status_and_oversized_bodies_are_refused():
    not_found = await resolve_remote_images([URL], client_factory=fake_client(b"", status=404))
    assert not_found[URL].reason == "http_status"

    oversized = await resolve_remote_images(
        [URL],
        max_bytes=16,
        client_factory=fake_client(PAYLOAD, headers={"content-length": str(len(PAYLOAD))}),
    )
    assert oversized[URL].reason == "too_large"


async def test_a_body_that_is_not_an_image_is_refused():
    resolutions = await resolve_remote_images(
        [URL], client_factory=fake_client(b"not a png", headers={"content-type": "image/png"})
    )

    assert resolutions[URL].reason == "not_image"


async def test_a_non_public_destination_is_blocked_before_connecting(monkeypatch):
    from openviking.utils import network_guard

    monkeypatch.setattr(network_guard, "_resolve_host_addresses", lambda host: {"127.0.0.1"})
    resolutions = await resolve_remote_images([URL], client_factory=fake_client(PAYLOAD))

    assert resolutions[URL].reason == "blocked"


async def test_each_url_is_fetched_once_even_when_cited_twice():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=PAYLOAD)

    await resolve_remote_images(
        [URL, URL],
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert calls == [URL]


class MemoryFS:
    def __init__(self):
        self.files = {}
        self.dirs = set()

    def create_temp_uri(self):
        return "viking://temp/chapter-test"

    async def mkdir(self, uri, **kwargs):
        self.dirs.add(uri)

    async def write_file(self, uri, content, **kwargs):
        self.files[uri] = content

    async def write_file_bytes(self, uri, content, **kwargs):
        self.files[uri] = content


def chapter_plan(body: str):
    from openviking.parse.chapter_splitter import Chapter, ChapterPlan

    return ChapterPlan(
        [Chapter("c1", "10.2 进行操作时的注意事项", 1, body, 0, len(body), match_status="exact")],
        "markdown_headings",
        "matched",
        [],
    )


async def apply_chapter(body: str, resolutions, config=None, monkeypatch=None):
    fs = MemoryFS()
    parser = MarkdownParser(config=config or ParserConfig())
    monkeypatch.setattr(MarkdownParser, "_get_viking_fs", lambda _: fs)
    monkeypatch.setattr(
        MarkdownParser, "_resolve_remote_chapter_images", AsyncMock(return_value=resolutions)
    )
    layout = parser._compute_chapter_layout(
        chapter_plan(body), "viking://temp/chapter-test", "viking://temp/chapter-test/manual", "manual"
    )
    await parser._apply_layout(layout)
    published = next(uri for uri in fs.files if uri.endswith(".md") and "正文" in fs.files[uri])
    return fs, layout, published


BODY = "# 10.2进行操作时的注意事项\n\n正文\n\n![示意图](" + URL + ")\n"


async def test_chapter_publishes_the_fetched_figure_locally(monkeypatch):
    fs, layout, published = await apply_chapter(
        BODY, {URL: RemoteImage(URL, PAYLOAD, ".png")}, monkeypatch=monkeypatch
    )

    assert fs.files[f"{published.rsplit('/', 1)[0]}/images/{DIGEST}.png"] == PAYLOAD
    content = fs.files[published]
    assert f"images/{DIGEST}.png" in content
    assert URL not in content
    assert not [entry for entry in layout.meta.get("info", []) if "REMOTE" in entry["code"]]


async def test_dead_link_drops_one_occurrence_and_reports_it_instead_of_aborting(monkeypatch):
    failure = RemoteImageFailure("dns", URL, "NXDOMAIN")
    fs, layout, published = await apply_chapter(BODY, {URL: failure}, monkeypatch=monkeypatch)

    content = fs.files[published]
    assert URL not in content and "正文" in content
    reported = [entry for entry in layout.meta["info"] if entry["code"] == "CHAPTER_REMOTE_IMAGE_UNRESOLVED"]
    assert reported[0]["count"] == 1
    assert reported[0]["failures"] == [{"url": URL, "reason": "dns", "detail": "NXDOMAIN"}]
    assert not any(uri.endswith(".png") for uri in fs.files)


async def test_disabling_remote_fetch_keeps_the_document_publishable(monkeypatch):
    fs, layout, published = await apply_chapter(
        BODY, {}, config=ParserConfig(fetch_remote_images=False), monkeypatch=monkeypatch
    )

    assert URL not in fs.files[published]
    reported = [entry for entry in layout.meta["info"] if entry["code"] == "CHAPTER_REMOTE_IMAGE_UNRESOLVED"]
    assert reported[0]["failures"][0]["detail"] == "remote fetch disabled"


async def test_document_gathered_remote_references_are_fetched_once_per_document(monkeypatch):
    fs = MemoryFS()
    parser = MarkdownParser()
    monkeypatch.setattr(MarkdownParser, "_get_viking_fs", lambda _: fs)
    seen = []

    async def _resolve(references, **kwargs):
        seen.extend(references)
        return {}

    monkeypatch.setattr(remote_images, "resolve_remote_images", _resolve)
    body = "# 10.2\n\n正文\n\n![a](" + URL + ")\n\n![again](" + URL + ")\n"
    layout = parser._compute_chapter_layout(
        chapter_plan(body), "viking://temp/chapter-test", "viking://temp/chapter-test/manual", "manual"
    )
    await parser._resolve_remote_chapter_images(layout)

    assert seen == [URL]


class PublishFS(MemoryFS):
    """Published tree: only the markdown file exists, its figures are remote links."""

    def __init__(self, files):
        super().__init__()
        self.files.update(files)

    async def glob(self, pattern, uri=None, ctx=None):
        prefix = uri.rstrip("/")
        suffix = pattern.rsplit("*", 1)[-1]
        return {
            "matches": [
                key
                for key in self.files
                if key.startswith(prefix + "/") and key.endswith(suffix)
            ]
        }

    async def ls(self, uri, ctx=None):
        parent = uri.rstrip("/")
        found: dict[str, bool] = {}
        for key in list(self.files) + sorted(self.dirs):
            if not key.startswith(parent + "/"):
                continue
            head, sep, _ = key[len(parent) + 1 :].partition("/")
            found[head] = found.get(head, False) or bool(sep) or key in self.dirs
        if not found:
            raise KeyError(uri)
        return [{"name": name, "isDir": is_dir} for name, is_dir in found.items()]

    async def read_file(self, uri, ctx=None):
        return self.files[uri]

    async def rm(self, uri, **kwargs):
        self.files.pop(uri, None)


MD_URI = "viking://resources/manual/10_2/10_2.md"
MD_DIR = MD_URI.rsplit("/", 1)[0]
SIBLING = f"{MD_DIR}/images/843287508581410.jpg"


async def test_post_commit_rewrite_stores_a_remote_figure_and_points_vikingsrc_at_it(
    monkeypatch
):
    from openviking.parse import image_rewrite

    fs = PublishFS({MD_URI: "# 10.2 进行操作时的注意事项\n\n正文\n\n![示意图](" + URL + ")\n"})
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)
    requested = []

    async def _resolve(references, **kwargs):
        references = list(references)
        requested.extend(references)
        return {reference: RemoteImage(reference, PAYLOAD, ".png") for reference in references}

    monkeypatch.setattr(remote_images, "resolve_remote_images", _resolve)

    stats = await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert requested == [URL]
    assert stats["images_materialized"] == 1
    image_uri = f"viking://resources/manual/10_2/images/{DIGEST}.png"
    assert fs.files[image_uri] == PAYLOAD
    published = fs.files[MD_URI]
    assert 'vikingsrc="' + image_uri + '"' in published
    assert "<figcaption>" in published
    assert URL not in published


async def test_post_commit_rewrite_leaves_an_unreachable_reference_for_the_author_to_see(
    monkeypatch
):
    from openviking.parse import image_rewrite

    fs = PublishFS({MD_URI: "# 10.2\n\n正文\n\n![示意图](" + URL + ")\n"})
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)

    async def _resolve(references, **kwargs):
        return {reference: RemoteImageFailure("dns", reference, "NXDOMAIN") for reference in references}

    monkeypatch.setattr(remote_images, "resolve_remote_images", _resolve)

    stats = await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert stats["references_unresolved"] == 1
    assert stats["images_materialized"] == 0
    # Published prose is never silently edited down: the dead link stays visible
    # instead of the occurrence disappearing.
    assert URL in fs.files[MD_URI]


async def test_scheme_reference_resolves_the_stored_sibling_by_stem(monkeypatch):
    """Uploads carry only ``src``; an opaque scheme reference names the asset
    without its stored extension, so the sibling listing completes the pointer."""
    from openviking.parse import image_rewrite

    fs = PublishFS(
        {
            MD_URI: (
                "# 6.6.10 启动电机\n\n正文\n\n"
                '<figure>\n    <img src="kb://images/843287508581410" width="300"/>\n'
                "</figure>\n\n"
                "![示意图](kb://images/843287508577340)\n"
            ),
            SIBLING: b"jpeg-bytes",
            f"{MD_DIR}/images/843287508577340.jpeg": b"other-bytes",
        }
    )
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)

    stats = await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert stats["files_processed"] == 1
    assert stats["references_rewritten"] == 2
    published = fs.files[MD_URI]
    assert f'vikingsrc="{SIBLING}"' in published
    assert (
        f'vikingsrc="{MD_DIR}/images/843287508577340.jpeg"' in published
    )
    # The authored tag keeps its shape; only the pointer attribute is added.
    assert '<img src="kb://images/843287508581410" width="300" vikingsrc=' in published
    assert "](kb://" not in published


async def test_scheme_reference_without_a_stored_sibling_stays_untouched(monkeypatch):
    from openviking.parse import image_rewrite

    md = '# 6.6\n\n正文\n\n<img src="kb://images/999" width="300"/>\n'
    fs = PublishFS({MD_URI: md, SIBLING: b"jpeg-bytes"})
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)

    stats = await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert stats["references_rewritten"] == 0
    assert fs.files[MD_URI] == md


async def test_post_commit_rewrite_skips_fetching_when_configured_off(monkeypatch):
    from openviking.parse import image_rewrite

    fs = PublishFS({MD_URI: "# 10.2\n\n正文\n\n![示意图](" + URL + ")\n"})
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        image_rewrite, "_remote_fetch_policy", lambda: (False, remote_images.DEFAULT_TIMEOUT_SECONDS)
    )
    called = []
    monkeypatch.setattr(remote_images, "resolve_remote_images", AsyncMock(side_effect=called.append))

    await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert called == []
    assert URL in fs.files[MD_URI]


class ShadowingFS(PublishFS):
    """Reads of a published URI serve the published revision unless the caller
    asked for draft sources — the projection contract of ``VikingFS.read_file``."""

    def __init__(self, working, published):
        super().__init__(working)
        self.published = published

    async def read_file(self, uri, ctx=None):
        from openviking.indexing.content import _draft

        if uri in self.published and not _draft.get():
            return self.published[uri]
        return self.files[uri]


async def test_reingest_normalizes_the_working_tree_not_the_published_revision(monkeypatch):
    """A re-ingested branch must be rewritten from the bytes just copied in.

    Once the URI has a published revision, reads without draft sources serve
    that revision back. The rewrite pass runs right after the working tree is
    overwritten, so if it read the published copy instead it would find an
    already-normalized document (rewrite count 0), drop the mapping sidecar and
    leave the fresh verbatim markdown to be published without vikingsrc.
    """
    from openviking.parse import image_rewrite

    image_uri = f"{MD_DIR}/844775934052881.jpg"
    verbatim = "# 10.2 进行操作时的注意事项\n\n正文\n\n![示意图](images/844775934052881.jpg)\n"
    previous_revision = (
        "<figure>\n"
        f'    <img src="images/844775934052881.jpg" vikingsrc="{image_uri}"/>\n'
        "    <figcaption>\n    液压回路示意图\n    </figcaption>\n"
        "</figure>\n"
    )
    fs = ShadowingFS(
        {
            MD_URI: verbatim,
            image_uri: PAYLOAD,
            f"{MD_DIR}/.image_mappings.json": (
                '{"10_2.md": {"images/844775934052881.jpg": "844775934052881.jpg"}}'
            ),
        },
        published={MD_URI: previous_revision},
    )
    monkeypatch.setattr(image_rewrite, "get_viking_fs", lambda: fs)

    stats = await image_rewrite.rewrite_image_uris("viking://resources/manual")

    assert stats["files_processed"] == 1
    rewritten = fs.files[MD_URI]
    assert f'vikingsrc="{image_uri}"' in rewritten
    assert "<figcaption>" in rewritten
    # The outcome derives from the working tree, not the shadowing revision.
    assert "液压回路示意图" not in rewritten
    assert not any(uri.endswith(".image_mappings.json") for uri in fs.files)
