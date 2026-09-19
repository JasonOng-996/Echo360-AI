# Data handling

EchoLecture AI runs locally. Institution login and MFA are completed by the user in a dedicated Edge browser profile. That profile stores local session cookies; the app does not request the user's institution password.

API credentials and configuration are saved in `%LOCALAPPDATA%\EchoLectureAI\` by default. API keys are currently stored as plain text in the local `.env` file, not encrypted. Distributed source packages and app builds must not include that file, the browser profile, or the user's downloaded course data. The GUI's log export masks recognized API keys and URL query parameters; logs may still include course names and local paths.

When enabled, AI analysis sends transcript text and sampled video frames to the user's selected provider using that user's credential. Provider processing, retention and billing depend on the provider and account. The complete MP4 is not uploaded by this implementation.

The HTTP fallback uses the exact URL returned by an authorized browser download and cookies applicable to that URL. It does not obtain new permissions or bypass login, MFA or download restrictions.

Old settings and final notes may have local backups. Deleting a credential in the GUI clears its saved value; removing all app settings and the dedicated browser profile removes the local session data. Downloaded lecture materials remain in the separately selected output folder.
