"""Independent evaluation ledger: provenance, leakage, freezing, and honest reporting."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

TOOL = Path(__file__).parents[2] / "docs/spec/tools/evaluate_gold.py"
spec = importlib.util.spec_from_file_location("evaluate_gold", TOOL)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def sample(ident="one", **changes):
    record = {"id": ident, "population": "synthetic", "split": "test", "sample_kind": "retrieval",
              "query": "How do I repair the circuit?", "categories": ["semantic"],
              "document_id": "manual-one", "machine_id": "machine-one", "passage_group": "paragraph-one",
              "generator": {"name": "candidate-generator", "version": "1", "code_sha256": "a" * 64,
                            "input_sha256": "b" * 64, "provenance_verified": True},
              "source": {"locator": "controlled://manual/page/10", "content_sha256": "c" * 64,
                         "original_sha256": "d" * 64}}
    record.update(changes)
    return record


def reviewed(**changes):
    record = sample(population="gold", expected={"relevant_source_ids": ["paragraph-one"]}, **changes)
    record["review"] = {"kind": "human", "reviewer": "independent-reviewer", "reviewed_at": "2026-10-04",
                        "decision": "approved", "oracle": "original_source", "source_supported": True,
                        "evidence_locator": "controlled://review/page10", "original_sha256": "d" * 64,
                        "sample_sha256": evaluation.review_digest(record)}
    return record


def bundle(tmp_path, records=None):
    destination = tmp_path / "v1"
    manifest = evaluation.freeze(records or [reviewed()], destination, {"parser": "one", "R": 20, "F": 10})
    return destination, manifest


def run_result(manifest, ids=("one",)):
    return {"dataset_sha256": manifest["dataset_sha256"],
            "evaluation_config_sha256": manifest["evaluation_config_sha256"],
            "run": {"hardware": "test-machine", "versions": {"runner": "1"}, "concurrency": 1, "temperature": "cold"},
            "observations": [{"id": ident, "passed": True, "failed": False, "latency_ms": 12} for ident in ids]}


def test_machine_generated_candidate_cannot_claim_gold():
    assert not evaluation.audit([sample(population="gold")])["valid"]
    assert evaluation.audit([sample()])["counts"]["gold"] == {}


@pytest.mark.parametrize("field,value", [("kind", "model"), ("oracle", "tested_llm"),
                                          ("source_supported", False), ("reviewer", ""),
                                          ("original_sha256", "e" * 64), ("sample_sha256", "e" * 64)])
def test_gold_requires_independent_human_bound_review(field, value):
    record = reviewed(); record["review"][field] = value
    assert not evaluation.audit([record])["valid"]


def test_edit_after_human_review_invalidates_sample():
    record = reviewed(); record["query"] = "changed intent"
    assert not evaluation.audit([record])["valid"]


def test_missing_generator_versions_or_unverified_provenance_block_gold():
    record = reviewed(); record["generator"]["provenance_verified"] = False
    record["review"]["sample_sha256"] = evaluation.review_digest(record)
    assert any("provenance" in e for e in evaluation.audit([record])["errors"])
    record = sample(); del record["generator"]["code_sha256"]
    assert not evaluation.audit([record])["valid"]


@pytest.mark.parametrize("group", ["document_id", "machine_id", "passage_group"])
def test_grouped_split_blocks_paraphrases_even_with_different_source_hashes(group):
    a = sample()
    b = sample("two", split="train", query="Circuit repair instructions?", document_id="other",
               machine_id="other", passage_group="other",
               source={"locator": "page2", "content_sha256": "e" * 64, "original_sha256": "f" * 64})
    b[group] = a[group]
    report = evaluation.audit([a, b])
    assert not report["valid"]
    assert group in {leak["kind"] for leak in report["leakage"]}


@pytest.mark.parametrize("duplicate", ["query", "support_excerpt"])
def test_normalized_duplicate_queries_and_passages_are_cross_group_leakage(duplicate):
    a = sample(**{duplicate: "Section 2: Hydraulic Circuit!"})
    b = sample("two", split="validation", document_id="two", machine_id="two", passage_group="two",
               source={"locator": "page2", "content_sha256": "e" * 64, "original_sha256": "f" * 64},
               **{duplicate: "section2 hydraulic circuit"})
    assert not evaluation.audit([a, b])["valid"]


def test_title_bias_reported_without_inventing_semantic_gold():
    report = evaluation.audit([sample(candidate_title="Circuit")])
    assert any("title-derived" in w for w in report["warnings"])
    assert report["full_gold_acceptance"] is False


def test_sample_kind_counts_cannot_inflate_retrieval_gold():
    report = evaluation.audit([reviewed(sample_kind="section_boundary")])
    assert report["unmet_gold_coverage"]["retrieval_total"]["actual"] == 0
    assert report["unmet_gold_coverage"]["section_boundary_total"]["actual"] == 1


def test_freeze_keeps_populations_separate_and_does_not_promote(tmp_path):
    records = [sample("two"), reviewed()]
    destination, manifest = bundle(tmp_path, records)
    assert len(evaluation.read_jsonl(destination / "gold.jsonl")) == 1
    assert len(evaluation.read_jsonl(destination / "synthetic.jsonl")) == 1
    _, frozen = evaluation.verify(destination)
    assert {r["population"] for r in frozen} == {"gold", "synthetic"}
    with pytest.raises(ValueError, match="already exists"):
        evaluation.freeze(records, destination, manifest["evaluation_config"])


def test_invalid_leaky_freeze_does_not_create_bundle(tmp_path):
    records = [sample(), sample("two", split="train")]
    with pytest.raises(ValueError, match="leakage"):
        evaluation.freeze(records, tmp_path / "v1", {"R": 10})
    assert not (tmp_path / "v1").exists()


@pytest.mark.parametrize("filename", ["gold.jsonl", "synthetic.jsonl"])
def test_modified_frozen_file_is_rejected(tmp_path, filename):
    destination, _ = bundle(tmp_path)
    with (destination / filename).open("a") as handle: handle.write("{}\n")
    with pytest.raises(ValueError, match="changed"):
        evaluation.verify(destination)


def test_changed_configuration_marks_results_stale(tmp_path):
    destination, manifest = bundle(tmp_path)
    results = run_result(manifest); results["evaluation_config_sha256"] = "f" * 64
    assert evaluation.score(destination, results)["status"] == "STALE"


def test_wrong_dataset_hash_cannot_score(tmp_path):
    destination, manifest = bundle(tmp_path)
    results = run_result(manifest); results["dataset_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="dataset hash"):
        evaluation.score(destination, results)


@pytest.mark.parametrize("ids", [(), ("other",), ("one", "one")])
def test_exact_result_coverage_required(tmp_path, ids):
    destination, manifest = bundle(tmp_path)
    with pytest.raises(ValueError): evaluation.score(destination, run_result(manifest, ids))


def test_small_gold_ci_exposes_lack_of_independent_machine_groups(tmp_path):
    destination, manifest = bundle(tmp_path)
    report = evaluation.score(destination, run_result(manifest))
    assert report["categories"]["total"]["ci95_machine_cluster_bootstrap"] is None
    assert report["categories"]["total"]["ci95_wilson_descriptive"][0] < .3
    assert report["categories"]["dtc"]["n"] == 0
    assert report["categories"]["dtc"]["ci95_wilson_descriptive"] is None
    assert report["performance"]["p95_ms"] == 12


def test_multi_machine_cluster_ci_is_reproducible(tmp_path):
    record = reviewed(id="two", machine_id="two", document_id="two", passage_group="two")
    destination, manifest = bundle(tmp_path, [reviewed(), record])
    results = run_result(manifest, ("one", "two")); results["observations"][1]["passed"] = False
    report = evaluation.score(destination, results)
    assert report["categories"]["total"]["ci95_machine_cluster_bootstrap"] == [0, 1]
    assert report == evaluation.score(destination, results)


def test_no_gold_means_not_run_even_if_synthetic_exists(tmp_path):
    destination, manifest = bundle(tmp_path, [sample()])
    report = evaluation.score(destination, run_result(manifest))
    assert report["status"] == "NOT_RUN" and report["ci95"] is None


@pytest.mark.parametrize("latency", [-1, float("nan"), True])
def test_invalid_latency_is_not_a_performance_claim(tmp_path, latency):
    destination, manifest = bundle(tmp_path); results = run_result(manifest)
    results["observations"][0]["latency_ms"] = latency
    with pytest.raises(ValueError): evaluation.score(destination, results)


def test_real_i003_import_is_only_unreviewed_synthetic():
    source = TOOL.parent.parent / "evaluation/sy215c-candidates-I003.jsonl"
    rows = evaluation.import_candidates(source, "d" * 64, "SY215C", "manual-sy215c")
    report = evaluation.audit(rows)
    assert report["valid"] and report["counts"]["synthetic"]["total"] == 50
    assert report["counts"]["gold"] == {}
    assert all(r["generator"]["original_generator_version"] == "unknown" for r in rows)


@pytest.mark.parametrize("changes", [{"id": []}, {"id": None}, {"population": []}, {"sample_kind": []},
                                     {"categories": [[]]}, {"split": {}}, {"source": []}, {"source": {"content_sha256": []}}, {"generator": 1}])
def test_malformed_candidate_shape_reports_validation_error(changes):
    report = evaluation.audit([sample(**changes)])
    assert report["valid"] is False
    assert report["errors"]


def test_nonobject_candidate_reports_validation_error():
    assert not evaluation.audit([123])["valid"]


def test_malformed_review_is_rejected_for_gold_without_crashing():
    record = reviewed(); record["review"] = []
    assert not evaluation.audit([record])["valid"]


def test_original_generator_unknown_cannot_become_gold_by_only_reviewing(tmp_path):
    source = TOOL.parent.parent / "evaluation/sy215c-candidates-I003.jsonl"
    record = evaluation.import_candidates(source, "d" * 64, "SY215C", "manual-sy215c")[0]
    record.update(population="gold", split="test", expected={"answer": "reviewed"})
    record["review"] = copy.deepcopy(reviewed()["review"])
    record["review"]["sample_sha256"] = evaluation.review_digest(record)
    assert any("provenance" in e for e in evaluation.audit([record])["errors"])


def test_performance_ci_is_explicitly_unavailable_for_one_machine(tmp_path):
    destination, manifest = bundle(tmp_path)
    report = evaluation.score(destination, run_result(manifest))
    assert report["performance"]["ci95_machine_cluster_bootstrap"] is None
    assert report["performance"]["failure_ci95_wilson_descriptive"][1] > .7


def test_malformed_observation_reports_value_error(tmp_path):
    destination, manifest = bundle(tmp_path); results = run_result(manifest)
    results["observations"] = [42]
    with pytest.raises(ValueError, match="observations"):
        evaluation.score(destination, results)
