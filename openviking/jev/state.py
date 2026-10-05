# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Typesafe state/decision models for the JEV-style diagnostic PoC (F12 / KB-033).

This is an isolated research PoC. It borrows the design idea "typed state +
typed question -> structured decision" from the Eigent / TypeSafe JEV material.
It does NOT claim a JEV service is integrated, does not use local model weights,
and never rewrites authoritative Wiki knowledge or config applicability.

All models are frozen dataclasses with explicit enums. Invalid input is rejected
with :class:`StateValidationError` instead of being silently coerced.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum


class JevError(ValueError):
    """Base error for the JEV PoC package."""


class StateValidationError(JevError):
    """Raised when a typed state/decision violates the PoC contract."""


class Applicability(str, Enum):
    """Applicability of a candidate fault under the known configuration."""

    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNDETERMINED = "undetermined"


class EvidenceKind(str, Enum):
    """Kind of observed evidence carried by the diagnostic state."""

    DTC = "dtc"
    MEASUREMENT = "measurement"
    OBSERVATION = "observation"
    SOURCE = "source"


class ScoreLabel(str, Enum):
    """Whether a ranking value may be called a probability or only a score."""

    SCORE = "score"
    PROBABILITY = "probability"


class FaultSemantics(str, Enum):
    """Fault population semantics; the two must never be force-normalized alike.

    SINGLE_FAULT_MUTUALLY_EXCLUSIVE: exactly one candidate is the true fault, so
    relative masses may be normalized to sum to 1. MULTI_FAULT_INDEPENDENT: each
    candidate is an independent event, so scores must NOT be normalized to 1.
    """

    SINGLE_FAULT_MUTUALLY_EXCLUSIVE = "single_fault_mutually_exclusive"
    MULTI_FAULT_INDEPENDENT = "multi_fault_independent"


class DecisionKind(str, Enum):
    """Kind of structured decision returned by the engine."""

    RANKING = "ranking"
    ABSTAIN = "abstain"
    NEEDS_MORE_INFO = "needs_more_info"


class QuestionKind(str, Enum):
    """Typed questions the deterministic engine can answer."""

    RANK_CANDIDATE_FAULTS = "rank_candidate_faults"
    NEXT_CHECK = "next_check"
    CONFIRM_ROOT_CAUSE = "confirm_root_cause"


def _ensure_uri(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateValidationError(f"{label} must be a non-empty Viking URI")
    if not value.startswith("viking://"):
        raise StateValidationError(f"{label} must be a Viking URI")
    if any(ord(char) < 33 for char in value) or "\\" in value:
        raise StateValidationError(f"{label} contains invalid characters")
    parts = value.split("/")
    if any(part in {".", ".."} for part in parts):
        raise StateValidationError(f"{label} must not contain relative path segments")
    return value


def _ensure_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateValidationError(f"{label} must be a non-empty string")
    return value


def _ensure_enum(value: object, enum_type, label: str):
    if not isinstance(value, enum_type):
        raise StateValidationError(f"{label} must be a {enum_type.__name__} value")
    return value


def _ensure_uri_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise StateValidationError(f"{label} must be a sequence of Viking URIs")
    items = tuple(value)
    for item in items:
        _ensure_uri(item, label)
    if len(set(items)) != len(items):
        raise StateValidationError(f"{label} must not contain duplicate URIs")
    return items


def _ensure_text_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise StateValidationError(f"{label} must be a sequence of strings")
    items = tuple(value)
    for item in items:
        _ensure_text(item, label)
    if len(set(items)) != len(items):
        raise StateValidationError(f"{label} must not contain duplicate entries")
    return items


def _ensure_finite(value: object, label: str, *, minimum: float | None = None) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise StateValidationError(f"{label} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise StateValidationError(f"{label} must be finite")
    if minimum is not None and number < minimum:
        raise StateValidationError(f"{label} must be >= {minimum}")
    return number


@dataclass(frozen=True)
class Evidence:
    """A single observed evidence item, keyed by its authoritative URI."""

    uri: str
    kind: EvidenceKind
    observed: bool
    summary: str = ""

    def __post_init__(self) -> None:
        _ensure_uri(self.uri, "evidence.uri")
        _ensure_enum(self.kind, EvidenceKind, "evidence.kind")
        if type(self.observed) is not bool:
            raise StateValidationError("evidence.observed must be a boolean")
        if not isinstance(self.summary, str):
            raise StateValidationError("evidence.summary must be a string")


@dataclass(frozen=True)
class CandidateFault:
    """A candidate fault with its authoritative edges and applicability."""

    fault_uri: str
    component_uri: str
    failure_mode: str
    applicability: Applicability = Applicability.UNDETERMINED
    evidence_uris: tuple[str, ...] = ()
    initial_rank: int | None = None

    def __post_init__(self) -> None:
        _ensure_uri(self.fault_uri, "candidate_fault.fault_uri")
        _ensure_uri(self.component_uri, "candidate_fault.component_uri")
        _ensure_text(self.failure_mode, "candidate_fault.failure_mode")
        _ensure_enum(self.applicability, Applicability, "candidate_fault.applicability")
        object.__setattr__(
            self,
            "evidence_uris",
            _ensure_uri_tuple(self.evidence_uris, "candidate_fault.evidence_uris"),
        )
        if self.initial_rank is not None and (
            type(self.initial_rank) is not int or self.initial_rank < 0
        ):
            raise StateValidationError(
                "candidate_fault.initial_rank must be a non-negative integer"
            )


@dataclass(frozen=True)
class DeviceContext:
    """Device / configuration anchors; ``config_known`` derives from the anchor."""

    device_uri: str | None = None
    config_spec_uri: str | None = None

    def __post_init__(self) -> None:
        if self.device_uri is not None:
            _ensure_uri(self.device_uri, "device.device_uri")
        if self.config_spec_uri is not None:
            _ensure_uri(self.config_spec_uri, "device.config_spec_uri")

    @property
    def config_known(self) -> bool:
        return self.config_spec_uri is not None


@dataclass(frozen=True)
class CompletedCheck:
    """A check already performed; ``resolved`` means it discriminated faults."""

    check_id: str
    procedure_uri: str | None = None
    resolved: bool = False
    summary: str = ""

    def __post_init__(self) -> None:
        _ensure_text(self.check_id, "completed_check.check_id")
        if self.procedure_uri is not None:
            _ensure_uri(self.procedure_uri, "completed_check.procedure_uri")
        if type(self.resolved) is not bool:
            raise StateValidationError("completed_check.resolved must be a boolean")
        if not isinstance(self.summary, str):
            raise StateValidationError("completed_check.summary must be a string")


@dataclass(frozen=True)
class AvailableCheck:
    """A candidate procedure the engine may recommend as the next check."""

    check_id: str
    procedure_uri: str
    target_components: tuple[str, ...]
    estimated_cost: float
    discriminates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ensure_text(self.check_id, "available_check.check_id")
        _ensure_uri(self.procedure_uri, "available_check.procedure_uri")
        object.__setattr__(
            self,
            "target_components",
            _ensure_uri_tuple(self.target_components, "available_check.target_components"),
        )
        if not self.target_components:
            raise StateValidationError("available_check.target_components must not be empty")
        object.__setattr__(
            self,
            "estimated_cost",
            _ensure_finite(self.estimated_cost, "estimated_cost", minimum=0.0),
        )
        object.__setattr__(
            self,
            "discriminates",
            _ensure_uri_tuple(self.discriminates, "available_check.discriminates"),
        )


@dataclass(frozen=True)
class DiagnosticState:
    """Canonical typed diagnostic state; the engine never mutates it in place."""

    canonical_symptoms: tuple[str, ...]
    device: DeviceContext
    evidence: tuple[Evidence, ...] = ()
    completed_checks: tuple[CompletedCheck, ...] = ()
    candidate_faults: tuple[CandidateFault, ...] = ()
    available_checks: tuple[AvailableCheck, ...] = ()
    unknowns: tuple[str, ...] = ()
    semantics: FaultSemantics = FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_symptoms",
            _ensure_text_tuple(self.canonical_symptoms, "canonical_symptoms"),
        )
        if not self.canonical_symptoms:
            raise StateValidationError("canonical_symptoms must not be empty")
        if not isinstance(self.device, DeviceContext):
            raise StateValidationError("device must be a DeviceContext")
        _ensure_enum(self.semantics, FaultSemantics, "semantics")
        for name in ("evidence", "completed_checks", "candidate_faults", "available_checks"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise StateValidationError(f"{name} must be a sequence")
            object.__setattr__(self, name, tuple(value))
        object.__setattr__(self, "unknowns", _ensure_text_tuple(self.unknowns, "unknowns"))
        self._check_members()
        self._check_uniqueness()

    def _check_members(self) -> None:
        for item in self.evidence:
            if not isinstance(item, Evidence):
                raise StateValidationError("evidence entries must be Evidence")
        for item in self.completed_checks:
            if not isinstance(item, CompletedCheck):
                raise StateValidationError("completed_checks entries must be CompletedCheck")
        for item in self.candidate_faults:
            if not isinstance(item, CandidateFault):
                raise StateValidationError("candidate_faults entries must be CandidateFault")
        for item in self.available_checks:
            if not isinstance(item, AvailableCheck):
                raise StateValidationError("available_checks entries must be AvailableCheck")

    def _check_uniqueness(self) -> None:
        self._unique("evidence URIs", [item.uri for item in self.evidence])
        self._unique("check ids", [item.check_id for item in self.completed_checks])
        self._unique("candidate fault URIs", [item.fault_uri for item in self.candidate_faults])
        self._unique("available check ids", [item.check_id for item in self.available_checks])
        declared = {item.uri for item in self.evidence}
        for fault in self.candidate_faults:
            missing = set(fault.evidence_uris) - declared
            if missing:
                raise StateValidationError(
                    "candidate fault references undeclared evidence: " + ", ".join(sorted(missing))
                )

    @staticmethod
    def _unique(label: str, values: list[str]) -> None:
        if len(set(values)) != len(values):
            raise StateValidationError(f"duplicate {label} in state")

    @property
    def observed_evidence_uris(self) -> frozenset[str]:
        return frozenset(item.uri for item in self.evidence if item.observed)

    @property
    def applicable_faults(self) -> tuple[CandidateFault, ...]:
        return tuple(
            fault
            for fault in self.candidate_faults
            if fault.applicability is Applicability.APPLICABLE
        )

    @property
    def not_applicable_faults(self) -> tuple[CandidateFault, ...]:
        return tuple(
            fault
            for fault in self.candidate_faults
            if fault.applicability is Applicability.NOT_APPLICABLE
        )


@dataclass(frozen=True)
class RankedCandidate:
    """A ranked candidate fault carrying a score and its (separate) ranking."""

    fault_uri: str
    component_uri: str
    failure_mode: str
    rank: int
    score: float
    score_label: ScoreLabel
    evidence_uris: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ensure_uri(self.fault_uri, "ranked_candidate.fault_uri")
        _ensure_uri(self.component_uri, "ranked_candidate.component_uri")
        _ensure_text(self.failure_mode, "ranked_candidate.failure_mode")
        if type(self.rank) is not int or self.rank < 0:
            raise StateValidationError("ranked_candidate.rank must be a non-negative integer")
        object.__setattr__(
            self, "score", _ensure_finite(self.score, "ranked_candidate.score", minimum=0.0)
        )
        _ensure_enum(self.score_label, ScoreLabel, "ranked_candidate.score_label")
        object.__setattr__(
            self,
            "evidence_uris",
            _ensure_uri_tuple(self.evidence_uris, "ranked_candidate.evidence_uris"),
        )


@dataclass(frozen=True)
class NextCheck:
    """A recommended next check with its deterministic selection rationale."""

    check_id: str
    procedure_uri: str
    estimated_cost: float
    rationale: str
    discriminates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _ensure_text(self.check_id, "next_check.check_id")
        _ensure_uri(self.procedure_uri, "next_check.procedure_uri")
        object.__setattr__(
            self, "estimated_cost", _ensure_finite(self.estimated_cost, "cost", minimum=0.0)
        )
        _ensure_text(self.rationale, "next_check.rationale")
        object.__setattr__(
            self,
            "discriminates",
            _ensure_uri_tuple(self.discriminates, "next_check.discriminates"),
        )


@dataclass(frozen=True)
class Decision:
    """Structured decision: ranking + next check + evidence + confidence info.

    ``kind`` is RANKING when a ranked shortlist exists, NEEDS_MORE_INFO when the
    evidence is insufficient but a discriminating check is available, and ABSTAIN
    when the engine cannot responsibly proceed. The engine never fabricates state.
    """

    kind: DecisionKind
    semantics: FaultSemantics
    score_label: ScoreLabel
    ranking: tuple[RankedCandidate, ...] = ()
    next_check: NextCheck | None = None
    evidence_uris: tuple[str, ...] = ()
    confidence: float | None = None
    reasons: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _ensure_enum(self.kind, DecisionKind, "decision.kind")
        _ensure_enum(self.semantics, FaultSemantics, "decision.semantics")
        _ensure_enum(self.score_label, ScoreLabel, "decision.score_label")
        if self.confidence is not None:
            object.__setattr__(
                self, "confidence", _ensure_finite(self.confidence, "confidence")
            )
        object.__setattr__(self, "ranking", tuple(self.ranking))
        for item in self.ranking:
            if not isinstance(item, RankedCandidate):
                raise StateValidationError("ranking entries must be RankedCandidate")
        if self.next_check is not None and not isinstance(self.next_check, NextCheck):
            raise StateValidationError("next_check must be a NextCheck")
        object.__setattr__(
            self, "evidence_uris", _ensure_uri_tuple(self.evidence_uris, "decision.evidence_uris")
        )
        object.__setattr__(self, "reasons", _ensure_text_tuple(self.reasons, "decision.reasons"))
        object.__setattr__(
            self, "unknowns", _ensure_text_tuple(self.unknowns, "decision.unknowns")
        )
        if self.kind is DecisionKind.RANKING and not self.ranking:
            raise StateValidationError("a RANKING decision must carry at least one candidate")
        if self.kind is DecisionKind.ABSTAIN and self.ranking:
            raise StateValidationError("an ABSTAIN decision must not carry ranked candidates")
        if self.kind is DecisionKind.ABSTAIN and self.next_check is not None:
            raise StateValidationError("an ABSTAIN decision must not recommend a check")

    @property
    def score_sum(self) -> float:
        return float(sum(item.score for item in self.ranking))

    @property
    def normalized_to_one(self) -> bool:
        """True only when a mutually-exclusive single-fault ranking sums to 1."""
        if self.semantics is not FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE:
            return False
        if not self.ranking:
            return False
        return abs(self.score_sum - 1.0) < 1e-9