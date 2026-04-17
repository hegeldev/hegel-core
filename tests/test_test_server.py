"""Tests for the test server error simulation modes."""

import socket
from threading import Thread

import cbor2

from hegel.protocol.connection import Connection
from hegel.test_server import run_test_server
from tests.client import ClientConnection


def _create_socket_pair():
    """Create a connected pair of sockets."""
    return socket.socketpair()


def _start_server(server_sock, mode):
    """Start test server in a thread and return the thread."""
    conn = Connection(server_sock)
    t = Thread(target=run_test_server, args=(conn, mode), daemon=True)
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

        Closing the server's Connection puts SHUTDOWN in stream queues,
        causing ConnectionError when the handler tries to read/write.
        """
        s1, s2 = _create_socket_pair()
        server_conn = Connection(s1)
        server_thread = Thread(
            target=run_test_server,
            args=(server_conn, "stop_test_on_generate"),
            daemon=True,
        )
        server_thread.start()

        conn = _setup_client(s2)
        _send_run_test(conn)
        # Close the server connection, putting SHUTDOWN in all streams.
        # The handler will get ConnectionError when it tries to use a stream.
        server_conn.close()

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

        def run():
            conn = Connection(s1)
            try:
                run_test_server(conn, "crash_after_handshake")
            except SystemExit as e:
                exit_codes.append(e.code)

        server_thread = Thread(target=run, daemon=True)
        server_thread.start()

        with _setup_client(s2):
            pass  # Handshake succeeds; server exits immediately after

        server_thread.join(timeout=2.0)
        assert exit_codes == [1]

    def test_crash_after_handshake_with_stderr(self, capsys):
        """Server writes error to stderr then exits with code 1."""
        s1, s2 = _create_socket_pair()
        exit_codes = []

        def run():
            conn = Connection(s1)
            try:
                run_test_server(conn, "crash_after_handshake_with_stderr")
            except SystemExit as e:
                exit_codes.append(e.code)

        server_thread = Thread(target=run, daemon=True)
        server_thread.start()

        with _setup_client(s2):
            pass

        server_thread.join(timeout=2.0)
        assert exit_codes == [1]


class TestTestServerErrors:
    def test_unknown_mode_raises(self):
        s1, s2 = _create_socket_pair()
        with Connection(s1) as conn:
            errors = []

            def run():
                try:
                    run_test_server(conn, "nonexistent_mode")
                except ValueError as e:
                    errors.append(e)

            t = Thread(target=run, daemon=True)
            t.start()

            with _setup_client(s2) as client:
                # Send run_test but don't wait for response — the server will
                # raise ValueError after receiving it, closing the connection.
                test_stream = client.new_stream()
                client.control_stream.write_request(
                    cbor2.dumps(
                        {
                            "command": "run_test",
                            "test_cases": 1,
                            "stream_id": test_stream.stream_id,
                        },
                    ),
                )

            t.join(timeout=5.0)
            assert len(errors) == 1
            assert "nonexistent_mode" in str(errors[0])
