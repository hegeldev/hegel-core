"""Tests for stateful (rule-based) testing and swarm rule selection."""

import pytest

from hegel.protocol import RequestError
from tests.client import assume, run_state_machine
from tests.client.client import _request


def _longest_run(sequence: list[int]) -> int:
    """Length of the longest run of identical consecutive elements."""
    longest = current = 0
    previous = None
    for value in sequence:
        current = current + 1 if value == previous else 1
        previous = value
        longest = max(longest, current)
    return longest


def test_swarm_produces_long_runs_of_one_rule(client):
    """Swarm testing disables a subset of rules per test case, so some test
    cases run the same rule many times in a row.

    With three rules and uniform selection, a run of 20 identical rules is
    vanishingly unlikely ((1/3)**19 per starting point) — only the all-minimal
    test case (every draw 0) produces one. With swarm testing long runs are
    common: whenever feature flags leave a single rule enabled, every step
    picks that survivor. So we assert on the *fraction* of test cases with a
    long run, not merely its existence.
    """
    runs: list[list[int]] = []

    def test():
        sequence: list[int] = []

        def make_rule(index):
            def rule():
                sequence.append(index)

            rule.__name__ = f"rule_{index}"
            return rule

        rules = [make_rule(i) for i in range(3)]
        run_state_machine(rules, steps=25)
        runs.append(sequence)

    client.run_test(test, test_cases=100, seed=0, database=None)

    long_run_count = sum(1 for sequence in runs if _longest_run(sequence) >= 20)
    assert long_run_count >= 10


def test_invariants_run_and_assume_skips_a_step(client):
    """Invariants run up front and after every applied rule; a rule that
    rejects via ``assume`` skips its step without aborting the test case."""

    def test():
        applied: list[str] = []

        def noop():
            applied.append("noop")

        def rejects():
            assume(False)

        def invariant():
            assert all(name == "noop" for name in applied)

        run_state_machine(
            [noop, rejects],
            invariants=[invariant],
            steps=20,
        )

    client.run_test(test, test_cases=30, seed=0, database=None)
    assert client.last_result["passed"]


def test_state_machine_rejects_malformed_rule(client):
    """A rule whose shape does not match the protocol is rejected when the
    state machine is registered, rather than being silently accepted."""

    def test():
        with pytest.raises(RequestError):
            _request(
                {
                    "command": "new_state_machine",
                    "rules": [{"name": "ok"}, {"label": "wrong key"}],
                    "invariants": [],
                }
            )

    client.run_test(test, test_cases=1, database=None)
