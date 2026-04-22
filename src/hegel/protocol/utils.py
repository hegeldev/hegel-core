import os
from typing import NewType


class ProtocolError(Exception):
    """
    The server has encountered an internal protocol error. If this is raised, someone,
    either the server or the client, has implemented the protocol incorrectly.
    """


class ConnectionClosedError(ProtocolError):
    """The remote end closed the connection."""


class RequestError(Exception):
    """
    The server has encountered an application-level error. This might be expected during
    normal operation, for example if invalid arguments are passed from the user to
    Hypothesis through the client.

    The server will send back a packet with the payload {"error": msg, "type": error_type}
    to the client when it encounters RequestError.
    """

    def __init__(self, message, *, error_type):
        super().__init__(message)
        self.error_type = error_type


StreamId = NewType("StreamId", int)
MessageId = NewType("MessageId", int)

STREAM_TIMEOUT = float(os.getenv("HEGEL_STREAM_TIMEOUT", 30))
