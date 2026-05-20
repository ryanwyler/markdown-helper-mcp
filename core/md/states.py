"""Section state machine: schema-aware (v2).

Replaces the v1 fixed enum with agent-declared schemas.

THREE LAYERS
============

1. PSEUDO-STATES (tool-managed, schema-independent):
     STATE_LOADED   = "loaded"    - sections ingested from disk on
                                    fresh open. Writing transitions
                                    to a declared state. No force.
     STATE_READONLY = "readonly"  - foreign-doc sections. All writes
                                    refuse regardless of force.

2. DECLARED SCHEMA (agent-declared at create/open time):
     A list of declared states with optional role flags:
       initial         - new sections start here
       working         - sub-agent dispatch lands here
       terminal        - force required to overwrite
       needsAttention  - shows in nextFocus / review-reject default

     Bare-string shorthand: positional inference applies.
       states=["todo","doing","done"]
         -> first becomes initial, last becomes terminal,
            middle is a working state.

3. FORCE GATE:
     needs_force(state, schema) -> True iff `state` is a declared
     terminal state. Pseudo-states never need force (loaded
     transitions out, readonly always refuses).

Default schema: [pending, done].
  pending  : initial
  done     : terminal

Schema persists in the workspace `meta` table as JSON; survives save
and reopen-with-workspace. Reopen-without-workspace (e.g. closed and
reopened later) falls back to the default -- state is workflow
metadata, not document metadata.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Pseudo-states (tool-managed, outside any declared schema)
# ---------------------------------------------------------------------------

STATE_LOADED = "loaded"
STATE_READONLY = "readonly"

PSEUDO_STATES = (STATE_LOADED, STATE_READONLY)


# ---------------------------------------------------------------------------
# Declared schema
# ---------------------------------------------------------------------------

DEFAULT_INITIAL = "pending"
DEFAULT_TERMINAL = "done"
DEFAULT_STATES = (DEFAULT_INITIAL, DEFAULT_TERMINAL)


@dataclass(frozen=True)
class StateDef:
    """A single declared state in a schema.

    Role flags. None of these are required; positional inference fills
    them in when the agent passes a bare-string list.

      initial: True for the state new sections enter at create-time
        and that loaded sections transition to on first write (when no
        explicit state is provided). Exactly one initial state per
        schema.
      working: True for "sub-agent is processing" states. Default
        target of markdown_dispatch's resultState.
      terminal: True for "accepted, finished" states. Force required
        to overwrite. Multiple terminals are allowed (rare).
      needsAttention: True for "needs the writer's eye again". Maps
        to review-reject and shows in nextFocus.
    """
    name: str
    initial: bool = False
    working: bool = False
    terminal: bool = False
    needsAttention: bool = False


@dataclass(frozen=True)
class Schema:
    """The state schema declared for a doc.

    Use Schema.from_param() to build from agent input.
    Use Schema.default() for the default [pending, done] schema.
    """
    states: tuple[StateDef, ...] = field(default_factory=tuple)

    @classmethod
    def default(cls) -> "Schema":
        return cls(states=(
            StateDef(name=DEFAULT_INITIAL, initial=True),
            StateDef(name=DEFAULT_TERMINAL, terminal=True),
        ))

    @classmethod
    def from_param(cls, raw: Any) -> "Schema":
        """Build a Schema from the agent's `states` param value.

        Accepts:
          None or empty list  -> default schema
          ["a","b","c"]       -> bare-string list, positional inference
          [{"name":"a"}, ...] -> object form, explicit role flags

        Mixed forms also work (each item is independently a string or
        an object).

        Raises ValueError on:
          - duplicate names
          - missing `name` on an object entry
          - reserved pseudo-state names (loaded, readonly)
          - more than one initial
          - zero terminals after positional inference
        """
        if raw is None:
            return cls.default()
        if isinstance(raw, str):
            raise ValueError(
                "states must be a list (e.g. ['pending','done']), not a string"
            )
        if not isinstance(raw, (list, tuple)):
            raise ValueError("states must be a list of names or objects")
        if len(raw) == 0:
            return cls.default()

        # Normalize each entry to a partially-filled dict.
        partials: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for i, item in enumerate(raw):
            if isinstance(item, str):
                name = item
                flags: dict[str, bool] = {}
            elif isinstance(item, dict):
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"states[{i}]: object form requires 'name' (string)"
                    )
                flags = {
                    k: bool(item.get(k, False))
                    for k in ("initial", "working", "terminal", "needsAttention")
                }
            else:
                raise ValueError(
                    f"states[{i}]: expected string or object, got {type(item).__name__}"
                )
            if name in PSEUDO_STATES:
                raise ValueError(
                    f"states[{i}]: name {name!r} is reserved (pseudo-state)"
                )
            if name in seen_names:
                raise ValueError(f"states[{i}]: duplicate name {name!r}")
            seen_names.add(name)
            partials.append({"name": name, **flags})

        # Positional inference: if NO state has initial=True, the first
        # entry becomes initial. If NO state has terminal=True, the
        # last entry becomes terminal. Track whether the flags were
        # set EXPLICITLY by the agent vs by inference, so we can
        # reject single-state schemas formed by accidental coincidence
        # while still allowing them when the agent meant it.
        any_initial_explicit = any(p.get("initial") for p in partials)
        any_terminal_explicit = any(p.get("terminal") for p in partials)
        if not any_initial_explicit:
            partials[0]["initial"] = True
        if not any_terminal_explicit:
            partials[-1]["terminal"] = True

        # Validate: at most one initial, at least one terminal.
        initials = [p["name"] for p in partials if p.get("initial")]
        terminals = [p["name"] for p in partials if p.get("terminal")]
        if len(initials) > 1:
            raise ValueError(
                f"schema may have at most one initial state; got: {initials}"
            )
        if not terminals:
            raise ValueError("schema must have at least one terminal state")

        # Reject single-state schemas formed by positional defaulting.
        # The single state ends up both initial and terminal, so every
        # fresh section starts in a force-required state and the first
        # write traps. If the agent explicitly set both flags on the
        # same state they had to type it, so we trust them.
        if (
            len(partials) == 1
            and not any_initial_explicit
            and not any_terminal_explicit
        ):
            raise ValueError(
                "single-state schemas trap the agent: the only state "
                "is both initial and terminal, so the first write "
                "requires force. Declare at least two states (e.g. "
                "['draft','done']), or pass an object with explicit "
                "role flags if you really want one state."
            )

        return cls(states=tuple(
            StateDef(
                name=p["name"],
                initial=p.get("initial", False),
                working=p.get("working", False),
                terminal=p.get("terminal", False),
                needsAttention=p.get("needsAttention", False),
            )
            for p in partials
        ))

    # -- lookups -------------------------------------------------------

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.states)

    def get(self, name: str) -> StateDef | None:
        for s in self.states:
            if s.name == name:
                return s
        return None

    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.states)

    def initial_state(self) -> str:
        for s in self.states:
            if s.initial:
                return s.name
        # All schemas built via from_param() have an initial; this
        # fallback only applies if a Schema was constructed directly
        # without one.
        return self.states[0].name if self.states else DEFAULT_INITIAL

    def terminal_state(self) -> str | None:
        """First terminal state in declaration order. Used as the
        `accept` shortcut target."""
        for s in self.states:
            if s.terminal:
                return s.name
        return None

    def working_state(self) -> str | None:
        for s in self.states:
            if s.working:
                return s.name
        return None

    def needs_attention_state(self) -> str | None:
        for s in self.states:
            if s.needsAttention:
                return s.name
        return None

    # -- serialization --------------------------------------------------

    def to_response(self) -> list[dict[str, Any]]:
        """The agent-facing schema shape. List of objects, one per
        state, with role flags ONLY when set (compact). Pass-back
        compatible: this is the same shape the agent passes to
        markdown_open's `states` param for the object form.

        Example for [pending, in-progress, blocked, done, archived]
        declared via positional inference:

            [
              {"name": "pending",     "initial":  true},
              {"name": "in-progress"},
              {"name": "blocked"},
              {"name": "done"},
              {"name": "archived",    "terminal": true},
            ]

        Use this in any response that wants to show the schema to the
        agent so they can see WHICH state is terminal, working, etc.
        Knowing role flags matters: terminal states need force=true
        to overwrite, working states are where dispatch lands, etc.
        """
        out: list[dict[str, Any]] = []
        for s in self.states:
            entry: dict[str, Any] = {"name": s.name}
            if s.initial:
                entry["initial"] = True
            if s.working:
                entry["working"] = True
            if s.terminal:
                entry["terminal"] = True
            if s.needsAttention:
                entry["needsAttention"] = True
            out.append(entry)
        return out

    def to_json(self) -> str:
        return json.dumps([
            {
                "name": s.name,
                "initial": s.initial,
                "working": s.working,
                "terminal": s.terminal,
                "needsAttention": s.needsAttention,
            }
            for s in self.states
        ], separators=(",", ":"))

    @classmethod
    def from_json(cls, blob: str | None) -> "Schema":
        if not blob:
            return cls.default()
        try:
            arr = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return cls.default()
        if not isinstance(arr, list) or not arr:
            return cls.default()
        try:
            return cls(states=tuple(
                StateDef(
                    name=item["name"],
                    initial=bool(item.get("initial", False)),
                    working=bool(item.get("working", False)),
                    terminal=bool(item.get("terminal", False)),
                    needsAttention=bool(item.get("needsAttention", False)),
                )
                for item in arr
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ))
        except (KeyError, TypeError):
            return cls.default()


# ---------------------------------------------------------------------------
# Force gate + state validation
# ---------------------------------------------------------------------------

def needs_force(current_state: str, schema: Schema) -> bool:
    """Return True iff overwriting a section in `current_state` requires
    force=true.

    True only for declared terminal states. Pseudo-states never need
    force (loaded transitions out on write; readonly always refuses).
    Non-terminal declared states accept writes freely.
    """
    if current_state in PSEUDO_STATES:
        # readonly is refused upstream; loaded is freely writable.
        return False
    sd = schema.get(current_state)
    return sd is not None and sd.terminal


def validate_state(schema: Schema, name: str) -> None:
    """Raise ValueError with a descriptive message if `name` is not a
    declared state in `schema`. Pseudo-states are NOT valid (agents
    can't transition INTO a pseudo-state).
    """
    if name in PSEUDO_STATES:
        raise ValueError(
            f"state {name!r} is a tool-managed pseudo-state and cannot be "
            f"set directly. Valid states: {list(schema.names())}"
        )
    if not schema.has(name):
        raise ValueError(
            f"state {name!r} is not in this doc's schema. "
            f"Valid states: {list(schema.names())}"
        )


def is_terminal(state: str, schema: Schema) -> bool:
    """True iff state is a declared terminal state."""
    if state in PSEUDO_STATES:
        return False
    sd = schema.get(state)
    return sd is not None and sd.terminal


def auto_transition(current_state: str, schema: Schema) -> str:
    """Default target state for a write when the agent didn't pass an
    explicit `state` param.

    - From `loaded`: transition to the schema's initial state (the
      agent is taking ownership of a disk-loaded section).
    - From a declared state: stay where we are. The write updates
      content but doesn't reshape workflow. Agent passes `state`
      explicitly when they want movement.
    - From `readonly`: not callable -- writes refuse upstream.
    """
    if current_state == STATE_LOADED:
        return schema.initial_state()
    return current_state


# ---------------------------------------------------------------------------
# Counts helper
# ---------------------------------------------------------------------------

def collect_state_counts(states: Iterable[str]) -> dict[str, int]:
    """Tally a flat iterable of state names into a {name: count} map.
    Sorted by name for stable output.
    """
    counts: dict[str, int] = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "STATE_LOADED",
    "STATE_READONLY",
    "PSEUDO_STATES",
    "DEFAULT_INITIAL",
    "DEFAULT_TERMINAL",
    "DEFAULT_STATES",
    "StateDef",
    "Schema",
    "needs_force",
    "validate_state",
    "is_terminal",
    "auto_transition",
    "collect_state_counts",
]
