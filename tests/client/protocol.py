import contextlib
import socket
import struct
import zlib
from collections import defaultdict, deque
from typing import Any

import cbor2
from cbor2 import CBORTag

from hegel.protocol.connection import HANDSHAKE_STRING
from hegel.protocol.packet import (
    CLOSE_STREAM_MESSAGE_ID,
    CLOSE_STREAM_PAYLOAD,
    PACKET_HEADER_FORMAT,
    PACKET_MAGIC,
    PACKET_TERMINATOR,
    REPLY_BIT,
    Packet,
)
from hegel.protocol.utils import (
    ConnectionClosedError,
    MessageId,
    ProtocolError,
    RequestError,
    StreamId,
)
from hegel.schema import HEGEL_STRING_TAG


def read_exact(sock: socket.socket, *, n: int) -> bytes:
    """Read exactly n bytes from the socket."""
    if n < 0:
        raise ValueError(f"read_exact: n must be non-negative, got {n}")
    if n == 0:
        return b""

    data = bytearray()
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if chunk:
            data.extend(chunk)
            continue

        if not data:
            raise ConnectionClosedError("Connection closed")
        raise ProtocolError(
            f"Connection closed during socket read (bytes read so far: {data!r})"
        )
    return bytes(data)


def read_packet(sock: socket.socket, *, timeout: float | None = None) -> Packet:
    sock.settimeout(timeout)
    header = read_exact(sock, n=struct.calcsize(PACKET_HEADER_FORMAT))
    sock.settimeout(None)
    magic, checksum, stream, message_id, length = struct.unpack(
        PACKET_HEADER_FORMAT, header
    )
    if magic != PACKET_MAGIC:
        raise ProtocolError(
            f"Bad magic: expected 0x{PACKET_MAGIC:08X}, got 0x{magic:08X}"
        )

    is_reply = (message_id & REPLY_BIT) != 0
    if is_reply:
        message_id ^= REPLY_BIT

    payload = read_exact(sock, n=length)
    terminator = read_exact(sock, n=1)[0]
    if terminator != PACKET_TERMINATOR:
        raise ProtocolError(
            f"Bad terminator: expected 0x{PACKET_TERMINATOR:02X}, got 0x{terminator:02X}"
        )

    zeroed_header = header[:4] + b"\x00\x00\x00\x00" + header[8:]
    if zlib.crc32(zeroed_header + payload) != checksum:
        raise ProtocolError("Packet checksum mismatch")

    return Packet(
        stream_id=stream,
        message_id=message_id,
        payload=payload,
        is_reply=is_reply,
    )


def write_packet(sock: socket.socket, packet: Packet) -> None:
    message_id: int = packet.message_id
    if packet.is_reply:
        message_id |= REPLY_BIT
    zeroed_header = struct.pack(
        ">5I", PACKET_MAGIC, 0, packet.stream_id, message_id, len(packet.payload)
    )
    checksum = zlib.crc32(zeroed_header + packet.payload)
    header = struct.pack(
        ">5I",
        PACKET_MAGIC,
        checksum,
        packet.stream_id,
        message_id,
        len(packet.payload),
    )
    sock.sendall(header + packet.payload + bytes([PACKET_TERMINATOR]))


def _decode_hook(_decoder: object, tag: CBORTag) -> object:
    if tag.tag == HEGEL_STRING_TAG:
        return tag.value.decode("utf-8", "surrogatepass")
    return tag


class ClientStream:
    def __init__(
        self,
        connection: "ClientConnection",
        stream_id: StreamId,
    ) -> None:
        self.connection = connection
        self.stream_id = stream_id

        self.requests: deque[Packet] = deque()
        self.replies: dict[MessageId, Packet] = {}

        self.next_message_id = MessageId(1)
        self.closed = False

    def close(self):
        """Close this stream."""
        if self.closed:
            return

        self.closed = True
        if self.connection.running:
            self.connection.write_packet(
                Packet(
                    payload=CLOSE_STREAM_PAYLOAD,
                    message_id=CLOSE_STREAM_MESSAGE_ID,
                    stream_id=self.stream_id,
                    is_reply=False,
                ),
            )

    def _receive_one(self) -> None:
        """Read packets from the socket until one for this stream arrives."""
        packet = self.connection.receive_packet_for_stream(self.stream_id)
        if packet.is_reply:
            assert packet.message_id not in self.replies
            self.replies[packet.message_id] = packet
        else:
            self.requests.append(packet)

    def send_request(self, payload: dict) -> Any:
        """Send a CBOR request and block until reply arrives. Returns the result."""
        packet = self.write_request(cbor2.dumps(payload))
        reply = self.read_reply(packet.message_id)
        result = cbor2.loads(reply.payload, tag_hook=_decode_hook)
        if "error" in result:
            raise RequestError(result["error"], error_type=result["type"])
        return result["result"]

    def write_request(self, payload: bytes) -> Packet:
        """Write a request packet to the socket. Returns the packet."""
        assert isinstance(payload, bytes)
        packet = Packet(
            payload=payload,
            stream_id=self.stream_id,
            is_reply=False,
            message_id=self.next_message_id,
        )
        self.connection.write_packet(packet)
        self.next_message_id = MessageId(self.next_message_id + 1)
        return packet

    def write_reply_bytes(self, message_id: MessageId, payload: bytes) -> None:
        self.connection.write_packet(
            Packet(
                payload=payload,
                stream_id=self.stream_id,
                is_reply=True,
                message_id=message_id,
            ),
        )

    def write_reply(self, message_id: MessageId, value: Any) -> None:
        self.write_reply_bytes(message_id, cbor2.dumps({"result": value}))

    def write_reply_error(
        self,
        message_id: MessageId,
        *,
        error: str,
        error_type: str,
    ) -> None:
        self.write_reply_bytes(
            message_id, cbor2.dumps({"error": error, "type": error_type})
        )

    def read_reply(self, message_id: MessageId) -> Packet:
        """Wait to receive a reply to ``message_id``, and return it."""
        while message_id not in self.replies:
            self._receive_one()
        return self.replies.pop(message_id)

    def read_request(self) -> Packet:
        """Wait to receive a request, and return it."""
        while not self.requests:
            self._receive_one()
        return self.requests.popleft()


class ClientConnection:
    """Client-side multiplexed socket connection to a Hegel server."""

    def __init__(self, socket: socket.socket):
        self.streams: dict[StreamId, ClientStream] = {}
        self.running = True

        self._pending_packets: defaultdict[StreamId, deque[Packet]] = defaultdict(deque)
        self._socket = socket
        self._next_stream_id = 1

        # special stream for connection-level commands
        self.control_stream = self._make_stream(StreamId(0))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def receive_packet_for_stream(self, stream_id: StreamId) -> Packet:
        """Read packets from the socket until one for ``stream_id`` arrives.

        Packets for other streams are stashed in per-stream pending queues.
        Close-stream packets mark the target stream as closed.
        """
        # Check pending first
        pending = self._pending_packets.get(stream_id)
        if pending:
            return pending.popleft()

        # Read from socket until we get one for our stream
        while True:
            try:
                packet = read_packet(self._socket)
            except (OSError, ProtocolError, AssertionError):
                self.running = False
                raise ConnectionError("Connection closed") from None

            if packet.payload == CLOSE_STREAM_PAYLOAD:
                assert packet.message_id == CLOSE_STREAM_MESSAGE_ID
                stream = self.streams[packet.stream_id]
                stream.closed = True
                continue

            if packet.stream_id == stream_id:
                return packet

            # Stash for another stream
            self._pending_packets[packet.stream_id].append(packet)

    def write_packet(self, packet: Packet) -> None:
        """Write a packet to the socket."""
        write_packet(self._socket, packet)

    def close(self) -> None:
        """Close the connection."""
        if not self.running:
            return

        self.running = False
        with contextlib.suppress(OSError):
            self._socket.shutdown(socket.SHUT_RDWR)
        self._socket.close()

    def send_handshake(self) -> str:
        """Initiate handshake as a client. Returns the server protocol version."""
        packet = self.control_stream.write_request(HANDSHAKE_STRING)
        reply = self.control_stream.read_reply(packet.message_id)
        payload = reply.payload.decode("utf-8")
        assert payload.startswith("Hegel/")
        return payload.removeprefix("Hegel/")

    def _make_stream(self, stream_id: StreamId) -> ClientStream:
        """Create and register a stream."""
        stream = ClientStream(connection=self, stream_id=stream_id)
        self.streams[stream.stream_id] = stream
        return stream

    def new_stream(self) -> ClientStream:
        """Create a new logical stream on this connection (odd IDs for client)."""
        stream_id = StreamId((self._next_stream_id << 1) | 1)
        self._next_stream_id += 1
        return self._make_stream(stream_id)

    def connect_stream(self, stream_id: StreamId) -> ClientStream:
        """Connect to a stream created by the server (even IDs)."""
        assert stream_id not in self.streams
        assert stream_id & 1 == 0  # server streams are even
        return self._make_stream(stream_id)
