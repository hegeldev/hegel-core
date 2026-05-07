"""Tests for the observability emission added in server.py.

These tests register observability callbacks via
``with_observability_callback(..., all_threads=True)`` because the hegel
server runs in a daemon thread (and dispatches ``_run_test`` onto a
``ThreadPoolExecutor`` worker), so observations are delivered from a
different thread than the one that registers the callback.

A few of the emission sites — the per-case observation in
``execute_once_for_engine`` after the client raises in is_final mode, the
"minimal failing example" replay, and the "explicit example" record after
a single_test_case failure — are delivered after the client has already
returned. ``wait_for`` polls the captured list to bridge that gap. The
``cutoff`` filter in ``capture_observations`` discards observations from
*previous* runs that arrive late (their ``HegelState.run_start`` predates
the cutoff), so a slow MFE delivery from one ``client.run_test`` call
doesn't leak into the next call's capture.
"""

import contextlib
import time
from collections.abc import Callable

import pytest
from hypothesis.internal.observability import (
    InfoObservation,
    Observation,
    TestCaseObservation,
    observability_enabled,
    with_observability_callback,
)

from tests.client import assume, generate_from_schema


@contextlib.contextmanager
def capture_observations():
    """Collect every observation delivered while the block is active.

    Observations whose ``run_start`` predates the block entry are filtered
    out. The server delivers some observations *after* the client returns,
    so a slow delivery from a previous run can race into the next capture
    block — without this filter, those late observations would be
    misattributed to the new run.
    """
    collected: list[Observation] = []
    cutoff = time.time()

    def callback(observation: Observation, _thread_id: int) -> None:
        if observation.run_start >= cutoff:
            collected.append(observation)

    with with_observability_callback(callback, all_threads=True):  # type: ignore[arg-type]
        yield collected


def wait_for(condition: Callable[[], object], *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError(f"Condition not met within {timeout}s")


def _testcases(obs):
    return [o for o in obs if isinstance(o, TestCaseObservation)]


def _infos(obs):
    return [o for o in obs if isinstance(o, InfoObservation)]


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_observability_disabled_by_default():
    # No callback registered anywhere — observability_enabled() is False.
    assert not observability_enabled()


def test_per_thread_callback_does_not_receive_server_observations(client):
    """The server runs ``_run_test`` on a thread-pool worker. A per-thread
    callback registered on the test thread is never invoked for those
    observations — hegel relies on consumers using ``all_threads=True``
    (or the file-output callback that hypothesis registers for them when
    ``HYPOTHESIS_EXPERIMENTAL_OBSERVABILITY`` is set, which is itself
    ``all_threads=True``)."""
    collected: list[Observation] = []

    def cb(observation):
        collected.append(observation)

    def test():
        generate_from_schema({"type": "integer"})

    with with_observability_callback(cb):  # all_threads=False
        assert observability_enabled()
        client.run_test(test)

    assert collected == []


def test_passing_run_test(client, monkeypatch):
    # This test asserts default-backend behavior (no "using backend" suffix in
    # how_generated). The antithesis CI run sets ANTITHESIS_OUTPUT_DIR for the
    # whole suite, which switches the runner to hypothesis-urandom and adds
    # the suffix; clear it here so the default-backend assertions hold.
    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 10_000})
        generate_from_schema({"type": "boolean"})

    with capture_observations() as obs:
        client.run_test(test)

    cases = _testcases(obs)
    infos = _infos(obs)

    expected_cases = client.last_result["test_cases"]
    assert expected_cases == 100
    assert len(cases) == expected_cases

    # Exactly one info record (Hegel Statistics).
    assert len(infos) == 1
    info = infos[0]
    assert info.type == "info"
    assert info.title == "Hegel Statistics"
    assert info.property == "<unknown>"
    assert isinstance(info.content, str)
    # describe_statistics produces a per-phase summary.
    assert "generate phase" in info.content

    # All observations from one _run_test invocation share a single
    # HegelState.run_start. (No MFE here, so no second final_state.)
    assert len({o.run_start for o in obs}) == 1

    # Per-case shape & static fields.
    for c in cases:
        assert c.type == "test_case"
        assert c.property == "<unknown>"
        assert c.representation == "<unknown>"
        # No MFE / explicit-example records on a passing run.
        assert c.how_generated.startswith("during ")
        assert "phase" in c.how_generated
        # Default backend → no backend suffix.
        assert "using backend" not in c.how_generated
        # Phase metadata matches the how_generated string.
        assert c.metadata.phase is not None
        assert c.metadata.phase in c.how_generated
        # data_status is always populated.
        assert c.metadata.data_status is not None
        # OBSERVABILITY_CHOICES defaults off.
        assert c.metadata.choice_nodes is None
        assert c.metadata.choice_spans is None
        # timing always carries the wall-clock "overall" key.
        assert "overall" in c.timing
        assert c.timing["overall"] >= 0

    # At least one observation should describe the generate phase.
    assert any("generate phase" in c.how_generated for c in cases)

    # At least one passing case captured both per-draw timings.
    passed_cases = [c for c in cases if c.status == "passed"]
    assert any(
        "generate:unlabeled_0" in c.timing and "generate:unlabeled_1" in c.timing
        for c in passed_cases
    )


def test_failing_run_test(client):
    def test():
        x = generate_from_schema(
            {"type": "integer", "min_value": 0, "max_value": 1000},
        )
        assert x <= 10

    with capture_observations() as obs:
        with pytest.raises(AssertionError):
            client.run_test(test)
        # The MFE observation is delivered after the client has already
        # raised, so we have to wait for it.
        wait_for(
            lambda: any(
                c.how_generated == "minimal failing example" for c in _testcases(obs)
            )
        )

    cases = _testcases(obs)
    mfes = [c for c in cases if c.how_generated == "minimal failing example"]
    failed = [c for c in cases if c.status == "failed"]

    # Exactly one MFE for a single distinct failure.
    assert len(mfes) == 1
    assert mfes[0].status == "failed"
    # All observations from one client.run_test invocation — including the
    # MFE replay — share a single run_start, so consumers can group them
    # as one logical run.
    assert len({o.run_start for o in obs}) == 1
    # status_reason is the InterestingOrigin string the client supplied —
    # for hegel that string is built as "<ExceptionType> at <file>:<line>".
    assert "AssertionError" in mfes[0].status_reason

    # At least the original failing case + the MFE replay are "failed".
    assert len(failed) >= 2

    # status_reason on a non-MFE failed case is also derived from the
    # client-supplied origin (via data.interesting_origin), so AssertionError
    # appears there too.
    non_mfe_failed = [c for c in failed if c.how_generated != "minimal failing example"]
    assert non_mfe_failed
    assert any("AssertionError" in c.status_reason for c in non_mfe_failed)


def test_single_test_case_passing(client):
    def test():
        generate_from_schema({"type": "integer"})

    with capture_observations() as obs:
        client.single_test_case(test)

    cases = _testcases(obs)
    assert len(cases) == 1
    case = cases[0]
    assert case.how_generated == "explicit example"
    assert case.status == "passed"
    # No info record from single_test_case (didn't drive the runner).
    assert _infos(obs) == []


def test_single_test_case_failing(client):
    def test():
        raise AssertionError("boom")

    with capture_observations() as obs:
        with pytest.raises(AssertionError):
            client.single_test_case(test)
        # Observation is delivered after the client has raised.
        wait_for(lambda: _testcases(obs))

    cases = _testcases(obs)
    assert len(cases) == 1
    assert cases[0].how_generated == "explicit example"
    assert cases[0].status == "failed"
    assert _infos(obs) == []


def test_single_test_case_invalid_gave_up(client):
    def test():
        assume(False)

    with capture_observations() as obs:
        client.single_test_case(test)

    cases = _testcases(obs)
    assert len(cases) == 1
    assert cases[0].how_generated == "explicit example"
    assert cases[0].status == "gave_up"


# ---------------------------------------------------------------------------
# failure_blob — replays the blob (1 explicit example) and, if it
# reproduces, also runs the MFE replay (1 minimal failing example).
# ---------------------------------------------------------------------------


def test_failure_blob_reproducing(client):
    def test():
        x = generate_from_schema(
            {"type": "integer", "min_value": 0, "max_value": 1000},
        )
        assert x <= 10

    with pytest.raises(AssertionError):
        client.run_test(test)
    blob = client.last_result["failure_blobs"][0]

    with capture_observations() as obs:
        with pytest.raises(AssertionError):
            client.run_test(test, failure_blob=blob)
        wait_for(
            lambda: any(
                c.how_generated == "minimal failing example" for c in _testcases(obs)
            )
        )

    cases = _testcases(obs)
    explicit = [c for c in cases if c.how_generated == "explicit example"]
    mfe = [c for c in cases if c.how_generated == "minimal failing example"]

    assert len(explicit) == 1
    assert explicit[0].status == "failed"
    assert len(mfe) == 1
    assert mfe[0].status == "failed"
    # failure_blob path explicitly suppresses the Hegel Statistics info
    # record (didn't drive the runner).
    assert _infos(obs) == []
    # The blob replay and the subsequent MFE replay share a single
    # run_start.
    assert len({o.run_start for o in obs}) == 1


# ---------------------------------------------------------------------------
# Mixed status mapping (passed + gave_up in a single run).
# ---------------------------------------------------------------------------


def test_status_mapping_passed_and_gave_up(client):
    def test():
        x = generate_from_schema(
            {"type": "integer", "min_value": 0, "max_value": 100},
        )
        assume(x % 2 == 0)

    with capture_observations() as obs:
        client.run_test(test)

    statuses = {c.status for c in _testcases(obs)}
    # Even values pass the assume; odd values are rejected (gave_up).
    assert "passed" in statuses
    assert "gave_up" in statuses


# ---------------------------------------------------------------------------
# Backend suffix in how_generated under the hypothesis-urandom backend.
# ---------------------------------------------------------------------------


def test_how_generated_includes_backend_under_antithesis(client, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTITHESIS_OUTPUT_DIR", str(tmp_path))

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 10_000})

    with capture_observations() as obs:
        client.run_test(test)

    # Backend suffix appears only when the runner is actually using the
    # alternative backend (i.e. _switch_to_hypothesis_provider is False).
    assert any(
        "using backend='hypothesis-urandom'" in c.how_generated for c in _testcases(obs)
    )
