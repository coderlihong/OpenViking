#!/usr/bin/env python3
"""Offline, dependency-free KB evaluation ledger. Never promotes synthetic data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

SCHEMA_VERSION = "kb-evaluation-v1"
CATEGORIES = ("model", "dtc", "semantic", "table", "image", "tail", "fuzzy_meta",
              "exact_term", "paraphrase", "fuzzy_model", "negative", "section_boundary", "diagnostic", "title_derived_lookup")
MINIMA = {"exact_term": 15, "paraphrase": 15, "fuzzy_model": 15, "image": 15,
          "negative": 15}
SPLITS = {"train", "validation", "test", "unassigned"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def review_digest(record):
    """Human reviewer binds attestation to all sample fields, not just the query."""
    return digest({key: value for key, value in record.items() if key != "review"})


def read_jsonl(path):
    records = []
    for line_no, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_no}: expected object")
            records.append(value)
    return records


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def text(value):
    return isinstance(value, str) and bool(value.strip())


def normalize(value):
    return "".join(c for c in str(value).casefold() if c.isalnum())


def audit(records):
    if not isinstance(records, list):
        raise ValueError("records must be an array of objects")
    errors, warnings, leaks = [], [], []
    seen, groups = set(), defaultdict(list)
    counts = {population: Counter() for population in ("gold", "synthetic")}
    for record in records:
        if not isinstance(record, dict):
            errors.append("sample must be an object")
            continue
        ident = record.get("id")
        prefix = str(ident)
        if not text(ident) or ident in seen:
            errors.append(f"{prefix}: missing/duplicate id")
            continue
        seen.add(ident)
        pop = record.get("population")
        if not isinstance(pop, str) or pop not in counts:
            errors.append(f"{prefix}: population must be gold or synthetic")
            continue
        counts[pop]["total"] += 1
        kind = record.get("sample_kind")
        if not isinstance(kind, str) or kind not in {"retrieval", "image_annotation", "section_boundary", "diagnostic"}:
            errors.append(f"{prefix}: invalid sample_kind")
        else:
            counts[pop]["kind:" + kind] += 1
        categories = record.get("categories")
        if (not isinstance(categories, list) or not categories or
                any(not isinstance(c, str) or c not in CATEGORIES for c in categories) or len(categories) != len(set(categories))):
            errors.append(f"{prefix}: invalid categories")
            categories = []
        if kind == "retrieval":
            counts[pop].update(categories)
        for field in ("document_id", "machine_id", "passage_group", "query"):
            if not text(record.get(field)):
                errors.append(f"{prefix}: missing {field}")
        if not isinstance(record.get("split"), str) or record.get("split") not in SPLITS:
            errors.append(f"{prefix}: invalid split")
        if record.get("split") == "unassigned":
            warnings.append(f"{prefix}: unassigned; cannot be used for held-out evaluation")
        generator = record.get("generator", {})
        if not isinstance(generator, dict):
            generator = {}
        if (not isinstance(generator, dict) or not text(generator.get("version")) or
                not text(generator.get("name")) or not sha(generator.get("input_sha256")) or
                not sha(generator.get("code_sha256"))):
            errors.append(f"{prefix}: generator name/version/code/input hash required")
        source = record.get("source", {})
        if not isinstance(source, dict):
            source = {}
        review = record.get("review", {})
        if not isinstance(review, dict):
            review = {}
        if (not isinstance(source, dict) or not text(source.get("locator")) or
                not sha(source.get("content_sha256")) or not sha(source.get("original_sha256"))):
            errors.append(f"{prefix}: source locator/content/original hashes required")
        if pop == "gold":
            if (not isinstance(review, dict) or review.get("kind") != "human" or
                    not text(review.get("reviewer")) or not text(review.get("reviewed_at")) or
                    review.get("decision") != "approved" or
                    review.get("oracle") not in ("original_source", "independent_human") or
                    review.get("source_supported") is not True or
                    not text(review.get("evidence_locator")) or
                    review.get("original_sha256") != source.get("original_sha256") or
                    review.get("sample_sha256") != review_digest(record)):
                errors.append(f"{prefix}: gold requires human approval bound to sample and independent original evidence")
            if generator.get("provenance_verified") is not True:
                errors.append(f"{prefix}: gold generator provenance unverified")
            if record.get("split") == "unassigned":
                errors.append(f"{prefix}: gold split unassigned")
            if not isinstance(record.get("expected"), dict) or not record["expected"]:
                errors.append(f"{prefix}: gold expected labels required")
        elif review.get("decision") == "approved":
            warnings.append(f"{prefix}: reviewed synthetic retained as synthetic; no automatic promotion")
        if generator.get("provenance_verified") is not True:
            warnings.append(f"{prefix}: generator provenance incomplete")
        title = normalize(record.get("candidate_title", ""))
        if title and title in normalize(record.get("query", "")):
            warnings.append(f"{prefix}: title-derived query bias; not independent semantic evidence")
        # Group across populations as well: tuning on synthetic from test gold is leakage.
        if not isinstance(record.get("split"), str) or record.get("split") not in SPLITS:
            continue
        for field in ("document_id", "machine_id", "passage_group"):
            key = normalize(record.get(field, ""))
            if key:
                groups[(field, key)].append(record)
        for field, value in (("source_content", source.get("content_sha256")),
                             ("duplicate_query", normalize(record.get("query", ""))),
                             ("duplicate_excerpt", normalize(record.get("support_excerpt", "")))):
            if isinstance(value, str) and value:
                groups[(field, value)].append(record)
    for (kind, key), members in sorted(groups.items()):
        splits = {r.get("split") for r in members} - {"unassigned"}
        if len(splits) > 1:
            leaks.append({"kind": kind, "group": key, "ids": sorted(r["id"] for r in members),
                          "splits": sorted(splits)})
    if leaks:
        errors.append("cross-split leakage found; exclude or regroup the recorded samples before freezing")
    gold = counts["gold"]
    unmet = {cat: {"required": minimum, "actual": gold[cat]}
             for cat, minimum in MINIMA.items() if gold[cat] < minimum}
    for kind, minimum in {"retrieval": 100, "image_annotation": 30, "section_boundary": 50, "diagnostic": 20}.items():
        actual = gold["kind:" + kind]
        if actual < minimum:
            unmet[kind + "_total"] = {"required": minimum, "actual": actual}
    for cat in ("model", "dtc", "semantic", "table", "image", "tail", "fuzzy_meta"):
        if not gold[cat]:
            unmet.setdefault(cat, {"required": 1, "actual": 0})
    return {"schema_version": SCHEMA_VERSION, "valid": not errors, "errors": errors,
            "warnings": warnings, "leakage": leaks, "counts": {k: dict(v) for k, v in counts.items()},
            "category_counts": {pop: {cat: values[cat] for cat in CATEGORIES} for pop, values in counts.items()},
            "sample_kind_counts": {pop: {kind: values["kind:" + kind] for kind in
                                          ("retrieval", "image_annotation", "section_boundary", "diagnostic")}
                                   for pop, values in counts.items()},
            "independent_groups": {field: len({normalize(r[field]) for r in records if isinstance(r, dict) and text(r.get(field))})
                                   for field in ("document_id", "machine_id")},
            "unmet_gold_coverage": unmet, "full_gold_acceptance": False,
            "limitations": ["Human attestations require external review of cited originals; this tool cannot prove their truth.",
                             "Coverage alone cannot establish quality, production FLOW, or parser acceptance.",
                             "Declared passage groups are mandatory; semantic paraphrase detection requires human review."]}


def freeze(records, output, evaluation_config):
    report = audit(records)
    if not report["valid"]:
        raise ValueError("dataset invalid: " + "; ".join(report["errors"]))
    output = Path(output)
    if output.exists():
        raise ValueError("freeze destination already exists; choose a new version")
    if not isinstance(evaluation_config, dict) or not evaluation_config:
        raise ValueError("nonempty frozen evaluation config required")
    payloads = {}
    for population in ("gold", "synthetic"):
        rows = sorted((r for r in records if r["population"] == population), key=lambda r: r["id"])
        payloads[population] = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True,
                                                 allow_nan=False) + "\n" for r in rows)
    manifest = {"schema_version": SCHEMA_VERSION, "dataset_sha256": digest(sorted(records, key=lambda r: r["id"])),
                "evaluation_config": evaluation_config, "evaluation_config_sha256": digest(evaluation_config),
                "tool_sha256": file_digest(__file__), "audit": report,
                "files": {p + ".jsonl": hashlib.sha256(body.encode()).hexdigest()
                          for p, body in payloads.items()}}
    output.mkdir(parents=True)
    for pop, body in payloads.items():
        (output / (pop + ".jsonl")).write_text(body)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def verify(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported manifest schema")
    records = []
    if set(manifest.get("files", {})) != {"gold.jsonl", "synthetic.jsonl"}:
        raise ValueError("unexpected frozen file list")
    for filename, expected in manifest["files"].items():
        if file_digest(bundle / filename) != expected:
            raise ValueError(f"frozen file changed: {filename}")
        rows = read_jsonl(bundle / filename)
        if any(r.get("population") != filename.removesuffix(".jsonl") for r in rows):
            raise ValueError("frozen populations mixed")
        records.extend(rows)
    if digest(manifest["evaluation_config"]) != manifest["evaluation_config_sha256"]:
        raise ValueError("frozen evaluation config changed")
    if digest(sorted(records, key=lambda r: r["id"])) != manifest["dataset_sha256"]:
        raise ValueError("frozen dataset hash changed")
    if not audit(records)["valid"]:
        raise ValueError("frozen data invalid")
    return manifest, records


def wilson(successes, n):
    if not n:
        return None
    z = 1.959963984540054
    p = successes / n
    denominator = 1 + z * z / n
    middle = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0, middle - half), min(1, middle + half)]


def percentile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def cluster_ci(rows):
    groups = defaultdict(list)
    for record, result in rows:
        groups[record["machine_id"]].append(int(result["passed"]))
    if len(groups) < 2:
        return None
    rng = random.Random(0)
    members = list(groups.values())
    draws = []
    for _ in range(2000):
        sample = [value for _ in members for value in rng.choice(members)]
        draws.append(sum(sample) / len(sample))
    return [percentile(draws, .025), percentile(draws, .975)]



def performance_ci(rows):
    groups = defaultdict(list)
    for record, result in rows:
        groups[record["machine_id"]].append(result)
    if len(groups) < 2:
        return None
    rng, draws = random.Random(0), {"p50_ms": [], "p95_ms": [], "failure_rate": []}
    members = list(groups.values())
    for _ in range(2000):
        sample = [value for _ in members for value in rng.choice(members)]
        latencies = [value["latency_ms"] for value in sample]
        draws["p50_ms"].append(percentile(latencies, .5))
        draws["p95_ms"].append(percentile(latencies, .95))
        draws["failure_rate"].append(sum(value["failed"] for value in sample) / len(sample))
    return {name: [percentile(values, .025), percentile(values, .975)] for name, values in draws.items()}


def score(bundle, results, population="gold", split="test"):
    if not isinstance(results, dict):
        raise ValueError("results must be an object")
    manifest, records = verify(bundle)
    if population not in {"gold", "synthetic"} or split not in {"test", "validation", "train"}:
        raise ValueError("invalid evaluation population/split")
    selected = {r["id"]: r for r in records if r["population"] == population and r["split"] == split}
    if results.get("dataset_sha256") != manifest["dataset_sha256"]:
        raise ValueError("results do not reference frozen dataset hash")
    if results.get("evaluation_config_sha256") != manifest["evaluation_config_sha256"]:
        return {"status": "STALE", "reason": "R/F/model/chunk/schema/evaluation config changed; rerun required"}
    if not selected:
        return {"status": "NOT_RUN", "population": population, "split": split, "sample_count": 0,
                "ci95": None, "reason": "No eligible samples; no quality/performance claim supported"}
    observations = results.get("observations", [])
    if (not isinstance(observations, list) or any(not isinstance(r, dict) or not text(r.get("id")) for r in observations) or
            len({r.get("id") for r in observations}) != len(observations)):
        raise ValueError("duplicate or invalid observations")
    if {r.get("id") for r in observations} != set(selected):
        raise ValueError("exact selected sample coverage required; missing/extra results rejected")
    for result in observations:
        if (not isinstance(result.get("passed"), bool) or not isinstance(result.get("failed"), bool) or
                not isinstance(result.get("latency_ms"), (int, float)) or isinstance(result["latency_ms"], bool) or
                not math.isfinite(result["latency_ms"]) or result["latency_ms"] < 0 or
                (result["failed"] and result["passed"])):
            raise ValueError("invalid pass/failure/latency observation")
    context = results.get("run", {})
    if (not isinstance(context, dict) or not text(context.get("hardware")) or
            not isinstance(context.get("versions"), dict) or not context["versions"] or
            not isinstance(context.get("concurrency"), int) or isinstance(context["concurrency"], bool) or
            context["concurrency"] < 1 or context.get("temperature") not in {"cold", "warm"}):
        raise ValueError("run hardware/versions/concurrency/cold-warm context required")
    rows = [(selected[r["id"]], r) for r in observations]
    categories = {}
    for category in ("total",) + CATEGORIES:
        subset = rows if category == "total" else [(r, v) for r, v in rows if category in r["categories"]]
        n, successes = len(subset), sum(v["passed"] for _, v in subset)
        categories[category] = {"n": n, "successes": successes, "rate": successes / n if n else None,
                                "ci95_wilson_descriptive": wilson(successes, n),
                                "ci95_machine_cluster_bootstrap": cluster_ci(subset),
                                "machine_groups": len({r["machine_id"] for r, _ in subset})}
    latencies = [v["latency_ms"] for _, v in rows]
    return {"status": "MEASURED", "population": population, "split": split, "run": context,
            "dataset_sha256": manifest["dataset_sha256"], "categories": categories,
            "performance": {"n": len(rows), "p50_ms": percentile(latencies, .5),
                            "p95_ms": percentile(latencies, .95),
                            "failure_rate": sum(v["failed"] for _, v in rows) / len(rows),
                            "failure_ci95_wilson_descriptive": wilson(sum(v["failed"] for _, v in rows), len(rows)),
                            "ci95_machine_cluster_bootstrap": performance_ci(rows)},
            "limitations": ["Wilson intervals are descriptive and assume independent observations; correlated passages violate that assumption.",
                             "Machine cluster bootstrap is unavailable with fewer than two machine groups; small groups cannot support generalization.",
                             "Observed pass flags are runner judgments, not independent gold creation; synthetic results never establish gold acceptance."]}


def import_candidates(path, original_sha256, machine_id, document_id):
    """Conservative migration of I003; never invent original generator provenance."""
    if not sha(original_sha256):
        raise ValueError("original SHA256 required")
    records = []
    for candidate in read_jsonl(path):
        records.append({"id": candidate["candidate_id"], "population": "synthetic", "split": "unassigned", "sample_kind": "retrieval",
                        "query": candidate["query"], "categories": ["title_derived_lookup"],
                        "candidate_title": candidate["candidate_title"],
                        "document_id": document_id, "machine_id": machine_id,
                        "passage_group": candidate["fixture_content_sha256"],
                        "support_excerpt": candidate["candidate_support_excerpt"],
                        "source": {"locator": candidate["fixture_relative_path"],
                                   "content_sha256": candidate["fixture_content_sha256"],
                                   "original_sha256": original_sha256,
                                   "artifact_sha256": candidate["source_artifact_sha256"],
                                   "production_uri_verified": candidate["source_uri_verified_in_production"]},
                        "generator": {"name": "I003-title-derived-candidate-import", "version": "1",
                                      "code_sha256": file_digest(__file__), "input_sha256": file_digest(path),
                                      "provenance_verified": False,
                                      "original_generator_version": "unknown"},
                        "legacy_status": candidate["status"]})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-candidates")
    imp.add_argument("input"); imp.add_argument("output")
    imp.add_argument("--original-sha256", required=True); imp.add_argument("--machine-id", required=True)
    imp.add_argument("--document-id", required=True)
    for command in ("audit", "freeze"):
        p = sub.add_parser(command); p.add_argument("input"); p.add_argument("output")
        if command == "freeze": p.add_argument("--config", required=True)
    p = sub.add_parser("verify"); p.add_argument("bundle")
    p = sub.add_parser("score"); p.add_argument("bundle"); p.add_argument("results"); p.add_argument("output")
    p.add_argument("--population", choices=("gold", "synthetic"), default="gold")
    p.add_argument("--split", choices=("test", "validation", "train"), default="test")
    args = parser.parse_args()
    try:
        if args.command == "import-candidates":
            rows = import_candidates(args.input, args.original_sha256, args.machine_id, args.document_id)
            with Path(args.output).open("x") as handle:
                for row in rows: handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return
        if args.command == "audit": result = audit(read_jsonl(args.input))
        elif args.command == "freeze":
            freeze(read_jsonl(args.input), args.output, json.loads(Path(args.config).read_text())); return
        elif args.command == "verify":
            manifest, rows = verify(args.bundle)
            print(json.dumps({"valid": True, "dataset_sha256": manifest["dataset_sha256"], "count": len(rows)})); return
        else:
            result = score(args.bundle, json.loads(Path(args.results).read_text()), args.population, args.split)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        if args.command == "audit" and not result["valid"]: raise SystemExit(1)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
