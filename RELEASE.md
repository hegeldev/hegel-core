RELEASE_TYPE: patch

Add `crash_after_handshake` and `crash_after_handshake_with_stderr` test modes. These simulate a server that crashes immediately after completing the protocol handshake, allowing client libraries to test crash detection and error reporting without reimplementing the binary protocol in test scripts.
