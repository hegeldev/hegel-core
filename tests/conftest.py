import contextlib
import os
import socket as socket_module
from threading import Thread

import pytest
import trio
import trio.socket

from hegel.protocol import Connection
from hegel.server import run_server_on_connection
from tests.client import Client, ClientConnection


@pytest.fixture
def socket_pair():
    s1, s2 = socket_module.socketpair()
    yield s1, s2
    # suppress because a test might already have closed the socket
    with contextlib.suppress(OSError):
        s1.close()
    with contextlib.suppress(OSError):
        s2.close()


@pytest.fixture
def socket():
    # Use a socketpair so the socket is connected — recv() on an unconnected
    # socket raises immediately, which would cause the reader task to exit
    # and close the connection before the test runs.
    s1, s2 = socket_module.socketpair()
    yield s1
    with contextlib.suppress(OSError):
        s1.close()
    with contextlib.suppress(OSError):
        s2.close()


def _make_client():
    server_socket, client_socket = socket_module.socketpair()

    server_fd = os.dup(server_socket.fileno())
    server_socket.close()

    async def _run_server():
        trio_sock = trio.socket.fromfd(
            server_fd, socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        os.close(server_fd)
        stream = trio.SocketStream(trio_sock)
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="Server")
            await run_server_on_connection(conn)

    thread = Thread(target=trio.run, args=(_run_server,), daemon=True)
    thread.start()

    client_connection = ClientConnection(client_socket)
    client = Client(client_connection)
    return client, client_connection, thread


@pytest.fixture
def client():
    client, conn, thread = _make_client()
    with conn:
        yield client
    thread.join(timeout=5)
