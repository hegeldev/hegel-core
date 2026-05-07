RELEASE_TYPE: minor

This patch bumps `PROTOCOL_VERSION` to `0.14`. The previous release (0.7.1) added the `uuid` schema type without bumping the protocol, so client libraries had no way to negotiate "this server understands `uuid`" at handshake. Bumping now lets clients that emit `{"type": "uuid"}` schemas require protocol `0.14` and fail cleanly against older servers instead of getting an `Unsupported schema` error at draw time.
