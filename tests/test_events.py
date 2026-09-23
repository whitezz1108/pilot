"""The event log: append-only, ordered, serializable, and free of gold data.

The log is the primary audit artifact, so its guarantees are tested directly
rather than inferred from the runner's behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from pilot01.events import Event, EventCollector, EventType, utc_now
from pilot01.schemas import ErrorCondition
from pilot01.workflow.runner import WorkflowRunner
from pilot01.workflow.transitions import NodeRegistry, ProtocolError


def test_event_ids_are_dense_and_monotonic(make_state, runner):
    outcome = runner.run(make_state("A0V0"))
    assert [event.event_id for event in outcome.events] == list(range(len(outcome.events)))


def test_event_log_is_append_only_in_execution_order(make_state, runner):
    outcome = runner.run(make_state("A0V0"))
    types = [event.event_type for event in outcome.events]

    assert types == [
        EventType.RUN_STARTED,
        EventType.NODE_STARTED,
        EventType.AGENT_INPUT_CREATED,
        EventType.AGENT_OUTPUT_RECEIVED,
        EventType.NODE_COMPLETED,
        EventType.TRANSITION,
        EventType.NODE_STARTED,
        EventType.AGENT_INPUT_CREATED,
        EventType.AGENT_OUTPUT_RECEIVED,
        EventType.NODE_COMPLETED,
        EventType.TRANSITION,
        EventType.NODE_STARTED,
        EventType.AGENT_INPUT_CREATED,
        EventType.AGENT_OUTPUT_RECEIVED,
        EventType.NODE_COMPLETED,
        EventType.TRANSITION,
        EventType.RUN_COMPLETED,
    ]
    assert [event.node for event in outcome.events if event.event_type is EventType.NODE_STARTED] == [
        "analyst",
        "manager",
        "compliance",
    ]


def test_collector_exposes_no_mutation_api():
    collector = EventCollector("run-1")
    for forbidden in ("remove", "pop", "clear", "insert", "extend", "sort", "reverse", "__setitem__"):
        assert not hasattr(collector, forbidden), forbidden


def test_event_snapshot_is_immutable():
    collector = EventCollector("run-1")
    collector.append(EventType.RUN_STARTED)
    snapshot = collector.events
    assert isinstance(snapshot, tuple)
    with pytest.raises(TypeError):
        snapshot[0] = None  # type: ignore[index]
    assert len(collector) == 1


def test_events_are_frozen_models():
    collector = EventCollector("run-1")
    event = collector.append(EventType.RUN_STARTED)
    assert isinstance(event, Event)
    with pytest.raises(ValidationError):
        event.payload = {"tampered": True}
    with pytest.raises(ValidationError):
        event.event_type = EventType.RUN_COMPLETED


def test_collector_requires_a_timezone_aware_clock():
    naive = EventCollector("run-1", clock=lambda: datetime(2026, 1, 1, 12, 0, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.append(EventType.RUN_STARTED)
    assert utc_now().tzinfo is not None


def test_jsonl_roundtrip(make_state, runner):
    outcome = runner.run(make_state("A1V1"))
    lines = outcome.to_jsonl().strip().splitlines()
    assert len(lines) == len(outcome.events)

    for line, original in zip(lines, outcome.events, strict=True):
        restored = Event.model_validate(json.loads(line))
        assert restored == original
        assert restored.run_id == outcome.run_id


def test_write_jsonl_creates_the_file(make_state, runner, tmp_path):
    outcome = runner.run(make_state("A0V0"))
    target = tmp_path / "nested" / "events.jsonl"

    collector = EventCollector(outcome.run_id)
    for event in outcome.events:
        collector.append(event.event_type, node=event.node, payload=event.payload)
    written = collector.write_jsonl(target)

    assert written == target and target.is_file()
    assert len(target.read_text(encoding="utf-8").strip().splitlines()) == len(outcome.events)


def test_event_log_records_the_treatment_for_audit(make_state, runner):
    """The log is experimenter-facing, so the treatment must be recoverable from it."""
    state = make_state("A1V1", error_condition=ErrorCondition.E1)
    outcome = runner.run(state)
    started = outcome.events[0]
    assert started.event_type is EventType.RUN_STARTED
    assert started.payload["error_condition"] == "E1"
    assert started.payload["condition_id"] == "A1V1"
    assert started.payload["source_access"] is True
    assert started.payload["verification_required"] is True
    assert started.payload["memo_claim_count"] == len(state.analyst_memo.claims)


def test_event_log_does_not_carry_gold(make_state, runner):
    """Gold is joined at scoring time; it is deliberately kept out of the log."""
    outcome = runner.run(make_state("A1V1"))
    log = outcome.to_jsonl()
    assert "gold" not in log
    assert "987654321" not in log


def test_agent_input_events_record_the_restricted_view(make_state, runner):
    outcome = runner.run(make_state("A0V0"))
    inputs = {
        event.node: event
        for event in outcome.events
        if event.event_type is EventType.AGENT_INPUT_CREATED
    }
    assert set(inputs) == {"analyst", "manager", "compliance"}
    assert inputs["manager"].payload["restricted"] is True
    assert inputs["compliance"].payload["restricted"] is True
    # The analyst is an artifact loader, and its input says so.
    assert inputs["analyst"].payload["restricted"] is False


def test_protocol_errors_are_logged(make_state, workflow, conditions, make_registry):
    real = make_registry()
    registry = NodeRegistry([real.get("analyst")])  # manager and compliance missing
    runner = WorkflowRunner(workflow=workflow, conditions=conditions, registry=registry)
    state = make_state("A0V0")

    with pytest.raises(ProtocolError):
        runner.run(state)

    events = runner._collector.events
    assert events[-1].event_type is EventType.PROTOCOL_ERROR
    assert events[-1].payload["error_type"] == "ProtocolError"
    assert not [e for e in events if e.event_type is EventType.RUN_COMPLETED]
