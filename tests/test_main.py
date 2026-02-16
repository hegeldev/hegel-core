"""Tests for __main__.py CLI."""

import contextlib
import importlib.metadata
import os
import socket
import tempfile
import time
from pathlib import Path
from threading import Thread

import pytest
from click.testing import CliRunner
from client import Client

from hegel.__main__ import main
from hegel.protocol import Connection


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

        conn = Connection(sock)
        try:
            yield Client(conn)
        finally:
            conn.close()
            t.join(timeout=5)
        return

    raise RuntimeError(f"timed out waiting for socket at {socket_path}")


def test_version():
    result = CliRunner().invoke(main, ["--version"])
    version = importlib.metadata.version("hegel")
    assert result.output.strip() == f"hegel (version {version})"


@pytest.mark.parametrize("verbosity", ["normal", "verbose", "debug"])
def test_cli(socket_path, verbosity):
    with _client_and_server(socket_path, "--verbosity", verbosity) as client:
        client.run_test("test", lambda: None, test_cases=1)


def test_cli_cleans_up_stale_socket(socket_path):
    socket_path.touch()

    with _client_and_server(socket_path) as client:
        client.run_test("test", lambda: None, test_cases=1)


def test_run_server_with_test_mode(socket_path):
    """Test run_server routes to test_server when HEGEL_TEST_MODE is set."""
    with _client_and_server(
        socket_path, env={"HEGEL_TEST_MODE": "empty_test"}
    ) as client:
        client.run_test("test", lambda: None, test_cases=1)
