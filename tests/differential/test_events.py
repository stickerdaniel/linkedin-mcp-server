"""The event log keeps to its schema, and the counts keep skips."""

from __future__ import annotations

import json

import pytest

from differential.events import (
    BASE_FIELDS,
    COUNTS_FILE,
    Counts,
    EventLog,
    publish,
    read_jsonl,
    validate,
)


def test_every_emitted_record_carries_the_schema_and_reads_back(tmp_path):
    log = EventLog(tmp_path, run="run-1", platform="test-platform")
    log.emit(
        experiment="K1", row="H-R1", actor="frontend", kind="user.output", line="x"
    )
    log.emit(experiment="K3", row="H-R1", actor="watcher", kind="process.start", pid=7)

    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    for record in records:
        assert set(BASE_FIELDS) <= set(record)
        assert record["run"] == "run-1"
        assert record["platform"] == "test-platform"
    assert records[0]["line"] == "x"
    assert records[1]["pid"] == 7
    assert log.records() == records


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"actor": "somebody"}, "unknown actor"),
        ({"kind": "process.started"}, "unknown event kind"),
        ({"experiment": "K9"}, "unknown experiment"),
        ({"row": ""}, "row is empty"),
        ({"t": "now"}, "time is not a number"),
    ],
)
def test_a_record_outside_the_schema_is_refused(tmp_path, change, message):
    record = {
        "t": 1.0,
        "run": "r",
        "experiment": "K1",
        "row": "H-R1",
        "platform": "p",
        "actor": "harness",
        "kind": "row.outcome",
        **change,
    }
    with pytest.raises(ValueError, match=message):
        validate(record)
    log = EventLog(tmp_path, run="r")
    with pytest.raises(ValueError):
        log.append(record)
    assert log.records() == []


def test_a_record_missing_a_base_field_is_refused():
    with pytest.raises(ValueError, match="lacks"):
        validate({"t": 1.0, "run": "r", "experiment": "K1", "row": "H-R1"})


def test_a_torn_last_line_is_dropped_not_fatal(tmp_path):
    path = tmp_path / "watcher.jsonl"
    path.write_text('{"a": 1}\n{"b": 2}\n{"c": ')
    assert read_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_counts_keep_skips_apart_from_executed_and_failed(tmp_path):
    counts = Counts()
    for status in ("executed", "executed", "skipped", "failed"):
        counts.record(
            experiment="K3",
            row="H-R1",
            column="integrated",
            status=status,
            platform="p",
        )
    counts.record(
        experiment="K1", row="H-R1", column="integrated", status="skipped", platform="p"
    )

    written = json.loads(counts.write(tmp_path).read_text())
    assert written == {
        "cases": [
            {
                "experiment": "K1",
                "row": "H-R1",
                "platform": "p",
                "column": "integrated",
                "executed": 0,
                "skipped": 1,
                "failed": 0,
            },
            {
                "experiment": "K3",
                "row": "H-R1",
                "platform": "p",
                "column": "integrated",
                "executed": 2,
                "skipped": 1,
                "failed": 1,
            },
        ]
    }


def test_counts_refuse_an_unknown_status():
    with pytest.raises(ValueError):
        Counts().record(
            experiment="K1", row="H-R1", column="integrated", status="passed"
        )


def test_publishing_copies_the_evidence_only_when_asked(tmp_path):
    run_dir = tmp_path / "run"
    log = EventLog(run_dir, run="r1")
    log.emit(experiment="K1", row="H-R1", actor="harness", kind="row.outcome")
    Counts().write(run_dir)

    assert publish(run_dir, None, "r1") is None
    target = publish(run_dir, str(tmp_path / "out"), "r1")
    assert target is not None and target == tmp_path / "out" / "r1"
    assert (target / "events.jsonl").read_text() == log.path.read_text()
    assert (target / COUNTS_FILE).exists()


def test_publishing_copies_only_the_evidence_files(tmp_path):
    run_dir = tmp_path / "run"
    row = run_dir / "rows" / "K3"
    row.mkdir(parents=True)
    for name in ("watcher.jsonl", "watcher.stderr", "identity.json", "failures.json"):
        (row / name).write_text(name)
    # What must never leave the runner, even if a later change put it here.
    for name in ("cookies.json", "leaf-key.pem", "ca.pem", "source-state.json"):
        (row / name).write_text("secret")
    (row / "profile").mkdir()
    (row / "profile" / "Cookies").write_text("secret")
    EventLog(run_dir, run="r1").emit(
        experiment="K3", row="H-R1", actor="harness", kind="row.outcome"
    )

    target = publish(run_dir, str(tmp_path / "out"), "r1")
    assert target is not None
    copied = sorted(
        str(path.relative_to(target)).replace("\\", "/")
        for path in target.rglob("*")
        if path.is_file()
    )
    assert copied == [
        "events.jsonl",
        "rows/K3/failures.json",
        "rows/K3/identity.json",
        "rows/K3/watcher.jsonl",
        "rows/K3/watcher.stderr",
    ]


# --- The kinds a call that loses its caller records ----------------------------

#: One valid record of each, by the fields its kind requires.
_CALL_LOSS = {
    "gate.entered": {
        "path": "/in/synthetic-r4/details/education/",
        "ordinal": 1,
        "entered_monotonic_ns": 10,
    },
    "gate.released": {
        "path": "/in/synthetic-r4/details/education/",
        "ordinal": 1,
        "entered_monotonic_ns": 10,
        "released_monotonic_ns": 20,
        "terminal": "served",
    },
    "loss": {"loss": "pipe", "target": "host_stub", "monotonic_ns": 30},
    "phase": {"name": "post-loss", "monotonic_ns": 40},
    "attempt": {"attempt": "preflight", "classification": None},
}


def _call_loss(kind: str, **change) -> dict:
    return {
        "t": 1.0,
        "run": "r",
        "experiment": "K3",
        "row": "H-R4",
        "platform": "p",
        "actor": "harness",
        "kind": kind,
        **_CALL_LOSS[kind],
        **change,
    }


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        *[(kind, {}) for kind in _CALL_LOSS],
        *[("gate.released", {"terminal": t}) for t in ("deadline", "peer-gone")],
        *[
            ("loss", {"loss": loss})
            for loss in (
                "eof",
                "host-killed",
                "frontend-killed",
                "server-killed",
                "owner-killed",
            )
        ],
        *[("attempt", {"attempt": a}) for a in ("dispatch", "election", "replay")],
        ("attempt", {"classification": "UNREACHABLE"}),
    ],
)
def test_a_call_loss_record_with_its_fields_reads_back(tmp_path, kind, change):
    log = EventLog(tmp_path, run="r")
    log.append(_call_loss(kind, **change))
    (record,) = log.records()
    assert record["kind"] == kind


@pytest.mark.parametrize(
    ("kind", "name"),
    [(kind, name) for kind, fields in _CALL_LOSS.items() for name in fields],
)
def test_a_call_loss_record_missing_a_field_is_refused(tmp_path, kind, name):
    record = _call_loss(kind)
    del record[name]
    with pytest.raises(ValueError, match=f"lacks {name}"):
        validate(record)
    log = EventLog(tmp_path, run="r")
    with pytest.raises(ValueError):
        log.append(record)
    assert log.records() == []


@pytest.mark.parametrize(
    ("kind", "name", "value"),
    [
        ("gate.entered", "path", ""),
        ("gate.entered", "ordinal", -1),
        ("gate.entered", "ordinal", True),
        ("gate.entered", "entered_monotonic_ns", 1.5),
        ("gate.released", "terminal", "released"),
        ("gate.released", "released_monotonic_ns", None),
        ("loss", "loss", "killed"),
        ("loss", "target", "somebody"),
        ("loss", "monotonic_ns", "now"),
        ("phase", "name", ""),
        ("phase", "monotonic_ns", None),
        ("attempt", "attempt", "retry"),
        ("attempt", "classification", ""),
        ("attempt", "classification", 3),
    ],
)
def test_a_call_loss_field_outside_its_values_is_refused(kind, name, value):
    with pytest.raises(ValueError, match=f"{name} is invalid"):
        validate(_call_loss(kind, **{name: value}))
