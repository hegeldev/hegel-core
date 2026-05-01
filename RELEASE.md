RELEASE_TYPE: patch

This patch changes the default Hegel server settings when running inside Antithesis (i.e. when `ANTITHESIS_OUTPUT_DIR` is set in the environment) to disable health checks and database. Health checks are designed for the sort of small fast test you would run in your unit tests and are not sensible defaults for Antithesis, and the database is essentially useless inside Antithesis as replay is done via the fuzzer.
