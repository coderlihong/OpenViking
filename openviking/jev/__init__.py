# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Isolated JEV-style typed state/decision PoC (F12 / KB-033).

This package is a self-contained research PoC. It is not imported by any core
OpenViking module (parse/indexing/retrieve/wiki/service/storage), so disabling or
removing it cannot affect core ingestion, retrieval or deterministic Wiki
traversal (TC-12.4). It never rewrites authoritative knowledge or config
applicability, and it makes no network or model calls.
"""
from openviking.jev.calibration import (
    CalibrationError,
    CalibrationReport,
    ReliabilityBin,
    brier_score,
    calibrate,
    calibrate_and_label,
    expected_calibration_error,
    probability_label,
    reliability_curve,
)
from openviking.jev.engine import Question, compare_initial_order, decide, with_calibration
from openviking.jev.state import (
    Applicability,
    AvailableCheck,
    CandidateFault,
    CompletedCheck,
    Decision,
    DecisionKind,
    DeviceContext,
    DiagnosticState,
    Evidence,
    EvidenceKind,
    FaultSemantics,
    JevError,
    NextCheck,
    QuestionKind,
    RankedCandidate,
    ScoreLabel,
    StateValidationError,
)

__all__ = [
    "Applicability",
    "AvailableCheck",
    "CalibrationError",
    "CalibrationReport",
    "CandidateFault",
    "CompletedCheck",
    "Decision",
    "DecisionKind",
    "DeviceContext",
    "DiagnosticState",
    "Evidence",
    "EvidenceKind",
    "FaultSemantics",
    "JevError",
    "NextCheck",
    "Question",
    "QuestionKind",
    "RankedCandidate",
    "ReliabilityBin",
    "ScoreLabel",
    "StateValidationError",
    "brier_score",
    "calibrate",
    "calibrate_and_label",
    "compare_initial_order",
    "decide",
    "expected_calibration_error",
    "probability_label",
    "reliability_curve",
    "with_calibration",
]