import contextlib
import socket as socket_module
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import trio
import trio.socket
from hypothesis import HealthCheck, Phase, given, settings as Settings

from hegel.protocol import Connection


class Found(Exception):
    pass


def find_any(strategy, condition, *, settings=None):
    @Settings(
        settings,
        max_examples=1000,
        phases=set(Phase) - {Phase.shrink},
        suppress_health_check=list(HealthCheck),
    )
    @given(strategy)
    def test(value):
        if condition(value):
            raise Found

    try:
        test()
    except Found:
        return
    raise AssertionError("No example found satisfying condition")


@contextlib.contextmanager
def run_trio_server(
    server_socket: socket_module.socket,
    async_fn: Callable[[Connection], Coroutine[Any, Any, None]],
    *,
    name: str | None = None,
    debug: bool | None = None,
):
    """Run an async function with a Connection in a background trio thread.

    Yields the background thread. Joins it (up to 5 s) on exit and re-raises
    any exception that occurred on the server side.

    Usage::

        async def server_side(conn):
            await conn.receive_handshake()
            ...

        def test_something(socket_pair):
            server_socket, client_socket = socket_pair
            with run_trio_server(server_socket, server_side) as t:
                with ClientConnection(client_socket) as client_conn:
                    client_conn.send_handshake()
    """
    errors: list[BaseException] = []

    async def _main():
        try:
            stream = trio.SocketStream(trio.socket.from_stdlib_socket(server_socket))
            async with trio.open_nursery() as nursery:
                conn = Connection(stream, nursery=nursery, name=name, debug=debug)
                try:
                    await async_fn(conn)
                finally:
                    await conn.close()
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=trio.run, args=(_main,), daemon=True)
    t.start()
    try:
        yield t
    finally:
        t.join(timeout=5)
    if errors:
        raise errors[0]
