# Validation of 2.1.0

- Python: 3.12.14 on Linux.
- FFmpeg: `ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers`.
- Offline tests: **22 passed, 1 skipped, 0 failed, 0 errors**.
- Source modules and the PyInstaller spec parse successfully.

Coverage includes configuration round trips and backups; invalid downloads; closed-page fallback; preservation of an existing file when a replacement fails; progress after a failed lecture; cancellation cleanup; empty/incomplete API responses; resuming vision batches; cache invalidation and output checksums; frame limits; credential clearing and log redaction. A real synthetic MP4 was created with FFmpeg, its duration read and a JPEG frame extracted.

The skipped test constructs the Windows GUI and saves settings. A display server and Windows are unavailable here. Browser operations and AI SDK responses in the tests are simulated. No institution credentials or API keys were used. The package index was unreachable from this environment, so runtime dependencies, provider SDK integration, live Echo360 selectors and the HTTPX network transfer were not exercised end to end.

The Windows launcher, PyInstaller executable, packaged GUI self-test, Inno Setup installer and GitHub workflow have not been run on Windows in this session. They are included for the next acceptance/build step. No Windows EXE is present in this archive.

Reproduce tests with `python -m unittest discover -s tests -v`. Windows build commands are in README_CN.md.
