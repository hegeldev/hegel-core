import contextlib
import os
import socket
import sys
from threading import Lock, Thread
from typing import TYPE_CHECKING, Any

import cbor2

from hegel.protocol.packet import (
    CLOSE_STREAM_MESSAGE_ID,
    CLOSE_STREAM_PAYLOAD,
    Packet,
    read_packet,
    write_packet,
)
from hegel.protocol.utils import (
    SHUTDOWN,
    ConnectionClosedError,
    ProtocolError,
    StreamId,
)

if TYPE_CHECKING:
    from hegel.protocol.stream import Stream

PROTOCOL_VERSION = "0.10"
HANDSHAKE_STRING = b"hegel_handshake_start"


def _is_protocol_debug():
    value = os.environ.get("HEGEL_PROTOCOL_DEBUG")
    value = value.lower() if value is not None else None
    if value not in {
        None,
        "1",
        "0",
        "true",
        "false",
    }:  # pragma: no cover # tested in subprocess
        raise ValueError(
            "invalid value for HEGEL_PROTOCOL_DEBUG: expected either '1', '0', 'true', "
            f"'false', or unset, but got {value!r}"
        )
    return value in {"1", "true"}


class Connection:
    """
    The server-side half of the Hegel wire protocol. The other half is the client, and
    is intended to be a Hegel library like hegel-rust.

    The intended use is for a single connection to be used for the entire test suite.
    A connection can be used simultaneously by multiple tests.

    At the lowest level, the protocol is bytes moving across the transport layer. The
    transport layer is currently unix sockets, though this may change to support windows.
    Bytes sent over the socket always consist of logical packets (see the Packet class).
    Packets on the protocol have a stream_id, which logically organizes packets. See the
    Stream class for details.

    Protocol
    --------

    A high-level description of the full protocol between a server and a client.

    Handshake
    ~~~~~~~~~

    The protocol between a server and a client starts with a handshake:

    - The client sends a packet on the control stream with payload
      b"hegel_handshake_start"
    - The server responds with a packet on the control stream with payload
      b"Hegel/{PROTOCOL_VERSION}"

    Test case lifetime
    ~~~~~~~~~~~~~~~~~~

    After the handshake, the lifetime of a test on the protocol is:

    - The client sends a {"command": "run_test"} cbor packet on the control
      stream. The payload includes a stream_id C1 and various test settings.
    - The server responds with a reply packet containing the cbor payload True.
    - We now start sending and executing test cases. The server sends a
      {"event": "test_case", "stream_id": C2} cbor packet on stream C1.
      C2 is conceptually the stream for this specific test case.
    - The client sends a {"command": ...} cbor packet, typically "generate",
      on C2. The server responds with an appropriate cbor packet, typically the result
      of drawing from the requested schema.
    - The client repeats until it sends a {"command": "mark_complete"} cbor packet,
      at which point the server breaks out of its listening loop.
    - The server sends a {"event": "test_done", "results": ...} cbor packet on C1.
    - For any test cases which were marked complete with status "interesting", the
      server repeats the test case loop, but now with the {"event": "test_case"} cbor
      packet including `"is_final": True`.
    """

    def __init__(
        self,
        socket: Any,
        *,
        name: str | None = None,
        debug: bool | None = None,
    ):
        self.name = name
        self._debug = _is_protocol_debug() if debug is None else debug

        self.streams: dict[StreamId, Stream] = {}
        self.running = True

        self.__writer_lock = Lock()
        self.__close_lock = Lock()
        self.__socket = socket
        self.__next_stream_id = 1
        self._handshake_done = False

        # special stream for connection-level commands
        self.control_stream = self._make_stream(StreamId(0), role="Control")

        self._reader_thread = Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _debug_print(self, *args):
        if not self._debug:
            return

        print(*args, file=sys.stderr)

    def _debug_packet(self, packet: Packet, *, direction: str) -> None:
        if not self._debug:
            return

        try:
            payload_repr: Any = packet.payload.decode("ascii")
        except UnicodeDecodeError:
            try:
                payload_repr = cbor2.loads(packet.payload)
            except Exception:
                payload_repr = packet.payload

        stream = self.streams.get(packet.stream_id)
        stream_label = (
            str(stream)
            if stream is not None
            else f"<unknown stream {packet.stream_id}>"
        )
        self._debug_print(
            f"[{self.name or '?'}] {direction} ch={stream_label}"
            f" message_id={packet.message_id}"
            f" {'reply' if packet.is_reply else 'request'}: {payload_repr!r}",
        )

    def close(self) -> None:
        """Close the connection and clean up resources."""
        with self.__close_lock:
            if not self.running:
                return
            self.running = False

        # Hold the writer lock while closing the socket so that no
        # write_packet call can be in flight when the fd is closed.
        with self.__writer_lock:
            with contextlib.suppress(OSError):
                self.__socket.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self.__socket.close()
            streams = list(self.streams.values())
        for v in streams:
            if not v.closed:
                v.unprocessed_packets.put(SHUTDOWN)

    def _reader_loop(self) -> None:
        try:
            while self.running:
                packet = read_packet(self.__socket)

                stream = self.streams.get(packet.stream_id)
                if stream is None:
                    self._debug_print(
                        f"Received packet for unknown stream {packet.stream_id}"
                    )
                    self._send_error_reply(
                        packet, f"stream {packet.stream_id} is not registered"
                    )
                    continue

                self._debug_packet(packet, direction="RECEIVE")
                if packet.payload == CLOSE_STREAM_PAYLOAD:
                    if packet.message_id != CLOSE_STREAM_MESSAGE_ID:
                        self._debug_print(
                            f"Ignoring close packet with wrong message_id"
                            f" {packet.message_id} for {stream}"
                        )
                        continue
                    self._debug_print(f"Received close for {stream}")
                    stream.closed = True
                    stream.unprocessed_packets.put(SHUTDOWN)
                else:
                    if stream.closed:
                        self._debug_print(f"Received packet for closed stream {stream}")
                        self._send_error_reply(packet, f"stream {stream} is closed")
                        continue
                    stream.unprocessed_packets.put(packet)
        except (ConnectionClosedError, OSError) as exc:
            if self.running:
                print(
                    f"Reader loop exiting: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
        finally:
            if self.running:
                self.close()

    def _send_error_reply(self, packet: Packet, message: str) -> None:
        """Send an error reply for a request that can't be delivered.

        Only sends a reply for request packets (not replies). If the write
        fails (e.g. connection is closing), the error is silently ignored.
        """
        if packet.is_reply:
            return
        try:
            error_payload = cbor2.dumps({"error": message, "type": "ProtocolError"})
            self.write_packet(
                Packet(
                    stream_id=packet.stream_id,
                    message_id=packet.message_id,
                    is_reply=True,
                    payload=error_payload,
                )
            )
        except (OSError, ConnectionClosedError):
            pass

    def write_packet(self, packet: Packet) -> None:
        with self.__writer_lock:
            if not self.running:
                raise ConnectionError("Connection closed")
            self._debug_packet(packet, direction="SEND")
            write_packet(self.__socket, packet)

    def receive_handshake(self):
        if self._handshake_done:
            raise ProtocolError("Handshake already completed")

        packet = self.control_stream.read_request()
        if packet.payload != HANDSHAKE_STRING:
            raise ProtocolError(
                f"Bad handshake: expected {HANDSHAKE_STRING!r}, got {packet.payload!r}"
            )
        # we expect the payload to be pure ASCII. ASCII and utf-8 overlap, so passing
        # "ascii" as the encoding is equivalent in the standard case, but gives us a
        # fail-fast error otherwise.
        self.control_stream.write_reply_bytes(
            packet.message_id, f"Hegel/{PROTOCOL_VERSION}".encode("ascii")
        )
        self._handshake_done = True

    def _make_stream(self, stream_id: StreamId, *, role: str | None = None) -> "Stream":
        """Create and register a stream."""
        from hegel.protocol.stream import Stream

        stream = Stream(connection=self, stream_id=stream_id, role=role)
        with self.__writer_lock:
            if stream.stream_id in self.streams:
                raise ProtocolError(f"Stream {stream.stream_id} is already registered")
            self.streams[stream.stream_id] = stream
        return stream

    def new_stream(self, *, role: str | None = None) -> "Stream":
        if not self._handshake_done:
            raise ProtocolError("Cannot create streams before handshake")
        # server streams get even ids
        with self.__writer_lock:
            stream_id = StreamId(self.__next_stream_id << 1)
            self.__next_stream_id += 1
        return self._make_stream(stream_id, role=role)

    def register_client_stream(
        self, stream_id: StreamId, *, role: str | None = None
    ) -> "Stream":
        """
        Register a new stream created by a client.

        Because both a client and a server may create a stream in the protocol, this
        method lets the server create the logical Stream object required to store packets
        sent over that stream.

        In practice, once a stream is made, no distinction is made between it having
        been created by the client or the server. This method's name explicitly mentions
        the client origin for protocol hygiene, not because it has a fundamental impact.
        """
        if not self._handshake_done:
            raise ProtocolError("Cannot register streams before handshake")
        if stream_id in self.streams:
            raise ProtocolError(f"Stream {stream_id} is already registered")
        if stream_id & 1 != 1:
            raise ProtocolError(f"Client stream id must be odd, got {stream_id}")
        return self._make_stream(stream_id, role=role)
