RELEASE_TYPE: patch

This patch changes the default Hegel server settings when running inside Antithesis (i.e. when `ANTITHESIS_OUTPUT_DIR` is set in the environment): all health checks are now suppressed and the example database is disabled, regardless of what the client requests. Health-check timing measurements and database persistence are not meaningful inside Antithesis's deterministic simulator.
