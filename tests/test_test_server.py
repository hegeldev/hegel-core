"""Tests for the test server error simulation modes."""

import socket
from threading import Thread

import cbor2
import trio
import trio.socket

from hegel.protocol.connection import Connection
from tests.client import ClientConnection
from tests.test_server_modes import run_test_server


def _create_socket_pair():
    """Create a connected pair of sockets."""
    return socket.socketpair()


def _start_server(server_sock, mode):
    """Start test server in a thread and return the thread."""

    async def _run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(server_sock))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="Server")
            try:
                await run_test_server(conn, mode)
            finally:
                await conn.close()

    t = Thread(target=trio.run, args=(_run,), daemon=True)
    t.start()
    return t


def _start_server_catching(server_sock, mode, errors, catch_types=(ValueError,)):
    """Start test server in a thread, catching specified exceptions into errors."""

    async def _run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(server_sock))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="Server")
            try:
                await run_test_server(conn, mode)
            except BaseException as e:
                if isinstance(e, catch_types):
                    errors.append(e)
                else:
                    raise
            finally:
                await conn.close()

    t = Thread(target=trio.run, args=(_run,), daemon=True)
    t.start()
    return t


def _setup_client(client_sock):
    """Set up client connection and perform handshake."""
    conn = ClientConnection(client_sock)
    conn.send_handshake()
    return conn


def _send_run_test(conn):
    """Send a run_test command and return the test stream."""
    test_stream = conn.new_stream()
    packet = conn.control_stream.write_request(
        cbor2.dumps(
            {
                "command": "run_test",
                "test_cases": 1,
                "stream_id": test_stream.stream_id,
            },
        ),
    )
    conn.control_stream.read_reply(packet.message_id)
    return test_stream


def _receive_test_case(test_stream, conn):
    """Receive a test_case event and return the data stream."""
    packet = test_stream.read_request()
    message = cbor2.loads(packet.payload)
    assert message["event"] == "test_case"
    data_stream = conn.connect_stream(
        message["stream_id"],
    )
    test_stream.write_reply(packet.message_id, None)
    return data_stream, message.get("is_final", False)


def _send_generate(data_stream):
    """Send a generate command and return the response."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "generate", "schema": {"type": "boolean"}}),
    )
    return data_stream.read_reply(packet.message_id)


def _send_generate_expect_error(data_stream):
    """Send a generate command expecting a RequestError."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "generate", "schema": {"type": "boolean"}}),
    )
    raw = cbor2.loads(data_stream.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_start_span(data_stream, label=1):
    """Send a start_span command."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "start_span", "label": label}),
    )
    return data_stream.read_reply(packet.message_id)


def _send_new_collection(data_stream, *, min_size=0, max_size=10):
    """Send a new_collection command and return the collection ID."""
    packet = data_stream.write_request(
        cbor2.dumps(
            {
                "command": "new_collection",
                "min_size": min_size,
                "max_size": max_size,
            },
        ),
    )
    reply = cbor2.loads(data_stream.read_reply(packet.message_id).payload)
    return reply["result"]


def _send_new_collection_expect_error(data_stream, *, min_size=0, max_size=10):
    """Send a new_collection command expecting a StopTest error."""
    packet = data_stream.write_request(
        cbor2.dumps(
            {
                "command": "new_collection",
                "min_size": min_size,
                "max_size": max_size,
            },
        ),
    )
    raw = cbor2.loads(data_stream.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_collection_more_expect_error(data_stream, collection_id):
    """Send a collection_more command expecting a StopTest error."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "collection_more", "collection_id": collection_id}),
    )
    raw = cbor2.loads(data_stream.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_mark_complete(data_stream, *, status="VALID"):
    """Send a mark_complete command."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "mark_complete", "status": status, "origin": None}),
    )
    return data_stream.read_reply(packet.message_id)


def _send_mark_complete_expect_error(data_stream, *, status="VALID"):
    """Send mark_complete expecting a RequestError."""
    packet = data_stream.write_request(
        cbor2.dumps({"command": "mark_complete", "status": status, "origin": None}),
    )
    raw = cbor2.loads(data_stream.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _receive_test_done(test_stream):
    """Receive a test_done event."""
    packet = test_stream.read_request()
    message = cbor2.loads(packet.payload)
    assert message["event"] == "test_done"
    test_stream.write_reply(packet.message_id, None)
    return message["results"]


class TestStopTestOnGenerate:
    def test_server_sends_stop_test_on_second_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            # First test case: normal flow
            data_ch1, _ = _receive_test_case(test_stream, conn)
            _send_generate(data_ch1)
            _send_mark_complete(data_ch1)

            # Second test case: StopTest on generate
            data_ch2, _ = _receive_test_case(test_stream, conn)
            error = _send_generate_expect_error(data_ch2)
            assert error["type"] == "StopTest"

            # Don't send mark_complete — that's the correct behavior
            # Receive test_done
            _receive_test_done(test_stream)

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            # Go through both test cases
            data_ch1, _ = _receive_test_case(test_stream, conn)
            _send_generate(data_ch1)
            _send_mark_complete(data_ch1)

            data_ch2, _ = _receive_test_case(test_stream, conn)
            _send_generate_expect_error(data_ch2)

            results = _receive_test_done(test_stream)
            assert "passed" in results

        server_thread.join(timeout=2.0)


class TestStopTestOnMarkComplete:
    def test_server_sends_stop_test_on_mark_complete(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_mark_complete")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)
            _send_generate(data_stream)

            error = _send_mark_complete_expect_error(data_stream)
            assert error["type"] == "StopTest"

            # Don't send further commands — that's correct behavior
            _receive_test_done(test_stream)

        server_thread.join(timeout=2.0)


class TestErrorResponse:
    def test_server_sends_error_on_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)
            error = _send_generate_expect_error(data_stream)
            assert error["type"] == "RequestError"

            # client should send mark_complete with INTERESTING
            _send_mark_complete(data_stream, status="INTERESTING")

            _receive_test_done(test_stream)

        server_thread.join(timeout=2.0)


class TestEmptyTest:
    def test_server_sends_test_done_immediately(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "empty_test")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            # Should get test_done immediately, no test_case events
            results = _receive_test_done(test_stream)
            assert results["passed"] is True

        server_thread.join(timeout=2.0)


class TestErrorResponseNoMarkComplete:
    def test_server_handles_client_not_sending_mark_complete(self):
        """Test error_response mode when client doesn't send mark_complete."""
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)
            error = _send_generate_expect_error(data_stream)
            assert error["type"] == "RequestError"

            # Don't send mark_complete — close the data stream instead
            # This triggers the TimeoutError/ConnectionError path
            data_stream.close()

            _receive_test_done(test_stream)

        server_thread.join(timeout=5.0)


class TestConnectionErrorHandling:
    def test_server_handles_early_client_disconnect(self):
        """Test server handles client disconnecting mid-protocol."""
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        with _setup_client(s2) as conn:
            _send_run_test(conn)

            # Close immediately without going through test lifecycle

        server_thread.join(timeout=5.0)

    def test_server_handles_connection_error_from_stream(self):
        """Tests the except ConnectionError handler in run_test_server.

        Closing the client connection causes the server's reader loop to detect
        EOF, closing all stream channels and raising ConnectionError in the handler.
        """
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        conn = _setup_client(s2)
        _send_run_test(conn)
        # Close the client connection — server detects EOF and raises ConnectionError.
        conn.close()

        server_thread.join(timeout=5.0)


class TestStopTestOnCollectionMore:
    def test_server_sends_stop_test_on_collection_more(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_collection_more")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)

            # client sends start_span (LIST) + new_collection normally
            _send_start_span(data_stream, label=1)
            collection_id = _send_new_collection(data_stream)
            assert isinstance(collection_id, int)

            # collection_more should get StopTest
            error = _send_collection_more_expect_error(data_stream, collection_id)
            assert error["type"] == "StopTest"

            # Don't send further commands
            _receive_test_done(test_stream)

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_collection_more")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)
            _send_start_span(data_stream, label=1)
            collection_id = _send_new_collection(data_stream)
            _send_collection_more_expect_error(data_stream, collection_id)

            results = _receive_test_done(test_stream)
            assert "passed" in results

        server_thread.join(timeout=2.0)


class TestStopTestOnNewCollection:
    def test_server_sends_stop_test_on_new_collection(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_new_collection")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)

            # client sends start_span (LIST) normally
            _send_start_span(data_stream, label=1)

            # new_collection should get StopTest
            error = _send_new_collection_expect_error(data_stream)
            assert error["type"] == "StopTest"

            # Don't send further commands
            _receive_test_done(test_stream)

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_new_collection")

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)

            data_stream, _ = _receive_test_case(test_stream, conn)
            _send_start_span(data_stream, label=1)
            _send_new_collection_expect_error(data_stream)

            results = _receive_test_done(test_stream)
            assert "passed" in results

        server_thread.join(timeout=2.0)


class TestCrashAfterHandshake:
    def test_crash_after_handshake(self):
        """Server completes handshake then exits with code 1."""
        s1, s2 = _create_socket_pair()
        exit_codes = []

        async def _run():
            stream = trio.SocketStream(trio.socket.from_stdlib_socket(s1))
            async with trio.open_nursery() as nursery:
                conn = Connection(stream, nursery=nursery, name="Server")
                try:
                    await run_test_server(conn, "crash_after_handshake")
                except SystemExit as e:
                    exit_codes.append(e.code)
                finally:
                    await conn.close()

        server_thread = Thread(target=trio.run, args=(_run,), daemon=True)
        server_thread.start()

        with _setup_client(s2):
            pass  # Handshake succeeds; server exits immediately after

        server_thread.join(timeout=2.0)
        assert exit_codes == [1]

    def test_crash_after_handshake_with_stderr(self, capsys):
        """Server writes error to stderr then exits with code 1."""
        s1, s2 = _create_socket_pair()
        exit_codes = []

        async def _run():
            stream = trio.SocketStream(trio.socket.from_stdlib_socket(s1))
            async with trio.open_nursery() as nursery:
                conn = Connection(stream, nursery=nursery, name="Server")
                try:
                    await run_test_server(conn, "crash_after_handshake_with_stderr")
                except SystemExit as e:
                    exit_codes.append(e.code)
                finally:
                    await conn.close()

        server_thread = Thread(target=trio.run, args=(_run,), daemon=True)
        server_thread.start()

        with _setup_client(s2):
            pass

        server_thread.join(timeout=2.0)
        assert exit_codes == [1]


class TestTestServerErrors:
    def test_unknown_mode_raises(self):
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "nonexistent_mode", errors)

        with _setup_client(s2):
            # The server raises ValueError before reading run_test (unknown mode),
            # so just completing the handshake is enough to trigger the error.
            pass

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "nonexistent_mode" in str(errors[0])


class TestTestServerCommandValidation:
    def test_non_run_test_command_raises(self):
        """Test that sending a non-run_test command raises ValueError."""
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "empty_test", errors)

        with _setup_client(s2) as conn:
            conn.control_stream.write_request(
                cbor2.dumps({"command": "bogus"}),
            )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "Expected run_test" in str(errors[0])

    def test_non_generate_command_raises_in_handle_normal_generate(self):
        """Test _handle_normal_generate raises when client sends wrong command."""
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "stop_test_on_generate", errors)

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)
            data_ch1, _ = _receive_test_case(test_stream, conn)
            data_ch1.write_request(
                cbor2.dumps(
                    {"command": "mark_complete", "status": "VALID", "origin": None}
                ),
            )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "Expected generate" in str(errors[0])

    def test_non_mark_complete_command_raises_in_wait_for_mark_complete(self):
        """Test _wait_for_mark_complete raises when client sends wrong command."""
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "stop_test_on_mark_complete", errors)

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)
            data_ch, _ = _receive_test_case(test_stream, conn)
            _send_generate(data_ch)
            data_ch.write_request(
                cbor2.dumps({"command": "generate", "schema": {"type": "boolean"}}),
            )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "Expected mark_complete" in str(errors[0])

    def test_non_generate_in_stop_test_mode_second_test_case(self):
        """Test _mode_stop_test_on_generate raises for wrong command on 2nd test case."""
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "stop_test_on_generate", errors)

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)
            data_ch1, _ = _receive_test_case(test_stream, conn)
            _send_generate(data_ch1)
            _send_mark_complete(data_ch1)

            data_ch2, _ = _receive_test_case(test_stream, conn)
            data_ch2.write_request(
                cbor2.dumps(
                    {"command": "mark_complete", "status": "VALID", "origin": None}
                ),
            )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "Expected generate" in str(errors[0])

    def test_non_generate_in_error_response_mode(self):
        """Test _mode_error_response raises for wrong command."""
        s1, s2 = _create_socket_pair()
        errors = []
        t = _start_server_catching(s1, "error_response", errors)

        with _setup_client(s2) as conn:
            test_stream = _send_run_test(conn)
            data_ch, _ = _receive_test_case(test_stream, conn)
            data_ch.write_request(
                cbor2.dumps(
                    {"command": "mark_complete", "status": "VALID", "origin": None}
                ),
            )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "Expected generate" in str(errors[0])
