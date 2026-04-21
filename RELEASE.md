RELEASE_TYPE: patch

This patch fixes several concurrency bugs and improves error handling robustness in the protocol layer.

The server's reader loop no longer crashes when it receives a packet for an unknown or already-closed stream, or a malformed close-stream packet. Instead, it sends an error reply back to the client (for request packets) and continues processing. This means clients that make protocol mistakes will now get a clear ProtocolError response instead of the server silently dying.

Several race conditions in the protocol layer have been fixed. `Connection.close()` and `Stream.close()` now use dedicated locks to ensure their check-and-set guards are atomic, preventing concurrent callers from double-closing. `Connection.close()` holds the writer lock while closing the socket, so no `write_packet` call can be mid-flight when the fd is yanked. `Stream.write_request` protects the message ID increment with a lock so concurrent writers get unique IDs. `Connection.new_stream` allocates stream IDs under the writer lock. `receive_handshake` now sets `_handshake_done` after the handshake reply is sent rather than before.

Bare `assert` statements throughout the protocol and server code have been replaced with explicit error raises (`ProtocolError`, `ValueError`, `ConnectionError`) with descriptive messages. This prevents assertion-removal in optimized Python builds and gives clients and logs meaningful diagnostics.

`StdioTransport.sendall` now converts `ValueError` (from writing to a closed file descriptor) to `OSError`, so the existing error handling in the protocol layer catches it correctly. This fixes the "ValueError: I/O operation on closed file" error that could occur when the client disconnects while the server is still writing.
