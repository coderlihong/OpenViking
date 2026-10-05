"""Acceptance harness regression tests; no live service or model required."""
import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("flow_w1_pdf", Path(__file__).parent / "flows" / "flow_w1_pdf.py")
flow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow)

SOURCE = "viking://resources/raw/book/chapter.md"
TARGET = "viking://resources/wiki/sany"


class FakeKB:
    def __init__(self, drafts=None):
        self.drafts = drafts or []
        self.read_indices = []

    async def get_wiki_draft(self, job_id, index):
        self.read_indices.append(index)
        return self.drafts[index]

    async def get_ingestion_job(self, job_id):
        return {"state": "running", "stage": "wiki_extract"}


def fixture():
    candidate = {"uri": TARGET + "/Thing/a.md", "contributors": [SOURCE],
                 "evidence": [{"source_uri": SOURCE, "quote": "original"}]}
    drafts = [{"source_uri": SOURCE, "source_chunk": i, "build_id": "build",
               "plan_digest": "digest", "candidates": [] if i == 0 else [candidate]}
              for i in range(2)]
    receipt = {"private": True, "state": "draft_ready", "diagnostic_publishable": False,
               "build_id": "build", "plan_digest": "digest", "candidate_count": 1,
               "receipts": [{"source_uri": SOURCE, "source_chunk": i, "private": True,
                             "state": "draft_ready", "candidate_count": i} for i in range(2)]}
    final = {"checkpoint": {"draft_receipt": receipt, "diagnostic_published": False}}
    return FakeKB(drafts), final


def check(kb, final):
    return asyncio.run(flow.validate_drafts(kb, "job", final, {SOURCE}, TARGET, {SOURCE: "original source text"}))


def test_empty_first_fragment_does_not_hide_later_candidates():
    kb, final = fixture()
    result = check(kb, final)
    assert kb.read_indices == [0, 1]
    assert result["candidate_count"] == 1
    assert all(v for k, v in result.items() if k.startswith("assert_"))


def test_every_evidence_must_be_traceable_even_when_one_is_valid():
    kb, final = fixture()
    kb.drafts[1]["candidates"][0]["evidence"].append(
        {"source_uri": "viking://resources/foreign/secret.md", "quote": "foreign"})
    assert check(kb, final)["assert_source_traceable"] is False


def test_candidate_total_mismatch_fails():
    kb, final = fixture()
    final["checkpoint"]["draft_receipt"]["candidate_count"] = 2
    assert check(kb, final)["assert_candidate_counts_match"] is False


def test_public_receipt_or_diagnostic_publication_fails():
    for mutation in ("receipt", "diagnostic"):
        kb, final = fixture()
        if mutation == "receipt":
            final["checkpoint"]["draft_receipt"]["receipts"][1]["private"] = False
        else:
            final["checkpoint"]["diagnostic_published"] = True
        assert check(kb, final)["assert_private_no_diagnostic_publish"] is False


def test_timeout_reports_running_observation_and_resume_instruction():
    now = [0.0]
    async def sleep(seconds):
        now[0] += seconds
    final, timeout = asyncio.run(flow.poll_job(FakeKB(), "existing-job",
        deadline_seconds=10, poll_interval=3, clock=lambda: now[0], sleep=sleep))
    assert now[0] == 10
    assert final["state"] == "running"
    assert timeout["reason"] == "observation_deadline_exceeded"
    assert timeout["job_still_active"] is True
    assert "--job-id existing-job" in timeout["next_step"]


def test_terminal_job_does_not_wait():
    class ReadyKB:
        async def get_ingestion_job(self, job_id):
            return {"state": "draft_ready"}
    final, timeout = asyncio.run(flow.poll_job(ReadyKB(), "job", deadline_seconds=10, poll_interval=3))
    assert final["state"] == "draft_ready"
    assert timeout is None


def test_resume_validates_existing_job_without_upload_or_execution(monkeypatch):
    from argparse import Namespace
    kb, final = fixture()
    final.update({"id": "job", "state": "draft_ready", "kind": "W1", "payload": {"build_id": "build"}})

    async def get_job(job_id):
        assert job_id == "job"
        return final

    async def get_build(build_id):
        assert build_id == "build"
        return {"build_id": "build", "state": "prepared", "manifest": {
            "sources": [{"uri": SOURCE}, {"uri": "viking://resources/raw/book/index.md"}]}}

    kb.get_ingestion_job, kb.get_wiki_build = get_job, get_build

    class Client:
        def __init__(self, **kwargs):
            self.kb = kb
        async def initialize(self):
            pass
        async def close(self):
            pass
        async def read_raw(self, uri):
            return "original source text"
        async def _upload_temp_file(self, path):
            raise AssertionError("Resume must not upload or change source epochs")

    monkeypatch.setattr(flow, "AsyncHTTPClient", Client)
    result = asyncio.run(flow.run(Namespace(job_id="job", build_id=None, pdf=None,
        url="http://invalid", key="test-placeholder", timeout=1, deadline_seconds=10,
        poll_interval=1, poll_attempts=None, target_prefix=TARGET)))
    assert result["resumed_existing_job"] is True
    assert result["passed"] is True
    assert "key" not in result["args"]


def test_legal_source_uri_with_invented_quote_fails():
    kb, final = fixture()
    kb.drafts[1]["candidates"][0]["evidence"][0]["quote"] = "invented quote"
    result = check(kb, final)
    assert result["assert_source_traceable"] is True
    assert result["assert_quotes_verbatim"] is False
