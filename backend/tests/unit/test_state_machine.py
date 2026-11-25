"""State machine: every transition, every listed invalid transition, terminal absorption,
and a Hypothesis rule-based machine (>= 500 examples)."""

from __future__ import annotations

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from lbf.domain import state_machine as sm
from lbf.domain.enums import TERMINAL_STATES, OperationState

S = OperationState


def test_table_covers_every_plan_transition_id():
    ids = {t.id for t in sm.TRANSITIONS}
    for tid in sm.PLAN_TRANSITION_IDS:
        assert tid in ids, tid


@pytest.mark.parametrize("transition", sm.TRANSITIONS, ids=lambda t: t.id)
def test_each_transition_is_accepted_from_every_source(transition):
    for source in transition.sources:
        if source is None:
            assert sm.is_valid(None, transition.target)
            continue
        assert sm.require(source, transition.target, transition.id) is transition
        assert sm.is_valid(source, transition.target, transition.actor)


@pytest.mark.parametrize("transition", sm.TRANSITIONS, ids=lambda t: t.id)
def test_each_transition_is_rejected_from_other_sources(transition):
    for source in S:
        if source in transition.sources:
            continue
        with pytest.raises(sm.InvalidTransition):
            sm.require(source, transition.target, transition.id)


@pytest.mark.parametrize(
    ("source", "target"), sm.EXPLICITLY_INVALID, ids=lambda v: v.value if isinstance(v, S) else str(v)
)
def test_st_neg_explicitly_invalid(source, target):
    assert not sm.is_valid(source, target)


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_st_neg_terminal_states_are_absorbing(terminal):
    for target in S:
        assert not sm.is_valid(terminal, target)


def test_st_neg_no_user_transition_into_engine_only_states():
    forbidden = {S.SUBMITTING, S.AWAITING_ACK, S.RECONCILING, S.CONFIRMED}
    user_into = {t.id for t in sm.TRANSITIONS if t.actor == "USER" and t.target in forbidden}
    assert user_into == {"T29"}  # verified resolution backed by a backend read is the only exception


def test_st_neg_redelivery_only_from_unknown_and_system():
    into_submitting = [t for t in sm.TRANSITIONS if t.target is S.SUBMITTING]
    assert {t.id for t in into_submitting} == {"T09", "T27"}
    assert all(t.actor == "SYSTEM" for t in into_submitting)


def test_allowed_state_changes_excludes_self_and_terminal_sources():
    pairs = sm.allowed_state_changes()
    assert all(a != b for a, b in pairs)
    assert not any(a in TERMINAL_STATES for a, _ in pairs)
    assert (S.READY, S.APPROVED) in pairs
    assert (S.UNKNOWN, S.READY) not in pairs


class OperationMachine(RuleBasedStateMachine):
    """Random command sequences never reach an invalid transition; terminal states are absorbing."""

    def __init__(self) -> None:
        super().__init__()
        self.state: S | None = None
        self.history: list[S] = []

    @rule()
    def create(self) -> None:
        if self.state is None:
            self.state = sm.require(None, S.DRAFT, "T01").target
            self.history.append(self.state)

    @precondition(lambda self: self.state is not None)
    @rule(to=st.sampled_from(list(S)), tid=st.sampled_from([t.id for t in sm.TRANSITIONS]))
    def attempt(self, to: S, tid: str) -> None:
        before = self.state
        transition = sm.BY_ID[tid]
        if before in transition.sources and transition.target == to and before not in TERMINAL_STATES:
            self.state = sm.require(before, to, tid).target
            self.history.append(self.state)
        else:
            with pytest.raises(sm.InvalidTransition):
                sm.require(before, to, tid)
            assert self.state == before

    @invariant()
    def terminal_absorbing(self) -> None:
        for i, s in enumerate(self.history):
            if s in TERMINAL_STATES:
                assert all(x == s for x in self.history[i:])

    @invariant()
    def every_step_valid(self) -> None:
        for a, b in zip(self.history, self.history[1:], strict=False):
            assert sm.is_valid(a, b)


OperationMachine.TestCase.settings = settings(max_examples=500, stateful_step_count=30, deadline=None)
TestOperationMachine = OperationMachine.TestCase
