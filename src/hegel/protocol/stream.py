import contextlib
import math
from collections import deque
from typing import TYPE_CHECKING, Any

import cbor2
import trio

from hegel.protocol.packet import (
    CLOSE_STREAM_MESSAGE_ID,
    CLOSE_STREAM_PAYLOAD,
    Packet,
)
from hegel.protocol.utils import (
    STREAM_TIMEOUT,
    MessageId,
    ProtocolError,
    RequestError,
    StreamId,
)

if TYPE_CHECKING:
    from hegel.protocol.connection import Connection


class Stream:
    """
    A stream organizes packets sent over the protocol. Every packet is attached to a
    single stream, according to that packet's stream_id.

    Streams may be "created" by either the client or the server. There is no explicit
    negotiation to create a new stream. Rather, the client or server simply sends packets
    with a stream_id of the new stream's id. In practice, however, the only place the
    protocol currently allows implicitly creating a new stream is in the run_test command,
    where that packet's stream_id is treated as a new stream created by the client.

    There is always a control stream with id 0, which is used for protocol-level
    communication such as the handshake negotiation.
    """

    def __init__(
        self,
        connection: "Connection",
        stream_id: StreamId,
        role: str | None = None,
    ) -> None:
        if stream_id <= 0 and role != "Control":
            raise ProtocolError(
                f"Stream id must be positive (got {stream_id}), or role must be 'Control'"
            )

        self.connection = connection
        self.stream_id = stream_id
        self.role = role

        self._packet_send, self._packet_receive = trio.open_memory_channel[Any](
            math.inf
        )
        self.requests: deque[Packet] = deque()
        self.replies: dict[MessageId, Packet] = {}
        self._routed_reply_ids: set[MessageId] = set()

        self.next_message_id = MessageId(1)
        self._write_lock = trio.Lock()
        self._close_lock = trio.Lock()
        self.closed = False

    def __repr__(self):
        if self.role is None and self.connection.name is None:
            return f"Stream {self.stream_id}"
        if self.role is None:
            return f"{self.connection.name} stream [id={self.stream_id}]"
        return f"{self.connection.name} stream [id={self.stream_id}] ({self.role})"

    async def close(self) -> None:
        """Close this stream. Writes a close-stream notification packet to the socket."""
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
        with contextlib.suppress(trio.ClosedResourceError):
            await self._packet_send.aclose()
        if self.connection.running:
            await self.connection.write_packet(
                Packet(
                    payload=CLOSE_STREAM_PAYLOAD,
                    message_id=CLOSE_STREAM_MESSAGE_ID,
                    stream_id=self.stream_id,
                    is_reply=False,
                ),
            )

    async def _read_one_packet(self, timeout: float | None = STREAM_TIMEOUT) -> None:
        """Wait for one packet and route it to requests or replies."""
        try:
            if self.closed:
                # Drain any already-queued packets before raising.
                try:
                    packet = self._packet_receive.receive_nowait()
                except trio.WouldBlock:
                    raise ConnectionError(f"{self!r} is closed") from None
            elif timeout is None:
                packet = await self._packet_receive.receive()
            else:
                with trio.move_on_after(timeout) as cancel_scope:
                    packet = await self._packet_receive.receive()
                if cancel_scope.cancelled_caught:
                    raise TimeoutError(
                        f"Timed out after {timeout}s waiting for a message on {self!r}",
                    )
        except trio.EndOfChannel:
            raise ConnectionError("Connection closed") from None

        if packet.is_reply:
            self._routed_reply_ids.discard(packet.message_id)
            self.replies[packet.message_id] = packet
        else:
            self.requests.append(packet)

    async def send_request(self, payload: dict) -> Any:
        """Send a CBOR request and await the decoded reply result."""
        packet = await self.write_request(cbor2.dumps(payload))
        reply = await self.read_reply(packet.message_id)
        result = cbor2.loads(reply.payload)
        if "error" in result:
            raise RequestError(result["error"], error_type=result["type"])
        return result["result"]

    async def write_request(self, payload: bytes) -> Packet:
        """Write a request packet to the socket. Returns the packet."""
        async with self._write_lock:
            packet = Packet(
                payload=payload,
                stream_id=self.stream_id,
                is_reply=False,
                message_id=self.next_message_id,
            )
            await self.connection.write_packet(packet)
            self.next_message_id = MessageId(self.next_message_id + 1)
        return packet

    async def write_reply(self, message_id: MessageId, value: Any) -> None:
        await self.write_reply_bytes(message_id, cbor2.dumps({"result": value}))

    async def write_reply_error(
        self,
        message_id: MessageId,
        error: str,
        error_type: str,
    ) -> None:
        await self.write_reply_bytes(
            message_id, cbor2.dumps({"error": error, "type": error_type})
        )

    async def write_reply_bytes(self, message_id: MessageId, payload: bytes) -> None:
        """Write a reply packet to the socket."""
        await self.connection.write_packet(
            Packet(
                payload=payload,
                stream_id=self.stream_id,
                is_reply=True,
                message_id=message_id,
            ),
        )

    async def read_reply(
        self, message_id: MessageId, *, timeout: float | None = STREAM_TIMEOUT
    ) -> Packet:
        """Wait to receive a reply to ``message_id``, and return it."""
        while message_id not in self.replies:
            await self._read_one_packet(timeout=timeout)
        return self.replies.pop(message_id)

    async def read_request(self, *, timeout: float | None = STREAM_TIMEOUT) -> Packet:
        """Wait to receive a request, and return it."""
        while not self.requests:
            await self._read_one_packet(timeout=timeout)
        return self.requests.popleft()
