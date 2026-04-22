import contextlib
import importlib.metadata
import os
import sys

import click
import trio
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
@click.option(
    "--verbosity",
    type=click.Choice(["quiet", "normal", "verbose", "debug"]),
    default="normal",
    help="Verbosity level. Corresponds to hypothesis.Verbosity.",
)
def main(verbosity):
    """Run the Hegel test server using stdin/stdout for protocol communication."""
    trio.run(run_server_stdio, Verbosity(verbosity))


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
