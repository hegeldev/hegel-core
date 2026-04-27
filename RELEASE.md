RELEASE_TYPE: patch

This patch removes the unused Unix socket transport from the `hegel` server. The server now always communicates with its client over stdin/stdout, matching how all current libraries spawn it. The CLI's `--stdio` flag is still accepted for backward compatibility, but is now a no-op.
