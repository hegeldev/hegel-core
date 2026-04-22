import contextlib
import importlib.metadata
import os
import sys
from pathlib import Path

import click
import trio
import trio.socket
from hypothesis import Verbosity
from hypothesis.configuration import set_hypothesis_home_dir

from hegel.protocol.connection import Connection
from hegel.server import run_server_on_connection
from hegel.test_server import run_test_server


class _StdioStream(trio.abc.Stream):
    """Wrap stdin/stdout file descriptors as a trio bidirectional stream."""

    def __init__(self, read_fd: int, write_fd: int):
        self._read = trio.lowlevel.FdStream(read_fd)
        self._write = trio.lowlevel.FdStream(write_fd)

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        return await self._read.receive_some(max_bytes)

    async def send_all(self, data: bytes) -> None:
        await self._write.send_all(data)

    async def wait_send_all_might_not_block(self) -> None:  # pragma: no cover
        await self._write.wait_send_all_might_not_block()

    async def send_eof(self) -> None:  # pragma: no cover
        await self._write.aclose()

    async def aclose(self) -> None:
        with contextlib.suppress(OSError):
            await self._read.aclose()
        with contextlib.suppress(OSError):
            await self._write.aclose()


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
        trio.run(run_server_stdio, verbosity)
    else:
        if socket_path is None:
            raise click.UsageError("Socket path is required when not using --stdio.")
        socket_path = Path(socket_path)

        # Clean up any existing socket before starting
        with contextlib.suppress(FileNotFoundError):
            socket_path.unlink()

        trio.run(run_server, socket_path, verbosity)


async def run_server(
    socket_path: Path, verbosity: Verbosity = Verbosity.normal
) -> None:
    if verbosity >= Verbosity.debug:
        os.environ["HEGEL_PROTOCOL_DEBUG"] = "1"

    set_hypothesis_home_dir(".hegel")

    server_sock = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
    await server_sock.bind(str(socket_path))
    server_sock.listen(1)

    if verbosity >= Verbosity.verbose:
        print(f"Listening on {socket_path}", file=sys.stderr)

    try:
        client_sock, _ = await server_sock.accept()

        if verbosity >= Verbosity.verbose:
            print("Client connected", file=sys.stderr)

        stream = trio.SocketStream(client_sock)
        test_mode = os.environ.get("HEGEL_PROTOCOL_TEST_MODE")
        async with trio.open_nursery() as nursery:
            connection = Connection(stream, nursery=nursery, name="Server")
            if test_mode:
                await run_test_server(connection, test_mode)
            else:
                await run_server_on_connection(connection)

        if verbosity >= Verbosity.verbose:
            print("Client disconnected", file=sys.stderr)

    finally:
        server_sock.close()


async def run_server_stdio(verbosity: Verbosity = Verbosity.normal) -> None:
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

    if verbosity >= Verbosity.verbose:
        print("Running in stdio mode", file=sys.stderr)

    stream = _StdioStream(protocol_in_fd, protocol_out_fd)
    test_mode = os.environ.get("HEGEL_PROTOCOL_TEST_MODE")
    async with trio.open_nursery() as nursery:
        connection = Connection(stream, nursery=nursery, name="Server")
        try:
            if test_mode:
                await run_test_server(connection, test_mode)
            else:
                await run_server_on_connection(connection)

            if verbosity >= Verbosity.verbose:
                print("Client disconnected", file=sys.stderr)
        finally:
            await connection.close()


if __name__ == "__main__":  # pragma: no cover
    main()
