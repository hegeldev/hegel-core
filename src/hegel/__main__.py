import contextlib
import importlib.metadata
import os
import sys
from typing import Any

import click
import trio
import trio.abc
from hypothesis import Verbosity
from hypothesis.configuration import set_hypothesis_home_dir

from hegel.protocol.connection import Connection
from hegel.server import run_server_on_connection


class _AsyncFileReceiveStream(trio.abc.ReceiveStream):
    def __init__(self, f: Any) -> None:
        self._f = f

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        return await self._f.read(max_bytes or 65536)

    async def aclose(self) -> None:
        with contextlib.suppress(OSError):
            await self._f.aclose()


class _AsyncFileSendStream(trio.abc.SendStream):
    def __init__(self, f: Any) -> None:
        self._f = f

    async def send_all(self, data: bytes) -> None:
        await self._f.write(data)

    async def wait_send_all_might_not_block(self) -> None:  # pragma: no cover
        pass

    async def aclose(self) -> None:
        with contextlib.suppress(OSError):
            await self._f.aclose()


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

    receive_stream = _AsyncFileReceiveStream(
        trio.wrap_file(os.fdopen(protocol_in_fd, "rb", buffering=0))
    )
    send_stream = _AsyncFileSendStream(
        trio.wrap_file(os.fdopen(protocol_out_fd, "wb", buffering=0))
    )
    test_mode = os.environ.get("HEGEL_PROTOCOL_TEST_MODE")
    async with trio.open_nursery() as nursery:
        connection = Connection(
            receive_stream, send_stream, nursery=nursery, name="Server"
        )
        try:
            if test_mode:
                try:
                    from tests.test_server_modes import run_test_server
                except ImportError as exc:  # pragma: no cover
                    raise RuntimeError(
                        f"HEGEL_PROTOCOL_TEST_MODE={test_mode!r} requires hegel to be "
                        "run from a development checkout where tests/ is importable"
                    ) from exc
                await run_test_server(connection, test_mode)
            else:
                await run_server_on_connection(connection)

            if verbosity >= Verbosity.verbose:
                print("Client disconnected", file=sys.stderr)
        finally:
            await connection.close()


if __name__ == "__main__":  # pragma: no cover
    main()
