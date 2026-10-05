"""Run bounded synthetic parser probes without changing deployed worker code.

Prints only contract evidence. Credentials remain in process memory. A successful
probe establishes artifact coverage/identity, never semantic or gold acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pymupdf


def run(root: Path, validator: Path, report: Path, timeout: float, backend_only=None, image_id=None):
    spec = importlib.util.spec_from_file_location("probe_artifact", validator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    evidence = {"scope": "two-page synthetic document contract, not independent gold",
                "validator_sha256": hashlib.sha256(validator.read_bytes()).hexdigest(), "runs": []}
    headers = {"Authorization": "Bearer " + (root / "secrets/parser.key").read_text().strip()}
    with tempfile.TemporaryDirectory(prefix="ov-parser-probe-") as temporary:
        directory = Path(temporary)
        source = directory / "synthetic-two-pages.pdf"
        with pymupdf.open() as document:
            for index in range(2):
                page = document.new_page()
                page.insert_text((72, 72), f"Contract section {index + 1}")
                page.insert_text((72, 95), "Inspection: disconnect power before checking the synthetic circuit.")
                page.insert_text((72, 115), "Table: A = 10; B = 20. Synthetic fixture, no real repair claims.")
                image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 80), 0)
                image.clear_with(120)
                page.insert_image(pymupdf.Rect(72, 150, 152, 230), pixmap=image)
            document.save(source)
        evidence["input_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        evidence["input_pages"] = 2
        targets = [(backend_only, 9100)] if backend_only else [("teleocr", 9101), ("logics", 9102), ("mineru", 9103)]
        for backend, port in targets:
            entry = {"backend": backend, "requested_mode": "auto", "source_commit":
                     (root / f"locks/{backend}.commit.txt").read_text().strip()}
            if image_id:
                entry["container_image_id"] = image_id
            else:
                try:
                    inspected = subprocess.run(["docker", "inspect", f"ov-parser-worker-{backend}", "--format", "{{.Image}}"],
                                               capture_output=True, text=True)
                    entry["container_image_id"] = inspected.stdout.strip() if inspected.returncode == 0 else "unavailable"
                except FileNotFoundError:
                    entry["container_image_id"] = "unavailable"
            evidence["runs"].append(entry)
            job_id = None
            started = time.monotonic()
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=30) as client:
                    health = client.get("/health"); health.raise_for_status()
                    entry["deployed_health"] = health.json()
                    with source.open("rb") as stream:
                        response = client.post("/v1/jobs", files={"file": (source.name, stream, "application/pdf")},
                                               data={"backend": backend, "mode": "auto", "options": "{}"})
                    response.raise_for_status(); job_id = response.json()["job_id"]
                    entry["job_id"] = job_id
                    while True:
                        status = client.get(f"/v1/jobs/{job_id}"); status.raise_for_status()
                        state = status.json()
                        if state["status"] == "completed": break
                        if state["status"] in {"failed", "cancelled"}: raise ValueError("remote job failed")
                        if time.monotonic() - started > timeout: raise TimeoutError("bounded probe deadline")
                        time.sleep(1)
                    archive = directory / f"{backend}.zip"
                    with client.stream("GET", f"/v1/jobs/{job_id}/artifact") as response:
                        response.raise_for_status()
                        count = 0
                        with archive.open("wb") as handle:
                            for part in response.iter_bytes():
                                count += len(part)
                                if count > 64 * 1024**2 or time.monotonic() - started > timeout:
                                    raise ValueError("probe artifact resource bound exceeded")
                                handle.write(part)
                    output = directory / backend
                    module.safe_extract(archive, output, max_bytes=128 * 1024**2)
                    parsed = module.validate_document(output, backend=backend, mode="auto", options={}, expected_pages=2)
                    entry.update(status="CONTRACT_PASS", coverage=parsed["coverage"], version=parsed["version"],
                                 model_revision=parsed["model_revision"], effective_backend=parsed["backend"],
                                 effective_mode=parsed["mode"], asset_count=len(parsed["assets"]),
                                 block_count=len(parsed["blocks"]), artifact_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            except Exception as exc:
                entry.update(status="FAILED", error_type=type(exc).__name__)
                if job_id:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=5) as client:
                        try: client.delete(f"/v1/jobs/{job_id}")
                        except httpx.HTTPError: pass
            entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report.write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(entry), flush=True)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--backend", choices=("teleocr", "logics", "mineru"))
    parser.add_argument("--container-image-id")
    args = parser.parse_args()
    run(args.root, args.validator, args.report, args.timeout, args.backend, args.container_image_id)
