import pytest

from ar import agents
from ar.models import LineSelector, SourceRef
from ar.packet import DeliveredRead, Packet


def test_numbered_uses_selector_offset():
    r = DeliveredRead(
        source_ref=SourceRef(source_id="s"), selector=LineSelector(start=7, end=8),
        content="a\nb", read_receipt_id="read-1",
    )
    assert r.numbered() == "7: a\n8: b"


def test_run_requires_configured_agent():
    agents._agent = None
    p = Packet(packet_id="p", task_id="t", attempt_id="a", role="writer", objective="o")
    with pytest.raises(RuntimeError):
        agents.run("writer", p)
