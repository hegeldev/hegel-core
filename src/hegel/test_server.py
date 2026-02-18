"""
Test server that simulates error conditions for SDK conformance testing.

When HEGEL_TEST_MODE is set, the hegel binary runs this simplified server
instead of the full ConjectureRunner-based server. Each mode injects a
specific error condition to validate that SDKs handle errors correctly.

Modes:
- stop_test_on_generate: StopTest error on first generate of 2nd test case
- stop_test_on_mark_complete: StopTest error on mark_complete
- stop_test_on_collection_more: StopTest error on first collection_more
- stop_test_on_new_collection: StopTest error on new_collection
- error_response: RequestError on first generate
- empty_test: Immediately sends test_done with no test cases
"""

import time

from hegel.protocol import Channel, Connection, MessageId


def run_test_server(connection: Connection, mode: str) -> None:
    """Run a test server in the specified error simulation mode."""
    connection.receive_handshake()

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
        # Wait for run_test command on control channel
        msg_id, message = connection.control_channel.receive_request()
        assert message.get("command") == "run_test"
        test_channel = connection.connect_channel(
            message["channel"],
            role="Test channel",
        )
        connection.control_channel.send_response_value(msg_id, message=True)

        handler(connection, test_channel)
    except ConnectionError:
        pass
    finally:
        connection.close()


def _send_test_case(
    connection: Connection,
    test_channel: Channel,
    *,
    is_final: bool = False,
) -> Channel:
    """Send a test_case event and return the data channel."""
    data_channel = connection.new_channel(role="Data")
    test_channel.request(
        {
            "event": "test_case",
            "channel": data_channel.channel_id,
            "is_final": is_final,
        },
    ).get()
    return data_channel


def _handle_normal_generate(data_channel: Channel) -> None:
    """Handle generate commands normally, returning a boolean value."""
    msg_id, message = data_channel.receive_request()
    assert message.get("command") == "generate"
    data_channel.send_response_value(msg_id, message=True)


def _wait_for_mark_complete(data_channel: Channel) -> tuple[MessageId, dict]:
    """Wait for mark_complete command from SDK."""
    msg_id, message = data_channel.receive_request()
    assert message.get("command") == "mark_complete"
    return msg_id, message


def _send_test_done(test_channel: Channel, *, passed: bool = True) -> None:
    """Send test_done event."""
    test_channel.request(
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
    test_channel: Channel,
) -> None:
    """Send StopTest error on the first generate of the 2nd test case.

    1. Send 1st test_case, handle normally (generate + mark_complete)
    2. Send 2nd test_case, respond to generate with StopTest
    3. SDK must NOT send mark_complete after StopTest
    4. Wait briefly, close data channel, send test_done
    """
    # First test case: handle normally
    data_channel_1 = _send_test_case(connection, test_channel)
    _handle_normal_generate(data_channel_1)
    mc_id, _ = _wait_for_mark_complete(data_channel_1)
    data_channel_1.send_response_value(mc_id, message=None)
    data_channel_1.close()

    # Second test case: StopTest on generate
    data_channel_2 = _send_test_case(connection, test_channel)
    msg_id, message = data_channel_2.receive_request()
    assert message.get("command") == "generate"
    data_channel_2.send_response_error(
        msg_id,
        error="StopTest",
        error_type="StopTest",
    )

    # Wait briefly to see if SDK incorrectly sends mark_complete
    time.sleep(0.1)
    data_channel_2.close()

    _send_test_done(test_channel)


def _mode_stop_test_on_mark_complete(
    connection: Connection,
    test_channel: Channel,
) -> None:
    """Send StopTest error in response to mark_complete.

    1. Send test_case, handle generate normally
    2. When SDK sends mark_complete, respond with StopTest
    3. SDK must not send further commands on that channel
    4. Wait briefly, close data channel, send test_done
    """
    data_channel = _send_test_case(connection, test_channel)
    _handle_normal_generate(data_channel)

    mc_id, _ = _wait_for_mark_complete(data_channel)
    data_channel.send_response_error(
        mc_id,
        error="StopTest",
        error_type="StopTest",
    )

    time.sleep(0.1)
    data_channel.close()

    _send_test_done(test_channel)


def _handle_commands_until(
    data_channel: Channel,
    *,
    stop_on: str,
) -> MessageId:
    """Handle commands normally until the specified command is received.

    Returns the message ID of the target command (so the caller can send
    an error response). Handles generate, start_span, stop_span, and
    new_collection commands normally along the way.
    """
    collection_counter = 0
    while True:
        msg_id, message = data_channel.receive_request()
        command = message.get("command")

        if command == stop_on:
            return msg_id

        if command == "generate":
            data_channel.send_response_value(msg_id, message=True)
        elif command == "start_span":
            data_channel.send_response_value(msg_id, message=None)
        elif command == "stop_span":
            data_channel.send_response_value(msg_id, message=None)
        elif command == "new_collection":
            name = f"collection_{collection_counter}"
            collection_counter += 1
            data_channel.send_response_value(msg_id, message=name)
        elif command == "collection_more":
            data_channel.send_response_value(
                msg_id,
                message={"more": True, "channel": data_channel.channel_id},
            )
        elif command == "collection_reject":
            data_channel.send_response_value(msg_id, message=None)
        elif command == "mark_complete":
            data_channel.send_response_value(msg_id, message=None)
        else:
            data_channel.send_response_value(msg_id, message=None)


def _mode_stop_test_on_collection_more(
    connection: Connection,
    test_channel: Channel,
) -> None:
    """Send StopTest error on the first collection_more command.

    1. Send test_case, handle generate/spans/new_collection normally
    2. Respond to first collection_more with StopTest
    3. SDK must stop the collection loop and not send further commands
    4. Wait briefly, close data channel, send test_done
    """
    data_channel = _send_test_case(connection, test_channel)

    msg_id = _handle_commands_until(data_channel, stop_on="collection_more")
    data_channel.send_response_error(
        msg_id,
        error="StopTest",
        error_type="StopTest",
    )

    time.sleep(0.1)
    data_channel.close()

    _send_test_done(test_channel)


def _mode_stop_test_on_new_collection(
    connection: Connection,
    test_channel: Channel,
) -> None:
    """Send StopTest error on the new_collection command.

    1. Send test_case, handle generate/spans normally
    2. Respond to new_collection with StopTest
    3. SDK must abort immediately
    4. Wait briefly, close data channel, send test_done
    """
    data_channel = _send_test_case(connection, test_channel)

    msg_id = _handle_commands_until(data_channel, stop_on="new_collection")
    data_channel.send_response_error(
        msg_id,
        error="StopTest",
        error_type="StopTest",
    )

    time.sleep(0.1)
    data_channel.close()

    _send_test_done(test_channel)


def _mode_error_response(
    connection: Connection,
    test_channel: Channel,
) -> None:
    """Send a RequestError on the first generate command.

    1. Send test_case
    2. When SDK sends generate, respond with RequestError
    3. SDK should handle the error gracefully
    4. Send test_done
    """
    data_channel = _send_test_case(connection, test_channel)

    msg_id, message = data_channel.receive_request()
    assert message.get("command") == "generate"
    data_channel.send_response_error(
        msg_id,
        error="Simulated error for testing",
        error_type="RequestError",
    )

    # SDK should send mark_complete with INTERESTING status after the error
    try:
        mc_id, _ = data_channel.receive_request(timeout=2.0)
        data_channel.send_response_value(mc_id, message=None)
    except (TimeoutError, ConnectionError):
        pass

    data_channel.close()
    _send_test_done(test_channel)


def _mode_empty_test(
    connection: Connection,
    test_channel: Channel,
) -> None:
    """Send test_done immediately with no test cases.

    Validates the edge case where no test_case events are sent.
    """
    _send_test_done(test_channel)
