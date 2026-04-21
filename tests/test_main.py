"""Tests for __main__.py CLI."""

import contextlib
import importlib.metadata
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from threading import Thread

import pytest
from click.testing import CliRunner
from hypothesis import Verbosity

from hegel.__main__ import StdioTransport, main, run_server_stdio
from tests.client import Client, ClientConnection


@pytest.fixture
def socket_path():
    # Socket paths are limit to 104 characters on macos. This precludes using
    # the tmp_dir pytest fixture, which creates a longer path. We'll handroll
    # our own fixture that provides a shorter path.
    #
    # See https://unix.stackexchange.com/questions/367008.
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "test.sock"


@contextlib.contextmanager
def _client_and_server(socket_path, *args, env=None):
    """Start the CLI server and yield a connected Client."""

    def run_cli():
        if env:
            old_env = {}
            for k, v in env.items():
                old_env[k] = os.environ.get(k)
                os.environ[k] = v
        try:
            CliRunner().invoke(
                main,
                [str(socket_path), *args],
                catch_exceptions=False,
            )
        finally:
            if env:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    t = Thread(target=run_cli, daemon=True)
    t.start()

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not socket_path.exists():
            time.sleep(0.01)
            continue

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(socket_path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            sock.close()
            time.sleep(0.01)
            continue

        with ClientConnection(sock) as conn:
            yield Client(conn)
        t.join(timeout=5)
        return

    raise RuntimeError(f"timed out waiting for socket at {socket_path}")


def test_version():
    result = CliRunner().invoke(main, ["--version"])
    version = importlib.metadata.version("hegel-core")
    assert result.output.strip() == f"hegel (version {version})"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
@pytest.mark.parametrize("verbosity", ["normal", "verbose", "debug"])
def test_cli(socket_path, verbosity):
    with _client_and_server(socket_path, "--verbosity", verbosity) as client:
        client.run_test(lambda: None, test_cases=1)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_cli_cleans_up_stale_socket(socket_path):
    socket_path.touch()

    with _client_and_server(socket_path) as client:
        client.run_test(lambda: None, test_cases=1)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_run_server_with_test_mode(socket_path):
    with _client_and_server(
        socket_path, env={"HEGEL_PROTOCOL_TEST_MODE": "empty_test"}
    ) as client:
        client.run_test(lambda: None, test_cases=1)


# --- StdioTransport tests ---


def test_stdio_transport_recv_and_sendall():
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    reader = os.fdopen(in_r, "rb")
    writer = os.fdopen(out_w, "wb", buffering=0)
    out_reader = os.fdopen(out_r, "rb")
    in_writer = os.fdopen(in_w, "wb", buffering=0)

    transport = StdioTransport(reader, writer)

    transport.sendall(b"hello")
    assert out_reader.read(5) == b"hello"

    in_writer.write(b"world")
    in_writer.flush()
    assert transport.recv(5) == b"world"

    transport.settimeout(1.0)
    transport.settimeout(None)

    transport.shutdown(socket.SHUT_RDWR)

    in_writer.close()
    out_reader.close()
    transport.close()


def test_stdio_transport_recv_eof():
    in_r, in_w = os.pipe()
    reader = os.fdopen(in_r, "rb")
    _, out_w = os.pipe()
    writer = os.fdopen(out_w, "wb", buffering=0)

    transport = StdioTransport(reader, writer)
    os.close(in_w)  # close write end → EOF
    assert transport.recv(10) == b""
    transport.close()


def test_stdio_transport_sendall_after_close_raises_oserror():
    """StdioTransport.sendall raises OSError (not ValueError) when writer is closed."""
    import io

    reader = io.BytesIO(b"")
    writer = io.BytesIO(b"")
    transport = StdioTransport(reader, writer)
    transport.close()
    with pytest.raises(OSError):
        transport.sendall(b"hello")


def test_stdio_transport_recv_none():
    """Cover the `data is None` branch in recv."""

    class NoneReader:
        def read(self, n):
            return None

        def close(self):
            pass

    transport = StdioTransport(NoneReader(), os.fdopen(os.pipe()[1], "wb", buffering=0))
    assert transport.recv(10) == b""
    transport.close()


# --- CLI argument validation tests ---


def test_cli_missing_socket_path():
    result = CliRunner().invoke(main, [])
    assert result.exit_code != 0
    assert "Socket path is required" in result.output


def test_cli_stdio_with_socket_path():
    result = CliRunner().invoke(main, ["--stdio", "/tmp/bogus.sock"])
    assert result.exit_code != 0
    assert "Cannot specify a socket path" in result.output


def test_cli_stdio_calls_run_server_stdio(monkeypatch):
    called = []
    monkeypatch.setattr(
        "hegel.__main__.run_server_stdio",
        lambda **kwargs: called.append(kwargs),
    )
    result = CliRunner().invoke(main, ["--stdio"])
    assert result.exit_code == 0
    assert len(called) == 1


# --- run_server_stdio integration test ---


@contextlib.contextmanager
def _redirect_stdio_to_pipes():
    """Replace fd 0 and fd 1 with pipes and yield the client ends."""
    server_read_fd, client_write_fd = os.pipe()
    client_read_fd, server_write_fd = os.pipe()

    saved_stdin = os.dup(0)
    saved_stdout = os.dup(1)
    saved_sys_stdout = sys.stdout

    os.dup2(server_read_fd, 0)
    os.dup2(server_write_fd, 1)
    os.close(server_read_fd)
    os.close(server_write_fd)

    try:
        yield client_read_fd, client_write_fd
    finally:
        os.dup2(saved_stdin, 0)
        os.dup2(saved_stdout, 1)
        os.close(saved_stdin)
        os.close(saved_stdout)
        sys.stdout = saved_sys_stdout


def _run_stdio_test(*, verbosity="normal", env=None):
    """Run run_server_stdio in a thread with pipes, yield a connected client."""
    old_env = {}
    if env:
        for k, v in env.items():
            old_env[k] = os.environ.get(k)
            os.environ[k] = v

    try:
        with _redirect_stdio_to_pipes() as (client_read_fd, client_write_fd):
            thread = Thread(
                target=run_server_stdio,
                kwargs={"verbosity": Verbosity(verbosity)},
                daemon=True,
            )
            thread.start()

            client_reader = os.fdopen(client_read_fd, "rb")
            client_writer = os.fdopen(client_write_fd, "wb", buffering=0)
            client_transport = StdioTransport(client_reader, client_writer)

            with ClientConnection(client_transport) as conn:
                client = Client(conn)
                client.run_test(lambda: None, test_cases=1)

            thread.join(timeout=5)
    finally:
        if env:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_run_server_stdio():
    _run_stdio_test()


def test_run_server_stdio_verbose():
    _run_stdio_test(verbosity="verbose")


def test_run_server_stdio_debug():
    _run_stdio_test(verbosity="debug")


def test_run_server_stdio_test_mode():
    _run_stdio_test(env={"HEGEL_PROTOCOL_TEST_MODE": "empty_test"})
