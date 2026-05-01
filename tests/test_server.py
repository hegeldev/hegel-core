"""Tests for server.py uncovered paths."""

import socket
import sys
import time
from threading import Thread

import pytest
from hypothesis import strategies as st
from hypothesis.errors import UnsatisfiedAssumption

from hegel.protocol import Connection, RequestError
from hegel.schema import FROM_SCHEMA_CACHE, from_schema
from hegel.server import run_server_on_connection
from tests.client import (
    Client,
    ClientConnection,
    FlakyTest,
    HealthCheckFailure,
    assume,
    collection,
    generate_from_schema,
    start_span,
    stop_span,
    target,
)
from tests.client.client import _request

try:
    ExceptionGroup
except NameError:  # pragma: no cover
    from exceptiongroup import ExceptionGroup


def test_start_and_stop_span(client):
    def test():
        start_span(1)
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 10})
        stop_span()

    client.run_test(test, test_cases=10)


def test_stop_span_with_discard(client):
    def test():
        start_span(1)
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 10})
        stop_span(discard=True)

    client.run_test(test, test_cases=10)


def test_unknown_command(client):
    with pytest.raises(ConnectionError):
        client._control.send_request({"command": "bogus"})


def test_unknown_command_on_data_stream(client):
    """Unknown command on data stream raises RequestError via handle_requests."""

    def test():
        with pytest.raises(RequestError, match="Unknown command"):
            _request({"command": "bogus_data_command"})

    client.run_test(test, test_cases=1)


def test_mark_complete_with_overrun_status_raises(client):
    """Sending mark_complete with OVERRUN status is rejected by the server."""

    def test():
        generate_from_schema({"type": "integer"})
        with pytest.raises(RequestError, match="Unexpected mark_complete status"):
            _request({"command": "mark_complete", "status": "OVERRUN", "origin": None})

    client.run_test(test, test_cases=1)


def test_cache_eviction():
    # Fill the cache beyond its max size
    for i in range(FROM_SCHEMA_CACHE.max_size + 10):
        schema = {"type": "integer", "min_value": i, "max_value": i + 100}
        from_schema(schema)

    assert len(FROM_SCHEMA_CACHE) <= FROM_SCHEMA_CACHE.max_size


def test_collection_with_no_max_size(client):
    def test():
        c = collection(min_size=1)
        result = []
        while c.more():
            val = generate_from_schema(
                {"type": "integer", "min_value": 0, "max_value": 100},
            )
            result.append(val)
        assert len(result) >= 1

    client.run_test(test, test_cases=10)


def test_collection_reject_on_server(client):
    """Test collection_reject command is handled by the server.

    Tests that the server handles the collection_reject command by calling
    collection.reject() with the provided reason.
    """

    def test():
        # Explicitly use collection.reject() to trigger the server-side
        # collection_reject handler.
        c = collection(min_size=1, max_size=5)
        result = []
        while c.more():
            val = generate_from_schema(
                {"type": "integer", "min_value": 0, "max_value": 100},
            )
            if val % 2 != 0:
                c.reject()
            else:
                result.append(val)

    client.run_test(test, test_cases=20)


def test_unsatisfied_assumption_in_handler(client, monkeypatch):
    class AlwaysRejectStrategy(st.SearchStrategy):
        def do_draw(self, data):
            raise UnsatisfiedAssumption

    reject = AlwaysRejectStrategy()
    monkeypatch.setattr("hegel.server.from_schema", lambda _: reject)

    def test():
        generate_from_schema({"type": "integer"})

    client.run_test(test, test_cases=10)


def test_future_cancel_on_connection_error(monkeypatch):
    """Test that pending futures with ConnectionError are cancelled.

    Tests the except (ConnectionError, TimeoutError): f.cancel() branch
    in run_server_on_connection's cleanup. We patch _run_one to raise
    ConnectionError, ensuring f.result() deterministically hits that path.
    """
    server_socket, client_socket = socket.socketpair()

    def raise_connection_error(*args, **kwargs):
        raise ConnectionError("test disconnect")

    monkeypatch.setattr("hegel.server._run_test", raise_connection_error)
    thread = Thread(
        target=run_server_on_connection,
        args=(Connection(server_socket),),
        daemon=True,
    )
    thread.start()

    with ClientConnection(client_socket) as client_connection:
        client = Client(client_connection)
        stream = client_connection.new_stream()
        client._control.send_request(
            {
                "command": "run_test",
                "stream_id": stream.stream_id,
                "test_cases": 100,
            },
        )

    thread.join(timeout=10)


def test_exception_in_run_one_is_printed_and_reraised(monkeypatch):
    """Tests the except Exception handler in _run_one that prints traceback.

    When an unexpected exception occurs inside _run_one (e.g., during
    ConjectureRunner.run()), it's caught, the traceback is printed,
    and the exception is re-raised.
    """
    server_socket, client_socket = socket.socketpair()

    def raise_runtime_error(*args, **kwargs):
        raise RuntimeError("simulated runner failure")

    monkeypatch.setattr("hegel.server.ConjectureRunner.run", raise_runtime_error)
    thread = Thread(
        target=run_server_on_connection,
        args=(Connection(server_socket),),
        daemon=True,
    )
    thread.start()

    with ClientConnection(client_socket) as client_connection:
        client = Client(client_connection)
        stream = client_connection.new_stream()
        client._control.send_request(
            {
                "command": "run_test",
                "stream_id": stream.stream_id,
                "test_cases": 10,
            },
        )

    thread.join(timeout=10)


def test_base_exception_in_server():
    """Test that BaseException in server loop is caught and printed.

    Tests the except BaseException handler in run_server_on_connection's main
    loop, which catches non-ConnectionError exceptions and prints the traceback.
    We let the handshake complete normally, then patch receive_request to raise
    KeyboardInterrupt on the next call (the main loop).
    """
    server_socket, client_socket = socket.socketpair()
    server_conn = Connection(server_socket)

    original_receive = server_conn.control_stream.read_request
    call_count = 0

    def patched_receive(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise KeyboardInterrupt("simulated")
        return original_receive(*args, **kwargs)

    def server():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(server_conn.control_stream, "read_request", patched_receive)
            run_server_on_connection(server_conn)

    thread = Thread(target=server, daemon=True)
    thread.start()

    with ClientConnection(client_socket) as client_conn:
        client_conn.send_handshake()
        time.sleep(0.3)
    thread.join(timeout=5)


def test_passing(client):
    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x >= 0
        assert x <= 100

    client.run_test(test, test_cases=50)


def test_failing(client):
    def test():
        assert (
            generate_from_schema({"type": "integer", "min_value": 0, "max_value": 1000})
            <= 10
        )

    with pytest.raises(AssertionError):
        client.run_test(test, test_cases=100)


def test_assume(client):
    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assume(x % 2 == 0)
        assert x % 2 == 0

    client.run_test(test, test_cases=100)


def test_multiple_tests_on_connection(client):
    def test1():
        x = generate_from_schema({"type": "integer"})
        assert isinstance(x, int)

    def test2():
        s = generate_from_schema({"type": "string", "min_size": 0, "max_size": 10})
        assert isinstance(s, str)

    client.run_test(test1, test_cases=20)
    client.run_test(test2, test_cases=20)


def test_target(client):
    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        target(float(x), "size")

    client.run_test(test, test_cases=50)


def test_pool_basic(client):
    """Tests new_pool, pool_add, pool_generate, and pool_consume commands."""

    def test():
        pool_id = _request({"command": "new_pool"})
        assert isinstance(pool_id, int)

        # Add some variables to the pool
        v1 = _request({"command": "pool_add", "pool_id": pool_id})
        v2 = _request({"command": "pool_add", "pool_id": pool_id})
        assert v1 != v2

        # Generate a variable from the pool
        v = _request({"command": "pool_generate", "pool_id": pool_id})
        assert v in (v1, v2)

        # Consume a variable from the pool
        _request({"command": "pool_consume", "pool_id": pool_id, "variable_id": v1})

    client.run_test(test, test_cases=10)


def test_pool_generate_with_consume(client):
    """Tests pool_generate with consume=True."""

    def test():
        pool_id = _request({"command": "new_pool"})
        _request({"command": "pool_add", "pool_id": pool_id})
        _request({"command": "pool_add", "pool_id": pool_id})

        # Generate and consume in one step
        v = _request({"command": "pool_generate", "pool_id": pool_id, "consume": True})
        assert isinstance(v, int)

    client.run_test(test, test_cases=10)


def test_pool_generate_from_empty_pool(client):
    """Tests that generating from an empty pool marks the test case invalid."""

    def test():
        pool_id = _request({"command": "new_pool"})
        # Pool is empty, generate should cause the test case to be marked invalid
        # which results in UnsatisfiedAssumption -> DataExhausted on the client side
        # or it just gets marked invalid and the server moves on
        _request({"command": "pool_generate", "pool_id": pool_id})

    client.run_test(test, test_cases=10)


def test_reproduce_failure(client):
    def test():
        assert (
            generate_from_schema({"type": "integer", "min_value": 0, "max_value": 1000})
            <= 10
        )

    with pytest.raises(AssertionError):
        client.run_test(test, test_cases=100)

    blob = client.last_result["failure_blobs"][0]
    assert isinstance(blob, bytes)

    with pytest.raises(AssertionError):
        client.run_test(test, failure_blob=blob)


def test_reproduce_failure_blob_no_longer_fails(client):
    """When a blob no longer reproduces, the client raises RuntimeError."""

    def failing_test():
        assert (
            generate_from_schema({"type": "integer", "min_value": 0, "max_value": 1000})
            <= 10
        )

    with pytest.raises(AssertionError):
        client.run_test(failing_test, test_cases=100)

    blob = client.last_result["failure_blobs"][0]

    # The blob was for failing_test, but we replay with a test that always passes.
    with pytest.raises(AssertionError, match="failure blob did not reproduce"):
        client.run_test(lambda: None, failure_blob=blob)


def test_reproduce_failure_result_not_in_passing_test(client):
    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x >= 0

    client.run_test(test, test_cases=50)
    assert client.last_result["failure_blobs"] == []


def test_multiple_blobs(client):
    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x <= 10

        y = generate_from_schema({"type": "integer", "min_value": -10, "max_value": -1})
        assert y >= 0

    with pytest.raises(ExceptionGroup):
        client.run_test(test, test_cases=50)
    assert len(client.last_result["failure_blobs"]) == 2


def test_derandomize_with_database_key(client):
    """Tests that derandomize=True derives seed from database_key."""

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 1000})

    client.run_test(test, test_cases=5, derandomize=True, database_key=b"my_test_key")


def test_derandomize_without_database_key(client):
    """Tests that derandomize=True without database_key uses seed=0."""
    # This just needs to not crash — the branch where database_key is None
    # and derandomize is True should set seed=0.

    def test():
        generate_from_schema({"type": "integer"})

    client.run_test(test, test_cases=5, derandomize=True)


def test_database_none_disables_persistence(client):
    """Tests that database=None nullifies database_key."""

    def test():
        generate_from_schema({"type": "integer"})

    client.run_test(test, test_cases=5, database_key=b"some_key", database=None)


def test_database_set_preserves_database_key(client):
    """Tests that setting database to a path preserves the database_key."""

    def test():
        generate_from_schema({"type": "integer"})

    client.run_test(test, test_cases=5, database=".hegel/examples")


def test_pool_generate_with_mostly_removed_variables(client):
    """Tests the fallback path in Variables.generate when random picks hit removed variables.

    When all 3 random picks land on removed variables, the method falls back to
    using the last variable in the list (lines 82-90 of server.py).
    """

    def test():
        pool_id = _request({"command": "new_pool"})
        # Add many variables
        variables = []
        for _ in range(20):
            v = _request({"command": "pool_add", "pool_id": pool_id})
            variables.append(v)

        # Consume all except the last one. The last one won't be trimmed
        # because it's not at the end of the removed set after consume().
        # Actually, consume trims from the end of the list, so consuming
        # variables that are NOT at the end won't trigger trimming.
        for v in variables[:-1]:
            _request({"command": "pool_consume", "pool_id": pool_id, "variable_id": v})

        # Now generate - with 19/20 variables removed, the 3-attempt loop
        # will very likely fail to find a non-removed variable, triggering
        # the fallback path.
        result = _request({"command": "pool_generate", "pool_id": pool_id})
        assert result == variables[-1]

    client.run_test(test, test_cases=50)


def test_health_check_no_failure_by_default(client):
    """Normal test should not produce a health_check_failure."""

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x >= 0

    # Should complete without raising HealthCheckFailure
    client.run_test(test, test_cases=10)


def test_filter_too_much_detected(client, monkeypatch):
    """Test that always calls assume(False) triggers filter_too_much health check."""
    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assume(False)

    with pytest.raises(HealthCheckFailure, match="filter"):
        client.run_test(test, test_cases=100)


def test_filter_too_much_suppressed(client):
    """Suppressing filter_too_much allows the test to complete normally."""

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assume(False)

    # Should not raise - the health check is suppressed
    client.run_test(test, test_cases=100, suppress_health_check=["filter_too_much"])


def test_bad_health_check_name(client):
    """Sending an invalid health check name reports a clear error."""

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    with pytest.raises(ValueError, match=r"Unknown health check.*'not_a_real_check'"):
        client.run_test(test, test_cases=10, suppress_health_check=["not_a_real_check"])


def test_bad_phase_name(client):
    """Sending an invalid phase name reports a clear error."""

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    with pytest.raises(ValueError, match=r"Unknown phase.*'not_a_real_phase'"):
        client.run_test(test, test_cases=10, phases=["not_a_real_phase"])


def test_phases_restricts_run(client):
    """Passing a phases list is accepted and restricts which phases run."""

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    client.run_test(test, test_cases=10, phases=["generate"])
    assert client.last_result["passed"]


def test_data_too_large_detected(client, monkeypatch):
    """Generating too much data per test case triggers test_cases_too_large.

    We suppress large_initial_test_case so the zero-input check passes,
    then the health check period fires test_cases_too_large when inputs
    repeatedly overrun the entropy budget.
    """
    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)

    def test():
        for _ in range(500):
            generate_from_schema({"type": "string", "min_size": 50, "max_size": 100})

    with pytest.raises(HealthCheckFailure, match="entropy"):
        client.run_test(
            test,
            test_cases=100,
            suppress_health_check=["large_initial_test_case"],
        )


def test_data_too_large_suppressed(client):
    """Suppressing test_cases_too_large allows the test to complete."""

    def test():
        do_big = generate_from_schema({"type": "boolean"})
        if do_big:
            for _ in range(100):
                generate_from_schema({"type": "integer"})

    # Suppress all health checks since pathological inputs can trigger multiple.
    # Use fewer test_cases and lighter data to keep the antithesis backend fast.
    client.run_test(
        test,
        test_cases=15,
        suppress_health_check=[
            "test_cases_too_large",
            "too_slow",
            "large_initial_test_case",
        ],
    )


def _make_time_warp(monkeypatch):
    """Monkeypatch time.perf_counter to jump forward during draws.

    This avoids actually sleeping while still triggering the too_slow
    health check, which measures accumulated draw time.
    """
    import time as time_mod

    real_perf_counter = time_mod.perf_counter
    warp = [0.0]

    def warped_perf_counter():
        return real_perf_counter() + warp[0]

    monkeypatch.setattr(time_mod, "perf_counter", warped_perf_counter)
    return warp


def test_too_slow_detected(client, monkeypatch):
    """Slow draw operations trigger too_slow health check."""
    import hegel.server

    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)
    warp = _make_time_warp(monkeypatch)
    original = hegel.server.from_schema

    def slow_schema(schema):
        strategy = original(schema)

        class SlowStrategy(st.SearchStrategy):
            def do_draw(self, data):
                warp[0] += 5.0  # Each draw appears to take 5 seconds
                return strategy.do_draw(data)

        return SlowStrategy()

    monkeypatch.setattr("hegel.server.from_schema", slow_schema)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    with pytest.raises(HealthCheckFailure, match="slow"):
        client.run_test(test, test_cases=100)


def test_too_slow_suppressed(client, monkeypatch):
    """Suppressing too_slow allows the test to complete."""
    import hegel.server

    warp = _make_time_warp(monkeypatch)
    original = hegel.server.from_schema

    def slow_schema(schema):
        strategy = original(schema)

        class SlowStrategy(st.SearchStrategy):
            def do_draw(self, data):
                warp[0] += 5.0
                return strategy.do_draw(data)

        return SlowStrategy()

    monkeypatch.setattr("hegel.server.from_schema", slow_schema)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    client.run_test(test, test_cases=100, suppress_health_check=["too_slow"])


def test_large_base_example_detected(client, monkeypatch):
    """A test whose simplest input is very large triggers large_initial_test_case."""
    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)

    def test():
        # Generate many large collections - even the simplest input will be large
        for _ in range(50):
            generate_from_schema(
                {
                    "type": "list",
                    "elements": {"type": "integer"},
                    "min_size": 100,
                    "max_size": 100,
                }
            )

    with pytest.raises(HealthCheckFailure, match="smallest natural input"):
        client.run_test(test, test_cases=100)


def test_large_base_example_suppressed(client):
    """Suppressing large_initial_test_case allows the test to complete."""

    def test():
        for _ in range(10):
            generate_from_schema(
                {
                    "type": "list",
                    "elements": {"type": "integer"},
                    "min_size": 50,
                    "max_size": 50,
                }
            )

    # Suppress all health checks since pathological inputs can trigger multiple.
    # Use fewer test_cases and lighter data to keep the antithesis backend fast.
    client.run_test(
        test,
        test_cases=15,
        suppress_health_check=[
            "large_initial_test_case",
            "test_cases_too_large",
            "too_slow",
        ],
    )


def test_antithesis_disables_all_health_checks(client, monkeypatch, tmp_path):
    """Under ANTITHESIS_OUTPUT_DIR, all health checks are suppressed even
    when the client does not request suppression."""
    monkeypatch.setenv("ANTITHESIS_OUTPUT_DIR", str(tmp_path))

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assume(False)

    # Without Antithesis suppression this would raise HealthCheckFailure
    # for filter_too_much.
    client.run_test(test, test_cases=100)


def test_antithesis_disables_database(client, monkeypatch, tmp_path):
    """Under ANTITHESIS_OUTPUT_DIR, the example database is forced off,
    even when the client requests one."""
    from hypothesis import HealthCheck, settings as real_settings

    import hegel.server

    monkeypatch.setenv("ANTITHESIS_OUTPUT_DIR", str(tmp_path))

    captured: dict = {}

    def capture_settings(**kwargs):
        captured.update(kwargs)
        return real_settings(**kwargs)

    monkeypatch.setattr(hegel.server, "settings", capture_settings)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    db_path = tmp_path / "client-requested-db"
    client.run_test(
        test,
        test_cases=5,
        database=str(db_path),
        database_key=b"some_key",
    )

    assert captured["database"] is None
    assert set(captured["suppress_health_check"]) == set(HealthCheck)
    assert not db_path.exists()


def test_flaky_data_generation(client, monkeypatch):
    """Test that FlakyStrategyDefinition during generate is caught and reported.

    Directly raises FlakyStrategyDefinition from inside data.draw() via a
    custom strategy, simulating what happens when the datatree detects
    inconsistent choice types.
    """
    from hypothesis.errors import FlakyStrategyDefinition as FSD

    import hegel.server

    call_count = [0]
    original = hegel.server.from_schema

    def raising_from_schema(schema):
        call_count[0] += 1
        strategy = original(schema)
        if call_count[0] == 3:

            class FlakyStrat(st.SearchStrategy):
                def do_draw(self, data):
                    raise FSD(
                        "Inconsistent data generation! "
                        "Data generation behaved differently between runs."
                    )

            return FlakyStrat()
        return strategy

    monkeypatch.setattr("hegel.server.from_schema", raising_from_schema)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    with pytest.raises(FlakyTest, match="Your data generation is non-deterministic"):
        client.run_test(test, test_cases=10)


def test_flaky_test_results(client):
    """Test that ExitReason.flaky during shrinking is detected and reported.

    Uses a counter to make the test fail on early runs but pass on later
    ones. During shrinking, the runner replays the failing example but the
    test passes, triggering ExitReason.flaky.
    """
    run_count = [0]

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 10})
        run_count[0] += 1
        # Fail on early runs, pass on later ones (including shrinking replay).
        if run_count[0] <= 3:
            raise AssertionError("deliberate flaky failure")

    with pytest.raises(FlakyTest, match="Your test produced different outcomes"):
        client.run_test(test, test_cases=20)


def test_origin_with_error_message_breaks_dedup(client, monkeypatch):
    """If origin includes the error message (which contains the generated value),
    every failure looks distinct. This verifies the server actually treats
    different origin strings as different bugs."""
    import tests.client.client as client_mod

    def bad_extract_origin(exc, tb):
        # BAD: includes str(exc) which contains the generated value
        return f"{type(exc).__name__}: {exc}"

    monkeypatch.setattr(client_mod, "_extract_origin", bad_extract_origin)

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x <= 10, f"Got {x}"

    with pytest.raises((AssertionError, ExceptionGroup)):
        client.run_test(test, test_cases=100)
    assert client.last_result["interesting_test_cases"] > 1


def test_origin_without_error_message_deduplicates(client):
    """With correct origin (exc_type + file:line), all failures from the same
    assertion deduplicate to 1, even when error messages differ."""

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x <= 10, f"Got {x}"

    with pytest.raises(AssertionError):
        client.run_test(test, test_cases=100)
    assert client.last_result["interesting_test_cases"] == 1


def test_origin_with_full_stack_trace_breaks_dedup(client, monkeypatch):
    """If origin includes the full stack trace, the same bug reached via
    different call paths appears as multiple distinct bugs."""
    import traceback as tb_mod

    import tests.client.client as client_mod

    def bad_extract_origin(exc, tb):
        # BAD: includes the full stack trace
        if tb is not None:
            frames = tb_mod.format_tb(tb)
            return f"{type(exc).__name__}\n{''.join(frames)}"
        return f"{type(exc).__name__}"

    monkeypatch.setattr(client_mod, "_extract_origin", bad_extract_origin)

    def buggy(x):
        assert x <= 10

    def path_a(x):
        buggy(x)

    def path_b(x):
        buggy(x)

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        if x % 2 == 0:
            path_a(x)
        else:
            path_b(x)

    with pytest.raises((AssertionError, ExceptionGroup)):
        client.run_test(test, test_cases=100)
    assert client.last_result["interesting_test_cases"] > 1


def test_origin_with_innermost_frame_deduplicates_call_sites(client):
    """With correct origin (innermost frame only), the same bug reached via
    different call paths deduplicates to 1."""

    def buggy(x):
        assert x <= 10

    def path_a(x):
        buggy(x)

    def path_b(x):
        buggy(x)

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        if x % 2 == 0:
            path_a(x)
        else:
            path_b(x)

    with pytest.raises(AssertionError):
        client.run_test(test, test_cases=100)
    assert client.last_result["interesting_test_cases"] == 1


def test_flaky_message_for_non_strategy_flaky():
    """Test that _flaky_message returns the test result message for
    non-FlakyStrategyDefinition errors like FlakyReplay."""
    from hypothesis.errors import FlakyReplay

    from hegel.server import FLAKY_TEST_RESULT_MSG, _flaky_message

    assert _flaky_message(FlakyReplay("test")) == FLAKY_TEST_RESULT_MSG


def test_server_metrics_file(client, monkeypatch, tmp_path):
    """Tests that the server writes per-test-case generate counts when
    CONFORMANCE_SERVER_METRICS_FILE is set."""
    import json

    metrics_file = tmp_path / "server_metrics.jsonl"
    monkeypatch.setenv("CONFORMANCE_SERVER_METRICS_FILE", str(metrics_file))

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        generate_from_schema({"type": "boolean"})

    client.run_test(test, test_cases=5)

    lines = [
        json.loads(line)
        for line in metrics_file.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) >= 5
    for entry in lines:
        assert entry["generate_call_count"] == 2


def test_server_run_metrics_passing(client, monkeypatch, tmp_path):
    """Tests that the server writes interesting_test_cases=0 to the run
    metrics file when all test cases pass."""
    import json

    run_metrics_file = tmp_path / "run_metrics.json"
    monkeypatch.setenv("CONFORMANCE_SERVER_RUN_METRICS_FILE", str(run_metrics_file))

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x >= 0

    client.run_test(test, test_cases=10)

    result = json.loads(run_metrics_file.read_text(encoding="utf-8"))
    assert result["interesting_test_cases"] == 0


def test_server_run_metrics_single_failure(client, monkeypatch, tmp_path):
    """Tests that the server writes interesting_test_cases=1 when all failures
    share the same origin (correct deduplication)."""
    import json

    run_metrics_file = tmp_path / "run_metrics.json"
    monkeypatch.setenv("CONFORMANCE_SERVER_RUN_METRICS_FILE", str(run_metrics_file))

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x <= 10, f"Got {x}"

    with pytest.raises(AssertionError):
        client.run_test(test, test_cases=100)

    result = json.loads(run_metrics_file.read_text(encoding="utf-8"))
    assert result["interesting_test_cases"] == 1


def test_server_run_metrics_multiple_failures(client, monkeypatch, tmp_path):
    """Tests that the server reports multiple distinct failures when origins
    include the error message (breaking deduplication)."""
    import json

    import tests.client.client as client_mod

    run_metrics_file = tmp_path / "run_metrics.json"
    monkeypatch.setenv("CONFORMANCE_SERVER_RUN_METRICS_FILE", str(run_metrics_file))

    def bad_extract_origin(exc, tb):
        return f"{type(exc).__name__}: {exc}"

    monkeypatch.setattr(client_mod, "_extract_origin", bad_extract_origin)

    def test():
        x = generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})
        assert x <= 10, f"Got {x}"

    with pytest.raises((AssertionError, ExceptionGroup)):
        client.run_test(test, test_cases=100)

    result = json.loads(run_metrics_file.read_text(encoding="utf-8"))
    assert result["interesting_test_cases"] > 1


def test_single_test_case_runs_single_passing_case(client):
    call_count = [0]

    def test():
        call_count[0] += 1
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    client.single_test_case(test)

    assert call_count[0] == 1
    assert client.last_result["test_cases"] == 1
    assert client.last_result["valid_test_cases"] == 1
    assert client.last_result["interesting_test_cases"] == 0
    assert client.last_result["passed"] is True


def test_single_test_case_is_final(client):
    """A single_test_case runs in is_final=True mode."""
    from tests.client.client import _is_final

    seen_is_final = []

    def test():
        seen_is_final.append(_is_final.get())

    client.single_test_case(test)
    assert seen_is_final == [True]


def test_single_test_case_invalid_assume(client):
    def test():
        assume(False)

    client.single_test_case(test)

    assert client.last_result["test_cases"] == 1
    assert client.last_result["invalid_test_cases"] == 1
    assert client.last_result["valid_test_cases"] == 0
    assert client.last_result["interesting_test_cases"] == 0
    assert client.last_result["passed"] is True


def test_single_test_case_failing_propagates_exception(client):
    call_count = [0]

    def test():
        call_count[0] += 1
        raise AssertionError("boom")

    with pytest.raises(AssertionError, match="boom"):
        client.single_test_case(test)

    assert call_count[0] == 1


def test_single_test_case_no_shrinking_no_replay(client):
    """single_test_case runs exactly once even when the test fails."""
    call_count = [0]

    def test():
        call_count[0] += 1
        # A test that would normally be shrunk extensively.
        x = generate_from_schema(
            {"type": "integer", "min_value": 0, "max_value": 10**9},
        )
        assert x < 0

    with pytest.raises(AssertionError):
        client.single_test_case(test)

    assert call_count[0] == 1


def test_single_test_case_with_seed_is_deterministic(client, monkeypatch):
    monkeypatch.delenv("ANTITHESIS_OUTPUT_DIR", raising=False)
    seen = []

    def test():
        seen.append(
            generate_from_schema(
                {"type": "integer", "min_value": 0, "max_value": 10**9},
            )
        )

    client.single_test_case(test, seed=12345)
    client.single_test_case(test, seed=12345)

    assert seen[0] == seen[1]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="hypothesis-urandom falls back to the deterministic backend on Windows",
)
def test_single_test_case_with_seed_is_nondeterministic_under_urandom(
    client, monkeypatch, tmp_path
):
    """Under the hypothesis-urandom backend the seed is ignored, so the same
    seed produces different values across runs."""
    monkeypatch.setenv("ANTITHESIS_OUTPUT_DIR", str(tmp_path))
    seen = set()

    def test():
        seen.add(
            generate_from_schema(
                {"type": "integer", "min_value": 0, "max_value": 10**9},
            )
        )

    # Four runs with the same seed. Under urandom, each draw is independent,
    # so the chance of all four producing the same value is ~(10**-9)**3.
    for _ in range(4):
        client.single_test_case(test, seed=12345)

    assert len(seen) > 1


def test_single_test_case_large_entropy_budget(client):
    """A single_test_case can generate far more data than Hypothesis's default
    per-test-case entropy budget would normally allow."""

    def test():
        for _ in range(20_000):
            generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    client.single_test_case(test)
    assert client.last_result["valid_test_cases"] == 1


def test_single_test_case_generation_works(client):
    """A single_test_case can use the full generator surface (lists, spans, etc.)."""

    def test():
        xs = generate_from_schema(
            {
                "type": "list",
                "elements": {"type": "integer", "min_value": 0, "max_value": 100},
                "min_size": 1,
                "max_size": 5,
            }
        )
        assert 1 <= len(xs) <= 5
        for x in xs:
            assert 0 <= x <= 100

    client.single_test_case(test)
