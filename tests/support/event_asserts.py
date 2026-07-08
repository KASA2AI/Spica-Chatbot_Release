"""Reusable, format-agnostic assertions for the turn event contract (C0).

REFACTOR_PLAN_CORE §0 defines a *two-layer* event model. These helpers exist so
every later stage (C1 onwards) can reorder concurrency/telemetry freely and only
fail when the part that is actually a contract changes:

  - ORDERED AXIS (precise): ``unit_ready`` indices ascend 0,1,2,... contiguously;
    ``done`` comes after every ``unit_ready``; nothing (``unit_ready``/``done``)
    follows an ``error``. This is the only thing that needs an ordering guarantee.
  - TELEMETRY (loose): ``status`` / ``unit_text_ready`` / ``unit_visual_ready`` /
    ``unit_audio_*`` are asserted *present/absent only* -- never order or count,
    never timing values.

Every event-shape access goes through the ``event_kind`` / ``event_field`` seam,
so the helpers run identically over legacy ``{"event", "data"}`` dicts and over
``RuntimeEvent`` dataclasses. The two representations are interchangeable, which
is exactly what lets C1.5's "zero behaviour change" rest on these assertions.
"""

from __future__ import annotations

from typing import Any, Iterable

AXIS_KINDS = ("unit_ready", "done", "error")


# --- format-agnostic seam (dict OR RuntimeEvent) --------------------------- #

def event_kind(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("event") or "")
    return str(getattr(event, "kind", type(event).__name__) or "")


def event_field(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get("data", {}).get(name, default)
    return getattr(event, name, default)


# --- small convenience selectors ------------------------------------------ #

def unit_ready_events(events: Iterable[Any]) -> list[Any]:
    return [e for e in events if event_kind(e) == "unit_ready"]


def status_states(events: Iterable[Any]) -> set[str]:
    """Set of ``status`` sub-states seen (presence only -- never count/order)."""
    return {event_field(e, "state") for e in events if event_kind(e) == "status"}


# --- the two contract assertions ------------------------------------------ #

def assert_ordered_axis(
    events: Iterable[Any],
    expected_units: Any = None,
    *,
    terminal: str | None = "done",
) -> None:
    """Assert the ordered main axis of a turn's event stream.

    Always enforced:
      * ``unit_ready`` indices, in emission order, are exactly 0..N-1
        (strictly ascending, contiguous from 0);
      * every ``unit_ready`` carries a truthy ``emotion``;
      * after the first ``error`` there is no later ``unit_ready``/``done``;
      * if a ``done`` exists it is unique, comes after every ``unit_ready``,
        and nothing on the axis follows it.

    ``terminal``:
      * ``"done"``  -> a ``done`` must exist and no ``error`` may exist;
      * ``"error"`` -> an ``error`` must exist and no ``done`` may exist;
      * ``None``    -> no terminal requirement.

    ``expected_units`` (optional):
      * ``None``       -> assert structure only, not unit count/content;
      * ``int``        -> assert exactly that many ``unit_ready`` events;
      * ``list[str]``  -> assert each unit's ``display_text`` in order;
      * ``list[dict]`` -> per item check ``text`` and/or ``emotion`` keys.
    """
    events = list(events)
    kinds = [event_kind(e) for e in events]
    ready = unit_ready_events(events)
    ready_positions = [i for i, k in enumerate(kinds) if k == "unit_ready"]
    done_positions = [i for i, k in enumerate(kinds) if k == "done"]
    error_positions = [i for i, k in enumerate(kinds) if k == "error"]

    # (1) contiguous ascending indices from 0
    indices = [event_field(e, "index") for e in ready]
    assert indices == list(range(len(ready))), (
        f"unit_ready indices must be 0..N contiguous ascending, got {indices}"
    )

    # (2) every unit carries an emotion
    for e in ready:
        assert event_field(e, "emotion"), (
            f"unit_ready index={event_field(e, 'index')} missing emotion"
        )

    # (3) an error terminates the axis
    if error_positions:
        first_error = error_positions[0]
        late = [
            kinds[i]
            for i in range(first_error + 1, len(kinds))
            if kinds[i] in ("unit_ready", "done")
        ]
        assert not late, f"no unit_ready/done allowed after error, found {late}"

    # (4) done is unique, last on the axis, after all unit_ready
    if done_positions:
        assert len(done_positions) == 1, (
            f"expected exactly one done event, got {len(done_positions)}"
        )
        done_pos = done_positions[0]
        if ready_positions:
            assert max(ready_positions) < done_pos, (
                "done must come after every unit_ready"
            )

    # terminal expectation
    if terminal == "done":
        assert done_positions, f"expected a 'done' event, kinds={kinds}"
        assert not error_positions, f"did not expect an 'error' on the done path, kinds={kinds}"
    elif terminal == "error":
        assert error_positions, f"expected an 'error' event, kinds={kinds}"
        assert not done_positions, f"did not expect a 'done' on the error path, kinds={kinds}"
    elif terminal is not None:
        raise ValueError(f"unknown terminal {terminal!r}")

    # content of units
    if expected_units is None:
        return
    if isinstance(expected_units, int):
        assert len(ready) == expected_units, (
            f"expected {expected_units} unit(s), got {len(ready)}"
        )
        return
    assert len(ready) == len(expected_units), (
        f"expected {len(expected_units)} unit(s), got {len(ready)}"
    )
    for e, spec in zip(ready, expected_units):
        if isinstance(spec, str):
            assert event_field(e, "display_text") == spec, (
                f"unit text mismatch: {event_field(e, 'display_text')!r} != {spec!r}"
            )
            continue
        if "text" in spec:
            assert event_field(e, "display_text") == spec["text"], (
                f"unit text mismatch: {event_field(e, 'display_text')!r} != {spec['text']!r}"
            )
        if "emotion" in spec:
            assert event_field(e, "emotion") == spec["emotion"], (
                f"unit emotion mismatch: {event_field(e, 'emotion')!r} != {spec['emotion']!r}"
            )


def assert_telemetry_present(
    events: Iterable[Any],
    kinds: Iterable[str],
    *,
    present: bool = True,
) -> None:
    """Assert telemetry kinds appear (or never appear). No order/count checks.

    Each entry in ``kinds`` is either a bare event kind (e.g. ``"unit_text_ready"``)
    or ``"status:<state>"`` to require a ``status`` event with that sub-state
    (e.g. ``"status:tools"``). Presence only -- never how many or in what order.
    """
    events = list(events)
    seen = {event_kind(e) for e in events}
    seen_status = status_states(events)
    for spec in kinds:
        if spec.startswith("status:"):
            sub = spec.split(":", 1)[1]
            ok = sub in seen_status
        else:
            ok = spec in seen
        if present:
            assert ok, (
                f"expected telemetry {spec!r}; "
                f"kinds={sorted(seen)} status={sorted(s for s in seen_status if s)}"
            )
        else:
            assert not ok, f"telemetry {spec!r} must not appear"
