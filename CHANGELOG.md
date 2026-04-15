# Changelog

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
