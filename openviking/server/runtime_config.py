# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Runtime model configuration: read, validate, persist, and hot-reload.

The model sections of ov.conf (``vlm``, ``embedding``, ``rerank``,
``query_planner``) drive LLM calls made *per request* — session compression,
parsing, retrieval rerank, query planning all re-read
``get_openviking_config()`` on every call. Swapping those four fields on the
live singleton therefore takes effect without a restart. Only the embedder
is long-lived: it is constructed once at service init and pins the vector
collection's dimension, so embedding changes require a restart.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from openviking_cli.utils.config import (
    EmbeddingConfig,
    RerankConfig,
    VLMConfig,
)
from openviking_cli.utils.config.config_loader import (
    load_json_config,
    resolve_config_path,
)
from openviking_cli.utils.config.consts import (
    DEFAULT_OV_CONF,
    OPENVIKING_CONFIG_ENV,
)
from openviking_cli.utils.config.open_viking_config import (
    OpenVikingConfigSingleton,
)
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

RUNTIME_MODEL_SECTIONS = ("vlm", "embedding", "rerank", "query_planner", "wiki")

# Sections whose consumers build fresh clients from config on every call;
# replacing the singleton field is enough for them to pick up new values.
HOT_RELOADABLE_SECTIONS = frozenset({"vlm", "rerank", "query_planner", "wiki"})

# The embedder is constructed once during service init and its vector
# dimension must match the persisted collection schema.
RESTART_REQUIRED_SECTIONS = frozenset({"embedding"})

# Optional VLM sections: an explicit null clears them and they fall back to vlm.
OPTIONAL_VLM_SECTIONS = frozenset({"query_planner", "wiki"})


class RuntimeConfigError(ValueError):
    """Raised when a runtime model config patch fails validation."""


def resolve_ov_conf_path() -> Path:
    path = resolve_config_path(None, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
    if path is None:
        raise FileNotFoundError(
            "OpenViking configuration file not found; cannot manage runtime config."
        )
    return path


def read_runtime_model_config() -> Dict[str, Any]:
    """Return the current in-process model config sections."""
    config = OpenVikingConfigSingleton.get_instance()
    return {
        "vlm": config.vlm.model_dump(),
        "embedding": config.embedding.model_dump(),
        "rerank": config.rerank.model_dump(),
        "query_planner": (
            config.query_planner.model_dump() if config.query_planner else None
        ),
        "wiki": config.wiki.model_dump() if config.wiki else None,
    }


def _validate_section(name: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{name} must be an object")
    model_cls = {
        "vlm": VLMConfig,
        "embedding": EmbeddingConfig,
        "rerank": RerankConfig,
        "query_planner": VLMConfig,
        "wiki": VLMConfig,
    }[name]
    try:
        return model_cls.model_validate(_blankify_empty_strings(value))
    except Exception as exc:
        raise RuntimeConfigError(f"Invalid {name} config: {exc}") from exc


def _blankify_empty_strings(value: Dict[str, Any]) -> Dict[str, Any]:
    """Convert empty-string form fields back to None so a half-filled section
    doesn't read as "configured but missing model" during validation."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: None
                if isinstance(v, str) and not v.strip()
                else clean(v)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [None if isinstance(v, str) and not v.strip() else clean(v) for v in node]
        return node

    return clean(value)


def apply_runtime_model_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate *patch* against the model sections, write ov.conf, hot-swap.

    Sections merge field-level: keys the patch omits keep their values from
    ov.conf, so a partial update (e.g. flipping rerank.enabled) doesn't wipe
    the rest of the section. Raises RuntimeConfigError (without touching the
    file or the live singleton) when validation fails.
    """
    unknown = set(patch) - set(RUNTIME_MODEL_SECTIONS)
    if unknown:
        raise RuntimeConfigError(
            f"Unknown config sections: {sorted(unknown)}. "
            f"Allowed: {list(RUNTIME_MODEL_SECTIONS)}"
        )
    if not patch:
        raise RuntimeConfigError("Empty patch; provide at least one section")

    # Explicit nulls in the patch mean "clear this key"; they must not reach
    # pydantic (non-Optional fields would reject them).
    clears: Dict[str, Dict[str, None]] = {
        name: {k for k, v in value.items() if v is None}
        for name, value in patch.items()
        if isinstance(value, dict)
    }
    patch = {
        name: {k: v for k, v in value.items() if v is not None}
        if isinstance(value, dict)
        else value
        for name, value in patch.items()
    }

    validated: Dict[str, Any] = {
        name: _validate_section(name, patch[name]) for name in patch
    }

    conf_path = resolve_ov_conf_path()
    data = load_json_config(conf_path)

    # Full-object validation first: the merged config must pass the same
    # cross-section checks (e.g. embedding/vectordb dimension consistency,
    # cache requirements) that startup enforces.
    merged = dict(data)
    for name, model in validated.items():
        if model is None and name in OPTIONAL_VLM_SECTIONS:
            merged[name] = None
            continue
        patched_dump = model.model_dump(mode="json", exclude_none=True)
        previous = data.get(name) if isinstance(data.get(name), dict) else {}
        # Field-level merge: patch wins, omitted keys keep the file's value.
        merged[name] = {**previous, **patched_dump}
        # Nulls in the patch clear their keys from the section.
        merged[name] = {k: v for k, v in merged[name].items() if k not in clears.get(name, ())}

    from openviking_cli.utils.config import OpenVikingConfig

    try:
        OpenVikingConfig.from_dict(merged)
    except Exception as exc:
        raise RuntimeConfigError(str(exc)) from exc

    # Persist: write to a temp file next to the target and rename over it so a
    # crash mid-write never truncates ov.conf.
    encoded = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    tmp_path = conf_path.with_suffix(conf_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(encoded)
    os.replace(tmp_path, conf_path)

    # Hot-swap the live singleton. pydantic's model_copy(update=...) skips
    # validation, but each section was already validated above. Rebuild from
    # the merged section so omitted fields keep their file values instead of
    # resetting to defaults.
    singleton = OpenVikingConfigSingleton
    current = singleton.get_instance()
    updated_fields: Dict[str, Any] = {}
    for name, model in validated.items():
        if model is None and name in OPTIONAL_VLM_SECTIONS:
            updated_fields[name] = None
        else:
            model_cls = model.__class__
            updated_fields[name] = model_cls.model_validate(merged[name])

    new_instance = current.model_copy(update=updated_fields)
    with singleton._lock:
        singleton._instance = new_instance

    # Drop cached VLM/embedder clients built from the previous config so the
    # next call rebuilds from the swapped sections.
    _invalidate_cached_instances(new_instance)

    restart_required = sorted(
        name for name in validated if name in RESTART_REQUIRED_SECTIONS
    )
    logger.info(
        "Runtime model config updated: %s (restart required: %s)",
        ", ".join(sorted(validated)),
        ", ".join(restart_required) if restart_required else "none",
    )
    return {
        "updated": sorted(validated),
        "restart_required": restart_required,
        "config": {
            "vlm": new_instance.vlm.model_dump(),
            "embedding": new_instance.embedding.model_dump(),
            "rerank": new_instance.rerank.model_dump(),
            "query_planner": (
                new_instance.query_planner.model_dump()
                if new_instance.query_planner
                else None
            ),
        },
    }


def _invalidate_cached_instances(config: Any) -> None:
    """Clear per-config cached model clients so new calls use new settings."""
    for vlm in (config.vlm, config.query_planner):
        if vlm is None:
            continue
        vlm._vlm_instance = None


def diff_sections(
    before: Dict[str, Any], after: Dict[str, Any]
) -> Tuple[str, ...]:
    return tuple(
        name
        for name in RUNTIME_MODEL_SECTIONS
        if before.get(name) != after.get(name)
    )


def _section_is_empty(value: Optional[Dict[str, Any]]) -> bool:
    if value is None:
        return True
    return not any(v is not None and v != {} and v != [] for v in value.values())
