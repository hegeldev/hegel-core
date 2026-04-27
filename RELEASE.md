RELEASE_TYPE: minor

This release removes the Unix socket transport from the `hegel` server. The
server now always communicates with clients over its stdin/stdout. The
positional `socket_path` argument has been removed from the CLI; the `--stdio`
flag is still accepted for backward compatibility with existing callers, but
is now a no-op (stdio is the only mode).

Library implementations should spawn `hegel` (with or without `--stdio`) and
read and write the binary protocol on the subprocess's stdout and stdin.
