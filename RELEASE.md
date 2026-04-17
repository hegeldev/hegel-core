RELEASE_TYPE: patch

This patch fixes several issues that prevented the conformance test runner from working on Windows. `NamedTemporaryFile` is now opened with `delete=False` so that subprocesses can read it (Windows otherwise holds an exclusive lock on the file), and `PYTHONUTF8=1` is set for conformance subprocesses so that metrics files are read and written as UTF-8 rather than the default Windows code page.
