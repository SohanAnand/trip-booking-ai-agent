"""Explicit agent session state machine.

Illegal transitions raise IllegalTransitionError. The orchestrator drives
transitions; the LLM never controls state directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentState(str, Enum):
    PARSING = "parsing"
    SEARCHING = "searching"
    RANKING = "ranking"
    PRESENTING = "presenting"
    AWAITING_SELECTION = "awaiting_selection"
    REVALIDATING = "revalidating"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETE = "complete"
    FAILED = "failed"


# Legal transitions. Anything not in this set raises IllegalTransitionError.
LEGAL_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.PARSING: {AgentState.SEARCHING, AgentState.FAILED},
    AgentState.SEARCHING: {AgentState.RANKING, AgentState.SEARCHING, AgentState.FAILED},
    AgentState.RANKING: {AgentState.PRESENTING, AgentState.FAILED},
    AgentState.PRESENTING: {AgentState.AWAITING_SELECTION, AgentState.FAILED},
    AgentState.AWAITING_SELECTION: {
        AgentState.SEARCHING,
        AgentState.REVALIDATING,
        AgentState.FAILED,
    },
    AgentState.REVALIDATING: {
        AgentState.AWAITING_APPROVAL,
        AgentState.PRESENTING,
        AgentState.FAILED,
    },
    AgentState.AWAITING_APPROVAL: {
        AgentState.EXECUTING,
        AgentState.REVALIDATING,
        AgentState.FAILED,
    },
    AgentState.EXECUTING: {AgentState.COMPLETE, AgentState.FAILED},
    AgentState.COMPLETE: set(),
    AgentState.FAILED: set(),
}


class IllegalTransitionError(Exception):
    pass


@dataclass
class AgentSession:
    request_id: str
    user_id: str
    state: AgentState = AgentState.PARSING
    replan_count: int = 0
    tool_call_count: int = 0
    cost_cents: int = 0
    elapsed_ms: int = 0
    selected_option_id: str | None = None
    booking_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def transition(self, new_state: AgentState) -> None:
        legal = LEGAL_TRANSITIONS.get(self.state, set())
        if new_state not in legal:
            raise IllegalTransitionError(
                f"Cannot transition from {self.state.value} to {new_state.value}; "
                f"legal next states are {sorted(s.value for s in legal)}"
            )
        self.state = new_state

    def is_terminal(self) -> bool:
        return self.state in (AgentState.COMPLETE, AgentState.FAILED)
