"""Fixed synthetic API bridge. No selectable origin, credentials or request logs."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
from urllib.parse import urlsplit

BACKEND_HOST, BACKEND_PORT = "api-eval-backend", 8769
CONNECT_TIMEOUT, READ_TIMEOUT = 2, 70
MAX_REQUEST_BODY, MAX_RESPONSE_BODY = 2 * 1024 * 1024, 8 * 1024 * 1024
MAX_LINE, MAX_HEADERS, MAX_CONCURRENT = 8192, 32768, 8
METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS", "HEAD"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}


class ProxyFailure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class HeaderBudgetReader:
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_HEADERS
    def readline(self, limit=-1):
        line = self.stream.readline(min(MAX_LINE + 1, self.remaining + 1, limit if limit >= 0 else MAX_LINE + 1))
        self.remaining -= len(line)
        if len(line) > MAX_LINE or self.remaining < 0:
            raise http.client.LineTooLong("bounded proxy headers")
        return line
    def __getattr__(self, key):
        return getattr(self.stream, key)


class BoundedResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = HeaderBudgetReader(self.fp)


def end_to_end(headers):
    excluded = set(HOP)
    for key, value in headers:
        if key.lower() == "connection":
            excluded.update(part.strip().lower() for part in value.split(","))
    return [(key, value) for key, value in headers if key.lower() not in excluded]


def validate_request(method, target, headers, body):
    if method not in METHODS:
        raise ProxyFailure(405, "METHOD")
    if not isinstance(target, str) or len(target) > MAX_LINE or not target.startswith("/") or target.startswith("//") or any(ord(c) < 32 or ord(c) == 127 for c in target):
        raise ProxyFailure(400, "TARGET")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or not (parsed.path.startswith("/api/v1/") or parsed.path in {"/health", "/openapi.json", "/control/start-case", "/control/snapshot", "/control/stop"}):
        raise ProxyFailure(404, "ROUTE")
    lengths = [value for key, value in headers if key.lower() == "content-length"]
    if len(lengths) > 1 or any(key.lower() in {"transfer-encoding", "upgrade", "expect"} for key, _ in headers):
        raise ProxyFailure(400, "FRAMING")
    if lengths and (not lengths[0].isdigit() or int(lengths[0]) != len(body)):
        raise ProxyFailure(400, "CONTENT_LENGTH")
    if len(body) > MAX_REQUEST_BODY:
        raise ProxyFailure(413, "REQUEST_LIMIT")
    if sum(len(key) + len(value) + 4 for key, value in headers) > MAX_HEADERS:
        raise ProxyFailure(431, "HEADERS_LIMIT")


def forward(method, target, headers, body, connection_factory=http.client.HTTPConnection):
    validate_request(method, target, headers, body)
    connection = connection_factory(BACKEND_HOST, BACKEND_PORT, timeout=CONNECT_TIMEOUT)
    connection.response_class = BoundedResponse
    timer, deadline_socket = None, None
    expired = threading.Event()
    def expire():
        expired.set()
        try:
            # HTTPConnection may clear .sock after HTTP/1.0 headers while the
            # response still owns its file. The duplicate controls that same
            # connection until its bounded body read has finished.
            if deadline_socket:
                deadline_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
    try:
        connection.connect()
        connection.sock.settimeout(READ_TIMEOUT)
        deadline_socket = connection.sock.dup()
        # A total exchange deadline also caps slow-drip headers/body, beyond
        # socket inactivity timeouts. Only the fixed backend socket is closed.
        timer = threading.Timer(READ_TIMEOUT, expire)
        timer.daemon = True
        timer.start()
        connection.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
        for key, value in end_to_end(headers):
            connection.putheader(key, value)
        connection.endheaders(body)
        response = connection.getresponse()
        result_headers = response.getheaders()
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BODY):
            raise ProxyFailure(502, "RESPONSE_LIMIT")
        data = response.read(MAX_RESPONSE_BODY + 1)
        if expired.is_set():
            raise ProxyFailure(504, "BACKEND_TIMEOUT")
        if len(data) > MAX_RESPONSE_BODY:
            raise ProxyFailure(502, "RESPONSE_LIMIT")
        if method != "HEAD" and length is not None and len(data) != int(length):
            raise ProxyFailure(502, "INCOMPLETE_RESPONSE")
        return response.status, end_to_end(result_headers), data
    except ProxyFailure:
        raise
    except (TimeoutError, socket.timeout):
        raise ProxyFailure(504, "BACKEND_TIMEOUT") from None
    except Exception:
        if expired.is_set():
            raise ProxyFailure(504, "BACKEND_TIMEOUT") from None
        raise ProxyFailure(502, "BACKEND_UNAVAILABLE") from None
    finally:
        if timer:
            timer.cancel()
        connection.close()
        if deadline_socket:
            deadline_socket.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "FixedSyntheticAPI"
    sys_version = ""
    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)
    def log_message(self, *args):
        pass
    def log_error(self, *args):
        pass
    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.send_response_only(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def parse_request(self):
        if len(self.raw_requestline) > MAX_LINE:
            self.requestline = self.request_version = self.command = ""
            self.send_error(414)
            return False
        original = self.rfile
        try:
            self.rfile = HeaderBudgetReader(original)
            return super().parse_request()
        finally:
            self.rfile = original
    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(MAX_LINE + 1)
            if len(self.raw_requestline) > MAX_LINE:
                self.requestline = self.request_version = self.command = ""
                self.send_error(414)
                self.close_connection = True
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            handler = getattr(self, "do_" + self.command, None)
            if handler is None:
                self.send_error(405)
                return
            handler()
            self.wfile.flush()
        except (TimeoutError, socket.timeout):
            self.close_connection = True
    def handle_request(self):
        self.close_connection = True
        try:
            headers = list(self.headers.items())
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()) or self.headers.get("Transfer-Encoding") or self.headers.get("Expect"):
                raise ProxyFailure(400, "FRAMING")
            size = int(lengths[0]) if lengths else 0
            if size > MAX_REQUEST_BODY:
                raise ProxyFailure(413, "REQUEST_LIMIT")
            body = self.rfile.read(size) if size else b""
            if len(body) != size:
                raise ProxyFailure(400, "INCOMPLETE_REQUEST")
            status, response_headers, data = forward(self.command, self.path, headers, body)
            self.send_response_only(status)
            for key, value in response_headers:
                self.send_header(key, value)
            if not any(key.lower() == "content-length" for key, _ in response_headers):
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except ProxyFailure as failure:
            self.send_error(failure.status)
        except (TimeoutError, socket.timeout):
            self.send_error(408)
        except Exception:
            self.send_error(502)
    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = handle_request
    do_CONNECT = do_TRACE = handle_request


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(CONNECT_TIMEOUT)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()
    def handle_error(self, request, client_address):
        pass  # Never log request paths, headers or bodies.


if __name__ == "__main__":
    # No environment override, public host port, alternate origin or CLI target.
    with Server(("0.0.0.0", 8769), Handler) as server:
        server.serve_forever(poll_interval=0.2)
