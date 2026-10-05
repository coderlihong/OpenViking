import hashlib
import json
import time
from pathlib import Path

import httpx
import pymupdf
from model_artifact import safe_extract, validate_document

root = Path("/models")
report = root / "state/manual-full-evaluation.json"
started = time.time()
evidence = {
    "status": "RUNNING",
    "backend": "mineru",
    "scope": "Full SY215C model parsing artifact coverage; not semantic retrieval quality",
    "input_pages": 1072,
    "source_sha256": "99a3a0d37696c7f76f8364a641e06b6a100d8e57f7030c1715beae0487196534",
    "started": started,
}


def save():
    report.write_text(json.dumps(evidence, indent=2))


save()
try:
    with (root / "tmp/manual.pdf").open("rb") as source:
        actual_sha = hashlib.file_digest(source, "sha256").hexdigest()
    if actual_sha != evidence["source_sha256"]:
        raise ValueError("Input is not the pinned SY215C evaluation fixture")
    with pymupdf.open(root / "tmp/manual.pdf") as pdf:
        if len(pdf) != evidence["input_pages"]:
            raise ValueError("Evaluation fixture page count mismatch")
    key = (root / "secrets/parser.key").read_text().strip()
    with httpx.Client(
        base_url="http://127.0.0.1:9100", headers={"Authorization": "Bearer " + key}, timeout=300
    ) as client:
        with (root / "tmp/manual.pdf").open("rb") as source:
            response = client.post(
                "/v1/jobs",
                files={"file": ("manual.pdf", source, "application/pdf")},
                data={"backend": "mineru", "mode": "auto", "options": "{}"},
            )
        response.raise_for_status()
        job_id = response.json()["job_id"]
        evidence["job_id"] = job_id
        save()
        while True:
            result = client.get("/v1/jobs/" + job_id)
            result.raise_for_status()
            state = result.json()
            evidence["job_status"] = state["status"]
            evidence["observed_at"] = time.time()
            save()
            if state["status"] == "completed":
                break
            if state["status"] in {"failed", "cancelled"}:
                raise RuntimeError(state.get("error") or state["status"])
            if time.time() - started > 7500:
                raise TimeoutError("Full manual deadline")
            time.sleep(10)
        archive = root / "tmp/manual-full-artifact.zip"
        with client.stream("GET", "/v1/jobs/" + job_id + "/artifact") as response:
            response.raise_for_status()
            with archive.open("wb") as target:
                for chunk in response.iter_bytes():
                    target.write(chunk)
    output = root / "tmp/manual-full-artifact"
    safe_extract(archive, output)
    parsed = validate_document(
        output, backend="mineru", mode="auto", options={}, expected_pages=1072
    )
    with archive.open("rb") as source:
        artifact_sha = hashlib.file_digest(source, "sha256").hexdigest()
    evidence.update(
        status="PASS",
        coverage=parsed["coverage"],
        assets=len(parsed["assets"]),
        blocks=len(parsed["blocks"]),
        elapsed_seconds=round(time.time() - started, 2),
        artifact_sha256=artifact_sha,
    )
except Exception as exc:
    evidence.update(status="FAIL", error=f"{type(exc).__name__}: {exc}")
finally:
    save()
print(json.dumps(evidence), flush=True)
