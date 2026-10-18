"""A failed directory-overview generation must not destroy published L0/L1 text.

The semantic stage used to answer a dead/timing-out model with
``[Directory overview is not generated]`` and write that over a good
``.abstract.md``/``.overview.md``.  It now reports "no content" so the caller
keeps the sidecar that is already on disk and the run surfaces a real failure.
"""

import asyncio
import types

from openviking.storage.queuefs import semantic_processor as sp


class _DeadVLM:
    def is_available(self):
        return True

    async def get_completion_async(self, prompt):  # pragma: no cover - always raises
        raise RuntimeError("Connection error.")


class _AliveVLM:
    def is_available(self):
        return True

    async def get_completion_async(self, prompt):
        return "# y\n\nA real overview."


def _config(vlm):
    return types.SimpleNamespace(
        vlm=vlm,
        semantic=types.SimpleNamespace(
            max_overview_prompt_chars=100000,
            overview_batch_size=32,
        ),
    )


def _processor(monkeypatch, vlm):
    monkeypatch.setattr(sp, "render_prompt", lambda *args, **kwargs: "prompt")
    monkeypatch.setattr(sp, "get_openviking_config", lambda: _config(vlm))
    return sp.SemanticProcessor(max_concurrent_llm=1)


def test_failed_overview_generation_reports_no_content(monkeypatch):
    processor = _processor(monkeypatch, _DeadVLM())
    result = asyncio.run(
        processor._generate_overview("viking://resources/raw/menus/m/1_x", [], [])
    )
    assert result is None


def test_successful_overview_still_returns_content(monkeypatch):
    processor = _processor(monkeypatch, _AliveVLM())
    result = asyncio.run(
        processor._generate_overview("viking://resources/raw/menus/m/1_x", [], [])
    )
    assert result == "# y\n\nA real overview."


def test_no_placeholder_text_is_ever_produced():
    import pathlib

    source = pathlib.Path(sp.__file__).read_text(encoding="utf-8")
    assert "Directory overview is not generated" not in source


def test_stale_placeholder_sidecars_are_identified_for_removal():
    """A directory left with only placeholder sidecars must not "keep" them."""
    from openviking.storage.queuefs.semantic_dag import placeholder_sidecar_names

    marker = "[Directory overview is not generated]"
    assert placeholder_sidecar_names(f"# 1_x\n\n{marker}", f"# 1_x\n\n{marker}") == [
        ".overview.md",
        ".abstract.md",
    ]
    assert placeholder_sidecar_names("# real overview", "# real abstract") == []
    assert placeholder_sidecar_names(None, f"# 1_x\n\n{marker}") == [".abstract.md"]
    assert placeholder_sidecar_names(None, None) == []


def test_placeholder_sidecars_are_treated_as_missing_so_the_directory_refreshes():
    """The incremental path must not skip a directory whose sidecars are placeholders."""
    from openviking.storage.queuefs.semantic_dag import strip_placeholder_sidecars

    marker = "[Directory overview is not generated]"
    assert strip_placeholder_sidecars(f"# 1_x\n\n{marker}", "# real abstract") == (
        None,
        "# real abstract",
    )
    assert strip_placeholder_sidecars("# real overview", f"# 1_x\n\n{marker}") == (
        "# real overview",
        None,
    )
    assert strip_placeholder_sidecars("# real overview", "# real abstract") == (
        "# real overview",
        "# real abstract",
    )
    assert strip_placeholder_sidecars(None, None) == (None, None)


def test_sidecar_lookup_falls_back_to_the_directory_uri_on_a_full_reindex():
    """A full reindex has no incremental target path; sidecars live in the dir."""
    from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor

    executor = SemanticDagExecutor.__new__(SemanticDagExecutor)
    executor._get_target_file_path = lambda _uri: None
    assert (
        executor._sidecar_dir_path("viking://resources/raw/menus/1/images")
        == "viking://resources/raw/menus/1/images"
    )
    executor._get_target_file_path = lambda _uri: "viking://resources/raw/menus/1"
    assert (
        executor._sidecar_dir_path("viking://resources/raw/menus/1/images")
        == "viking://resources/raw/menus/1"
    )
