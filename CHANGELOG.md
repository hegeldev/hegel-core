# Changelog

## 0.6.0 - 2026-04-30

This release makes the following breaking protocol changes:
- Removed `{"type": "sampled_from"}`. Instead of serializing the values to sample from, ask for an integer index and index into the collection of values on the client side.
- Removed `{"type": "null"}`. Use `{"type": "constant", "value": null}` instead.
- Replaced `{"type": "ipv4"}` and `{"type": "ipv6"}` with a single `{"type": "ip_address", "version": <4|6>}` schema.

The protocol version is now 0.12.

## 0.5.0 - 2026-04-29

This release changes the `one_of` protocol request to return a tuple of `[index, value]`, rather than just `value`.

## 0.4.14 - 2026-04-28

Pin our dependencies to below their next major version.

## 0.4.13 - 2026-04-28

This release tweaks how our conformance tests write metrics.

## 0.4.12 - 2026-04-28

Removes CBOR tagging from fraction and complex numbers.

## 0.4.11 - 2026-04-28

This release adds a `skip_unique` parameter to `ListConformance`.

## 0.4.10 - 2026-04-27

Add fraction and complex number schema types.

## 0.4.9 - 2026-04-27

This release adds a `command_prefix` argument to `run_conformance_tests` to control how conformance tests are run.

## 0.4.8 - 2026-04-27

This patch removes the unused Unix socket transport from the `hegel` server. The server now always communicates with its client over stdin/stdout, matching how all current libraries spawn it.

## 0.4.7 - 2026-04-22

This release adds a `single_test_case` top-level protocol command. When sent
instead of `run_test`, the server immediately hands a single test case to the
client in final mode and returns the result, with no shrinking, replay, or other
exploration. This is mostly intended for callers who are running workloads in
Antithesis, but is potentially useful for anyone who wishes to use Hegel for
flexible data generation on a system that they don't have a reset button for or
that exhibits significantly non-deterministic behaviour.

## 0.4.6 - 2026-04-21

This patch fixes several concurrency bugs and improves error handling robustness in the protocol layer.

The server's reader loop no longer crashes when it receives a packet for an unknown or already-closed stream, or a malformed close-stream packet. Instead, it sends an error reply back to the client (for request packets) and continues processing. This means clients that make protocol mistakes will now get a clear ProtocolError response instead of the server silently dying.

Several race conditions in the protocol layer have been fixed. `Connection.close()` and `Stream.close()` now use dedicated locks to ensure their check-and-set guards are atomic, preventing concurrent callers from double-closing. `Connection.close()` holds the writer lock while closing the socket, so no `write_packet` call can be mid-flight when the fd is yanked. `Stream.write_request` protects the message ID increment with a lock so concurrent writers get unique IDs. `Connection.new_stream` allocates stream IDs under the writer lock. `receive_handshake` now sets `_handshake_done` after the handshake reply is sent rather than before.

Bare `assert` statements throughout the protocol and server code have been replaced with explicit error raises (`ProtocolError`, `ValueError`, `ConnectionError`) with descriptive messages. This prevents assertion-removal in optimized Python builds and gives clients and logs meaningful diagnostics.

`StdioTransport.sendall` now converts `ValueError` (from writing to a closed file descriptor) to `OSError`, so the existing error handling in the protocol layer catches it correctly. This fixes the "ValueError: I/O operation on closed file" error that could occur when the client disconnects while the server is still writing.

## 0.4.5 - 2026-04-20

This release adds a new conformance test `OriginDeduplicationConformance`.

## 0.4.4 - 2026-04-20

This release is in support of getting hegel libraries working on Windows. It mostly fixes issues affecting the conformance testing.

Windows support still won't work in individual libraries until they also do work to support it.

## 0.4.3 - 2026-04-15

This patch adds a new `OneOfConformance` test, for the `one_of` generator.

This patch also adds recommended integer bound constants (`INT32_MIN`, `INT32_MAX`, `INT64_MIN`, `INT64_MAX`, `BIGINT_MIN`, `BIGINT_MAX`) for use in conformance test setup. Languages with arbitrary-precision integers should use the `BIGINT` bounds to exercise CBOR bignum tag decoding, which is not triggered by the narrower ranges most implementations currently use.

## 0.4.2 - 2026-04-15

Add `crash_after_handshake` and `crash_after_handshake_with_stderr` test modes. These simulate a server that crashes immediately after completing the protocol handshake, allowing client libraries to test crash detection and error reporting without reimplementing the binary protocol in test scripts.

## 0.4.1 - 2026-04-11

`ListConformance` and `DictConformance` now run twice; once with `{"mode": "basic"}`, and once with `{"mode": "non_basic"}`, indicating the element generators should be basic or non-basic respectively.

## 0.4.0 - 2026-04-10

This patch changes our CBOR tag for text fields from `6` to `91`, to avoid reserving a "Standards Action" tag, even though it is technically unassigned. See https://www.iana.org/assignments/cbor-tags/cbor-tags.xhtml.

The protocol version is now `0.10`.

## 0.3.2 - 2026-04-05

This patch implements a `no_surrogates` parameter for our `TextConformance` conformance test, for languages with UTF-8 strings.

## 0.3.1 - 2026-04-04

This release adds support for alphabet parameters in `{"type": "string"}` and `{"type": "regex"}` schemas, allowing control over generated characters. Supported parameters are `codec`, `min_codepoint`, `max_codepoint`, `categories`, `exclude_categories`, `exclude_characters`, and `include_characters`.

## 0.3.0 - 2026-04-01

Several breaking changes:
- Rename `channel` to `stream` everywhere.
- Restructure parameters and return values for `collection` commands.

## 0.2.5 - 2026-04-01

This patch changes how `const`, `sampled_from`, and `one_of` are defined in the protocol, to harmonize with the other generator definitions:

- `{"const": value}` is now `{"type": "constant", "value": value}`
- `{"sampled_from": [...]}` is now `{"type": "sampled_from", "values": [...]}`
- `{"one_of": [...]}` is now `{"type": "one_of", "generators": [...]}`

As a result, this patch bumps our protocol version to `0.8`.

## 0.2.4 - 2026-04-01

Add protocol support for reporting failure blobs back to the client. These are strings that can be used to reproduce a specific failure exactly.

## 0.2.3 - 2026-03-25

This release adds a --stdio flag to hegel-core that allows the calling process to communicate with it directly via stdin and stdout rather than going via a unix socket.

As well as simplifying the interactions with hegel-core, this should enable easier support for Windows later.

## 0.2.2 - 2026-03-19

Add support for the `derandomize` and `database` settings to the `run_test` payload in the protocol.

As a result, this release also bumps the protocol version to `0.7`.

## 0.2.1 - 2026-03-18

Hegel currently requires tests to be fully deterministic in their data generation, because Hypothesis does, but was not previously correctly reporting Hypothesis's flaky test errors back to the client (A test is flaky if it doesn't successfully replay - that is, when rerun with the same data generation, a different result is produced).

This release adds protocol support for reporting those flaky errors back to the client.

## 0.2.0 - 2026-03-18

This release adds support `HealthCheck` to the protocol. A health check is a proactive error raised by Hegel when we detect your test is likely to have degraded testing power or performance. The protocol now communicates health check errors back to the client as a result packet with the `health_check_failure` key set, and supports clients setting `suppress_health_check` in the `run_test` payload.

As a result, this release also bumps the protocol version to 0.5.

## 0.1.2 - 2026-03-17

Internal refactoring and documentation.

## 0.1.1 - 2026-03-17

The reader loop now exits gracefully when the remote end closes the connection, instead of raising an unhandled exception in the reader thread.

## 0.1.0 - 2026-03-13

Initial release!
