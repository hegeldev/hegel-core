RELEASE_TYPE: patch

This release adds `OriginDeduplicationConformance`, a new conformance test that verifies library clients format the `origin` field in `mark_complete` coarsely enough for the server to deduplicate failures correctly. Two modes are tested: error messages that include generated values, and the same bug reached via different call paths.
