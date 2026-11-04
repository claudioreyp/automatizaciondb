# SOURCE ONLY. Verify this exact already-present base before any build:
# sha256:93dc84393cbaa83f680830c2e53bc7567e1c73a5273f46a36bb72df46fa9f740
# Use --pull=false --network=none; no dependency installation or API replacement.
FROM escalar-qr-api-eval:16240c5
COPY --chown=10001:10001 sandbox.py /sandbox/api-sandbox.py
COPY --chown=10001:10001 notifier_fixture.py control_router.py visual_snapshot.py /sandbox/
COPY --chown=10001:10001 originals /sandbox/originals
COPY --chown=10001:10001 security /sandbox/security
