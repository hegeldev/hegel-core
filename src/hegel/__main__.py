import contextlib
import importlib.metadata
import os
import socket
import sys
from pathlib import Path

import click
from hypothesis import Verbosity

from hegel.protocol import Connection
from hegel.server import run_server_on_connection


@click.command()
@click.version_option(
    version=importlib.metadata.version("hegel"), message="hegel (version %(version)s)"
)
@click.argument("socket_path")
@click.option(
    "--verbosity",
    type=click.Choice(["quiet", "normal", "verbose", "debug"]),
    default="normal",
    help="Verbosity level. Corresponds to hypothesis.Verbosity.",
)
def main(socket_path, verbosity):
    """Run the Hegel test server, binding to socket_path."""
    verbosity = Verbosity(verbosity)
    socket_path = Path(socket_path)

    # Clean up any existing socket before starting
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()

    run_server(socket_path, verbosity=verbosity)


def run_server(socket_path: Path, *, verbosity: Verbosity = Verbosity.normal) -> None:
    if verbosity >= Verbosity.debug:
        os.environ["HEGEL_PROTOCOL_DEBUG"] = "1"

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.bind(str(socket_path))
    server_sock.listen(1)

    if verbosity >= Verbosity.verbose:
        print(f"Listening on {socket_path}", file=sys.stderr)

    try:
        client_sock, _ = server_sock.accept()

        if verbosity >= Verbosity.verbose:
            print("SDK connected", file=sys.stderr)

        connection = Connection(client_sock, name="Server")
        test_mode = os.environ.get("HEGEL_TEST_MODE")
        if test_mode:
            from hegel.test_server import run_test_server

            run_test_server(connection, test_mode)
        else:
            run_server_on_connection(connection)

        if verbosity >= Verbosity.verbose:
            print("SDK disconnected", file=sys.stderr)

    finally:
        server_sock.close()


if __name__ == "__main__":  # pragma: no cover
    main()
