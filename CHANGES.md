# Preparation changes

Implemented bounded actual ASGI request-body reads including chunked uploads, timeout and early rejection; added four regression checks. Added missing jsonschema direct dependency. Restored existing fake-provider/redaction-fixture tests (credential-shaped test string is deliberately synthetic and never sent). All 35 tests passed.
