import json

from ar.agents import analyst
from ar.agents.fake import FakeAgent
from ar.packet import DependencyItem, Packet

SHA = "ab" * 32


def _packet(*deps: DependencyItem) -> Packet:
    return Packet(packet_id="p", task_id="t", attempt_id="a", role="analyst", objective="o",
                  dependencies=list(deps))


def _run(run_id="run-1", status="succeeded") -> DependencyItem:
    body = {"run_id": run_id, "status": status, "outputs": {"mae": 1.5, "label": "ok"}, "output_sha256": SHA}
    return DependencyItem(kind="artifact", id=run_id, content=json.dumps(body))


def test_fake_emits_observations_from_succeeded_run():
    packet = _packet(_run())
    res = FakeAgent({}).run("analyst", packet)
    assert res.outcome == "produced" and res.model_calls == 0
    out = analyst.AnalysisResult.model_validate(res.payload)
    assert {(o.metric, o.value) for o in out.observations} == {("mae", 1.5), ("label", "ok")}
    assert all(o.experiment_run_id == "run-1" and o.output_sha256 == SHA for o in out.observations)
    assert out.limitations and out.failure_modes
    assert analyst.unsupported_citations(packet, out) == []


def test_no_run_needs_dependency():
    report = DependencyItem(kind="artifact", id="report", content="# Report")
    for packet in (_packet(), _packet(report)):
        res = FakeAgent({}).run("analyst", packet)
        assert res.outcome == "needs_dependency" and res.payload["observations"] == []


def test_unsucceeded_run_is_never_completed():
    for status in ("failed", "timed_out", "execution_unknown"):
        res = FakeAgent({}).run("analyst", _packet(_run(status=status)))
        assert res.outcome == "unable_to_complete" and res.payload["observations"] == []
        assert res.unresolved


def test_run_without_hash_or_with_mismatched_id():
    nohash = DependencyItem(kind="artifact", id="r", content=json.dumps(
        {"run_id": "r", "status": "succeeded", "outputs": {"m": 1}}))
    assert FakeAgent({}).run("analyst", _packet(nohash)).outcome == "unable_to_complete"
    spoof = DependencyItem(kind="artifact", id="r", content=_run("other").content)
    assert FakeAgent({}).run("analyst", _packet(spoof)).outcome == "needs_dependency"


def test_mixed_runs_cite_only_succeeded():
    res = FakeAgent({}).run("analyst", _packet(_run("ok"), _run("bad", status="failed")))
    assert res.outcome == "produced"
    assert {o["experiment_run_id"] for o in res.payload["observations"]} == {"ok"}
    assert any("bad" in u for u in res.unresolved)


def test_unsupported_citations_flags_foreign_runs_and_hashes():
    packet = _packet(_run())
    obs = [analyst.Observation(experiment_run_id="ghost", metric="m", value=1, output_sha256=SHA),
           analyst.Observation(experiment_run_id="run-1", metric="m", value=1, output_sha256="00" * 32)]
    out = analyst.AnalysisResult(summary="s", observations=obs, limitations=[], failure_modes=[])
    assert analyst.unsupported_citations(packet, out) == ["ghost:m", "run-1:m"]


def test_user_message_frames_run_as_data():
    msg = analyst.user_message(_packet(_run()))
    assert '<experiment_run id="run-1"' in msg and "untrusted" in analyst.SYSTEM
