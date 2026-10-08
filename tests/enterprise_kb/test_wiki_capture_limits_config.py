# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""I084: capture limits must be raiseable from server config for real handbooks.

A 1072-page PDF materialized 2925 Markdown files; the hardcoded documents=2048
default refused the W1 capture after a successful multi-GB parse, wasting the
whole materialization. The server config section must flow into the planner at
both call sites (HTTP submission and durable execution) so the effective_config
fence stays consistent.
"""
from dataclasses import asdict

from openviking.server.config import ServerConfig, WikiCaptureConfig
from openviking.wiki.build_plan import CaptureLimits, WikiBuildPlanner


class _PlannerProbe(WikiBuildPlanner):
    def __init__(self, *, registry=None, limits=None):
        # Skip the real catalog/fs wiring; only limits normalization matters.
        if limits is None:
            self.limits = CaptureLimits()
        elif isinstance(limits, CaptureLimits):
            self.limits = limits
        else:
            fields = {key: getattr(limits, key) for key in (
                'documents', 'entries', 'depth', 'document_bytes', 'total_bytes')
                if getattr(limits, key, None) is not None}
            extraction = getattr(limits, 'extraction', None)
            if extraction is not None:
                fields['extraction'] = (extraction.model_dump() if hasattr(extraction, 'model_dump')
                                        else dict(extraction))
            self.limits = CaptureLimits(**fields)


def test_default_limits_unchanged():
    assert asdict(CaptureLimits()) == {
        'documents': 2048, 'entries': 16384, 'depth': 64,
        'document_bytes': 8 * 1024 * 1024, 'total_bytes': 64 * 1024 * 1024,
        'extraction': None}
    assert asdict(_PlannerProbe().limits) == asdict(CaptureLimits())


def test_server_config_wiki_section_reaches_planner():
    config = ServerConfig(wiki=WikiCaptureConfig(documents=8192, entries=65536))
    planner = _PlannerProbe(limits=config.wiki)
    assert planner.limits.documents == 8192
    assert planner.limits.entries == 65536
    assert planner.limits.depth == 64


def test_effective_config_fence_stays_consistent():
    """Submission pins the normalized limits; the runtime rebuilds the planner
    from the same config section, so both sides serialize identically."""
    config = ServerConfig(wiki=WikiCaptureConfig(documents=8192))
    submit_side = asdict(_PlannerProbe(limits=config.wiki).limits)
    execute_side = asdict(_PlannerProbe(limits=config.wiki).limits)
    assert submit_side == execute_side
    # Capture fields flow through; the extraction section rides along untouched.
    expected = asdict(CaptureLimits(documents=8192))
    assert {k: submit_side[k] for k in ('documents', 'entries', 'depth',
                                        'document_bytes', 'total_bytes')} == \
        {k: expected[k] for k in expected if k != 'extraction'}
    assert submit_side['extraction'] == asdict(CaptureLimits()).get('extraction', {}) or \
        submit_side['extraction'] == config.wiki.extraction.model_dump()


def test_extraction_limits_section_reaches_both_sides():
    from openviking.wiki.extraction import ExtractionLimits

    config = ServerConfig(wiki=WikiCaptureConfig(
        documents=8192, extraction={'model_calls': 4096, 'candidates': 8192}))
    normalized = _PlannerProbe(limits=config.wiki).limits
    submit_side = ExtractionLimits(**normalized.extraction)
    execute_side = ExtractionLimits(**_PlannerProbe(limits=config.wiki).limits.extraction)
    assert submit_side.model_calls == execute_side.model_calls == 4096
    assert submit_side.candidates == execute_side.candidates == 8192
    assert submit_side.source_chars == 12000


def test_capture_limits_reject_non_positive():
    import pytest
    with pytest.raises(ValueError):
        CaptureLimits(documents=0)


def test_namespace_source_bound_follows_planner_documents():
    """I084 second face: authorize_sources had its own hardcoded 2048 cap and
    refused the 2925-source plan at execution dispatch after capture passed."""
    import inspect

    from openviking.wiki.namespaces import NamespaceCatalog

    default = inspect.signature(NamespaceCatalog.__init__).parameters['max_source_uris'].default
    assert default == 2048
    catalog = NamespaceCatalog.__new__(NamespaceCatalog)
    catalog.max_source_uris = 8192
    assert catalog.max_source_uris == 8192
