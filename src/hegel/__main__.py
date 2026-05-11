import contextlib
import importlib.metadata
import os
import sys

import click
from hypothesis import Verbosity
from hypothesis.configuration import set_hypothesis_home_dir

from hegel.protocol.connection import Connection
from hegel.server import run_server_on_connection
from hegel.test_server import run_test_server


class StdioTransport:
    """Transport that uses stdin/stdout for protocol communication.

    Provides the same interface as a socket (recv, sendall, settimeout, close)
    so it can be used transparently with the existing packet read/write code.
    """

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    def recv(self, n):
        data = self._reader.read(n)
        if data is None:
            return b""
        return data

    def sendall(self, data):
        try:
            self._writer.write(data)
            self._writer.flush()
        except ValueError as e:
            # BufferedWriter raises ValueError("I/O operation on closed file")
            # but the protocol layer only catches OSError.
            print(f"StdioTransport write failed: {e}", file=sys.stderr)
            raise OSError(str(e)) from e

    def settimeout(self, timeout):
        pass  # No timeout support for stdio

    def shutdown(self, how):
        pass  # No-op for stdio; closing the fds is sufficient

    def close(self):
        with contextlib.suppress(OSError):
            self._writer.close()
        with contextlib.suppress(OSError):
            self._reader.close()


@click.command()
@click.version_option(
    version=importlib.metadata.version("hegel-core"),
    message="hegel (version %(version)s)",
)
@click.option(
    "--verbosity",
    type=click.Choice(["quiet", "normal", "verbose", "debug"]),
    default="normal",
    help="Verbosity level. Corresponds to hypothesis.Verbosity.",
)
def main(verbosity):
    """Run the Hegel test server, communicating over stdin/stdout."""
    run_server_stdio(verbosity=Verbosity(verbosity))


def run_server_stdio(*, verbosity: Verbosity = Verbosity.normal) -> None:
    if verbosity >= Verbosity.debug:
        os.environ["HEGEL_PROTOCOL_DEBUG"] = "1"

    set_hypothesis_home_dir(".hegel")

    # Capture the real stdout for protocol I/O before redirecting.
    sys.stdout.flush()
    protocol_out_fd = os.dup(1)
    protocol_in_fd = os.dup(0)

    # Redirect fd 1 to stderr so any writes to fd 1 (including from C
    # extensions) go to stderr instead of contaminating the protocol stream.
    os.dup2(2, 1)
    # Also redirect Python-level sys.stdout to stderr.
    sys.stdout = sys.stderr

    # Keep stdin unbuffered so close() cannot block on a BufferedReader lock
    # held by the background protocol reader thread.
    protocol_reader = os.fdopen(protocol_in_fd, "rb", buffering=0)
    protocol_writer = os.fdopen(protocol_out_fd, "wb", buffering=0)

    if verbosity >= Verbosity.verbose:
        print("Running in stdio mode", file=sys.stderr)

    transport = StdioTransport(protocol_reader, protocol_writer)
    connection = Connection(transport, name="Server")

    try:
        test_mode = os.environ.get("HEGEL_PROTOCOL_TEST_MODE")
        if test_mode:
            run_test_server(connection, test_mode)
        else:
            run_server_on_connection(connection)

        if verbosity >= Verbosity.verbose:
            print("Client disconnected", file=sys.stderr)
    finally:
        connection.close()


if __name__ == "__main__":  # pragma: no cover
    main()
