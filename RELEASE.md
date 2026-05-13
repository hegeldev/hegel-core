RELEASE_TYPE: minor

This release adds a `report_multiple_failures` field to the `run_test`
protocol message, which maps to Hypothesis's `report_multiple_bugs`
setting. When `False`, Hypothesis collapses multi-bug runs to a single
failing example rather than surfacing one per distinct origin; the
default (`True`) preserves the existing behaviour.

The protocol version bumps from `0.14` to `0.15` so clients can detect
whether the server understands the new field.
