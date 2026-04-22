import socket
import struct
import zlib

import pytest
import trio
import trio.socket
from hypothesis import given, strategies as st

from hegel.protocol import Packet, ProtocolError
from hegel.protocol.packet import (
    PACKET_HEADER_FORMAT,
    PACKET_MAGIC,
    PACKET_TERMINATOR,
    TrioBufferedReader,
    aread_exact,
    aread_packet,
)
from hegel.protocol.utils import ConnectionClosedError
from tests.client.protocol import read_exact, read_packet, write_packet


def packets():
    return st.builds(
        Packet,
        message_id=st.integers(0, 1 << 31 - 1),
        stream_id=st.integers(0, 1 << 32 - 1),
    )


def _make_packet(
    *,
    magic=PACKET_MAGIC,
    checksum=None,
    stream_id=0,
    message_id=1,
    payload=b"payload",
    terminator=PACKET_TERMINATOR,
):
    length = len(payload)
    header_for_check = struct.pack(
        PACKET_HEADER_FORMAT, magic, 0, stream_id, message_id, length
    )
    if checksum is None:
        checksum = zlib.crc32(header_for_check + payload) & 0xFFFFFFFF
    header = struct.pack(
        PACKET_HEADER_FORMAT, magic, checksum, stream_id, message_id, length
    )
    return header + payload + bytes([terminator])


@given(packets())
def test_packet_roundtrip(packet):
    reader, writer = socket.socketpair()
    try:
        write_packet(writer, packet)
        assert read_packet(reader) == packet
    finally:
        reader.close()
        writer.close()


def test_read_exact_connection_closed_with_partial_data(socket_pair):
    reader, writer = socket_pair
    writer.sendall(b"abc")
    writer.close()
    with pytest.raises(ProtocolError, match="Connection closed during socket read"):
        read_exact(reader, n=10)


def test_read_exact_connection_closed_no_data(socket_pair):
    reader, writer = socket_pair
    writer.close()
    with pytest.raises(ConnectionClosedError, match="Connection closed"):
        read_exact(reader, n=10)


def test_read_packet_invalid_magic(socket_pair):
    reader, writer = socket_pair
    raw = _make_packet(magic=0xDEADBEEF)
    writer.sendall(raw)
    with pytest.raises(ProtocolError, match="Bad magic"):
        read_packet(reader)


def test_read_packet_invalid_terminator(socket_pair):
    reader, writer = socket_pair
    raw = _make_packet(terminator=0xFF)
    writer.sendall(raw)
    with pytest.raises(ProtocolError, match="Bad terminator"):
        read_packet(reader)


def test_read_packet_bad_checksum(socket_pair):
    reader, writer = socket_pair
    raw = _make_packet(checksum=0x12345678)
    writer.sendall(raw)
    with pytest.raises(ProtocolError, match="checksum mismatch"):
        read_packet(reader)


def test_read_exact_negative_n(socket_pair):
    reader, _ = socket_pair
    with pytest.raises(ValueError, match="non-negative"):
        read_exact(reader, n=-1)


def test_aread_exact_negative_n():
    async def run():
        r, w = trio.socket.socketpair()
        async with trio.SocketStream(r) as stream:
            w.close()
            with pytest.raises(ValueError, match="non-negative"):
                await aread_exact(stream, n=-1)

    trio.run(run)


def test_aread_exact_zero_n():
    async def run():
        r, w = trio.socket.socketpair()
        async with trio.SocketStream(r) as stream:
            w.close()
            result = await aread_exact(stream, n=0)
            assert result == b""

    trio.run(run)


def test_aread_exact_partial_data_then_close():
    async def run():
        r, w = trio.socket.socketpair()
        async with trio.SocketStream(r) as stream:
            await trio.SocketStream(w).send_all(b"hello")
            await trio.SocketStream(w).aclose()
            with pytest.raises(ProtocolError, match="bytes read so far"):
                await aread_exact(stream, n=10)

    trio.run(run)


def test_aread_packet_bad_magic():
    async def run():
        r, w = trio.socket.socketpair()
        raw = _make_packet(magic=0xDEADBEEF)
        await trio.SocketStream(w).send_all(raw)
        async with trio.SocketStream(r) as stream:
            with pytest.raises(ProtocolError, match="Bad magic"):
                await aread_packet(stream)

    trio.run(run)


def test_aread_packet_bad_checksum():
    async def run():
        r, w = trio.socket.socketpair()
        raw = _make_packet(checksum=0x12345678)
        await trio.SocketStream(w).send_all(raw)
        async with trio.SocketStream(r) as stream:
            with pytest.raises(ProtocolError, match="checksum mismatch"):
                await aread_packet(stream)

    trio.run(run)


def test_aread_packet_bad_terminator():
    async def run():
        r, w = trio.socket.socketpair()
        raw = _make_packet(terminator=0xFF)
        await trio.SocketStream(w).send_all(raw)
        async with trio.SocketStream(r) as stream:
            with pytest.raises(ProtocolError, match="Bad terminator"):
                await aread_packet(stream)

    trio.run(run)


def test_aread_exact_connection_closed_no_data():
    async def run():
        r, w = trio.socket.socketpair()
        async with trio.SocketStream(r) as stream:
            await trio.SocketStream(w).aclose()
            with pytest.raises(ConnectionClosedError, match="Connection closed"):
                await aread_exact(stream, n=10)

    trio.run(run)


def test_trio_buffered_reader_partial_data_then_close():
    async def run():
        r, w = trio.socket.socketpair()
        async with trio.SocketStream(r) as stream:
            reader = TrioBufferedReader(stream)
            await trio.SocketStream(w).send_all(b"partial")
            await trio.SocketStream(w).aclose()
            with pytest.raises(ProtocolError, match="bytes read so far"):
                await reader.read_exactly(100)

    trio.run(run)


def test_trio_buffered_reader_bad_terminator():
    async def run():
        r, w = trio.socket.socketpair()
        raw = _make_packet(terminator=0xFF)
        await trio.SocketStream(w).send_all(raw)
        await trio.SocketStream(w).aclose()
        async with trio.SocketStream(r) as stream:
            reader = TrioBufferedReader(stream)
            with pytest.raises(ProtocolError, match="Bad terminator"):
                await reader.read_packet()

    trio.run(run)
