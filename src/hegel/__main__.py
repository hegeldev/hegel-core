import contextlib
import importlib.metadata
import os
import socket
import sys
from pathlib import Path

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
@click.argument("socket_path", required=False, default=None)
@click.option(
    "--stdio",
    is_flag=True,
    default=False,
    help="Use stdin/stdout for protocol communication instead of a Unix socket.",
)
@click.option(
    "--verbosity",
    type=click.Choice(["quiet", "normal", "verbose", "debug"]),
    default="normal",
    help="Verbosity level. Corresponds to hypothesis.Verbosity.",
)
def main(socket_path, stdio, verbosity):
    """Run the Hegel test server, binding to socket_path."""
    verbosity = Verbosity(verbosity)

    if stdio:
        if socket_path is not None:
            raise click.UsageError("Cannot specify a socket path with --stdio.")
        run_server_stdio(verbosity=verbosity)
    else:
        if socket_path is None:
            raise click.UsageError("Socket path is required when not using --stdio.")
        socket_path = Path(socket_path)

        # Clean up any existing socket before starting
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()

        run_server(socket_path, verbosity=verbosity)


def run_server(socket_path: Path, *, verbosity: Verbosity = Verbosity.normal) -> None:
    if verbosity >= Verbosity.debug:
        os.environ["HEGEL_PROTOCOL_DEBUG"] = "1"

    set_hypothesis_home_dir(".hegel")

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(socket_path))
    server_sock.listen(1)

    if verbosity >= Verbosity.verbose:
        print(f"Listening on {socket_path}", file=sys.stderr)

    try:
        client_sock, _ = server_sock.accept()

        if verbosity >= Verbosity.verbose:
            print("Client connected", file=sys.stderr)

        connection = Connection(client_sock, name="Server")
        test_mode = os.environ.get("HEGEL_PROTOCOL_TEST_MODE")
        if test_mode:
            run_test_server(connection, test_mode)
        else:
            run_server_on_connection(connection)

        if verbosity >= Verbosity.verbose:
            print("Client disconnected", file=sys.stderr)

    finally:
        server_sock.close()


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

    protocol_reader = os.fdopen(protocol_in_fd, "rb")
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
