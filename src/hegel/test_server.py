"""
Test server that simulates error conditions for library conformance testing.

When HEGEL_PROTOCOL_TEST_MODE is set, the hegel binary runs this simplified server
instead of the full ConjectureRunner-based server. Each mode injects a
specific error condition to validate that clients handle errors correctly.

Modes:
- crash_after_handshake: Exit immediately after handshake (simulates server crash)
- crash_after_handshake_with_stderr: Write error to stderr then crash after handshake
- stop_test_on_generate: StopTest error on first generate of 2nd test case
- stop_test_on_mark_complete: StopTest error on mark_complete
- stop_test_on_collection_more: StopTest error on first collection_more
- stop_test_on_new_collection: StopTest error on new_collection
- error_response: RequestError on first generate
- empty_test: Immediately sends test_done with no test cases
"""

import sys
import time

import cbor2

from hegel.protocol import Connection, MessageId, Stream

# Modes that exit immediately after handshake, before processing run_test.
_CRASH_MODES = {
    "crash_after_handshake",
    "crash_after_handshake_with_stderr",
}


def run_test_server(connection: Connection, mode: str) -> None:
    """Run a test server in the specified error simulation mode."""
    connection.receive_handshake()

    # Crash modes exit immediately after handshake, before processing run_test.
    if mode in _CRASH_MODES:
        try:
            if mode == "crash_after_handshake_with_stderr":
                print(
                    "FakeServerError: intentional crash for testing",
                    file=sys.stderr,
                    flush=True,
                )
            sys.exit(1)
        finally:
            connection.close()

    modes = {
        "stop_test_on_generate": _mode_stop_test_on_generate,
        "stop_test_on_mark_complete": _mode_stop_test_on_mark_complete,
        "stop_test_on_collection_more": _mode_stop_test_on_collection_more,
        "stop_test_on_new_collection": _mode_stop_test_on_new_collection,
        "error_response": _mode_error_response,
        "empty_test": _mode_empty_test,
    }

    handler = modes.get(mode)
    if handler is None:
        raise ValueError(f"Unknown test mode: {mode!r}")

    try:
        # Wait for run_test command on control stream
        packet = connection.control_stream.read_request()
        message = cbor2.loads(packet.payload)
        assert message.get("command") == "run_test"
        test_stream = connection.register_client_stream(
            message["stream_id"],
            role="Test stream",
        )
        connection.control_stream.write_reply(packet.message_id, True)

        handler(connection, test_stream)
    except ConnectionError:
        pass
    finally:
        connection.close()


def _send_test_case(
    connection: Connection,
    test_stream: Stream,
    *,
    is_final: bool = False,
) -> Stream:
    """Send a test_case event and return the data stream."""
    data_stream = connection.new_stream(role="Data")
    test_stream.send_request(
        {
            "event": "test_case",
            "stream_id": data_stream.stream_id,
            "is_final": is_final,
        },
    ).get()
    return data_stream


def _read_cbor_request(
    stream: Stream, **kwargs: float | None
) -> tuple[MessageId, dict]:
    """Read a request from a stream and CBOR-decode it."""
    packet = stream.read_request(**kwargs)
    return packet.message_id, cbor2.loads(packet.payload)


def _handle_normal_generate(data_stream: Stream) -> None:
    """Handle generate commands normally, returning a boolean value."""
    msg_id, message = _read_cbor_request(data_stream)
    assert message.get("command") == "generate"
    data_stream.write_reply(msg_id, True)


def _wait_for_mark_complete(data_stream: Stream) -> tuple[MessageId, dict]:
    """Wait for mark_complete command from client."""
    msg_id, message = _read_cbor_request(data_stream)
    assert message.get("command") == "mark_complete"
    return msg_id, message


def _send_test_done(test_stream: Stream, *, passed: bool = True) -> None:
    """Send test_done event."""
    test_stream.send_request(
        {
            "event": "test_done",
            "results": {
                "passed": passed,
                "examples_run": 0,
                "valid_test_cases": 0,
                "invalid_test_cases": 0,
                "interesting_test_cases": 0,
            },
        },
    ).get()


def _mode_stop_test_on_generate(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send StopTest error on the first generate of the 2nd test case.

    1. Send 1st test_case, handle normally (generate + mark_complete)
    2. Send 2nd test_case, respond to generate with StopTest
    3. client must NOT send mark_complete after StopTest
    4. Wait briefly, close data stream, send test_done
    """
    # First test case: handle normally
    data_stream_1 = _send_test_case(connection, test_stream)
    _handle_normal_generate(data_stream_1)
    mc_id, _ = _wait_for_mark_complete(data_stream_1)
    data_stream_1.write_reply(mc_id, None)
    data_stream_1.close()

    # Second test case: StopTest on generate
    data_stream_2 = _send_test_case(connection, test_stream)
    msg_id, message = _read_cbor_request(data_stream_2)
    assert message.get("command") == "generate"
    data_stream_2.write_reply_error(msg_id, error="StopTest", error_type="StopTest")

    # Wait briefly to see if client incorrectly sends mark_complete
    time.sleep(0.1)
    data_stream_2.close()

    _send_test_done(test_stream)


def _mode_stop_test_on_mark_complete(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send StopTest error in response to mark_complete.

    1. Send test_case, handle generate normally
    2. When client sends mark_complete, respond with StopTest
    3. client must not send further commands on that stream
    4. Wait briefly, close data stream, send test_done
    """
    data_stream = _send_test_case(connection, test_stream)
    _handle_normal_generate(data_stream)

    mc_id, _ = _wait_for_mark_complete(data_stream)
    data_stream.write_reply_error(mc_id, error="StopTest", error_type="StopTest")

    time.sleep(0.1)
    data_stream.close()

    _send_test_done(test_stream)


def _handle_commands_until(
    data_stream: Stream,
    *,
    stop_on: str,
) -> MessageId:
    """Handle commands normally until the specified command is received.

    Returns the message ID of the target command (so the caller can send
    an error response). Responds to all intermediate commands with a
    simple success value appropriate for the command type.
    """
    collection_counter = 0
    while True:
        msg_id, message = _read_cbor_request(data_stream)
        command = message.get("command")

        if command == stop_on:
            return msg_id

        if command == "new_collection":
            data_stream.write_reply(msg_id, collection_counter)
            collection_counter += 1
        else:
            # All other commands (generate, start_span, stop_span,
            # mark_complete, etc.) get a simple None/True response.
            data_stream.write_reply(msg_id, None)


def _mode_stop_test_on_collection_more(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send StopTest error on the first collection_more command.

    1. Send test_case, handle generate/spans/new_collection normally
    2. Respond to first collection_more with StopTest
    3. client must stop the collection loop and not send further commands
    4. Wait briefly, close data stream, send test_done
    """
    data_stream = _send_test_case(connection, test_stream)

    msg_id = _handle_commands_until(data_stream, stop_on="collection_more")
    data_stream.write_reply_error(msg_id, error="StopTest", error_type="StopTest")

    time.sleep(0.1)
    data_stream.close()

    _send_test_done(test_stream)


def _mode_stop_test_on_new_collection(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send StopTest error on the new_collection command.

    1. Send test_case, handle generate/spans normally
    2. Respond to new_collection with StopTest
    3. client must abort immediately
    4. Wait briefly, close data stream, send test_done
    """
    data_stream = _send_test_case(connection, test_stream)

    msg_id = _handle_commands_until(data_stream, stop_on="new_collection")
    data_stream.write_reply_error(msg_id, error="StopTest", error_type="StopTest")

    time.sleep(0.1)
    data_stream.close()

    _send_test_done(test_stream)


def _mode_error_response(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send a RequestError on the first generate command.

    1. Send test_case
    2. When client sends generate, respond with RequestError
    3. client should handle the error gracefully
    4. Send test_done
    """
    data_stream = _send_test_case(connection, test_stream)

    msg_id, message = _read_cbor_request(data_stream)
    assert message.get("command") == "generate"
    data_stream.write_reply_error(
        msg_id,
        error="Simulated error for testing",
        error_type="RequestError",
    )

    # client should send mark_complete with INTERESTING status after the error
    try:
        mc_id, _ = _read_cbor_request(data_stream, timeout=2.0)
        data_stream.write_reply(mc_id, None)
    except (TimeoutError, ConnectionError):
        pass

    data_stream.close()
    _send_test_done(test_stream)


def _mode_empty_test(
    connection: Connection,
    test_stream: Stream,
) -> None:
    """Send test_done immediately with no test cases.

    Validates the edge case where no test_case events are sent.
    """
    _send_test_done(test_stream)
