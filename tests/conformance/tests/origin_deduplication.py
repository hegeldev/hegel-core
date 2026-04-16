#!/usr/bin/env python3
"""Reference conformance binary for origin deduplication tests.

Spawns the hegel server in --stdio mode and runs tests where failures
should deduplicate to a single interesting example when the origin is
correctly formatted. Reports interesting_test_cases from the server's
test_done response.

Two modes:
- value_in_error_message: the test fails with the generated value in the
  error message. A correct origin (exc_type + innermost file:line) will
  deduplicate all failures to 1.
- multiple_call_sites: the same buggy function is called from multiple
  code paths. A correct origin (using the innermost frame) will
  deduplicate to 1.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict, deque

import cbor2

from hegel.protocol.connection import HANDSHAKE_STRING
from hegel.protocol.packet import (
    CLOSE_STREAM_MESSAGE_ID,
    CLOSE_STREAM_PAYLOAD,
    Packet,
    read_packet,
    write_packet,
)


class _PipeTransport:
    """Wraps subprocess pipes to look like a socket for read_packet/write_packet."""

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    def recv(self, n):
        data = self._reader.read(n)
        if not data:
            return b""
        return data

    def sendall(self, data):
        self._writer.write(data)
        self._writer.flush()

    def settimeout(self, timeout):
        pass


class _Client:
    """Minimal hegel client for conformance testing."""

    def __init__(self, transport):
        self._transport = transport
        self._next_msg_id = defaultdict(lambda: 1)
        self._pending = defaultdict(deque)
        self._closed_streams = set()

    def handshake(self):
        msg_id = self._write_raw(0, HANDSHAKE_STRING)
        reply = self._read_reply(0, msg_id)
        assert reply.payload.decode("utf-8").startswith("Hegel/")

    def send_request(self, stream_id, payload_dict):
        """Send a CBOR request and wait for the reply. Returns the result."""
        msg_id = self._write_raw(stream_id, cbor2.dumps(payload_dict))
        reply = self._read_reply(stream_id, msg_id)
        result = cbor2.loads(reply.payload)
        return result["result"]

    def write_request(self, stream_id, payload_dict):
        """Send a CBOR request without waiting for the reply."""
        self._write_raw(stream_id, cbor2.dumps(payload_dict))

    def read_request(self, stream_id):
        """Read the next request packet for stream_id."""
        for i, p in enumerate(self._pending[stream_id]):
            if not p.is_reply:
                del self._pending[stream_id][i]
                return p
        while True:
            packet = self._read_any()
            if packet is None:
                continue
            if packet.stream_id == stream_id and not packet.is_reply:
                return packet
            self._pending[packet.stream_id].append(packet)

    def write_reply(self, stream_id, msg_id, value):
        write_packet(
            self._transport,
            Packet(
                payload=cbor2.dumps({"result": value}),
                stream_id=stream_id,
                is_reply=True,
                message_id=msg_id,
            ),
        )

    def close_stream(self, stream_id):
        self._closed_streams.add(stream_id)
        write_packet(
            self._transport,
            Packet(
                payload=CLOSE_STREAM_PAYLOAD,
                stream_id=stream_id,
                is_reply=False,
                message_id=CLOSE_STREAM_MESSAGE_ID,
            ),
        )

    def _write_raw(self, stream_id, payload_bytes):
        msg_id = self._next_msg_id[stream_id]
        self._next_msg_id[stream_id] += 1
        write_packet(
            self._transport,
            Packet(
                payload=payload_bytes,
                stream_id=stream_id,
                is_reply=False,
                message_id=msg_id,
            ),
        )
        return msg_id

    def _read_any(self):
        """Read one packet, skipping close-stream and closed-stream packets."""
        packet = read_packet(self._transport)
        if packet.payload == CLOSE_STREAM_PAYLOAD:
            return None
        if packet.stream_id in self._closed_streams:
            return None
        return packet

    def _read_reply(self, stream_id, msg_id):
        """Read packets until we get the reply with the given msg_id."""
        for i, p in enumerate(self._pending[stream_id]):
            if p.is_reply and p.message_id == msg_id:
                del self._pending[stream_id][i]
                return p
        while True:
            packet = self._read_any()
            if packet is None:
                continue
            if (
                packet.stream_id == stream_id
                and packet.is_reply
                and packet.message_id == msg_id
            ):
                return packet
            self._pending[packet.stream_id].append(packet)


def _extract_origin(exc, tb):
    """Extract origin: exc_type + innermost file:line.

    This is the correct implementation: it does NOT include the error
    message (which may contain generated values) or the full stack trace
    (which varies by call site).
    """
    filename = ""
    lineno = 0
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        filename = tb.tb_frame.f_code.co_filename
        lineno = tb.tb_lineno
    return f"{type(exc).__name__} at {filename}:{lineno}"


def _buggy_function(x):
    """A function with a single bug, always at the same location."""
    assert x <= 10


def _call_path_a(x):
    _buggy_function(x)


def _call_path_b(x):
    _buggy_function(x)


def _run_test_case(client, tc_stream_id, mode):
    """Handle one test case: generate a value, run the test, mark_complete."""
    value = client.send_request(
        tc_stream_id,
        {
            "command": "generate",
            "schema": {"type": "integer", "min_value": 0, "max_value": 100},
        },
    )

    status = "VALID"
    origin = None
    try:
        if mode == "value_in_error_message":
            assert value <= 10, f"Generated value {value} exceeded threshold 10"
        elif mode == "multiple_call_sites":
            if value % 2 == 0:
                _call_path_a(value)
            else:
                _call_path_b(value)
    except AssertionError as e:
        status = "INTERESTING"
        origin = _extract_origin(e, e.__traceback__)

    # Fire-and-forget: mark_complete triggers StopTest on the server,
    # so the reply is an error. Don't wait for it.
    client.write_request(
        tc_stream_id,
        {"command": "mark_complete", "status": status, "origin": origin},
    )
    client.close_stream(tc_stream_id)


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])
    mode = params["mode"]

    # Don't pass conformance-specific env vars to the server subprocess.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CONFORMANCE_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "hegel", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        transport = _PipeTransport(proc.stdout, proc.stdin)
        client = _Client(transport)
        client.handshake()

        test_stream_id = 3  # odd = client-created
        result = client.send_request(
            0,
            {
                "command": "run_test",
                "stream_id": test_stream_id,
                "test_cases": test_cases,
                "seed": 42,
            },
        )
        assert result is True

        # Handle test cases until test_done
        while True:
            packet = client.read_request(test_stream_id)
            message = cbor2.loads(packet.payload)

            if message["event"] == "test_done":
                client.write_reply(test_stream_id, packet.message_id, True)
                results = message["results"]
                break

            tc_stream_id = message["stream_id"]
            client.write_reply(test_stream_id, packet.message_id, None)
            _run_test_case(client, tc_stream_id, mode)

        # Handle final replays for interesting examples
        interesting_count = results["interesting_test_cases"]
        for _ in range(interesting_count):
            packet = client.read_request(test_stream_id)
            message = cbor2.loads(packet.payload)
            tc_stream_id = message["stream_id"]
            client.write_reply(test_stream_id, packet.message_id, None)
            _run_test_case(client, tc_stream_id, mode)

        with open(metrics_file, "a") as f:
            f.write(json.dumps({"interesting_test_cases": interesting_count}) + "\n")

    finally:
        proc.stdin.close()
        proc.wait()


if __name__ == "__main__":
    main()
