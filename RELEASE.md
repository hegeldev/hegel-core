RELEASE_TYPE: patch

This patch fixes a stdio-server shutdown deadlock that could occur under `uv`
when the server exited after an error while stdin remained open.
