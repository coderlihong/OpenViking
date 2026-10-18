# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Reindex executor runtime configuration."""

from pydantic import BaseModel, Field


class ReindexConfig(BaseModel):
    """Runtime limits for admin reindex execution."""

    file_vectorization_concurrency: int = Field(
        default=8,
        gt=0,
        description="Maximum number of files read, prepared, and enqueued concurrently",
    )
    subtree_shard_concurrency: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum number of child subtrees processed concurrently inside one "
            "recursive resource reindex. A serial recursive walk finishes the "
            "whole subtree's semantic (VLM) phase before the vector phase starts, "
            "which leaves the VLM idle for the whole vector phase; sharding runs "
            "the two phases of different children at the same time. 0 keeps the "
            "serial walk."
        ),
    )

    model_config = {"extra": "forbid"}
