# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic JEV-style PoC engine: typed state + typed question -> decision.

The engine is pure and synchronous: no network, no model weights, no storage
writes. It never rewrites authoritative Wiki knowledge or config applicability,
and it never invents state that the caller did not supply. When evidence is
insufficient it returns ABSTAIN or NEEDS_MORE_INFO instead of guessing.

Single-fault mutually-exclusive and multi-fault independent-event semantics are
kept separate. Only the mutually-exclusive case may be normalized to sum to 1;
independent-event scores are left as-is and are never force-normalized.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from openviking.jev.state import (
    Applicability,
    CandidateFault,
    Decision,
    DecisionKind,
    DiagnosticState,
    FaultSemantics,
    NextCheck,
    QuestionKind,
    RankedCandidate,
    ScoreLabel,
    StateValidationError,
)

# Base evidence weight per kind; deterministic and documented, not learned.
EVIDENCE_WEIGHTS = {"dtc": 1.0, "measurement": 0.8, "observation": 0.5, "source": 0.3}
APPLICABILITY_FACTOR = {
    Applicability.APPLICABLE: 1.0,
    Applicability.UNDETERMINED: 0.5,
    Applicability.NOT_APPLICABLE: 0.0,
}
# A candidate needs strictly positive supporting mass before we rank it; a
# candidate with no observed supporting evidence is dropped, not invented.
MIN_SUPPORT = 0.0
# An initial order is only "materially reordered" past this score gap.
REORDER_MARGIN = 0.25


@dataclass(frozen=True)
class Question:
    """A typed question asked against a :class:`DiagnosticState`."""

    kind: QuestionKind
    focus_fault_uri: str | None = None
    max_candidates: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, QuestionKind):
            raise StateValidationError("question.kind must be a QuestionKind value")
        if self.focus_fault_uri is not None and (
            not isinstance(self.focus_fault_uri, str) or not self.focus_fault_uri
        ):
            raise StateValidationError("question.focus_fault_uri must be a non-empty string")
        if self.max_candidates is not None and (
            type(self.max_candidates) is not int or self.max_candidates <= 0
        ):
            raise StateValidationError("question.max_candidates must be a positive integer")


def _evidence_mass(state: DiagnosticState, fault: CandidateFault) -> float:
    observed = state.observed_evidence_uris
    kind_by_uri = {item.uri: item.kind for item in state.evidence}
    mass = 0.0
    for uri in fault.evidence_uris:
        if uri in observed:
            mass += EVIDENCE_WEIGHTS[kind_by_uri[uri].value]
    return mass


def _raw_scores(state: DiagnosticState) -> dict[str, float]:
    scores: dict[str, float] = {}
    for fault in state.candidate_faults:
        support = _evidence_mass(state, fault)
        scores[fault.fault_uri] = support * APPLICABILITY_FACTOR[fault.applicability]
    return scores


def _has_applicability_conflict(state: DiagnosticState) -> bool:
    """A known config anchor plus an undetermined candidate is a real gap."""

    if not state.device.config_known:
        return False
    return any(
        fault.applicability is Applicability.UNDETERMINED for fault in state.candidate_faults
    )


def _rank(
    state: DiagnosticState, scores: dict[str, float], limit: int | None
) -> tuple[RankedCandidate, ...]:
    faults = {fault.fault_uri: fault for fault in state.candidate_faults}
    supported = [
        (uri, score)
        for uri, score in scores.items()
        if score > MIN_SUPPORT and faults[uri].applicability is not Applicability.NOT_APPLICABLE
    ]
    # Deterministic ordering: score desc, then authoritative URI asc as tiebreak.
    supported.sort(key=lambda item: (-item[1], item[0]))
    if limit is not None:
        supported = supported[:limit]

    if state.semantics is FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE:
        total = sum(score for _, score in supported)
        if total > 0:
            supported = [(uri, score / total) for uri, score in supported]

    label = ScoreLabel.SCORE  # Calibration gate: never PROBABILITY without a gold set.
    ranking = []
    for index, (uri, score) in enumerate(supported):
        fault = faults[uri]
        ranking.append(
            RankedCandidate(
                fault_uri=uri,
                component_uri=fault.component_uri,
                failure_mode=fault.failure_mode,
                rank=index,
                score=score,
                score_label=label,
                evidence_uris=tuple(
                    evidence_uri
                    for evidence_uri in fault.evidence_uris
                    if evidence_uri in state.observed_evidence_uris
                ),
            )
        )
    return tuple(ranking)


def _choose_next_check(
    state: DiagnosticState, ranking: tuple[RankedCandidate, ...]
) -> NextCheck | None:
    """Pick the cheapest unused check that discriminates the top candidates."""

    done = {check.check_id for check in state.completed_checks}
    top = [item.fault_uri for item in ranking]
    best: tuple[float, float, str, object] | None = None
    for check in state.available_checks:
        if check.check_id in done:
            continue
        discriminating = [uri for uri in check.discriminates if uri in top]
        # Prefer checks that separate the leaders, then the cheapest.
        coverage = float(len(discriminating))
        if coverage <= 0 and top:
            continue
        key = (-coverage, check.estimated_cost, check.check_id)
        if best is None or key < best[:3]:
            best = (key[0], key[1], check.check_id, (check, tuple(discriminating)))
    if best is None:
        return None
    check, discriminating = best[3]
    if discriminating:
        rationale = "separates current leading candidates at lowest estimated cost"
    else:
        rationale = "cheapest remaining check on the target components"
    return NextCheck(
        check_id=check.check_id,
        procedure_uri=check.procedure_uri,
        estimated_cost=check.estimated_cost,
        rationale=rationale,
        discriminates=tuple(discriminating),
    )


def _evidence_uris(state: DiagnosticState, ranking: tuple[RankedCandidate, ...]) -> tuple[str, ...]:
    seen: list[str] = []
    for item in ranking:
        for uri in item.evidence_uris:
            if uri not in seen:
                seen.append(uri)
    return tuple(sorted(seen))


def _abstain(state: DiagnosticState, reasons: tuple[str, ...]) -> Decision:
    return Decision(
        kind=DecisionKind.ABSTAIN,
        semantics=state.semantics,
        score_label=ScoreLabel.SCORE,
        next_check=None,
        evidence_uris=tuple(sorted(state.observed_evidence_uris)),
        confidence=None,
        reasons=reasons,
        unknowns=state.unknowns,
    )


def decide(state: DiagnosticState, question: Question) -> Decision:
    """Answer a typed question with a structured, deterministic decision."""

    if not isinstance(state, DiagnosticState):
        raise StateValidationError("state must be a DiagnosticState")
    if not isinstance(question, Question):
        raise StateValidationError("question must be a Question")

    if question.kind is QuestionKind.NEXT_CHECK:
        next_check = _choose_next_check(state, ())
        if next_check is None:
            return _abstain(state, ("no remaining discriminating check available",))
        return Decision(
            kind=DecisionKind.NEEDS_MORE_INFO,
            semantics=state.semantics,
            score_label=ScoreLabel.SCORE,
            next_check=next_check,
            evidence_uris=tuple(sorted(state.observed_evidence_uris)),
            confidence=None,
            reasons=("next-check-only question",),
            unknowns=state.unknowns,
        )

    if not state.candidate_faults:
        return _abstain(state, ("no candidate faults supplied; the PoC does not invent them",))

    scores = _raw_scores(state)
    ranking = _rank(state, scores, question.max_candidates)

    if question.kind is QuestionKind.CONFIRM_ROOT_CAUSE:
        if not ranking:
            return _abstain(state, ("no candidate has supporting observed evidence",))
        if len(ranking) > 1 and abs(ranking[0].score - ranking[1].score) < 1e-12:
            return Decision(
                kind=DecisionKind.NEEDS_MORE_INFO,
                semantics=state.semantics,
                score_label=ScoreLabel.SCORE,
                next_check=_choose_next_check(state, ranking),
                evidence_uris=_evidence_uris(state, ranking),
                confidence=None,
                reasons=("leading candidates are tied; a discriminating check is required",),
                unknowns=state.unknowns,
            )
        top = ranking[0]
        return Decision(
            kind=DecisionKind.RANKING,
            semantics=state.semantics,
            score_label=ScoreLabel.SCORE,
            ranking=ranking,
            next_check=None,
            evidence_uris=_evidence_uris(state, ranking),
            confidence=top.score,
            reasons=(f"confirmed leading root cause candidate: {top.fault_uri}",),
            unknowns=state.unknowns,
        )

    # Default: rank candidate faults.
    if not ranking:
        reasons = ["no candidate has supporting observed evidence"]
        if _has_applicability_conflict(state):
            reasons.append("config anchor known but candidate applicability undetermined")
        return _abstain(state, tuple(reasons))

    applicability_conflict = _has_applicability_conflict(state)
    insufficient = applicability_conflict or any(
        item.score_label is ScoreLabel.SCORE for item in ranking
    )
    next_check = _choose_next_check(state, ranking)
    if insufficient and next_check is not None:
        reasons = ["scores are uncalibrated and evidence is not yet decisive"]
        if applicability_conflict:
            reasons.append("config anchor known but candidate applicability undetermined")
        return Decision(
            kind=DecisionKind.NEEDS_MORE_INFO,
            semantics=state.semantics,
            score_label=ScoreLabel.SCORE,
            ranking=ranking,
            next_check=next_check,
            evidence_uris=_evidence_uris(state, ranking),
            confidence=None,
            reasons=tuple(reasons),
            unknowns=state.unknowns,
        )
    return Decision(
        kind=DecisionKind.RANKING,
        semantics=state.semantics,
        score_label=ScoreLabel.SCORE,
        ranking=ranking,
        next_check=next_check,
        evidence_uris=_evidence_uris(state, ranking),
        confidence=None,
        reasons=("deterministic ranking from observed evidence",),
        unknowns=state.unknowns,
    )


def compare_initial_order(
    state: DiagnosticState, ranking: tuple[RankedCandidate, ...]
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate disagreement with the Profile's initial order (TC-12.2).

    A different order is NOT automatically wrong: it is only flagged as a
    material reordering when the engine's leaders are separated by more than
    REORDER_MARGIN and contradict the supplied ``initial_rank``.
    """

    ranked_faults = {item.fault_uri: item for item in ranking}
    initial = sorted(
        (
            (fault.initial_rank, fault.fault_uri)
            for fault in state.candidate_faults
            if fault.initial_rank is not None and fault.fault_uri in ranked_faults
        ),
        key=lambda item: (item[0], item[1]),
    )
    if len(initial) < 2:
        return True, ()
    initial_top = initial[0][1]
    engine_top = ranking[0].fault_uri
    notes: list[str] = []
    consistent = True
    if initial_top != engine_top:
        gap = ranking[0].score - (ranking[1].score if len(ranking) > 1 else 0.0)
        if gap > REORDER_MARGIN:
            consistent = False
            notes.append(
                f"engine leads with {engine_top} over initial {initial_top} by margin {gap:.3f}"
            )
        else:
            notes.append("reordering is within the material margin; treat as neutral")
    return consistent, tuple(notes)


def with_calibration(
    decision: Decision, *, calibrated: bool, label: ScoreLabel
) -> Decision:
    """Relabel a decision's scores as probabilities only when calibration holds.

    The caller must pass ``calibrated=True`` from a verified calibration gate;
    this function never decides on its own that a score is a probability.
    """

    if label is ScoreLabel.PROBABILITY and not calibrated:
        raise StateValidationError("cannot label scores as probability without calibration")
    if decision.kind is not DecisionKind.RANKING:
        return decision
    ranking = tuple(replace(item, score_label=label) for item in decision.ranking)
    return replace(decision, ranking=ranking, score_label=label)