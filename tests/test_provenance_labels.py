import json
import shutil
import hashlib
import sqlite3
from pathlib import Path

import pytest

from ar import broker as broker_mod
from ar import evidence
from ar.completion import CompletionAssessment
from ar.experiment import ExperimentSpec, run_experiment
from ar.export import export_run
from ar.imports import register_import
from ar.models import ArtifactVersion, ClaimDraft, EvidenceLink, LineSelector, SourceRef

FIXTURE = Path(__file__).parent.parent / "fixtures" / "experiment"
LOG = "$ python train.py\nvalidation accuracy 0.93\nALL CHECKS PASSED\n"

def art_for(content: str) -> ArtifactVersion:
    return ArtifactVersion(artifact_id="art-1", revision=1, artifact_type="report",
                           content_sha256=hashlib.sha256(content.encode()).hexdigest(), producing_task="t-writer")


@pytest.fixture
def env(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    broker_mod.ensure_schema(conn)
    evidence.ensure_schema(conn)
    return tmp_path, conn, broker_mod.Broker(conn, tmp_path)


def make_import(root, broker, conn, text=LOG):
    log = root / "host.log"
    log.write_bytes(text.encode())
    return register_import(conn, broker, path=log, kind="experiment_log", executor="host-assistant")


def draft(rec, receipt, line=2, quote="validation accuracy 0.93"):
    link = EvidenceLink(relation="supports", source_ref=SourceRef(source_id=rec.source_id),
                        locator=LineSelector(start=line, end=line), quote=quote, read_receipt_id=receipt)
    return ClaimDraft(statement="The host says accuracy was 0.93.", evidence_links=[link])


def accepted_claim(root, conn, broker):
    rec = make_import(root, broker, conn)
    got = broker.read(task_id="t-r", attempt_id="a1", packet_id="p1", ref=SourceRef(source_id=rec.source_id))
    result = evidence.accept_claims(conn, broker, task_id="t-r", attempt_id="a1",
                                    drafts=[draft(rec, got.read_receipt_id)])
    assert [c.id for c in result.accepted] == ["claim-1"]
    return rec


def export(root, conn, content):
    return export_run(conn, project_root=root, artifact=art_for(content), content=content,
                      assessment=CompletionAssessment(complete=True), outcome="completed", run_id="run-1")


def test_claim_citing_an_import_is_labelled_in_report_and_manifest(env):
    root, conn, broker = env
    rec = accepted_claim(root, conn, broker)
    report, _, prov_path, _ = export(root, conn, "# Results\n\nAccuracy was high. [claim:claim-1]\n")
    text = report.read_text(encoding="utf-8")
    assert f"[host_reported: {rec.id}, executor host-assistant]" in text
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    assert prov["claims"][0]["evidence_links"][0]["provenance"] == "host_reported"
    assert prov["imports"][0]["provenance"] == "host_reported"
    assert "did not enforce" in prov["imports"][0]["assurance"]
    assert prov["experiments"] == []


def test_plain_source_links_carry_no_import_label(env):
    root, conn, broker = env
    paper = root / "paper-a.md"
    paper.write_text("line one\nBaseline is supported.\n", encoding="utf-8")
    ref = broker.register_source("paper-a", paper)
    got = broker.read(task_id="t", attempt_id="a1", packet_id="p", ref=ref)
    link = EvidenceLink(relation="supports", source_ref=ref, locator=LineSelector(start=2, end=2),
                        quote="Baseline is supported.", read_receipt_id=got.read_receipt_id)
    evidence.accept_claims(conn, broker, task_id="t", attempt_id="a1",
                           drafts=[ClaimDraft(statement="s", evidence_links=[link])])
    report, _, prov, _ = export(root, conn, "# R\n\nx [claim:claim-1]\n")
    assert "host_reported" not in report.read_text(encoding="utf-8")
    assert json.loads(prov.read_text(encoding="utf-8"))["claims"][0]["evidence_links"][0]["provenance"] is None


def test_inspect_claim_shows_the_label(env):
    root, conn, broker = env
    accepted_claim(root, conn, broker)
    assert "provenance: host_reported (import-1, executor host-assistant)" in evidence.inspect_claim(conn, "claim-1")


def test_direct_citations_of_runs_and_imports_become_footnotes(env):
    root, conn, broker = env
    rec = make_import(root, broker, conn)
    shutil.copytree(FIXTURE, root / "fixtures" / "experiment")
    spec = ExperimentSpec(
        id="spec-1", question="q", seed=7, working_dir="fixtures/experiment",
        input_files=["fixtures/experiment/data.csv", "fixtures/experiment/baseline_mae.py"],
        command=["python", "baseline_mae.py", "--seed", "7"], expected_outputs=["results.json"],
    )
    run = run_experiment(root, conn, spec)
    content = (f"# Results\n\nMAE was computed [run:{run.id}]. The host log agrees [import:{rec.id}]. "
               "Bad [run:exp-99].\n")
    report, _, prov_path, _ = export(root, conn, content)
    text = report.read_text(encoding="utf-8")
    assert f"[^{run.id}]" in text and f"[^{rec.id}]" in text and "[run:exp-99 UNRESOLVED]" in text
    assert f"[^{run.id}]: Experiment run {run.id} of spec-1 [runtime_captured]: succeeded" in text
    assert "NOT an OS sandbox" in text and "result acceptable" in text
    assert f"[^{rec.id}]: Imported experiment_log {rec.id} [host_reported]" in text
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    assert prov["experiments"][0]["id"] == run.id and prov["experiments"][0]["provenance"] == "runtime_captured"
    assert prov["experiments"][0]["output_hashes"] == run.output_hashes


def test_claim_cannot_use_a_derived_import_as_independent_evidence(env):
    root, conn, broker = env
    broker.create_artifact(task_id="t-w", attempt_id="a1", artifact_type="report", content=LOG)
    rec = make_import(root, broker, conn)  # same bytes as a generated artifact
    assert rec.provenance == "derived"
    got = broker.read(task_id="t-r", attempt_id="a1", packet_id="p1", ref=SourceRef(source_id=rec.source_id))
    result = evidence.accept_claims(conn, broker, task_id="t-r", attempt_id="a1",
                                    drafts=[draft(rec, got.read_receipt_id)])
    assert result.accepted == [] and "derived material" in result.rejected[0].reason
    assert evidence.list_claims(conn) == []
