RELEASE_TYPE: patch

This patch tightens the wire protocol by removing three redundant schema
branches. The public Python API is unchanged, but client libraries must
update the schemas they emit. `PROTOCOL_VERSION` is bumped to `0.12` so
out-of-version clients fail at handshake.

- The `{"type": "sampled_from", "values": [...]}` schema is removed. All
  client libraries already use the integer-index approach for `sampled_from`
  (`{"type": "integer", "min_value": 0, "max_value": len-1}` with a
  client-side `index -> values[index]` transform).
- The `{"type": "null"}` schema is removed. Clients should send
  `{"type": "constant", "value": null}` instead, which the existing
  `constant` branch already handles.
- The `{"type": "ipv4"}` and `{"type": "ipv6"}` schemas are merged into a
  single `{"type": "ip_addresses", "version": <4|6>}` schema, with
  `version` required. To generate either family, send a `one_of` of the
  two versions.
