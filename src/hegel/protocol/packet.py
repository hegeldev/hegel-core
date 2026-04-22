import socket
import struct
import zlib
from dataclasses import dataclass

import trio
import trio.abc

from hegel.protocol.utils import (
    ConnectionClosedError,
    MessageId,
    ProtocolError,
    StreamId,
)

# 5 unsigned 32-bit integers, big-endian:
# magic cookie, checksum, stream, message ID, payload length
PACKET_HEADER_FORMAT = ">5I"
# ASCII for "HEGL"
PACKET_MAGIC = 0x4845474C
PACKET_TERMINATOR = 0x0A  # '\n'
# If this is set in the message id, this packet is a reply to a previous packet
REPLY_BIT = 1 << 31

# Special payload that is sent on a stream when it is shut down. The shutdown
# is not acked and is handled specifially.
# Chosen to be invalid CBOR as per https://www.rfc-editor.org/rfc/rfc8949.html
# It is currently also not the prefix of any valid CBOR (this is a reserved)
# tag byte) but even if it became valid in future this would not be a problem.
CLOSE_STREAM_PAYLOAD = bytes([0b11111110])
CLOSE_STREAM_MESSAGE_ID = MessageId((1 << 31) - 1)


@dataclass(frozen=True, slots=True)
class Packet:
    """
    A logical message in the protocol.

    Packets are the only valid way to send bytes over the wire in the protocol. No "raw"
    bytes are ever sent.

    Wire format:

        0                   1                   2                   3
        0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |                     Magic (0x4845474C)                        |
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |                     Checksum (CRC32)                          |
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |                     Stream id                              |S|
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |R|                   Message id                                |
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |                     Payload length                            |
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        |                     Payload (variable length)                 |
        +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
        | Terminator 0x0A |
        +-+-+-+-+-+-+-+-+-+

    The first five fields comprise the header. Each field is a unsigned 32-bit big-endian
    integer:
    - Magic: The constant 0x4845474C (ASCII for "HEGL").
    - Checksum: CRC32 of the header with the checksum field zeroed, concatenated with
       the payload.
    - Stream id: The logical stream this packet is being sent over. The S (source) bit
       is 1 for streams created by the client, and 0 for streams created by the server.
       The S bit is only part of the protocol to allow both the client and server to
       create streams without coordination.
    - Message id: The id of the message. The R (reply) bit is set if this packet is a reply
       to a previous packet. The message id of a reply packet will be the same as the
       message id of a non-reply packet, but with the R bit set. The message id is
       included in the protocol to support out-of-order replies over the same stream.
    - Payload length: The length of the payload, in bytes.

    The header is followed by the variable-length payload field, and then a single
    terminator byte (0x0A).
    """

    stream_id: StreamId
    message_id: MessageId
    is_reply: bool
    payload: bytes


def _decode_raw_packet(header: bytes, payload: bytes) -> Packet:
    """Shared parsing logic for sync and async packet reading."""
    magic, checksum, stream, message_id, _length = struct.unpack(
        PACKET_HEADER_FORMAT, header
    )
    if magic != PACKET_MAGIC:
        raise ProtocolError(
            f"Bad magic: expected 0x{PACKET_MAGIC:08X}, got 0x{magic:08X}"
        )

    is_reply = (message_id & REPLY_BIT) != 0
    if is_reply:
        message_id ^= REPLY_BIT

    zeroed_header = header[:4] + b"\x00\x00\x00\x00" + header[8:]
    if zlib.crc32(zeroed_header + payload) != checksum:
        raise ProtocolError("Packet checksum mismatch")

    return Packet(
        stream_id=stream,
        message_id=message_id,
        payload=payload,
        is_reply=is_reply,
    )


def _encode_packet(packet: Packet) -> bytes:
    """Serialize a packet to bytes (shared by sync and async writers)."""
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
    return header + packet.payload + bytes([PACKET_TERMINATOR])


# ---------------------------------------------------------------------------
# Synchronous API (used by the test client in tests/client/)
# ---------------------------------------------------------------------------


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

    # checksum is defined as crc(header + payload), where the header's checksum has
    # been zeroed
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
    sock.sendall(_encode_packet(packet))


# ---------------------------------------------------------------------------
# Async API (used by the trio-based server)
# ---------------------------------------------------------------------------


async def aread_exact(stream: trio.abc.ReceiveStream, *, n: int) -> bytes:
    """Read exactly n bytes from a trio receive stream."""
    if n < 0:
        raise ValueError(f"aread_exact: n must be non-negative, got {n}")
    if n == 0:
        return b""

    data = bytearray()
    while len(data) < n:
        chunk = await stream.receive_some(n - len(data))
        if chunk:
            data.extend(chunk)
            continue

        if not data:
            raise ConnectionClosedError("Connection closed")
        raise ProtocolError(
            f"Connection closed during socket read (bytes read so far: {data!r})"
        )
    return bytes(data)


async def aread_packet(stream: trio.abc.ReceiveStream) -> Packet:
    """Read one packet from a trio receive stream."""
    header_size = struct.calcsize(PACKET_HEADER_FORMAT)
    header = await aread_exact(stream, n=header_size)
    _magic, _checksum, _stream_id, _message_id, length = struct.unpack(
        PACKET_HEADER_FORMAT, header
    )
    payload = await aread_exact(stream, n=length)
    terminator_byte = await aread_exact(stream, n=1)
    terminator = terminator_byte[0]
    if terminator != PACKET_TERMINATOR:
        raise ProtocolError(
            f"Bad terminator: expected 0x{PACKET_TERMINATOR:02X}, got 0x{terminator:02X}"
        )
    return _decode_raw_packet(header, payload)


async def awrite_packet(stream: trio.abc.SendStream, packet: Packet) -> None:
    """Write one packet to a trio send stream."""
    await stream.send_all(_encode_packet(packet))


class TrioBufferedReader:
    """Buffered reader that minimises receive_some calls.

    Fills an internal bytearray from the stream in large chunks (default 65536
    bytes), so that subsequent read_exactly calls are served from memory without
    hitting the event loop. This reduces the number of trio checkpoints from ~6
    per packet (header + payload + terminator each requiring their own
    receive_some) to roughly one per 65536 bytes of input.
    """

    _BUFSIZE = 65536

    def __init__(self, stream: trio.abc.ReceiveStream) -> None:
        self._stream = stream
        self._buf = bytearray()

    async def read_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = await self._stream.receive_some(self._BUFSIZE)
            if not chunk:
                if not self._buf:
                    raise ConnectionClosedError("Connection closed")
                raise ProtocolError(
                    f"Connection closed during socket read"
                    f" (bytes read so far: {bytes(self._buf)!r})"
                )
            self._buf.extend(chunk)
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    async def read_packet(self) -> Packet:
        header_size = struct.calcsize(PACKET_HEADER_FORMAT)
        header = await self.read_exactly(header_size)
        _, _, _, _, length = struct.unpack(PACKET_HEADER_FORMAT, header)
        payload = await self.read_exactly(length)
        terminator_byte = await self.read_exactly(1)
        if terminator_byte[0] != PACKET_TERMINATOR:
            raise ProtocolError(
                f"Bad terminator: expected 0x{PACKET_TERMINATOR:02X},"
                f" got 0x{terminator_byte[0]:02X}"
            )
        return _decode_raw_packet(header, payload)
