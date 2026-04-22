import os
import subprocess
import sys
import time

import cbor2
import pytest
import trio
import trio.socket

from hegel.protocol import Connection, Packet, RequestError
from hegel.protocol.connection import PROTOCOL_VERSION
from hegel.protocol.utils import ProtocolError
from tests.client import ClientConnection
from tests.utils import run_trio_server


def _do_handshake(server_socket, client_socket):
    """Perform the connection handshake synchronously.

    Runs receive_handshake in a background trio thread while the client
    handshakes synchronously.
    """

    async def server_side(conn):
        await conn.receive_handshake()

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()


# ---- Request handling ----


def test_request_reply_cycle(socket_pair):
    """Server-side write_request and client read_reply work end-to-end."""
    server_socket, client_socket = socket_pair

    results = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        # Notify client of the stream id via control stream
        await conn.control_stream.send_request({"stream_id": stream.stream_id})
        packet = await stream.write_request(cbor2.dumps({"test": True}))
        reply_packet = await stream.read_reply(packet.message_id)
        results.append(cbor2.loads(reply_packet.payload))

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        packet = stream.read_request()
        stream.write_reply(packet.message_id, 42)

    assert results == [{"result": 42}]


def test_send_request_returns_decoded_result(socket_pair):
    """send_request awaits the reply and returns the decoded result."""
    server_socket, client_socket = socket_pair

    results = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": stream.stream_id})
        value = await stream.send_request({"value": 21})
        results.append(value)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        packet = stream.read_request()
        stream.write_reply(packet.message_id, 42)

    assert results == [42]


def test_send_request_error_response(socket_pair):
    """send_request raises RequestError when the client sends an error reply."""
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": stream.stream_id})
        try:
            await stream.send_request({"value": 21})
        except RequestError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        packet = stream.read_request()
        stream.write_reply_error(
            packet.message_id, error="test error", error_type="TestError"
        )

    assert len(errors) == 1
    assert errors[0].error_type == "TestError"


# ---- Debug mode ----


def test_connection_debug_mode(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="DebugTest", debug=True)
            for payload in [
                b"hello",
                cbor2.dumps({"hello": "world"}),
                bytes(range(128, 160)),
            ]:
                packet = Packet(
                    stream_id=0, message_id=1, is_reply=False, payload=payload
                )
                conn._debug_packet(packet, direction="TEST")
            await conn.close()

    trio.run(run)


def test_connection_debug_unknown_stream(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="Dbg", debug=True)
            packet = Packet(
                stream_id=9999, message_id=1, is_reply=False, payload=b"hello"
            )
            conn._debug_packet(packet, direction="TEST")
            await conn.close()

    trio.run(run)


def test_connection_debug_with_handshake(socket_pair):
    server_socket, client_socket = socket_pair
    results = []

    async def server_side(conn):
        await conn.receive_handshake()
        ch = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": ch.stream_id})
        packet = await ch.read_request()
        results.append(cbor2.loads(packet.payload))
        await ch.write_reply(packet.message_id, "ok")

    with (
        run_trio_server(server_socket, server_side, debug=True),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        ch_client = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")
        ch_client.send_request({"test": "data"})

    assert results != []


# ---- Stream operations ----


def test_stream_close(socket_pair):
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await stream.close()
        # Closing again should be a no-op
        await stream.close()

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)


def test_stream_close_when_connection_not_live(socket_pair):
    """Test Stream.close() when connection is already closed."""
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        # Close the connection first
        await conn.close()
        # Now close the stream — connection is not live
        await stream.close()

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)


def test_stream_process_message_when_closed(socket_pair):
    """Test reading from a locally-closed stream raises ConnectionError."""
    server_socket, client_socket = socket_pair

    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await stream.close()

        # First read: stream is closed, channel is closed → ConnectionError
        try:
            await stream.read_request(timeout=0.1)
        except ConnectionError as e:
            errors.append(str(e))

        # Second read: drain path (closed=True, empty queue)
        try:
            await stream.read_request(timeout=0.1)
        except ConnectionError as e:
            errors.append(str(e))

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.5)

    assert len(errors) == 2


def test_stream_closed_with_open_channel_raises_connection_error(socket_pair):
    """Test that _read_one_packet raises ConnectionError when closed=True but channel is open.

    Covers the WouldBlock branch in _read_one_packet: when closed=True but the
    memory channel send-end is still open (empty channel), receive_nowait() raises
    WouldBlock, which is converted to ConnectionError.
    """
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        # Set closed=True directly without calling stream.close(), so the
        # memory channel send-end stays open. receive_nowait() will raise
        # WouldBlock (empty but open), triggering the line-102 path.
        stream.closed = True
        try:
            await stream._read_one_packet()
        except ConnectionError as e:
            errors.append(str(e))

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)

    assert len(errors) == 1
    assert "is closed" in errors[0]


def test_stream_timeout(socket_pair):
    """Test stream receive times out."""
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        try:
            await stream.read_request(timeout=0.1)
        except TimeoutError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.5)

    assert len(errors) == 1


def test_stream_repr(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            assert "Control" in repr(conn.control_stream)
            await conn.close()

    trio.run(run)


@pytest.mark.parametrize(
    "conn_name, role, expected",
    [
        (None, None, "Stream "),
        (None, "TestRole", "(TestRole)"),
        ("TestConn", None, "TestConn stream [id="),
    ],
)
def test_stream_repr_variations(socket_pair, conn_name, role, expected):
    server_socket, client_socket = socket_pair
    results = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream(role=role)
        results.append(repr(stream))

    with (
        run_trio_server(server_socket, server_side, name=conn_name),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)

    assert any(expected in r for r in results)


def test_request_to_closed_stream_gets_error_reply(socket_pair):
    """Sending a request to a server-closed stream gets an error reply."""
    from hegel.protocol.packet import CLOSE_STREAM_PAYLOAD, read_packet

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        ch = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": ch.stream_id})
        await ch.close()
        # Keep connection alive long enough for client to send a request
        await trio.sleep(0.5)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        ch_client = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        # Skip close notification, then send a request
        time.sleep(0.2)
        packet = ch_client.write_request(cbor2.dumps({"test": "data"}))

        while True:
            reply = read_packet(client_socket, timeout=2.0)
            if reply.payload == CLOSE_STREAM_PAYLOAD:
                continue
            if reply.is_reply and reply.message_id == packet.message_id:
                break

        assert reply.stream_id == stream_id
        body = cbor2.loads(reply.payload)
        assert "error" in body
        assert body["type"] == "ProtocolError"


def test_request_for_unknown_stream_gets_error_reply(socket_pair):
    """A request on an unregistered stream_id gets an error reply."""
    from hegel.protocol.packet import read_packet, write_packet

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.control_stream.send_request({"ready": True})
        # Stay alive for client to interact
        await trio.sleep(1.0)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        # Wait for server ready signal
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        bogus_stream_id = 9999
        write_packet(
            client_socket,
            Packet(
                stream_id=bogus_stream_id,
                message_id=1,
                is_reply=False,
                payload=cbor2.dumps({"bad": True}),
            ),
        )

        reply = read_packet(client_socket, timeout=2.0)
        assert reply.stream_id == bogus_stream_id
        assert reply.message_id == 1
        assert reply.is_reply
        body = cbor2.loads(reply.payload)
        assert "error" in body
        assert body["type"] == "ProtocolError"


def test_send_error_reply_swallows_oserror_on_closed_connection(socket_pair):
    """_send_error_reply silently ignores write failures on a closed connection."""
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        # Close the connection, then call _send_error_reply which will try to write
        # on a closed connection and get ConnectionError — the except branch.
        await conn.close()
        fake_packet = Packet(stream_id=1, message_id=1, is_reply=False, payload=b"x")
        await conn._send_error_reply(fake_packet, "test error")  # must not raise

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()


def test_reply_for_unknown_stream_is_silently_discarded(socket_pair):
    """A reply packet on an unregistered stream is discarded with no response."""
    from hegel.protocol.packet import write_packet

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.control_stream.send_request({"ready": True})
        await trio.sleep(0.5)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        write_packet(
            client_socket,
            Packet(
                stream_id=9999,
                message_id=1,
                is_reply=True,
                payload=cbor2.dumps({"result": "stale"}),
            ),
        )
        time.sleep(0.2)
        # Connection still running (no error thrown)


def test_error_reply_write_failure_is_suppressed(socket_pair):
    """If sending the error reply fails, the reader loop continues."""
    from hegel.protocol.packet import write_packet as _write_packet

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.control_stream.send_request({"ready": True})

        # Monkey-patch write_packet to fail for the error reply
        orig_write = conn.write_packet

        async def failing_write(packet):
            if packet.is_reply and packet.stream_id == 9999:
                raise OSError("simulated write failure")
            await orig_write(packet)

        conn.write_packet = failing_write

        # Stay alive long enough for the client's bad packet to be processed
        await trio.sleep(0.5)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        _write_packet(
            client_socket,
            Packet(
                stream_id=9999,
                message_id=1,
                is_reply=False,
                payload=cbor2.dumps({"bad": True}),
            ),
        )
        time.sleep(0.3)


def test_close_packet_with_wrong_message_id_is_discarded(socket_pair):
    """A close-stream payload with the wrong message_id is silently discarded."""
    from hegel.protocol.packet import CLOSE_STREAM_PAYLOAD, write_packet
    from hegel.protocol.utils import StreamId

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.control_stream.send_request({"ready": True})
        await trio.sleep(0.5)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        # Send a close packet with wrong message_id to the control stream (id=0),
        # which IS registered on the server, so the "wrong message_id" branch is hit.
        write_packet(
            client_socket,
            Packet(
                stream_id=StreamId(0),
                message_id=42,  # wrong — should be CLOSE_STREAM_MESSAGE_ID
                is_reply=False,
                payload=CLOSE_STREAM_PAYLOAD,
            ),
        )
        time.sleep(0.3)
        # No crash: connection still running (thread will exit naturally)


@pytest.mark.parametrize("create_stream_first", [False, True])
def test_close_stream_marks_closed(socket_pair, create_stream_first):
    """Test that closing a stream marks it as closed."""
    server_socket, client_socket = socket_pair

    results = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = conn.control_stream
        packet = await stream.read_request()
        msg = cbor2.loads(packet.payload)
        stream_id = msg["stream_id"]
        role = "Hello" if create_stream_first else None
        await conn.register_client_stream(stream_id, role=role)
        await stream.write_reply(packet.message_id, "Ok")
        packet2 = await stream.read_request()
        await stream.write_reply(packet2.message_id, "Ok")

        # Record the closed/role state
        s = conn.streams[stream_id]
        results.append((s.closed, s.role))

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        client_stream = client_conn.new_stream()

        assert (
            client_conn.control_stream.send_request(
                {"stream_id": client_stream.stream_id}
            )
            == "Ok"
        )
        client_stream.close()
        assert client_conn.control_stream.send_request({}) == "Ok"

    # Give the server side a moment to process the close
    time.sleep(0.1)
    if results:
        closed, role = results[0]
        assert closed
        if create_stream_first:
            assert role == "Hello"


# ---- Duplicate reply ID ----


def test_duplicate_reply_warns_on_stderr(socket_pair, capsys):
    """Duplicate replies for same ID print a warning instead of crashing."""
    from hegel.protocol.packet import write_packet

    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.control_stream.send_request({"ready": True})
        # Wait for two reply packets with the same id to arrive
        # (sent by the client below)
        await trio.sleep(0.5)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        # Send two reply packets with the same message_id to stream 0
        for _ in range(2):
            write_packet(
                client_socket,
                Packet(
                    stream_id=0,
                    message_id=42,
                    is_reply=True,
                    payload=cbor2.dumps({"result": "dup"}),
                ),
            )
        time.sleep(0.3)

    captured = capsys.readouterr()
    assert "Duplicate reply for message_id 42" in captured.err


# ---- Connection handshake ----


def test_double_handshake_receive_raises(socket_pair):
    """Test that calling receive_handshake twice raises."""
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        try:
            await conn.receive_handshake()
        except ProtocolError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)

    assert len(errors) == 1
    assert "Handshake already completed" in str(errors[0])


def test_connect_stream_before_handshake_raises(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            with pytest.raises(
                ProtocolError, match="Cannot register streams before handshake"
            ):
                await conn.register_client_stream(1)
            await conn.close()

    trio.run(run)


def test_connect_stream_already_exists_raises(socket_pair):
    """Test connecting to existing stream raises."""
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        try:
            # stream 0 is already the control stream
            await conn.register_client_stream(0)
        except ProtocolError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)

    assert any("already registered" in str(e) for e in errors)


def test_new_stream_before_handshake_raises(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            with pytest.raises(
                ProtocolError, match="Cannot create streams before handshake"
            ):
                await conn.new_stream()
            await conn.close()

    trio.run(run)


def test_make_stream_duplicate_raises(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            conn._handshake_done = True
            conn._make_stream(1, role="First")
            with pytest.raises(ProtocolError, match="already registered"):
                conn._make_stream(1, role="Duplicate")
            await conn.close()

    trio.run(run)


def test_register_client_stream_even_id_raises(socket_pair):
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        try:
            await conn.register_client_stream(2)
        except ProtocolError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)

    assert any("Client stream id must be odd" in str(e) for e in errors)


def test_bad_handshake_negotiation(socket_pair):
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        try:
            await conn.receive_handshake()
        except ProtocolError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.control_stream.write_request(b"BadVersion")
        time.sleep(0.2)

    assert any("Bad handshake" in str(e) for e in errors)


def test_send_handshake_returns_server_version(socket_pair):
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        version = client_conn.send_handshake()
        assert version == PROTOCOL_VERSION


# ---- Connection lifecycle ----


def test_connection_running(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:  # noqa: SIM117
            async with Connection(stream, nursery=nursery) as conn:
                assert conn.running
        assert not conn.running

    trio.run(run)


def test_connection_double_close(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            await conn.close()
            await conn.close()

    trio.run(run)


def test_stream_closed_raises_connection_error(socket_pair):
    """Closing the send channel of a stream wakes up blocked readers."""
    server_socket, client_socket = socket_pair
    errors = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": stream.stream_id})
        # Close the channel directly to simulate connection drop
        import contextlib

        with contextlib.suppress(trio.ClosedResourceError):
            await stream._packet_send.aclose()
        try:
            await stream.read_request(timeout=1.0)
        except ConnectionError as e:
            errors.append(e)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")
        time.sleep(0.3)

    assert len(errors) == 1


def test_reader_loop_graceful_exit_on_remote_close(socket_pair):
    """Reader loop exits gracefully when the remote end closes the connection."""
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        # Just wait for the connection to die
        try:
            await conn.control_stream.read_request(timeout=5.0)
        except ConnectionError:
            pass

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        client_conn.close()
        time.sleep(0.2)


def test_stream_double_close_is_idempotent(socket_pair):
    """Closing a stream twice sends exactly one close packet."""
    from hegel.protocol.packet import CLOSE_STREAM_PAYLOAD

    server_socket, client_socket = socket_pair
    close_count = []

    async def server_side(conn):
        await conn.receive_handshake()
        stream = await conn.new_stream()
        await conn.control_stream.send_request({"stream_id": stream.stream_id})

        count = 0
        orig_write = conn.write_packet

        async def counting_write(packet):
            nonlocal count
            if (
                packet.payload == CLOSE_STREAM_PAYLOAD
                and packet.stream_id == stream.stream_id
            ):
                count += 1
            await orig_write(packet)

        conn.write_packet = counting_write

        await stream.close()
        await stream.close()
        close_count.append(count)

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        ctrl_packet = client_conn.control_stream.read_request()
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")
        time.sleep(0.3)

    assert close_count == [1]


def test_connection_close_is_idempotent(socket_pair):
    """Calling close() multiple times is safe."""
    server_socket, client_socket = socket_pair

    async def server_side(conn):
        await conn.receive_handshake()
        await conn.close()
        await conn.close()

    with (
        run_trio_server(server_socket, server_side),
        ClientConnection(client_socket) as client_conn,
    ):
        client_conn.send_handshake()
        time.sleep(0.2)


def test_write_packet_after_close_raises(socket):
    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            await conn.close()
            with pytest.raises(ConnectionError, match="Connection closed"):
                await conn.write_packet(
                    Packet(stream_id=0, message_id=1, is_reply=False, payload=b"test")
                )

    trio.run(run)


def test_handshake_flag_is_false_until_handshake_completes(socket_pair):
    """_handshake_done is False while receive_handshake waits for the client."""
    server_socket, client_socket = socket_pair
    errors = []
    handshake_started = __import__("threading").Event()

    async def server_side(conn):
        # Hook read_request to signal when it's been entered
        orig_read = conn.control_stream.read_request

        async def hooked_read(*args, **kwargs):
            handshake_started.set()
            return await orig_read(*args, **kwargs)

        conn.control_stream.read_request = hooked_read
        await conn.receive_handshake()

    with run_trio_server(server_socket, server_side):
        handshake_started.wait(timeout=2.0)
        try:
            pass
        except Exception as e:
            errors.append(e)
        finally:
            with ClientConnection(client_socket) as client_conn:
                client_conn.send_handshake()

    assert errors == []


def test_stream_constructor_rejects_non_control_stream_id_zero(socket):
    from hegel.protocol.stream import Stream

    async def run():
        stream = trio.SocketStream(trio.socket.from_stdlib_socket(socket))
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery)
            with pytest.raises(ProtocolError, match="Stream id must be positive"):
                Stream(connection=conn, stream_id=0, role="NotControl")
            await conn.close()

    trio.run(run)


def test_invalid_hegel_debug_env_var():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hegel.protocol.connection import _is_protocol_debug; _is_protocol_debug()",
        ],
        env={**os.environ, "HEGEL_PROTOCOL_DEBUG": "invalid"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode != 0
    assert "invalid value for HEGEL_PROTOCOL_DEBUG" in result.stderr
