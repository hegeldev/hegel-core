RELEASE_TYPE: patch

This patch dispatches the `hegel-core-release` event to all language libraries on release (previously only hegel-rust and website received it), so that future hegel-core releases automatically open pin-bump PRs in hegel-go, hegel-cpp, hegel-typescript, and hegel-ocaml.
