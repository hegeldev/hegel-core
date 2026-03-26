"""Tests for the test server error simulation modes."""

import socket
from threading import Thread

import cbor2
import pytest

from hegel.protocol.connection import Connection
from hegel.test_server import run_test_server
from tests.client import ClientConnection


def _create_socket_pair():
    """Create a connected pair of sockets."""
    s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return s1, s2


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
    """Send a run_test command and return the test channel."""
    test_channel = conn.new_channel()
    packet = conn.control_channel.write_request(
        cbor2.dumps(
            {
                "command": "run_test",
                "test_cases": 1,
                "channel_id": test_channel.channel_id,
            },
        ),
    )
    conn.control_channel.read_reply(packet.message_id)
    return test_channel


def _receive_test_case(test_channel, conn):
    """Receive a test_case event and return the data channel."""
    packet = test_channel.read_request()
    message = cbor2.loads(packet.payload)
    assert message["event"] == "test_case"
    data_channel = conn.connect_channel(
        message["channel_id"],
    )
    test_channel.write_reply(packet.message_id, None)
    return data_channel, message.get("is_final", False)


def _send_generate(data_channel):
    """Send a generate command and return the response."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "generate", "schema": {"type": "boolean"}}),
    )
    return data_channel.read_reply(packet.message_id)


def _send_generate_expect_error(data_channel):
    """Send a generate command expecting a RequestError."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "generate", "schema": {"type": "boolean"}}),
    )
    raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_start_span(data_channel, label=1):
    """Send a start_span command."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "start_span", "label": label}),
    )
    return data_channel.read_reply(packet.message_id)


def _send_new_collection(data_channel, *, min_size=0, max_size=10):
    """Send a new_collection command and return the collection name."""
    packet = data_channel.write_request(
        cbor2.dumps(
            {
                "command": "new_collection",
                "name": "list",
                "min_size": min_size,
                "max_size": max_size,
            },
        ),
    )
    reply = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
    return reply["result"]


def _send_new_collection_expect_error(data_channel, *, min_size=0, max_size=10):
    """Send a new_collection command expecting a StopTest error."""
    packet = data_channel.write_request(
        cbor2.dumps(
            {
                "command": "new_collection",
                "name": "list",
                "min_size": min_size,
                "max_size": max_size,
            },
        ),
    )
    raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_collection_more_expect_error(data_channel, collection):
    """Send a collection_more command expecting a StopTest error."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "collection_more", "collection": collection}),
    )
    raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _send_mark_complete(data_channel, *, status="VALID"):
    """Send a mark_complete command."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "mark_complete", "status": status, "origin": None}),
    )
    return data_channel.read_reply(packet.message_id)


def _send_mark_complete_expect_error(data_channel, *, status="VALID"):
    """Send mark_complete expecting a RequestError."""
    packet = data_channel.write_request(
        cbor2.dumps({"command": "mark_complete", "status": status, "origin": None}),
    )
    raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
    assert "error" in raw
    return raw


def _receive_test_done(test_channel):
    """Receive a test_done event."""
    packet = test_channel.read_request()
    message = cbor2.loads(packet.payload)
    assert message["event"] == "test_done"
    test_channel.write_reply(packet.message_id, None)
    return message["results"]


class TestStopTestOnGenerate:
    def test_server_sends_stop_test_on_second_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        with _setup_client(s2) as conn:
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

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_generate")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            # Go through both test cases
            data_ch1, _ = _receive_test_case(test_channel, conn)
            _send_generate(data_ch1)
            _send_mark_complete(data_ch1)

            data_ch2, _ = _receive_test_case(test_channel, conn)
            _send_generate_expect_error(data_ch2)

            results = _receive_test_done(test_channel)
            assert "passed" in results

        server_thread.join(timeout=2.0)


class TestStopTestOnMarkComplete:
    def test_server_sends_stop_test_on_mark_complete(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_mark_complete")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)
            _send_generate(data_channel)

            error = _send_mark_complete_expect_error(data_channel)
            assert error["type"] == "StopTest"

            # Don't send further commands — that's correct behavior
            _receive_test_done(test_channel)

        server_thread.join(timeout=2.0)


class TestErrorResponse:
    def test_server_sends_error_on_generate(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)
            error = _send_generate_expect_error(data_channel)
            assert error["type"] == "RequestError"

            # client should send mark_complete with INTERESTING
            _send_mark_complete(data_channel, status="INTERESTING")

            _receive_test_done(test_channel)

        server_thread.join(timeout=2.0)


class TestEmptyTest:
    def test_server_sends_test_done_immediately(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "empty_test")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            # Should get test_done immediately, no test_case events
            results = _receive_test_done(test_channel)
            assert results["passed"] is True

        server_thread.join(timeout=2.0)


class TestErrorResponseNoMarkComplete:
    def test_server_handles_client_not_sending_mark_complete(self):
        """Test error_response mode when client doesn't send mark_complete."""
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "error_response")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)
            error = _send_generate_expect_error(data_channel)
            assert error["type"] == "RequestError"

            # Don't send mark_complete — close the data channel instead
            # This triggers the TimeoutError/ConnectionError path
            data_channel.close()

            _receive_test_done(test_channel)

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

    def test_server_handles_connection_error_from_channel(self):
        """Tests the except ConnectionError handler in run_test_server.

        Closing the server's Connection puts SHUTDOWN in channel queues,
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
        # Close the server connection, putting SHUTDOWN in all channels.
        # The handler will get ConnectionError when it tries to use a channel.
        server_conn.close()

        server_thread.join(timeout=5.0)


class TestStopTestOnCollectionMore:
    def test_server_sends_stop_test_on_collection_more(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_collection_more")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)

            # client sends start_span (LIST) + new_collection normally
            _send_start_span(data_channel, label=1)
            collection = _send_new_collection(data_channel)
            assert isinstance(collection, str)

            # collection_more should get StopTest
            error = _send_collection_more_expect_error(data_channel, collection)
            assert error["type"] == "StopTest"

            # Don't send further commands
            _receive_test_done(test_channel)

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_collection_more")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)
            _send_start_span(data_channel, label=1)
            collection = _send_new_collection(data_channel)
            _send_collection_more_expect_error(data_channel, collection)

            results = _receive_test_done(test_channel)
            assert "passed" in results

        server_thread.join(timeout=2.0)


class TestStopTestOnNewCollection:
    def test_server_sends_stop_test_on_new_collection(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_new_collection")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)

            # client sends start_span (LIST) normally
            _send_start_span(data_channel, label=1)

            # new_collection should get StopTest
            error = _send_new_collection_expect_error(data_channel)
            assert error["type"] == "StopTest"

            # Don't send further commands
            _receive_test_done(test_channel)

        server_thread.join(timeout=2.0)

    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        server_thread = _start_server(s1, "stop_test_on_new_collection")

        with _setup_client(s2) as conn:
            test_channel = _send_run_test(conn)

            data_channel, _ = _receive_test_case(test_channel, conn)
            _send_start_span(data_channel, label=1)
            _send_new_collection_expect_error(data_channel)

            results = _receive_test_done(test_channel)
            assert "passed" in results

        server_thread.join(timeout=2.0)


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
                test_channel = client.new_channel()
                client.control_channel.write_request(
                    cbor2.dumps(
                        {
                            "command": "run_test",
                            "test_cases": 1,
                            "channel_id": test_channel.channel_id,
                        },
                    ),
                )

            t.join(timeout=5.0)
            assert len(errors) == 1
            assert "nonexistent_mode" in str(errors[0])


class TestStopTestOnStartSpan:
    def test_lifecycle_completes(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "stop_test_on_start_span")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            # Send start_span — should get StopTest
            packet = data_channel.write_request(
                cbor2.dumps({"command": "start_span", "label": 1}),
            )
            raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
            assert raw.get("error") == "StopTest"
            # Read test_done
            done_packet = test_channel.read_request()
            done_msg = cbor2.loads(done_packet.payload)
            assert done_msg["event"] == "test_done"
            test_channel.write_reply(done_packet.message_id, None)
        t.join(timeout=5.0)


class TestServerCrash:
    def test_server_closes_connection(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "server_crash")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            # Server reads generate but crashes before replying.
            # The send succeeds but the reply read fails.
            with pytest.raises((ConnectionError, OSError)):
                _send_generate(data_channel)
        t.join(timeout=5.0)


class TestHealthCheckFailure:
    def test_results_contain_health_check_failure(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "health_check_failure")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            _send_generate(data_channel)
            _send_mark_complete(data_channel)
            done_packet = test_channel.read_request()
            done_msg = cbor2.loads(done_packet.payload)
            assert done_msg["event"] == "test_done"
            assert "health_check_failure" in done_msg["results"]
            test_channel.write_reply(done_packet.message_id, None)
        t.join(timeout=5.0)


class TestServerErrorInResults:
    def test_results_contain_error(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "server_error_in_results")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            _send_generate(data_channel)
            _send_mark_complete(data_channel)
            done_packet = test_channel.read_request()
            done_msg = cbor2.loads(done_packet.payload)
            assert done_msg["event"] == "test_done"
            assert "error" in done_msg["results"]
            test_channel.write_reply(done_packet.message_id, None)
        t.join(timeout=5.0)


class TestFlakyReplay:
    def test_server_sends_flaky_replay_error(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "flaky_replay")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            raw = _send_generate_expect_error(data_channel)
            assert "FlakyReplay" in str(raw)
            # Send mark_complete after the error (exercises the try path in the mode)
            _send_mark_complete(data_channel)
        t.join(timeout=5.0)


class TestFailedNoReason:
    def test_results_failed_with_no_error_fields(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "failed_no_reason")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            _send_generate(data_channel)
            _send_mark_complete(data_channel)
            done_packet = test_channel.read_request()
            done_msg = cbor2.loads(done_packet.payload)
            assert done_msg["event"] == "test_done"
            assert done_msg["results"]["passed"] is False
            assert "error" not in done_msg["results"]
            test_channel.write_reply(done_packet.message_id, None)
        t.join(timeout=5.0)


class TestStopTestOnPoolAdd:
    def test_server_sends_stop_test_on_pool_add(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "stop_test_on_pool_add")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            # Send new_pool and generate before pool_add (realistic sequence)
            # These are handled normally by _handle_commands_until
            p1 = data_channel.write_request(cbor2.dumps({"command": "new_pool"}))
            r1 = cbor2.loads(data_channel.read_reply(p1.message_id).payload)
            assert r1.get("result") == 0  # pool id
            p2 = data_channel.write_request(cbor2.dumps({"command": "generate"}))
            r2 = cbor2.loads(data_channel.read_reply(p2.message_id).payload)
            assert r2.get("result") is True  # boolean value
            # Now pool_add triggers StopTest
            packet = data_channel.write_request(
                cbor2.dumps({"command": "pool_add"}),
            )
            raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
            assert raw.get("error") == "StopTest"
        t.join(timeout=5.0)


class TestStopTestOnNewPool:
    def test_server_sends_stop_test_on_new_pool(self):
        s1, s2 = _create_socket_pair()
        t = _start_server(s1, "stop_test_on_new_pool")
        with _setup_client(s2) as client:
            test_channel = _send_run_test(client)
            data_channel, _ = _receive_test_case(test_channel, client)
            packet = data_channel.write_request(
                cbor2.dumps({"command": "new_pool"}),
            )
            raw = cbor2.loads(data_channel.read_reply(packet.message_id).payload)
            assert raw.get("error") == "StopTest"
        t.join(timeout=5.0)
