# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Who is allowed to be told which PDF model to use, and who must receive it.

A knowledge base is uploaded as a folder -- authored markdown chapters beside
their images, sometimes a PDF or two.  The chapter-model options name a *PDF*
model, so the request has to survive the folder and reach the PDFs inside it,
while the markdown files next to them keep building their chapter tree from
their own headings.

Both halves of that contract were broken: the router rejected the whole folder
because its own name has no ``.pdf`` suffix (an authored-markdown upload with a
model named in the options ingested zero files), and even when options reached
``DirectoryParser`` they were only ever handed to UnderstandingAPI jobs, so a
native PDF child silently parsed with the default route.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openviking.parse.parser_router import ParserRouter
from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.directory import DirectoryParser

CHAPTER_KEYS = ("structure_policy", "parse_backend", "model_mode", "backend_options")


class _FakeVikingFS:
    def __init__(self) -> None:
        self._temp_counter = 0

    def create_temp_uri(self) -> str:
        self._temp_counter += 1
        return f"viking://temp/chapter_options_{self._temp_counter}"

    async def mkdir(self, _uri: str, exist_ok: bool = False) -> None:
        _ = exist_ok


def _config(monkeypatch, *, understanding_enabled: bool = False) -> None:
    config = SimpleNamespace(
        parser_api=SimpleNamespace(
            enable=understanding_enabled,
            host="https://parser.example.com",
            api_key="test-key",
            enable_feishu_url=False,
            extensions=[],
            response_timeout_seconds=1800,
            http_timeout_seconds=10.0,
            upload_simple_max_bytes=512 * 1024 * 1024,
            upload_part_size_bytes=8 * 1024 * 1024,
            enable_resumable_upload=False,
        ),
        directory=SimpleNamespace(
            preserve_structure=True,
            max_files=1000,
            max_depth=10,
            max_concurrent=4,
        ),
    )
    monkeypatch.setattr(
        "openviking_cli.utils.config.open_viking_config.get_openviking_config",
        lambda: config,
    )


def _router_capturing_registry() -> tuple[ParserRouter, AsyncMock]:
    registry = SimpleNamespace(parse=AsyncMock(side_effect=lambda *_a, **_k: object()))
    return ParserRouter(registry), registry.parse


async def test_an_authored_markdown_keeps_its_own_chapters_when_a_model_is_named(
    monkeypatch, tmp_path: Path
):
    """The markdown's chapter tree comes from its headings; naming a PDF model is
    not a reason to refuse the file."""
    _config(monkeypatch)
    chapter = tmp_path / "chapter-10-2.md"
    chapter.write_text("# 10.2 注意事项\n\n拆开软管后须装上盲堵。\n", encoding="utf-8")

    router, registry_parse = _router_capturing_registry()
    result = await router.parse(str(chapter), parse_backend="local", strict=False)

    assert result is not None
    forwarded = registry_parse.await_args.kwargs
    assert not any(forwarded.get(key) is not None for key in CHAPTER_KEYS)


async def test_a_folder_upload_reaches_the_parser_instead_of_being_refused(
    monkeypatch, tmp_path: Path
):
    """The folder itself is not a PDF, and refusing it used to cost every file in
    it -- including the PDFs that do want the named model."""
    _config(monkeypatch)
    (tmp_path / "images").mkdir()
    (tmp_path / "chapter.md").write_text("# 章\n", encoding="utf-8")

    router, registry_parse = _router_capturing_registry()
    await router.parse(str(tmp_path), parse_backend="local")

    assert registry_parse.await_args.kwargs.get("parse_backend") == "local"


@pytest.mark.parametrize("chapter_options", [{"parse_backend": "local"}, {"structure_policy": "toc"}])
async def test_the_named_model_reaches_the_pdfs_inside_a_folder(
    monkeypatch, tmp_path: Path, chapter_options: dict
):
    _config(monkeypatch)
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "chapter.md").write_text("# 章\n", encoding="utf-8")

    seen: dict[str, dict] = {}

    async def _record(classified_file, parser, *args, **kwargs):
        seen[classified_file.path.name] = dict(kwargs.get("parse_options") or {})
        return {"ok": True, "meta": {}, "error": None}

    with (
        patch.object(BaseParser, "_get_viking_fs", return_value=_FakeVikingFS()),
        patch.object(DirectoryParser, "_process_single_file", new=AsyncMock(side_effect=_record)),
        patch.object(DirectoryParser, "_merge_temp", new=AsyncMock(return_value=True)),
    ):
        await DirectoryParser().parse(str(tmp_path), strict=True, **chapter_options)

    expected = dict(chapter_options)
    assert seen.get("manual.pdf") == expected
    assert seen.get("chapter.md") == {}
