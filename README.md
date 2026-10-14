# makma-prime-v2

- Local assistant with provider adapters, SQLite memory, missions, schedules, approvals, desktop tools and local HTTP/WebSocket UI.
- Older Makma Prime archive; seven common source files differ from current local Prime implementation.

## Setup

Python 3.12: create a fresh virtual environment; install requirements-tested.txt for the reproduced offline test environment. Run `python -B -m pytest -q -p no:cacheprovider tests` from the repository root. Real credentials and external/process effects are denied by conftest.py. For local interactive use, review requirements.txt (Windows desktop-control dependency included there), set only needed untracked environment variables, and run `python app.py`; bind only loopback. Provider requests require deliberate configuration and may incur charges. Never expose this local desktop assistant to a shared network.

## Verified changes

Implemented bounded actual ASGI request-body reads including chunked uploads, timeout and early rejection; added four regression checks. Added missing jsonschema direct dependency. Restored existing fake-provider/redaction-fixture tests (credential-shaped test string is deliberately synthetic and never sent). All 35 tests passed.

## Validation

- Reviewed pytest API/storage/fake-provider/body-limit suite: PASS

## Known limits

- Local single-user application only; session token and Origin/TrustedHost boundary are not remote multiuser authentication.
- Desktop actions were never exercised on the real PC; approval exactly-once and lockdown checked in simulation.
- Archived audit123 source is preserved locally for provenance but omitted from publication because it lacks the current local authority boundary.
- Declared Content-Length and actual body now limited to 13MB; body-read deadline 15s.
- Desktop PyAutoGUI dependency remains optional at runtime and was not installed or exercised in offline verification.
- Dependency versions are recorded for the actual test environment, not a security-certification claim.

## Rights

The owner explicitly confirmed candidate source/art ownership and permission to publish. Copyright remains reserved; no permissive license was invented. Dependency licenses remain those of their respective upstream projects. No private database, credentials, historical Git data, logs or personal state are included.
