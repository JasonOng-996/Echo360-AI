# Changes in 2.1.0

This source revision was reconstructed from the creation and modification commands in the user-supplied shared conversation. It builds on App 2.0 and carries forward the download-isolation approach described for script 1.2.2. It was not recovered by downloading the previous ZIP attachments.

- Fixed infinite recursion in `save_config` and added atomic writes plus a previous-settings backup.
- Isolated MP4 and transcript downloads from the main class list; stopped falling back to main-page clicks when worker matching fails.
- Added streaming fallback, cookie scoping, temporary downloads and basic container/transcript validation.
- Replaced open-ended section processing with bounded attempts and per-lecture status.
- Added parameter/input fingerprints, result checksums and partial-batch reuse; rejected empty and incomplete AI results.
- Preserved requested video analysis as a prerequisite for a combined summary.
- Added cooperative cancellation, stopped global input monkey-patching, propagated API failures, and returned failing CLI exit codes.
- Added a Chinese GUI with settings tabs, previous-version import, progress and log export.
- Generalized semester matching, separated same-date lectures with different titles, and honored small nonzero frame limits.
- Updated Windows packaging, install version, CI write permissions, regression gates and packaged-app startup checks.

Known scope: the download selectors retain the UQ layout established in the shared conversation. No new live browser trace was available for this revision. No Windows binary has been built in this environment.
