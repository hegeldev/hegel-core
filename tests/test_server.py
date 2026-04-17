"""Tests for server.py uncovered paths."""

import socket
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


def test_filter_too_much_detected(client):
    """Test that always calls assume(False) triggers filter_too_much health check."""

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


def test_data_too_large_detected(client):
    """Generating too much data per test case triggers test_cases_too_large.

    We suppress large_initial_test_case so the zero-input check passes,
    then the health check period fires test_cases_too_large when inputs
    repeatedly overrun the entropy budget.
    """

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


def test_large_base_example_detected(client):
    """A test whose simplest input is very large triggers large_initial_test_case."""

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


def test_flaky_message_for_non_strategy_flaky():
    """Test that _flaky_message returns the test result message for
    non-FlakyStrategyDefinition errors like FlakyReplay."""
    from hypothesis.errors import FlakyReplay

    from hegel.server import FLAKY_TEST_RESULT_MSG, _flaky_message

    assert _flaky_message(FlakyReplay("test")) == FLAKY_TEST_RESULT_MSG


def test_server_does_not_crash_when_writer_closed_during_flaky(monkeypatch, capfd):
    """Regression test for hegel-rust#191.

    Exercises the race where a FlakyStrategyDefinition is raised while
    the connection has already been torn down (e.g. the stdio reader
    thread noticed EOF and closed the transport). Before the fix the
    server's attempt to send test_done would hit a ValueError from the
    closed writer, which was not caught and would propagate out of the
    main loop as an uncaught exception with a noisy traceback.
    """
    import contextlib

    import cbor2
    from hypothesis.errors import FlakyStrategyDefinition as FSD

    import hegel.server

    # Make the third generate raise FSD, matching the shape of the real
    # failure in hegel-rust's CI.
    call_count = [0]
    original_from_schema = hegel.server.from_schema

    def raising_from_schema(schema):
        call_count[0] += 1
        strategy = original_from_schema(schema)
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

    # When the server tries to deliver test_done, pretend the underlying
    # transport has already been torn down — BrokenPipeError is what a real
    # closed socket / stdio transport raises once our transport layer has
    # normalised the error.
    original_write_packet = Connection.write_packet

    def failing_write_packet(self, packet):
        if packet.payload:
            try:
                payload = cbor2.loads(packet.payload)
            except Exception:
                payload = None
            if isinstance(payload, dict) and payload.get("event") == "test_done":
                raise BrokenPipeError("broken pipe")
        return original_write_packet(self, packet)

    monkeypatch.setattr(Connection, "write_packet", failing_write_packet)

    server_socket, client_socket = socket.socketpair()
    server_exception: list[BaseException] = []

    def run_server():
        try:
            run_server_on_connection(Connection(server_socket, name="Server"))
        except BaseException as e:
            server_exception.append(e)

    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()

    client_conn = ClientConnection(client_socket)
    client = Client(client_conn)

    def test():
        generate_from_schema({"type": "integer", "min_value": 0, "max_value": 100})

    # The client will never receive test_done (since the patched write_packet
    # drops it), so we run it on a thread with a short timeout and then drop
    # the connection — mirroring a client process that has crashed or exited.
    def run_client():
        with contextlib.suppress(Exception):
            client.run_test(test, test_cases=10)

    client_thread = Thread(target=run_client, daemon=True)
    client_thread.start()
    client_thread.join(timeout=3)

    client_conn.close()
    with contextlib.suppress(OSError):
        client_socket.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        client_socket.close()

    server_thread.join(timeout=5)
    assert not server_thread.is_alive()

    assert server_exception == []
    captured = capfd.readouterr()
    assert "Traceback" not in captured.err
    assert "broken pipe" not in captured.err


def test_stdio_transport_translates_closed_writer_to_broken_pipe():
    """Writes to a closed stdio writer raise ValueError, but the rest of the
    server treats disconnection as (ConnectionError, ProtocolError). The
    transport normalises that to BrokenPipeError so it flows through the
    existing connection-error handling.
    """
    import io

    from hegel.__main__ import StdioTransport

    reader = io.BytesIO(b"")
    writer = io.BytesIO()
    transport = StdioTransport(reader, writer)
    writer.close()

    with pytest.raises(BrokenPipeError):
        transport.sendall(b"some data")


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

    lines = [json.loads(line) for line in metrics_file.read_text().splitlines() if line]
    assert len(lines) >= 5
    for entry in lines:
        assert entry["generate_call_count"] == 2
