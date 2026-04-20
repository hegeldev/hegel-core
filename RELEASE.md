RELEASE_TYPE: patch

This patch fixes a crash in the server when a test case is completed. When the client sent `mark_complete`, the `StopTest` exception used internally by Hypothesis as a control-flow mechanism could escape the protocol handler, causing the server to send a spurious `StopTest` error reply for the `mark_complete` request and — in clients that share a single server process across concurrent tests — abort the server entirely.
