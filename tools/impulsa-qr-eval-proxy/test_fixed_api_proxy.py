import importlib.util
import io
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fixed_api_proxy_tested", ROOT / "fixed-api-proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class Timer:
    def __init__(self, seconds, callback):
        self.seconds, self.callback, self.cancelled = seconds, callback, False
    def start(self):
        assert self.seconds == 70
    def cancel(self):
        self.cancelled = True


class Sock:
    shutdown_called = False
    def settimeout(self, timeout):
        assert timeout == 70
    def dup(self):
        self.duplicate = Sock()
        return self.duplicate
    def shutdown(self, how):
        self.shutdown_called = True
    def close(self):
        pass


class Response:
    status = 422
    def __init__(self, data=b'\x89PNG\x00synthetic', length=None):
        self.data, self.length = data, str(len(data)) if length is None else length
    def getheaders(self):
        return [("Content-Type", "application/octet-stream"), ("Content-Length", self.length), ("X-Request-Id", "synthetic"), ("Connection", "close")]
    def getheader(self, key):
        return self.length if key == "Content-Length" else None
    def read(self, maximum):
        return self.data[:maximum]


class Connection:
    response = None
    def __init__(self, host, port, timeout):
        assert (host, port, timeout) == ("api-eval-backend", 8769, 2)
        self.sock, self.closed, self.headers = Sock(), False, []
    def connect(self):
        pass
    def putrequest(self, method, target, **options):
        self.request = method, target, options
    def putheader(self, key, value):
        self.headers.append((key, value))
    def endheaders(self, body):
        self.body = body
    def getresponse(self):
        return self.response or Response()
    def close(self):
        self.closed = True


class ProxyTests(unittest.TestCase):
    def test_fixed_destination_keeps_auth_headers_status_and_binary_exact(self):
        made = []
        def factory(*args, **kwargs):
            result = Connection(*args, **kwargs); made.append(result); return result
        headers = [("Host", "impulsa-qr-api-eval:8769"), ("Authorization", "Bearer SYNTHETIC-OPAQUE"), ("X-Evaluation-Control", "SYNTHETIC-CONTROL"), ("Content-Length", "2"), ("Connection", "close")]
        with patch.object(proxy.threading, "Timer", Timer):
            status, result_headers, data = proxy.forward("POST", "/api/v1/integrations/orders/validate-selection?x=1", headers, b"{}", factory)
        self.assertEqual(status, 422); self.assertEqual(data, b'\x89PNG\x00synthetic')
        self.assertIn(("Authorization", "Bearer SYNTHETIC-OPAQUE"), made[0].headers)
        self.assertIn(("Host", "impulsa-qr-api-eval:8769"), made[0].headers)
        self.assertIn(("X-Evaluation-Control", "SYNTHETIC-CONTROL"), made[0].headers)
        self.assertEqual(made[0].body, b"{}"); self.assertTrue(made[0].closed)
        self.assertNotIn(("Connection", "close"), result_headers)
        self.assertIn(("Content-Length", "14"), result_headers)

    def test_absolute_targets_other_paths_tunnels_and_ambiguous_framing_are_rejected(self):
        for method, target, headers, body in [("GET", "https://outside.test/api/v1/context", [], b""), ("GET", "//outside.test/x", [], b""),
            ("GET", "/private-file", [], b""), ("CONNECT", "/health", [], b""), ("POST", "/health", [("Content-Length", "0"), ("Content-Length", "0")], b""),
            ("POST", "/health", [("Transfer-Encoding", "chunked")], b""), ("GET", "/health#fragment", [], b"")]:
            with self.subTest(target=target, method=method), self.assertRaises(proxy.ProxyFailure):
                proxy.forward(method, target, headers, body, lambda *a, **k: self.fail("Rejected request created a backend connection"))

    def test_request_and_response_limits_close_the_backend_without_forwarding_excess(self):
        with self.assertRaises(proxy.ProxyFailure) as caught:
            proxy.validate_request("POST", "/health", [], b"x" * (proxy.MAX_REQUEST_BODY + 1))
        self.assertEqual(caught.exception.status, 413)
        result = Connection("api-eval-backend", 8769, 2)
        result.response = Response(b"", str(proxy.MAX_RESPONSE_BODY + 1))
        with patch.object(proxy.threading, "Timer", Timer), self.assertRaises(proxy.ProxyFailure) as caught:
            proxy.forward("GET", "/health", [], b"", lambda *a, **k: result)
        self.assertEqual(caught.exception.status, 502); self.assertTrue(result.closed)

    def test_connect_timeout_is_sanitized_and_uses_fixed_two_seconds(self):
        class TimeoutConnection(Connection):
            def connect(self):
                raise socket.timeout("no secret error body should escape")
        with self.assertRaises(proxy.ProxyFailure) as caught:
            proxy.forward("GET", "/health", [], b"", TimeoutConnection)
        self.assertEqual((caught.exception.status, caught.exception.code), (504, "BACKEND_TIMEOUT"))

    def test_deadline_can_interrupt_http10_body_even_after_connection_drops_its_socket(self):
        held = []
        class TimerCapture(Timer):
            def start(self):
                super().start(); held.append(self)
        class HTTP10Connection(Connection):
            def getresponse(self):
                old = self.sock
                self.sock = None
                held[0].callback()
                self.expired_duplicate = old.duplicate
                return Response()
        made = HTTP10Connection("api-eval-backend", 8769, 2)
        with patch.object(proxy.threading, "Timer", TimerCapture), self.assertRaises(proxy.ProxyFailure) as caught:
            proxy.forward("GET", "/health", [], b"", lambda *a, **k: made)
        self.assertEqual(caught.exception.status, 504)
        self.assertTrue(made.expired_duplicate.shutdown_called)
        self.assertTrue(held[0].cancelled)

    def test_header_reader_caps_line_and_total_before_reading_more(self):
        with self.assertRaises(Exception):
            proxy.HeaderBudgetReader(io.BytesIO(b"x" * (proxy.MAX_LINE + 1))).readline()
        reader = proxy.HeaderBudgetReader(io.BytesIO((b"x" * 7998 + b"\r\n") * 5))
        for _ in range(4):
            reader.readline()
        with self.assertRaises(Exception):
            reader.readline()

    def test_control_routes_keep_their_fixed_target_and_compose_has_no_published_ports(self):
        for path in ("/control/start-case", "/control/snapshot?business_id=2&sender=51999000004", "/control/stop", "/openapi.json"):
            proxy.validate_request("GET", path, [], b"")
        compose = (ROOT / "compose.example.yaml").read_text(encoding="utf8")
        self.assertNotIn("\n    ports:", compose)
        self.assertIn("cap_drop: [ALL]", compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("aliases: [impulsa-qr-api-eval]", compose)
        self.assertNotIn("n8n-n8n-1", compose)


if __name__ == "__main__":
    unittest.main()
