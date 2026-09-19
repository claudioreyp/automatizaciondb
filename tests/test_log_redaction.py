import logging

import pytest

from app.log_redaction import CredentialSafeFormatter


@pytest.mark.parametrize("value", [
    '/api/v1/ws/branches/2?access_token=private.jwt.value&other=ok',
    'wss://example.test/ws?dev_auth=private.jwt.value',
    'Authorization: Bearer private.jwt.value',
    'postgresql://user:private.jwt.value@db.example.test/pos',
    'password=private.jwt.value',
])
def test_server_logs_redact_credentials_without_dropping_context(value):
    record = logging.LogRecord('uvicorn.error', logging.ERROR, __file__, 1, 'Failed: %s', (value,), None)
    result = CredentialSafeFormatter('%(levelname)s %(message)s').format(record)
    assert 'private.jwt.value' not in result
    assert '[REDACTED]' in result
    assert result.startswith('ERROR Failed:')


def test_regular_diagnostics_remain_visible():
    record = logging.LogRecord('uvicorn.error', logging.ERROR, __file__, 1, 'Connection refused', (), None)
    assert CredentialSafeFormatter().format(record) == 'Connection refused'
