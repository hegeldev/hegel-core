import contextlib


class StdioTransport:
    """Socket-like wrapper around a pair of file objects.

    Used by tests to connect a ClientConnection to a server running over
    stdin/stdout (pipes).  Provides the same interface as socket.socket
    (recv, sendall, settimeout, shutdown, close).
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
        pass

    def shutdown(self, how):
        pass

    def close(self):
        with contextlib.suppress(OSError):
            self._writer.close()
        with contextlib.suppress(OSError):
            self._reader.close()
