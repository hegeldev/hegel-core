import os
import subprocess
import sys
import time
from threading import Thread

import cbor2
import pytest

from hegel.protocol import Connection, Packet, RequestError
from hegel.protocol.connection import PROTOCOL_VERSION
from hegel.protocol.utils import SHUTDOWN
from tests.client import ClientConnection


def _do_handshake(server: Connection, client: ClientConnection):
    t = Thread(target=server.receive_handshake, daemon=True)
    t.start()
    client.send_handshake()
    t.join(timeout=5)


def test_request_handling(socket_pair):
    def add_server(connection):
        connection.receive_handshake()
        handler_stream = connection.new_stream()

        @handler_stream.handle_requests
        def _(message):
            return {"sum": message["x"] + message["y"]}

    server_socket, client_socket = socket_pair
    thread = Thread(
        target=add_server,
        args=(Connection(server_socket),),
        daemon=True,
    )
    thread.start()
    with ClientConnection(client_socket) as client_connection:
        client_connection.send_handshake()

        # Server creates stream with id=2 (first non-control,
        # __next_stream_id=1, id = (1 << 1) | 0 = 2)
        send_stream = client_connection.connect_stream(2)
        assert send_stream.send_request({"x": 2, "y": 3}) == {"sum": 5}


def test_handle_requests_until(socket_pair):
    """handle_requests exits immediately when until returns True."""

    def add_server(connection):
        connection.receive_handshake()
        handler_stream = connection.new_stream()
        handler_stream.handle_requests(
            lambda message: None,
            until=lambda: True,
        )

    server_socket, client_socket = socket_pair
    thread = Thread(
        target=add_server,
        args=(Connection(server_socket),),
        daemon=True,
    )
    thread.start()
    with ClientConnection(client_socket) as client_connection:
        client_connection.send_handshake()
    thread.join(timeout=5)


@pytest.mark.parametrize(
    "name, payload",
    [
        ("DebugTest", b"hello"),
        ("DebugCBOR", cbor2.dumps({"hello": "world"})),
        ("DebugBin", bytes(range(128, 160))),
    ],
)
def test_connection_debug_mode(socket, name, payload):
    with Connection(socket, name=name, debug=True) as conn:
        packet = Packet(stream_id=0, message_id=1, is_reply=False, payload=payload)
        conn._debug_packet(packet, direction="TEST")


@pytest.mark.parametrize(
    "send_fn",
    [
        lambda ch: ch.write_request(cbor2.dumps({"test": "data"})),
        lambda ch: ch.write_request(b"\xfc\xfd\xfe"),
    ],
)
def test_connection_debug_with_handshake(socket_pair, send_fn):
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket, name="Server", debug=True) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)
        ch_client = client_conn.new_stream()
        server_conn.register_client_stream(ch_client.stream_id)
        send_fn(ch_client)
        time.sleep(0.2)


# ---- Stream operations ----


def test_stream_close(socket_pair):
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        stream = server_conn.new_stream()
        stream.close()
        # Closing again should be a no-op
        stream.close()


def test_stream_close_when_connection_not_live(socket_pair):
    """Test Stream.close() when connection is already closed.

    Tests that Stream.close() skips sending the close notification when
    connection.live is False.
    """
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        stream = server_conn.new_stream()
        # Close the connection first
        server_conn.close()
        # Now close the stream — connection is not live
        stream.close()


def test_stream_process_message_when_closed(socket_pair):
    """Test reading from a locally-closed stream raises ConnectionError."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        stream = server_conn.new_stream()
        stream.close()

        # First read consumes SHUTDOWN from the queue
        with pytest.raises(ConnectionError):
            stream.read_request(timeout=0.1)

        # Second read hits the empty-queue-but-closed path
        with pytest.raises(ConnectionError):
            stream.read_request(timeout=0.1)


def test_stream_timeout(socket_pair):
    """Test stream receive times out."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        stream = server_conn.new_stream()

        with pytest.raises(TimeoutError):
            stream.read_request(timeout=0.1)


def test_stream_repr(socket):
    with Connection(socket) as conn:
        assert "Control" in repr(conn.control_stream)


@pytest.mark.parametrize(
    "role, expected",
    [
        (None, "Stream "),
        ("TestRole", "(TestRole)"),
    ],
)
def test_stream_repr_variations(socket_pair, role, expected):
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)
        stream = server_conn.new_stream(role=role)
        assert expected in repr(stream)


def test_message_to_closed_stream(socket_pair):
    """Test sending a message to a closed stream."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        ch_server = server_conn.new_stream()
        ch_client = client_conn.connect_stream(ch_server.stream_id)

        # Close the stream on server side
        ch_server.close()
        time.sleep(0.2)

        # Now send a request to the closed stream from client
        ch_client.write_request(cbor2.dumps({"test": "data"}))
        time.sleep(0.2)


@pytest.mark.parametrize("create_stream_first", [False, True])
def test_close_stream_marks_closed(socket_pair, create_stream_first):
    """Test that closing a stream marks it as closed."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket, name="Server", debug=True) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):

        def server_side():
            server_conn.receive_handshake()
            stream = server_conn.control_stream
            # Server must always connect to the stream so the reader can route
            # the close packet.
            packet = stream.read_request()
            msg = cbor2.loads(packet.payload)
            stream_id = msg["stream_id"]
            role = "Hello" if create_stream_first else None
            server_conn.register_client_stream(stream_id, role=role)
            stream.write_reply(packet.message_id, "Ok")
            packet = stream.read_request()
            stream.write_reply(packet.message_id, "Ok")

        t = Thread(target=server_side, daemon=True)
        t.start()
        client_conn.send_handshake()

        client_stream_to_close = client_conn.new_stream()

        # Tell the server about the stream so it can connect
        assert (
            client_conn.control_stream.send_request(
                {"stream_id": client_stream_to_close.stream_id},
            )
            == "Ok"
        )

        client_stream_to_close.close()

        assert client_conn.control_stream.send_request({}) == "Ok"

        # The stream should now be closed on the server side
        stream = server_conn.streams[client_stream_to_close.stream_id]
        assert stream.closed
        if create_stream_first:
            assert stream.role == "Hello"


# ---- PendingRequest ----


def test_pending_request_double_get_raises(socket_pair):
    """Test server-side PendingRequest raises ValueError on second get()."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        errors = []

        def server_side():
            server_conn.receive_handshake()
            # Tell client which stream we're creating via control stream
            stream = server_conn.new_stream()
            server_conn.control_stream.send_request(
                {"stream_id": stream.stream_id}
            ).get()
            pending = stream.send_request({"value": 21})
            assert pending.get() == 42
            try:
                pending.get()
            except ValueError as e:
                errors.append(e)

        t = Thread(target=server_side, daemon=True)
        t.start()
        client_conn.send_handshake()

        # Server tells us the stream ID via control stream
        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        # Client receives server's request and replies
        packet = stream.read_request()
        stream.write_reply(packet.message_id, 42)
        t.join(timeout=5)
        assert len(errors) == 1
        assert "Cannot .get() more than once" in str(errors[0])


def test_pending_request_error_response(socket_pair):
    """Test server-side PendingRequest raises RequestError on error reply."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        errors = []

        def server_side():
            server_conn.receive_handshake()
            stream = server_conn.new_stream()
            server_conn.control_stream.send_request(
                {"stream_id": stream.stream_id}
            ).get()
            pending = stream.send_request({"value": 21})
            try:
                pending.get()
            except RequestError as e:
                errors.append(e)

        t = Thread(target=server_side, daemon=True)
        t.start()
        client_conn.send_handshake()

        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        # Client receives server's request and replies with an error
        packet = stream.read_request()
        stream.write_reply_error(
            packet.message_id, error="test error", error_type="TestError"
        )
        t.join(timeout=5)
        assert len(errors) == 1
        assert errors[0].error_type == "TestError"


def test_receive_reply(socket_pair):
    """Test receive_reply returns raw bytes on server side."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        results = []

        def server_side():
            server_conn.receive_handshake()
            stream = server_conn.new_stream()
            server_conn.control_stream.send_request(
                {"stream_id": stream.stream_id}
            ).get()
            packet = stream.write_request(cbor2.dumps({"test": True}))
            result = cbor2.loads(stream.read_reply(packet.message_id).payload)
            results.append(result)

        t = Thread(target=server_side, daemon=True)
        t.start()
        client_conn.send_handshake()

        ctrl_packet = client_conn.control_stream.read_request()
        stream_id = cbor2.loads(ctrl_packet.payload)["stream_id"]
        stream = client_conn.connect_stream(stream_id)
        client_conn.control_stream.write_reply(ctrl_packet.message_id, "Ok")

        packet = stream.read_request()
        stream.write_reply(packet.message_id, 42)
        t.join(timeout=5)
        assert results == [{"result": 42}]


# ---- Duplicate reply ID ----


def test_duplicate_reply_id_raises(socket):
    """Test that getting two replies for same ID raises."""
    with Connection(socket) as conn:
        stream = conn.control_stream

        # Manually inject two reply packets for the same ID
        stream.unprocessed_packets.put(
            Packet(stream_id=0, message_id=1, is_reply=True, payload=b"a")
        )
        stream.unprocessed_packets.put(
            Packet(stream_id=0, message_id=1, is_reply=True, payload=b"b")
        )

        # First one should work
        result = stream.read_reply(1).payload
        assert result == b"a"


def test_duplicate_reply_error(socket):
    """Test that duplicate replies for same ID raises AssertionError."""
    with Connection(socket) as conn:
        stream = conn.control_stream

        # Put a reply in the replies dict directly
        stream.replies[42] = b"first"

        # Now try to process another reply with same ID
        stream.unprocessed_packets.put(
            Packet(stream_id=0, message_id=42, is_reply=True, payload=b"second")
        )

        with pytest.raises(AssertionError):
            stream._Stream__read_one_packet()


# ---- Connection handshake ----


def test_double_handshake_receive_raises(socket_pair):
    """Test that calling receive_handshake twice raises."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):

        def server_side():
            server_conn.receive_handshake()
            with pytest.raises(AssertionError):
                server_conn.receive_handshake()

        t = Thread(target=server_side, daemon=True)
        t.start()
        client_conn.send_handshake()
        t.join(timeout=1)


def test_connect_stream_before_handshake_raises(socket):
    """Test that connect_stream before handshake raises."""
    with (
        Connection(socket) as conn,
        pytest.raises(AssertionError),
    ):
        conn.register_client_stream(1)


def test_connect_stream_already_exists_raises(socket_pair):
    """Test connecting to existing stream raises."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)

        # Connect to stream 0 which already exists (control stream)
        with pytest.raises(AssertionError):
            server_conn.register_client_stream(0)


def test_new_stream_before_handshake_raises(socket):
    """Test that new_stream before handshake raises."""
    with (
        Connection(socket) as conn,
        pytest.raises(AssertionError),
    ):
        conn.new_stream()


def test_bad_handshake_negotiation(socket_pair):
    """Test handshake with bad version string asserts."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):

        def send_bad():
            stream = client_conn.control_stream
            stream.write_request(b"BadVersion")

        t = Thread(target=send_bad, daemon=True)
        t.start()

        with pytest.raises(AssertionError):
            server_conn.receive_handshake()

        t.join(timeout=5)


def test_send_handshake_returns_server_version(socket_pair):
    """Test send_handshake returns the server's protocol version."""
    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        t = Thread(target=server_conn.receive_handshake, daemon=True)
        t.start()

        version = client_conn.send_handshake()
        assert version == PROTOCOL_VERSION

        t.join(timeout=5)


# ---- Connection lifecycle ----


def test_connection_running(socket):
    """Test Connection.running attribute."""
    with Connection(socket) as conn:
        assert conn.running
    assert not conn.running


def test_connection_double_close(socket):
    conn = Connection(socket)
    conn.close()
    conn.close()


def test_shutdown_in_inbox_raises(socket):
    """Test that SHUTDOWN in inbox raises ConnectionError."""
    with Connection(socket) as conn:
        stream = conn.control_stream
        stream.unprocessed_packets.put(SHUTDOWN)
        with pytest.raises(ConnectionError, match="Connection closed"):
            stream.read_request(timeout=0.1)


def test_reader_loop_clean_exit(socket_pair):
    """Test reader loop exits cleanly when running is set to False.

    Tests that the reader loop exits cleanly via the `while self.running`
    condition becoming False (rather than via an exception).
    We wrap the stream unprocessed_packets queue so that after the reader
    puts a packet into it, we set running = False. The reader then loops
    back, checks the condition, and exits cleanly.
    """
    server_socket, client_socket = socket_pair
    server_conn = Connection(server_socket)
    client_conn = ClientConnection(client_socket)

    _do_handshake(server_conn, client_conn)

    ch_client = client_conn.new_stream()
    ch_server = server_conn.register_client_stream(ch_client.stream_id)

    # Replace the queue with a wrapper that sets running = False after put
    real_queue = ch_server.unprocessed_packets

    class StoppingQueue:
        """Queue wrapper that stops the reader after receiving a packet."""

        def put(self, item):
            real_queue.put(item)
            server_conn.running = False

        def get(self, *args, **kwargs):
            return real_queue.get(*args, **kwargs)

        def get_nowait(self):
            return real_queue.get_nowait()

        def empty(self):
            return real_queue.empty()

    ch_server.unprocessed_packets = StoppingQueue()

    # Send a packet — the reader will process it, put it in the queue,
    # which sets running = False, then the reader loops back and exits.
    ch_client.write_request(cbor2.dumps({"test": "data"}))

    # Wait for the reader thread to exit
    time.sleep(0.3)
    # Now clean up
    client_conn.close()
    server_conn._Connection__socket.close()


def test_reader_loop_graceful_exit_on_remote_close(socket_pair):
    """Test reader loop exits gracefully when the remote end closes the connection.

    When the remote socket is closed, read_packet raises ProtocolError.
    The reader loop should catch this and exit without printing to stderr.
    """
    import threading

    server_socket, client_socket = socket_pair
    server_conn = Connection(server_socket)
    client_conn = ClientConnection(client_socket)
    _do_handshake(server_conn, client_conn)

    thread_errors = []
    original_excepthook = threading.excepthook

    def capture_excepthook(args):
        thread_errors.append(args)

    threading.excepthook = capture_excepthook
    try:
        # Close the client side — the server's reader loop should exit gracefully
        client_conn.close()
        server_conn._reader_thread.join(timeout=5)
        assert not server_conn.running
        assert thread_errors == []
    finally:
        threading.excepthook = original_excepthook
        server_conn.close()


def test_stream_close_emits_exactly_one_close_packet(socket_pair):
    """Regression: Stream.close() is check-then-set on self.closed, so N
    concurrent callers could all pass the guard and each emit a CLOSE_STREAM
    packet. The peer must see exactly one.

    The race is between two bytecodes and the GIL scheduler does not reliably
    preempt there, so we force a yield point by swapping in a subclass whose
    ``closed`` is a property that blocks on a Barrier while its value is False.
    """
    import threading as _threading

    from hegel.protocol.packet import CLOSE_STREAM_PAYLOAD
    from hegel.protocol.stream import Stream

    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)
        stream = server_conn.new_stream()

        n_workers = 16
        check_barrier = _threading.Barrier(n_workers)
        closed_box = [False]

        class RaceyStream(Stream):
            @property
            def closed(self):
                val = closed_box[0]
                if not val:
                    try:
                        check_barrier.wait(timeout=2.0)
                    except _threading.BrokenBarrierError:
                        pass
                return val

            @closed.setter
            def closed(self, value):
                closed_box[0] = value

        # Property on the class takes precedence over the instance attribute,
        # but strip the stale instance attribute for clarity.
        stream.__dict__.pop("closed", None)
        stream.__class__ = RaceyStream

        close_packet_count = 0
        count_lock = _threading.Lock()
        orig_write_packet = server_conn.write_packet

        def counting_write_packet(packet):
            nonlocal close_packet_count
            if (
                packet.payload == CLOSE_STREAM_PAYLOAD
                and packet.stream_id == stream.stream_id
            ):
                with count_lock:
                    close_packet_count += 1
            orig_write_packet(packet)

        server_conn.write_packet = counting_write_packet

        def worker():
            stream.close()

        threads = [Thread(target=worker, daemon=True) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert close_packet_count == 1


def test_connection_close_body_runs_once(socket_pair):
    """Regression: Connection.close() is check-then-set on self.running, so N
    concurrent callers could all pass the guard and each enqueue SHUTDOWN on
    every stream. SHUTDOWN must be enqueued exactly once per stream.

    As with Stream.close, we force a yield point by swapping in a subclass
    whose ``running`` is a property that blocks on a Barrier while True.
    """
    import threading as _threading

    server_socket, client_socket = socket_pair
    server_conn = Connection(server_socket)
    client_conn = ClientConnection(client_socket)
    _do_handshake(server_conn, client_conn)
    server_conn.new_stream()

    n_workers = 16
    check_barrier = _threading.Barrier(n_workers)
    running_box = [True]

    class RaceyConnection(Connection):
        @property
        def running(self):
            val = running_box[0]
            if val:
                try:
                    check_barrier.wait(timeout=2.0)
                except _threading.BrokenBarrierError:
                    pass
            return val

        @running.setter
        def running(self, value):
            running_box[0] = value

    server_conn.__dict__.pop("running", None)
    server_conn.__class__ = RaceyConnection

    put_count = 0
    count_lock = _threading.Lock()
    real_q = server_conn.control_stream.unprocessed_packets

    class CountingQueue:
        def put(self, item):
            if item is SHUTDOWN:
                nonlocal put_count
                with count_lock:
                    put_count += 1
            real_q.put(item)

        def get(self, *args, **kwargs):
            return real_q.get(*args, **kwargs)

    server_conn.control_stream.unprocessed_packets = CountingQueue()

    def worker():
        server_conn.close()

    threads = [Thread(target=worker, daemon=True) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    client_conn.close()

    assert put_count == 1


def test_close_tolerates_concurrent_new_stream(socket_pair):
    """Regression: Connection.close() must not raise when a new stream is added
    concurrently (previously iterated ``self.streams`` without the writer lock,
    which could raise 'dictionary changed size during iteration').
    """
    import threading as _threading

    server_socket, client_socket = socket_pair
    server_conn = Connection(server_socket)
    client_conn = ClientConnection(client_socket)
    _do_handshake(server_conn, client_conn)
    # A couple of streams so close() has something to iterate over.
    server_conn.new_stream()
    server_conn.new_stream()

    # Block close()'s iteration inside the body so we have a window in which
    # to mutate the streams dict from another thread.
    first_stream = next(iter(server_conn.streams.values()))
    iter_entered = _threading.Event()
    release_iter = _threading.Event()
    real_queue = first_stream.unprocessed_packets

    class HookedQueue:
        def put(self, item):
            iter_entered.set()
            release_iter.wait(timeout=5.0)
            real_queue.put(item)

        def get(self, *args, **kwargs):
            return real_queue.get(*args, **kwargs)

    first_stream.unprocessed_packets = HookedQueue()

    errors = []

    def closer():
        try:
            server_conn.close()
        except Exception as e:
            errors.append(("close", e))

    def creator():
        iter_entered.wait(timeout=5.0)
        try:
            server_conn.new_stream()
        except Exception as e:
            errors.append(("new_stream", e))
        release_iter.set()

    t_close = Thread(target=closer, daemon=True)
    t_create = Thread(target=creator, daemon=True)
    t_close.start()
    t_create.start()
    t_close.join(timeout=10)
    t_create.join(timeout=10)

    client_conn.close()

    assert errors == []


def test_write_request_concurrent_message_ids_unique(socket_pair):
    """Concurrent write_request on the same stream must produce unique message IDs.

    Regression test: the read-modify-write of ``Stream.next_message_id`` used
    to be non-atomic, so two concurrent calls could both observe the same id
    and emit two packets with the same message_id.
    """
    import threading as _threading

    server_socket, client_socket = socket_pair
    with (
        Connection(server_socket) as server_conn,
        ClientConnection(client_socket) as client_conn,
    ):
        _do_handshake(server_conn, client_conn)
        stream = server_conn.new_stream()

        n_workers = 8
        barrier = _threading.Barrier(n_workers)
        orig_write_packet = server_conn.write_packet

        def patched_write_packet(packet):
            # Force all workers to sit here with their message_id already read
            # from self.next_message_id. Under the bug they all captured the
            # same value; under the fix only one worker can be mid-write at a
            # time, so the first arrival times out the barrier and the rest
            # proceed through the (now broken) barrier immediately.
            try:
                barrier.wait(timeout=0.5)
            except _threading.BrokenBarrierError:
                pass
            orig_write_packet(packet)

        server_conn.write_packet = patched_write_packet

        results = []
        results_lock = _threading.Lock()

        def worker():
            packet = stream.write_request(cbor2.dumps({"n": 1}))
            with results_lock:
                results.append(packet.message_id)

        threads = [Thread(target=worker, daemon=True) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == n_workers
        assert len(set(results)) == n_workers


def test_write_packet_after_close_raises(socket):
    """Test that write_packet raises ConnectionError after close."""
    conn = Connection(socket)
    conn.close()
    with pytest.raises(ConnectionError, match="Connection closed"):
        conn.write_packet(
            Packet(stream_id=0, message_id=1, is_reply=False, payload=b"test")
        )


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
