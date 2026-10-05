# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Calibration gate for the JEV-style PoC (TC-12.3).

Ranking values from the engine are called ``score`` by default. They may only be
called probabilities after calibration against an independent diagnostic gold
set, evidenced by a reliability curve plus Brier score and ECE. This module
computes those statistics on caller-supplied labeled data; it performs no
network access and does not itself obtain the gold set.

A ``SyntheticLabelSet`` is explicitly synthetic and can never authorize the
probability label. Only a ``GoldLabelSet`` marked independently reviewed can.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from openviking.jev.state import ScoreLabel, StateValidationError


class CalibrationError(StateValidationError):
    """Raised when calibration input or the labeling gate is invalid."""


@dataclass(frozen=True)
class ReliabilityBin:
    """One bucket of the reliability curve."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    empirical_accuracy: float

    @property
    def gap(self) -> float:
        return abs(self.mean_confidence - self.empirical_accuracy)


@dataclass(frozen=True)
class CalibrationReport:
    """Brier / ECE / reliability-curve summary over a labeled set."""

    count: int
    brier: float
    ece: float
    bins: tuple[ReliabilityBin, ...]
    independent: bool
    synthetic: bool

    @property
    def probability_label_allowed(self) -> bool:
        """Only an independent, non-synthetic gold set may be called probability."""

        return self.independent and not self.synthetic


def _validate_pairs(
    confidences: Iterable[float], outcomes: Iterable[int]
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    confidence_values = tuple(confidences)
    outcome_values = tuple(outcomes)
    if not confidence_values:
        raise CalibrationError("calibration requires at least one labeled item")
    if len(confidence_values) != len(outcome_values):
        raise CalibrationError("confidence and outcome lengths differ")
    for value in confidence_values:
        if type(value) not in {int, float} or isinstance(value, bool):
            raise CalibrationError("confidences must be real numbers")
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise CalibrationError("confidences must be finite and within [0, 1]")
    for value in outcome_values:
        if type(value) is not int or value not in (0, 1):
            raise CalibrationError("outcomes must be 0 or 1 integers")
    return tuple(float(v) for v in confidence_values), outcome_values


def brier_score(confidences: Iterable[float], outcomes: Iterable[int]) -> float:
    """Mean squared error between confidence and the 0/1 outcome."""

    confidence_values, outcome_values = _validate_pairs(confidences, outcomes)
    total = sum((c - o) ** 2 for c, o in zip(confidence_values, outcome_values))
    return total / len(confidence_values)


def reliability_curve(
    confidences: Iterable[float], outcomes: Iterable[int], *, bins: int = 10
) -> tuple[ReliabilityBin, ...]:
    """Bucket predictions into equal-width bins over [0, 1]."""

    if type(bins) is not int or bins <= 0:
        raise CalibrationError("bins must be a positive integer")
    confidence_values, outcome_values = _validate_pairs(confidences, outcomes)
    width = 1.0 / bins
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for confidence, outcome in zip(confidence_values, outcome_values):
        index = min(int(confidence * bins), bins - 1)
        buckets[index].append((confidence, outcome))
    curve: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        lower = index * width
        upper = lower + width
        if not bucket:
            continue
        mean_conf = sum(item[0] for item in bucket) / len(bucket)
        accuracy = sum(item[1] for item in bucket) / len(bucket)
        curve.append(ReliabilityBin(lower, upper, len(bucket), mean_conf, accuracy))
    return tuple(curve)


def expected_calibration_error(
    confidences: Iterable[float], outcomes: Iterable[int], *, bins: int = 10
) -> float:
    """Weighted average gap between confidence and accuracy across bins."""

    confidence_values, outcome_values = _validate_pairs(confidences, outcomes)
    curve = reliability_curve(confidence_values, outcome_values, bins=bins)
    total = len(confidence_values)
    return sum(bin_.count / total * bin_.gap for bin_ in curve)


def calibrate(
    confidences: Iterable[float],
    outcomes: Iterable[int],
    *,
    independent: bool,
    synthetic: bool,
    bins: int = 10,
) -> CalibrationReport:
    """Compute a calibration report; ``independent``/``synthetic`` gate labeling."""

    if type(independent) is not bool or type(synthetic) is not bool:
        raise CalibrationError("independent and synthetic flags must be booleans")
    confidence_values, outcome_values = _validate_pairs(confidences, outcomes)
    if independent and synthetic:
        raise CalibrationError("a synthetic label set cannot be independent")
    return CalibrationReport(
        count=len(confidence_values),
        brier=brier_score(confidence_values, outcome_values),
        ece=expected_calibration_error(confidence_values, outcome_values, bins=bins),
        bins=reliability_curve(confidence_values, outcome_values, bins=bins),
        independent=independent,
        synthetic=synthetic,
    )


def probability_label(report: CalibrationReport) -> ScoreLabel:
    """Return the label the report authorizes; the sole source of PROBABILITY."""

    if not isinstance(report, CalibrationReport):
        raise CalibrationError("report must be a CalibrationReport")
    return ScoreLabel.PROBABILITY if report.probability_label_allowed else ScoreLabel.SCORE


def calibrate_and_label(
    confidences: Iterable[float],
    outcomes: Iterable[int],
    *,
    independent: bool,
    synthetic: bool,
    bins: int = 10,
) -> tuple[CalibrationReport, ScoreLabel]:
    """Convenience: compute the report and the label it authorizes together."""

    report = calibrate(
        confidences, outcomes, independent=independent, synthetic=synthetic, bins=bins
    )
    return report, probability_label(report)