# EchoLecture AI 2.1.0

A Windows desktop source test build for authorized Echo360 downloads and MP4 + VTT lecture notes. See [中文使用说明](README_CN.md) for setup and upgrading from the earlier scripts.

Run `run_app.bat` on Windows. The launcher creates a Python environment and installs dependencies; Microsoft Edge handles institution login and MFA. Each user supplies their own API key and model IDs.

This revision repairs recursive settings saving, isolates download tabs, validates and atomically saves assets, tracks incomplete tasks, resumes matching AI batches, and adds stop controls, settings import, progress and redacted log export. Default `video_max_frames=0` removes the total-frame cap while preserving key-frame selection.

Existing recordings remain in their original output folders. Old AI outputs without cache metadata are regenerated when analysis is requested; the previous final notes are retained in `.previous.md` files. Subsequent runs reuse matching batches. API requests are billable through the selected provider.

## Development and packaging

Use Python 3.12 for the Windows build. Runtime code requires Python 3.10 or later.

```text
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -v
pyinstaller --noconfirm --clean EchoLectureAI.spec
```

`build_windows.bat` performs dependency setup, tests, packaging, and an executable self-test. `build_installer.bat` uses Inno Setup 6. The expected installer is `release/EchoLectureAI-Setup-2.1.0.exe`.

No compiled Windows installer is included in this source archive. The GitHub Actions workflow builds on Windows, tests the packaged application, records dependency versions, hashes the installer and prepares a release draft for version tags. It has not been executed in this session.

The offline test suite mocks browser/SDK boundaries. Actual UQ authentication, download selectors, provider availability and Windows installation require live acceptance testing. See [validation](VALIDATION.md) and [data handling](PRIVACY.md).

## Design references

Downloads are awaited before closing the owning browser context, which otherwise removes its temporary downloads ([Playwright download lifecycle](https://playwright.dev/python/docs/downloads)). Fallback transfers use streaming rather than buffering the complete response ([HTTPX asynchronous streaming](https://www.python-httpx.org/async/)). Windows CI configuration follows [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
