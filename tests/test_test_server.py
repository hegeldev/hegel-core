"""Tests for the test server error simulation modes."""

import socket
from threading import Thread

import cbor2

from hegel.protocol import Connection
from hegel.test_server import run_test_server


def _create_socket_pair():
    """Create a connected pair of sockets."""
    s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return s1, s2


def _start_server(server_sock, mode):
    """Start test server in a thread and return the thread."""
    conn = Connection(server_sock, name="Server")
    t = Thread(target=run_test_server, args=(conn, mode), daemon=True)
    t.start()
    return t


def _setup_client(client_sock):
    """Set up client connection and perform handshake."""
    conn = Connection(client_sock, name="Client")
    conn.send_handshake()
    return conn


def _send_run_test(conn):
    """Send a run_test command and return the test channel."""
    test_channel = conn.new_channel(role="Test")
    msg_id = conn.control_channel.send_request(
        {
            "command": "run_test",
            "name": "test",
            "test_cases": 1,
            "channel": test_channel.channel_id,
        },
    )
    conn.control_channel.receive_response(msg_id)
    return test_channel


def _receive_test_case(test_channel, conn):
    """Receive a test_case event and return the data channel."""
    msg_id, message = test_channel.receive_request()
    assert message["event"] == "test_case"
    data_channel = conn.connect_channel(
        message["channel"],
        role="Data",
    )
    test_channel.send_response_value(msg_id, message=None)
    return data_channel, message.get("is_final", False)


def _send_generate(data_channel):
    """Send a generate command and return the response."""
    return data_channel.receive_response(
        data_channel.send_request(
            {"command": "generate", "schema": {"type": "boolean"}},
        ),
    )


def _send_generate_expect_error(data_channel):
    """Send a generate command expecting a RequestError."""
    msg_id = data_channel.send_request(
        {"command": "generate", "schema": {"type": "boolean"}},
    )
    raw = cbor2.loads(data_channel.receive_response_raw(msg_id))
    assert "error" in raw
    return raw


def _send_mark_complete(data_channel, *, status="VALID"):
    """Send a mark_complete command."""
    return data_channel.receive_response(
        data_channel.send_request(
            {"command": "mark_complete", "status": status, "origin": None},
        ),
    )


def _send_mark_complete_expect_error(data_channel, *, status="VALID"):
    """Send mark_complete expecting a RequestError."""
    msg_id = data_channel.send_request(
        {"command": "mark_complete", "status": status, "origin": None},
    )
    raw = cbor2.loads(data_channel.receive_response_raw(msg_id))
    assert "error" in raw
    return raw


def _receive_test_done(test_channel):
    """Receive a test_done event."""
    msg_id, message = test_channel.receive_request()
    assert message["event"] == "test_done"
    test_channel.send_response_value(msg_id, message=None)
    return message["results"]


class TestStopTestOnGenerate:
    def test_server_sends_stop_test_on_second_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        # First test case: normal flow
        data_ch1, _ = _receive_test_case(test_channel, conn)
        _send_generate(data_ch1)
        _send_mark_complete(data_ch1)

        # Second test case: StopTest on generate
        data_ch2, _ = _receive_test_case(test_channel, conn)
        error = _send_generate_expect_error(data_ch2)
        assert error["type"] == "StopTest"

        # Don't send mark_complete — that's the correct behavior
        # Receive test_done
        _receive_test_done(test_channel)

        conn.close()
        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        # Go through both test cases
        data_ch1, _ = _receive_test_case(test_channel, conn)
        _send_generate(data_ch1)
        _send_mark_complete(data_ch1)

        data_ch2, _ = _receive_test_case(test_channel, conn)
        _send_generate_expect_error(data_ch2)

        results = _receive_test_done(test_channel)
        assert "passed" in results

        conn.close()
        server_thread.join(timeout=2.0)


class TestStopTestOnMarkComplete:
    def test_server_sends_stop_test_on_mark_complete(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_mark_complete")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        data_ch, _ = _receive_test_case(test_channel, conn)
        _send_generate(data_ch)

        error = _send_mark_complete_expect_error(data_ch)
        assert error["type"] == "StopTest"

        # Don't send further commands — that's correct behavior
        _receive_test_done(test_channel)

        conn.close()
        server_thread.join(timeout=2.0)


class TestErrorResponse:
    def test_server_sends_error_on_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        data_ch, _ = _receive_test_case(test_channel, conn)
        error = _send_generate_expect_error(data_ch)
        assert error["type"] == "RequestError"

        # SDK should send mark_complete with INTERESTING
        _send_mark_complete(data_ch, status="INTERESTING")

        _receive_test_done(test_channel)

        conn.close()
        server_thread.join(timeout=2.0)


class TestEmptyTest:
    def test_server_sends_test_done_immediately(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "empty_test")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        # Should get test_done immediately, no test_case events
        results = _receive_test_done(test_channel)
        assert results["passed"] is True

        conn.close()
        server_thread.join(timeout=2.0)


class TestErrorResponseNoMarkComplete:
    def test_server_handles_client_not_sending_mark_complete(self):
        """Test error_response mode when SDK doesn't send mark_complete."""
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        conn = _setup_client(s2)
        test_channel = _send_run_test(conn)

        data_ch, _ = _receive_test_case(test_channel, conn)
        error = _send_generate_expect_error(data_ch)
        assert error["type"] == "RequestError"

        # Don't send mark_complete — close the data channel instead
        # This triggers the TimeoutError/ConnectionError path
        data_ch.close()

        _receive_test_done(test_channel)

        conn.close()
        server_thread.join(timeout=5.0)


class TestConnectionErrorHandling:
    def test_server_handles_early_client_disconnect(self):
        """Test server handles client disconnecting mid-protocol."""
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        conn = _setup_client(s2)
        _send_run_test(conn)

        # Close immediately without going through test lifecycle
        conn.close()

        server_thread.join(timeout=5.0)


class TestTestServerErrors:
    def test_unknown_mode_raises(self):
        s1, s2 = _create_socket_pair()
        conn = Connection(s1, name="Server")
        errors = []

        def run():
            try:
                run_test_server(conn, "nonexistent_mode")
            except ValueError as e:
                errors.append(e)

        t = Thread(target=run, daemon=True)
        t.start()

        client = _setup_client(s2)
        # Send run_test but don't wait for response — the server will
        # raise ValueError after receiving it, closing the connection.
        test_channel = client.new_channel(role="Test")
        client.control_channel.send_request(
            {
                "command": "run_test",
                "name": "test",
                "test_cases": 1,
                "channel": test_channel.channel_id,
            },
        )

        t.join(timeout=5.0)
        assert len(errors) == 1
        assert "nonexistent_mode" in str(errors[0])

        client.close()
