# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for the isolated JEV-style typed state/decision PoC (F12 / KB-033).

Covers TC-12.1 (typed candidates / next check / evidence / abstain), TC-12.2
(disagreement with the Profile initial order is not automatically wrong),
TC-12.3 (score vs probability labeling + Brier/ECE/reliability) and TC-12.4
(disabling the PoC leaves core paths unaffected).
"""
from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys

import pytest

from openviking.jev import (
    Applicability,
    AvailableCheck,
    CalibrationError,
    CandidateFault,
    CompletedCheck,
    DecisionKind,
    DeviceContext,
    DiagnosticState,
    Evidence,
    EvidenceKind,
    FaultSemantics,
    Question,
    QuestionKind,
    RankedCandidate,
    ScoreLabel,
    StateValidationError,
    brier_score,
    calibrate,
    calibrate_and_label,
    compare_initial_order,
    decide,
    expected_calibration_error,
    probability_label,
    reliability_curve,
    with_calibration,
)

DEV = "viking://resources/dev/sy215c.md"
CFG = "viking://resources/cfg/standard.md"
E1 = "viking://resources/ev/dtc-p0128.md"
E2 = "viking://resources/ev/pressure-low.md"
F1 = "viking://resources/fault/pump-worn.md"
F2 = "viking://resources/fault/sensor-drift.md"
C1 = "viking://resources/comp/pump.md"
C2 = "viking://resources/comp/sensor.md"
P1 = "viking://resources/proc/check-pressure.md"
P2 = "viking://resources/proc/swap-sensor.md"


def evidence(uri, kind=EvidenceKind.DTC, observed=True):
    return Evidence(uri=uri, kind=kind, observed=observed)


def fault(uri, comp, mode, applicability=Applicability.APPLICABLE, ev=(), initial_rank=None):
    return CandidateFault(
        fault_uri=uri,
        component_uri=comp,
        failure_mode=mode,
        applicability=applicability,
        evidence_uris=ev,
        initial_rank=initial_rank,
    )


def two_candidate_state(semantics=FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE, **kwargs):
    defaults = dict(
        canonical_symptoms=("液压系统压力低",),
        device=DeviceContext(device_uri=DEV, config_spec_uri=CFG),
        evidence=(evidence(E1), evidence(E2, EvidenceKind.MEASUREMENT)),
        candidate_faults=(
            fault(F1, C1, "worn", ev=(E1,), initial_rank=0),
            fault(F2, C2, "drift", ev=(E2,), initial_rank=1),
        ),
        semantics=semantics,
    )
    defaults.update(kwargs)
    return DiagnosticState(**defaults)


# --------------------------------------------------------------------------- #
# Typed state construction rejects invalid input (TC-12.1)
# --------------------------------------------------------------------------- #


class TestTypedStateValidation:
    def test_valid_state_round_trips(self):
        state = two_candidate_state()
        assert state.canonical_symptoms == ("液压系统压力低",)
        assert state.device.config_known is True
        assert len(state.candidate_faults) == 2

    def test_rejects_non_uri_evidence(self):
        with pytest.raises(StateValidationError):
            Evidence(uri="not-a-uri", kind=EvidenceKind.DTC, observed=True)

    def test_rejects_relative_path_segments(self):
        with pytest.raises(StateValidationError):
            Evidence(uri="viking://resources/../etc/passwd", kind=EvidenceKind.DTC, observed=True)

    def test_rejects_invalid_evidence_kind_enum(self):
        with pytest.raises(StateValidationError):
            Evidence(uri=E1, kind="dtc", observed=True)

    def test_rejects_non_boolean_observed(self):
        with pytest.raises(StateValidationError):
            Evidence(uri=E1, kind=EvidenceKind.DTC, observed=1)

    def test_rejects_invalid_applicability_enum(self):
        with pytest.raises(StateValidationError):
            fault(F1, C1, "worn", applicability="applicable")

    def test_rejects_unknown_semantics(self):
        with pytest.raises(StateValidationError):
            two_candidate_state(semantics="single")

    def test_rejects_duplicate_evidence_uris(self):
        with pytest.raises(StateValidationError):
            two_candidate_state(evidence=(evidence(E1), evidence(E1)))

    def test_rejects_duplicate_candidate_faults(self):
        with pytest.raises(StateValidationError):
            two_candidate_state(
                candidate_faults=(fault(F1, C1, "worn"), fault(F1, C2, "other"))
            )

    def test_rejects_undeclared_evidence_reference(self):
        with pytest.raises(StateValidationError):
            two_candidate_state(
                candidate_faults=(fault(F1, C1, "worn", ev=("viking://resources/ev/ghost.md",)),)
            )

    def test_rejects_empty_canonical_symptoms(self):
        with pytest.raises(StateValidationError):
            two_candidate_state(canonical_symptoms=())

    def test_rejects_non_positive_cost(self):
        with pytest.raises(StateValidationError):
            AvailableCheck(
                check_id="c1", procedure_uri=P1, target_components=(C1,), estimated_cost=-1.0
            )

    def test_rejects_infinite_cost(self):
        with pytest.raises(StateValidationError):
            AvailableCheck(
                check_id="c1",
                procedure_uri=P1,
                target_components=(C1,),
                estimated_cost=float("inf"),
            )

    def test_rejects_negative_initial_rank(self):
        with pytest.raises(StateValidationError):
            fault(F1, C1, "worn", initial_rank=-1)

    def test_rejects_non_decision_kind_in_question(self):
        with pytest.raises(StateValidationError):
            Question(kind="rank_candidate_faults")

    def test_rejects_non_positive_max_candidates(self):
        with pytest.raises(StateValidationError):
            Question(kind=QuestionKind.RANK_CANDIDATE_FAULTS, max_candidates=0)

    def test_rejects_ranking_decision_without_candidates(self):
        from openviking.jev import Decision

        with pytest.raises(StateValidationError):
            Decision(
                kind=DecisionKind.RANKING,
                semantics=FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE,
                score_label=ScoreLabel.SCORE,
            )

    def test_rejects_candidates_on_non_ranking_decision(self):
        from openviking.jev import Decision

        with pytest.raises(StateValidationError):
            Decision(
                kind=DecisionKind.ABSTAIN,
                semantics=FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE,
                score_label=ScoreLabel.SCORE,
                ranking=(
                    RankedCandidate(F1, C1, "worn", 0, 1.0, ScoreLabel.SCORE),
                ),
            )


# --------------------------------------------------------------------------- #
# Decision content: ranking + next check + evidence URIs (TC-12.1)
# --------------------------------------------------------------------------- #


class TestDecisionContent:
    def test_ranking_carries_scores_evidence_and_order(self):
        state = two_candidate_state()
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.kind is DecisionKind.RANKING
        assert len(decision.ranking) == 2
        # DTC evidence (weight 1.0) outranks the measurement evidence (0.8).
        assert decision.ranking[0].fault_uri == F1
        assert decision.ranking[0].rank == 0
        assert decision.ranking[1].rank == 1
        assert decision.ranking[0].evidence_uris == (E1,)
        assert decision.evidence_uris == (E1, E2)

    def test_next_check_selected_when_discriminating_check_exists(self):
        state = two_candidate_state(
            available_checks=(
                AvailableCheck(
                    check_id="pressure",
                    procedure_uri=P1,
                    target_components=(C1,),
                    estimated_cost=2.0,
                    discriminates=(F1, F2),
                ),
                AvailableCheck(
                    check_id="swap",
                    procedure_uri=P2,
                    target_components=(C2,),
                    estimated_cost=1.0,
                    discriminates=(F2,),
                ),
            )
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.kind is DecisionKind.NEEDS_MORE_INFO
        assert decision.next_check is not None
        # "pressure" discriminates both leaders, so it wins over the cheaper single.
        assert decision.next_check.check_id == "pressure"
        assert decision.next_check.procedure_uri == P1

    def test_completed_check_is_not_recommended_again(self):
        state = two_candidate_state(
            completed_checks=(CompletedCheck(check_id="pressure", procedure_uri=P1),),
            available_checks=(
                AvailableCheck(
                    check_id="pressure",
                    procedure_uri=P1,
                    target_components=(C1,),
                    estimated_cost=1.0,
                    discriminates=(F1, F2),
                ),
                AvailableCheck(
                    check_id="swap",
                    procedure_uri=P2,
                    target_components=(C2,),
                    estimated_cost=1.0,
                    discriminates=(F2,),
                ),
            ),
        )
        decision = decide(state, Question(QuestionKind.NEXT_CHECK))
        assert decision.next_check.check_id == "swap"

    def test_next_check_question_returns_needs_more_info(self):
        state = two_candidate_state(
            available_checks=(
                AvailableCheck(
                    check_id="pressure",
                    procedure_uri=P1,
                    target_components=(C1,),
                    estimated_cost=2.0,
                    discriminates=(F1, F2),
                ),
            )
        )
        decision = decide(state, Question(QuestionKind.NEXT_CHECK))
        assert decision.kind is DecisionKind.NEEDS_MORE_INFO
        assert decision.next_check.check_id == "pressure"

    def test_abstains_without_candidates_and_does_not_invent_state(self):
        state = two_candidate_state(candidate_faults=())
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.kind is DecisionKind.ABSTAIN
        assert decision.ranking == ()
        assert "invent" in " ".join(decision.reasons)

    def test_abstains_when_no_observed_evidence(self):
        state = two_candidate_state(
            evidence=(evidence(E1, observed=False), evidence(E2, EvidenceKind.MEASUREMENT, False)),
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.kind is DecisionKind.ABSTAIN

    def test_needs_more_info_when_config_known_but_applicability_undetermined(self):
        state = two_candidate_state(
            candidate_faults=(
                fault(F1, C1, "worn", Applicability.UNDETERMINED, ev=(E1,)),
                fault(F2, C2, "drift", Applicability.APPLICABLE, ev=(E2,)),
            ),
            available_checks=(
                AvailableCheck(
                    check_id="pressure",
                    procedure_uri=P1,
                    target_components=(C1,),
                    estimated_cost=1.0,
                    discriminates=(F1, F2),
                ),
            ),
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.kind is DecisionKind.NEEDS_MORE_INFO
        assert any("applicability" in reason for reason in decision.reasons)

    def test_not_applicable_candidate_is_excluded(self):
        state = two_candidate_state(
            candidate_faults=(
                fault(F1, C1, "worn", Applicability.NOT_APPLICABLE, ev=(E1,)),
                fault(F2, C2, "drift", Applicability.APPLICABLE, ev=(E2,)),
            ),
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert [item.fault_uri for item in decision.ranking] == [F2]

    def test_confirm_root_cause_tie_requires_more_info(self):
        state = two_candidate_state(
            evidence=(evidence(E1), evidence(E2)),
            candidate_faults=(
                fault(F1, C1, "worn", ev=(E1,)),
                fault(F2, C2, "drift", ev=(E2,)),
            ),
            available_checks=(
                AvailableCheck(
                    check_id="pressure",
                    procedure_uri=P1,
                    target_components=(C1,),
                    estimated_cost=1.0,
                    discriminates=(F1, F2),
                ),
            ),
        )
        decision = decide(state, Question(QuestionKind.CONFIRM_ROOT_CAUSE))
        assert decision.kind is DecisionKind.NEEDS_MORE_INFO
        assert decision.next_check is not None


# --------------------------------------------------------------------------- #
# Single-fault vs multi-fault semantics (TC-12.3)
# --------------------------------------------------------------------------- #


class TestFaultSemantics:
    def test_single_fault_mutually_exclusive_is_normalized_to_one(self):
        state = two_candidate_state(FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE)
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.normalized_to_one is True
        assert decision.score_sum == pytest.approx(1.0)

    def test_multi_fault_independent_is_not_normalized_to_one(self):
        state = two_candidate_state(FaultSemantics.MULTI_FAULT_INDEPENDENT)
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        # Independent-event scores keep their raw mass and must not sum to 1.
        assert decision.normalized_to_one is False
        assert decision.score_sum == pytest.approx(1.8)
        assert decision.semantics is FaultSemantics.MULTI_FAULT_INDEPENDENT

    def test_two_semantics_differ_for_same_evidence(self):
        single = decide(
            two_candidate_state(FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE),
            Question(QuestionKind.RANK_CANDIDATE_FAULTS),
        )
        multi = decide(
            two_candidate_state(FaultSemantics.MULTI_FAULT_INDEPENDENT),
            Question(QuestionKind.RANK_CANDIDATE_FAULTS),
        )
        assert single.score_sum != multi.score_sum
        assert single.normalized_to_one != multi.normalized_to_one

    def test_ordering_is_preserved_across_semantics(self):
        single = decide(
            two_candidate_state(FaultSemantics.SINGLE_FAULT_MUTUALLY_EXCLUSIVE),
            Question(QuestionKind.RANK_CANDIDATE_FAULTS),
        )
        multi = decide(
            two_candidate_state(FaultSemantics.MULTI_FAULT_INDEPENDENT),
            Question(QuestionKind.RANK_CANDIDATE_FAULTS),
        )
        assert [i.fault_uri for i in single.ranking] == [i.fault_uri for i in multi.ranking]


# --------------------------------------------------------------------------- #
# score vs probability labeling gate (TC-12.3)
# --------------------------------------------------------------------------- #


class TestLabelingGate:
    def test_uncalibrated_decision_is_labeled_score_not_probability(self):
        decision = decide(
            two_candidate_state(), Question(QuestionKind.RANK_CANDIDATE_FAULTS)
        )
        assert decision.score_label is ScoreLabel.SCORE
        assert all(item.score_label is ScoreLabel.SCORE for item in decision.ranking)

    def test_cannot_relabel_as_probability_without_calibration(self):
        decision = decide(
            two_candidate_state(), Question(QuestionKind.RANK_CANDIDATE_FAULTS)
        )
        with pytest.raises(StateValidationError):
            with_calibration(decision, calibrated=False, label=ScoreLabel.PROBABILITY)

    def test_can_relabel_as_probability_after_calibration(self):
        decision = decide(
            two_candidate_state(), Question(QuestionKind.RANK_CANDIDATE_FAULTS)
        )
        relabeled = with_calibration(decision, calibrated=True, label=ScoreLabel.PROBABILITY)
        assert relabeled.score_label is ScoreLabel.PROBABILITY
        assert all(item.score_label is ScoreLabel.PROBABILITY for item in relabeled.ranking)

    def test_score_label_enum_values(self):
        assert ScoreLabel.SCORE.value == "score"
        assert ScoreLabel.PROBABILITY.value == "probability"


# --------------------------------------------------------------------------- #
# Calibration statistics (TC-12.3)
# --------------------------------------------------------------------------- #


class TestCalibration:
    def test_brier_score_perfect_predictions(self):
        assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == pytest.approx(0.0)

    def test_brier_score_known_value(self):
        # (0.8-1)^2 + (0.6-0)^2 + (0.2-0)^2 = 0.04 + 0.36 + 0.04 = 0.44 / 3
        assert brier_score([0.8, 0.6, 0.2], [1, 0, 0]) == pytest.approx(0.44 / 3)

    def test_ece_zero_for_perfectly_calibrated_bin(self):
        # All predictions 0.5, half correct -> bin gap 0.
        assert expected_calibration_error([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.0)

    def test_ece_positive_for_overconfident_predictions(self):
        ece = expected_calibration_error([0.9, 0.9, 0.9, 0.9], [1, 0, 0, 0])
        assert ece == pytest.approx(0.65)

    def test_reliability_curve_bins_and_gaps(self):
        curve = reliability_curve([0.9, 0.9, 0.9, 0.9], [1, 0, 0, 0], bins=10)
        assert len(curve) == 1
        bin_ = curve[0]
        assert bin_.count == 4
        assert bin_.mean_confidence == pytest.approx(0.9)
        assert bin_.empirical_accuracy == pytest.approx(0.25)
        assert bin_.gap == pytest.approx(0.65)

    def test_synthetic_set_does_not_allow_probability_label(self):
        report, label = calibrate_and_label(
            [0.9, 0.1, 0.8, 0.2], [1, 0, 1, 0], independent=False, synthetic=True
        )
        assert report.synthetic is True
        assert report.probability_label_allowed is False
        assert label is ScoreLabel.SCORE
        assert probability_label(report) is ScoreLabel.SCORE

    def test_independent_gold_set_allows_probability_label(self):
        report, label = calibrate_and_label(
            [0.9, 0.1, 0.8, 0.2], [1, 0, 1, 0], independent=True, synthetic=False
        )
        assert report.probability_label_allowed is True
        assert label is ScoreLabel.PROBABILITY
        assert probability_label(report) is ScoreLabel.PROBABILITY

    def test_calibration_report_has_brier_and_ece(self):
        report = calibrate(
            [0.7, 0.4, 0.9], [1, 0, 1], independent=True, synthetic=False, bins=5
        )
        assert report.count == 3
        assert 0.0 <= report.brier <= 1.0
        assert 0.0 <= report.ece <= 1.0

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(CalibrationError):
            calibrate([0.5, 0.5], [1], independent=True, synthetic=False)

    def test_rejects_out_of_range_confidence(self):
        with pytest.raises(CalibrationError):
            calibrate([1.5], [1], independent=True, synthetic=False)

    def test_rejects_non_binary_outcome(self):
        with pytest.raises(CalibrationError):
            calibrate([0.5], [2], independent=True, synthetic=False)

    def test_rejects_empty_label_set(self):
        with pytest.raises(CalibrationError):
            calibrate([], [], independent=True, synthetic=False)

    def test_rejects_synthetic_and_independent_together(self):
        with pytest.raises(CalibrationError):
            calibrate([0.5], [1], independent=True, synthetic=True)

    def test_rejects_invalid_bin_count(self):
        with pytest.raises(CalibrationError):
            reliability_curve([0.5], [1], bins=0)


# --------------------------------------------------------------------------- #
# Disagreement with Profile initial order (TC-12.2)
# --------------------------------------------------------------------------- #


class TestInitialOrderDisagreement:
    def test_agreement_with_initial_order_is_consistent(self):
        state = two_candidate_state(
            candidate_faults=(
                fault(F1, C1, "worn", ev=(E1,), initial_rank=0),
                fault(F2, C2, "drift", ev=(E2,), initial_rank=1),
            ),
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        consistent, notes = compare_initial_order(state, decision.ranking)
        assert consistent is True
        assert notes == ()

    def test_material_reordering_is_flagged_not_auto_wrong(self):
        # Engine leads with F2 (DTC weight) while the Profile initial order leads F1.
        state = two_candidate_state(
            evidence=(evidence(E1, EvidenceKind.SOURCE), evidence(E2)),
            candidate_faults=(
                fault(F1, C1, "worn", ev=(E1,), initial_rank=0),
                fault(F2, C2, "drift", ev=(E2,), initial_rank=1),
            ),
        )
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert decision.ranking[0].fault_uri == F2
        consistent, notes = compare_initial_order(state, decision.ranking)
        assert consistent is False
        assert any("F2" in note or "margin" in note for note in notes)

    def test_small_gap_reordering_is_neutral(self):
        state = two_candidate_state(
            evidence=(evidence(E1), evidence(E2, EvidenceKind.DTC)),
            candidate_faults=(
                fault(F1, C1, "worn", ev=(E1,), initial_rank=0),
                fault(F2, C2, "drift", ev=(E2,), initial_rank=1),
            ),
        )
        # Both have equal DTC mass, so tie-break is URI asc -> F1 leads; neutral.
        decision = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        consistent, notes = compare_initial_order(state, decision.ranking)
        assert consistent is True


# --------------------------------------------------------------------------- #
# TC-12.4: core paths are unaffected by disabling the PoC
# --------------------------------------------------------------------------- #

CORE_MODULES = (
    "openviking.wiki.schema",
    "openviking.wiki.validation",
    "openviking.wiki.resolver",
    "openviking.wiki.catalog",
    "openviking.wiki.build_plan",
)


class TestCoreUnaffected:
    def test_core_modules_do_not_import_jev(self):
        """No core module under openviking/ may reference openviking.jev."""

        root = pathlib.Path(__file__).resolve().parents[2] / "openviking"
        offenders = []
        for path in root.rglob("*.py"):
            if "jev" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "openviking.jev" in text or "from .jev" in text or "import jev" in text:
                offenders.append(str(path))
        assert offenders == []

    def test_core_modules_import_without_jev_loaded(self):
        """Importing core modules must not pull the PoC into sys.modules."""

        code = (
            "import sys;"
            "import openviking.wiki.schema;"
            "import openviking.wiki.validation;"
            "import openviking.wiki.resolver;"
            "assert not [m for m in sys.modules if m.startswith('openviking.jev')],"
            "sorted(m for m in sys.modules if m.startswith('openviking.jev'));"
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith("ok")

    @pytest.mark.parametrize("module_name", CORE_MODULES)
    def test_core_module_importable(self, module_name):
        module = importlib.import_module(module_name)
        assert module is not None

    def test_engine_is_pure_and_does_not_mutate_state(self):
        state = two_candidate_state()
        before = (state.candidate_faults, state.evidence, state.semantics)
        decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        after = (state.candidate_faults, state.evidence, state.semantics)
        assert before == after

    def test_decisions_are_deterministic(self):
        state = two_candidate_state()
        first = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        second = decide(state, Question(QuestionKind.RANK_CANDIDATE_FAULTS))
        assert first == second