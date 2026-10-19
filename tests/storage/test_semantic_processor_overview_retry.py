# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Overview generation must converge on a smaller prompt and still write L0/L1.

Live, image-heavy directories timed out three times with the same ~200-char
per-summary floor, so the directory was left without ``.overview.md``.  These
tests pin the replacement contract: the first attempt uses the configured
sample limit unchanged, every retry halves the sample count and the per-entry
character budget, and a timeout wave still leaves a usable sidecar.
"""

from types import SimpleNamespace

import pytest

from openviking.storage.abstract_overview import write_abstract_overview
from openviking.storage.queuefs import semantic_processor as sp
from openviking.storage.queuefs.semantic_processor import SemanticProcessor

_REPLY = (
    "# 5.1 images\n\n"
    "Hydraulic schematic images from the working-principle chapter.\n\n"
    "## Contents\n\n"
    "- item"
)


class _ScriptedVLM:
    """Fail the first ``failures`` calls with a client timeout, then reply."""

    def __init__(self, failures: int = 0, reply: str = _REPLY):
        self.failures = failures
        self.reply = reply
        self.prompts = []

    def is_available(self):
        return True

    async def get_completion_async(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise TimeoutError("Request timed out.")
        return self.reply


class _FakeFS:
    """Minimal VikingFS surface used by ``write_abstract_overview``."""

    def __init__(self):
        self.files = {}
        self._async_agfs = self

    async def read_file(self, uri, ctx=None):
        if uri not in self.files:
            raise KeyError(uri)
        return self.files[uri]

    async def write_file(self, uri, content, ctx=None, lease_ref=None):
        self.files[uri] = content

    def _uri_to_path(self, uri, ctx=None):
        return uri

    async def pathlock_acquire_exact_batch(self, paths, timeout_secs=0.0):
        return {"paths": paths}

    async def pathlock_release(self, lease):
        return None


def _config(vlm, *, sample_limit: int = 32, prompt_chars: int = 100_000):
    return SimpleNamespace(
        vlm=vlm,
        semantic=SimpleNamespace(
            overview_sample_limit=sample_limit,
            max_overview_prompt_chars=prompt_chars,
            overview_batch_size=32,
            overview_max_chars=4000,
            abstract_max_chars=256,
        ),
        output_language_override="en",
    )


def _capture_prompts(monkeypatch, captured):
    def fake_render_prompt(_name, values):
        captured.append(dict(values))
        return f"files={values['file_summaries']}|children={values['children_abstracts']}"

    monkeypatch.setattr(sp, "render_prompt", fake_render_prompt)


def _files(count: int, chars: int) -> list:
    return [
        {"name": f"image_{index:02d}.png", "summary": "图" * chars}
        for index in range(count)
    ]


@pytest.fixture
def no_retry_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(sp.asyncio, "sleep", _sleep)


@pytest.mark.asyncio
async def test_happy_path_first_prompt_is_unchanged(monkeypatch):
    vlm = _ScriptedVLM()
    captured = []
    monkeypatch.setattr(sp, "get_openviking_config", lambda: _config(vlm))
    _capture_prompts(monkeypatch, captured)

    result = await SemanticProcessor()._generate_overview(
        "viking://resources/demo",
        file_summaries=[
            {"name": "a.md", "summary": "alpha"},
            {"name": "b.md", "summary": "beta"},
        ],
        children_abstracts=[{"name": "sub", "abstract": "child abstract"}],
    )

    assert result == _REPLY
    assert len(vlm.prompts) == 1
    assert captured[0]["file_summaries"] == (
        "- a.md (link: viking://input_sample_f1): alpha\n"
        "- b.md (link: viking://input_sample_f2): beta"
    )
    assert captured[0]["children_abstracts"] == (
        "- sub/ (link: viking://input_sample_c1): child abstract"
    )
    assert captured[0]["directory_coverage"] == (
        "Total direct entries: 3. All direct entries are represented in the summaries below."
    )


@pytest.mark.asyncio
async def test_first_attempt_respects_the_configured_sample_limit(monkeypatch):
    vlm = _ScriptedVLM()
    captured = []
    monkeypatch.setattr(
        sp, "get_openviking_config", lambda: _config(vlm, sample_limit=32)
    )
    _capture_prompts(monkeypatch, captured)

    await SemanticProcessor()._generate_overview(
        "viking://resources/wide/images",
        file_summaries=_files(40, 20),
        children_abstracts=[],
        total_files=40,
    )

    assert len(vlm.prompts) == 1
    assert vlm.prompts[0].count("(link: viking://input_sample_f") == 32
    assert len(captured[0]["file_summaries"].splitlines()) == 32
    assert "Summaries provided for this aggregation: 32" in captured[0]["directory_coverage"]


@pytest.mark.asyncio
async def test_timeouts_then_success_writes_overview_with_shrinking_prompts(
    monkeypatch, no_retry_sleep
):
    dir_uri = "viking://resources/raw/sy215c_menu/5_液压系统/5.1_工作原理/images"
    vlm = _ScriptedVLM(failures=2)
    monkeypatch.setattr(sp, "get_openviking_config", lambda: _config(vlm))
    monkeypatch.setattr(sp, "render_prompt", lambda _name, values: values["file_summaries"])

    processor = SemanticProcessor()
    result = await processor._generate_overview(
        dir_uri,
        file_summaries=_files(32, 400),
        children_abstracts=[],
        total_files=69,
    )

    assert result is not None
    assert "Hydraulic schematic images" in result
    assert len(vlm.prompts) == 3
    samples = [prompt.count("- image_") for prompt in vlm.prompts]
    assert samples == [32, 16, 8]
    prompt_chars = [len(prompt) for prompt in vlm.prompts]
    assert prompt_chars[0] > prompt_chars[1] > prompt_chars[2]

    overview, abstract = processor._normalize_overview_generation(result)
    fs = _FakeFS()
    wrote = await write_abstract_overview(
        viking_fs=fs,
        dir_uri=dir_uri,
        overview=overview,
        abstract=abstract,
        ctx=None,
        is_stale=lambda: False,
    )

    assert wrote.wrote
    assert "Hydraulic schematic images" in fs.files[f"{dir_uri}/.overview.md"]
    assert "Hydraulic schematic images" in fs.files[f"{dir_uri}/.abstract.md"]


@pytest.mark.asyncio
async def test_children_only_retry_also_shrinks_the_sample_count(
    monkeypatch, no_retry_sleep
):
    vlm = _ScriptedVLM(failures=2)
    monkeypatch.setattr(sp, "get_openviking_config", lambda: _config(vlm))
    monkeypatch.setattr(sp, "render_prompt", lambda _name, values: values["children_abstracts"])

    result = await SemanticProcessor()._generate_overview(
        "viking://resources/wide",
        file_summaries=[],
        children_abstracts=[
            {"name": f"child_{index:02d}", "abstract": "摘要" * 200} for index in range(32)
        ],
        total_children=513,
    )

    assert result == _REPLY
    assert len(vlm.prompts) == 3
    samples = [prompt.count("- child_") for prompt in vlm.prompts]
    assert samples == [32, 16, 8]
    assert len(vlm.prompts[0]) > len(vlm.prompts[1]) > len(vlm.prompts[2])


@pytest.mark.asyncio
async def test_exhausted_attempts_return_a_deterministic_overview(
    monkeypatch, no_retry_sleep
):
    dir_uri = "viking://resources/raw/sy215c_menu/9_故障诊断与排故/9.5_x/images"
    vlm = _ScriptedVLM(failures=3)
    monkeypatch.setattr(sp, "get_openviking_config", lambda: _config(vlm))
    monkeypatch.setattr(sp, "render_prompt", lambda _name, values: values["file_summaries"])

    processor = SemanticProcessor()
    result = await processor._generate_overview(
        dir_uri,
        file_summaries=_files(6, 120),
        children_abstracts=[],
        total_files=69,
    )

    assert len(vlm.prompts) == 3
    assert result is not None
    assert result.startswith("# images")
    assert "image_00.png" in result
    assert "[Directory overview is not ready]" not in result

    overview, abstract = processor._normalize_overview_generation(result)
    fs = _FakeFS()
    wrote = await write_abstract_overview(
        viking_fs=fs,
        dir_uri=dir_uri,
        overview=overview,
        abstract=abstract,
        ctx=None,
        is_stale=lambda: False,
    )

    assert wrote.wrote
    assert "image_00.png" in fs.files[f"{dir_uri}/.overview.md"]
    assert fs.files[f"{dir_uri}/.abstract.md"].strip()
