RELEASE_TYPE: patch

This patch fixes a server crash that could occur if the client disconnected while the server was handling a flaky test. Previously the server would print a noisy traceback and terminate; it now treats the disconnection as an ordinary end-of-connection and exits cleanly.
