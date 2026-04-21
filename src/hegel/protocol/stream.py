import sys
from collections import deque
from queue import Empty, SimpleQueue
from threading import Lock
from typing import TYPE_CHECKING, Any

import cbor2

from hegel.protocol.packet import (
    CLOSE_STREAM_MESSAGE_ID,
    CLOSE_STREAM_PAYLOAD,
    Packet,
)
from hegel.protocol.utils import (
    SHUTDOWN,
    STREAM_TIMEOUT,
    MessageId,
    ProtocolError,
    RequestError,
    StreamId,
)

if TYPE_CHECKING:
    from hegel.protocol.connection import Connection


class PendingRequest:
    """Future-like handle for an in-flight request."""

    def __init__(self, stream: "Stream", message_id: MessageId) -> None:
        self.__stream = stream
        self.__message_id = message_id
        self._closed = False

    def get(self) -> Any:
        """Block until reply arrives and return the result."""
        if self._closed:
            raise ValueError("Cannot .get() more than once")
        self._closed = True
        packet = self.__stream.read_reply(self.__message_id)
        payload = cbor2.loads(packet.payload)
        if "error" in payload:
            raise RequestError(payload["error"], error_type=payload["type"])
        return payload["result"]


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

        self.unprocessed_packets: SimpleQueue[Any] = SimpleQueue()
        self.requests: deque[Packet] = deque()
        self.replies: dict[MessageId, Packet] = {}

        self.next_message_id = MessageId(1)
        self._write_lock = Lock()
        self._close_lock = Lock()
        self.closed = False

    def __repr__(self):
        if self.role is None and self.connection.name is None:
            return f"Stream {self.stream_id}"
        if self.role is None:
            return f"{self.connection.name} stream [id={self.stream_id}]"
        return f"{self.connection.name} stream [id={self.stream_id}] ({self.role})"

    def close(self):
        """Close this stream. Writes a close stream notification packet to the socket."""
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        self.unprocessed_packets.put(SHUTDOWN)
        if self.connection.running:
            self.connection.write_packet(
                Packet(
                    payload=CLOSE_STREAM_PAYLOAD,
                    message_id=CLOSE_STREAM_MESSAGE_ID,
                    stream_id=self.stream_id,
                    is_reply=False,
                ),
            )

    def __read_one_packet(self, timeout: float | None = STREAM_TIMEOUT) -> None:
        """Wait for one packet from the reader thread."""
        # When the stream is closed, drain any already-queued packets with a
        # non-blocking get before raising.  The reader thread may have enqueued
        # packets *before* setting self.closed (from a peer close notification),
        # and those packets must still be consumed.
        try:
            packet = self.unprocessed_packets.get(timeout=0 if self.closed else timeout)
        except Empty:
            if self.closed:
                raise ConnectionError(f"{self!r} is closed") from None
            raise TimeoutError(
                f"Timed out after {timeout}s waiting for a message on {self!r}",
            ) from None
        if packet is SHUTDOWN:
            raise ConnectionError("Connection closed")

        if packet.is_reply:
            if packet.message_id in self.replies:
                print(
                    f"Duplicate reply for message_id {packet.message_id} on {self!r}",
                    file=sys.stderr,
                )
            self.replies[packet.message_id] = packet
        else:
            self.requests.append(packet)

    def send_request(self, payload: dict) -> PendingRequest:
        """Send a CBOR request and return a future for the reply."""
        packet = self.write_request(cbor2.dumps(payload))
        return PendingRequest(self, packet.message_id)

    def handle_requests(self, handler, *, until=lambda: False):
        """Process incoming requests until `until` is met."""
        while not until():
            packet = self.read_request(timeout=STREAM_TIMEOUT)
            message = cbor2.loads(packet.payload)
            try:
                result = handler(message)
            except BaseException as e:
                self.write_reply_error(
                    packet.message_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                if not isinstance(e, Exception):
                    raise
                continue
            self.write_reply(packet.message_id, result)

    def write_request(self, payload: bytes) -> Packet:
        """Write a request packet to the socket. Returns the packet."""
        with self._write_lock:
            packet = Packet(
                payload=payload,
                stream_id=self.stream_id,
                is_reply=False,
                message_id=self.next_message_id,
            )
            self.connection.write_packet(packet)
            self.next_message_id = MessageId(self.next_message_id + 1)
        return packet

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

    def write_reply_bytes(self, message_id: MessageId, payload: bytes) -> None:
        """Write a reply packet to the socket."""
        self.connection.write_packet(
            Packet(
                payload=payload,
                stream_id=self.stream_id,
                is_reply=True,
                message_id=message_id,
            ),
        )

    def read_reply(
        self, message_id: MessageId, *, timeout: float | None = STREAM_TIMEOUT
    ) -> Packet:
        """Wait to receive a reply to ``message_id``, and return it."""
        while message_id not in self.replies:
            self.__read_one_packet(timeout=timeout)
        return self.replies.pop(message_id)

    def read_request(self, *, timeout: float | None = STREAM_TIMEOUT) -> Packet:
        """Wait to receive a request, and return it."""
        while not self.requests:
            self.__read_one_packet(timeout=timeout)
        return self.requests.popleft()
