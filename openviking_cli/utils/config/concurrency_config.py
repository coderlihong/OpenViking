# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Global concurrency limits.

Concurrency is a deployment decision, not a code constant: a limit that only
exists in a module cannot be raised for a bigger host, and a limit duplicated in
five places drifts. Every tunable ceiling is declared here and resolved through
:func:`concurrency_config`, so one ``ov.conf`` section governs admission control
for the whole server. Features may keep their own *defaults* (how many workers
they would like), but never a hard cap that configuration cannot raise.
"""

from pydantic import BaseModel, Field


class ConcurrencyConfig(BaseModel):
    """Deployment-wide concurrency ceilings, overridable from ``ov.conf``."""

    model_config = {"extra": "forbid"}

    max_file_vectorization: int = Field(
        default=64,
        gt=0,
        description="Ceiling for files read, prepared and enqueued concurrently by one reindex",
    )
    max_subtree_shards: int = Field(
        default=32,
        gt=0,
        description="Ceiling for child subtrees processed concurrently inside one recursive reindex",
    )
    max_request_concurrency: int = Field(
        default=10,
        gt=0,
        description="Per-process cap on concurrently admitted heavy search/MCP requests",
    )
    grep_file_concurrency: int = Field(
        default=32,
        gt=0,
        description="Ceiling for files scanned concurrently by one grep/search traversal",
    )
    max_task_store_io: int = Field(
        default=8,
        gt=0,
        description=(
            "Concurrent task-store operations (create/update/list/delete). Every HTTP "
            "request that touches the tracker queues behind this, so a single slow "
            "AGFS call otherwise stalls unrelated requests behind it."
        ),
    )


def concurrency_config() -> ConcurrencyConfig:
    """Resolve the global limits, tolerating configs that predate this section."""
    from openviking_cli.utils.config import get_openviking_config

    config = getattr(get_openviking_config(), "concurrency", None)
    return config if isinstance(config, ConcurrencyConfig) else ConcurrencyConfig()


def clamp_file_vectorization(desired: int) -> int:
    """Clamp a feature's requested file concurrency to the deployment ceiling.

    Features keep their own defaults (``reindex``, ``queue_workers.add_resource``);
    the ceiling is deployment policy and lives only here.
    """
    try:
        value = int(desired)
    except (TypeError, ValueError):
        value = 0
    return max(1, min(value, concurrency_config().max_file_vectorization))


def effective_file_vectorization_concurrency() -> int:
    """Reindex file concurrency: the feature's request, clamped by the ceiling."""
    from openviking_cli.utils.config import get_openviking_config

    desired = getattr(getattr(get_openviking_config(), "reindex", None), "file_vectorization_concurrency", 0)
    return clamp_file_vectorization(desired)


def effective_subtree_shard_concurrency() -> int:
    """Child-subtree parallelism for one recursive reindex (0 = serial walk)."""
    from openviking_cli.utils.config import get_openviking_config

    desired = getattr(getattr(get_openviking_config(), "reindex", None), "subtree_shard_concurrency", 0)
    try:
        value = int(desired or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(value, concurrency_config().max_subtree_shards))
