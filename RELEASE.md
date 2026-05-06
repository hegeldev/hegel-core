RELEASE_TYPE: minor

This release adds support for the `uuid` schema type, exposing Hypothesis's
`uuids` strategy. UUIDs are returned to the client as strings in the
canonical hyphenated form. An optional `version` field selects a specific
UUID version (1-5).
