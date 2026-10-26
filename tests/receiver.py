"""A small local HTTP server for tests that records every request it gets. Stdlib only.

It answers by path: /fail gives 500, /slow waits `slow_seconds` then gives 200,
/redirect gives 302 pointing at /ok, /stall-body sends 200 headers but then
stalls `slow_seconds` before (never) sending the promised body, and anything
else gives 200.

Run it by hand to watch callbacks arrive:  uv run python tests/receiver.py 9000
"""

import sys
import threading
import time
from dataclasses import dataclass
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Received:
    """One request as the receiver saw it."""

    method: str
    path: str
    headers: Message  # header lookups ignore case, e.g. headers["content-type"]
    body: bytes
    arrived_at: float  # time.time() when it arrived


class _Handler(BaseHTTPRequestHandler):
    """Records each request on the server's Receiver, then answers by path."""

    # Handles POST, the method the dispatcher sends.
    def do_POST(self):
        self._record_and_answer()

    # Handles GET too, so a followed redirect (httpx turns a 302 into a GET) is still recorded.
    def do_GET(self):
        self._record_and_answer()

    # Reads the body, records the request, then sends the answer its path asks for.
    def _record_and_answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        receiver = self.server.receiver
        receiver.record(Received(self.command, self.path, self.headers, body, time.time()))
        if self.path == "/slow":
            time.sleep(receiver.slow_seconds)
        if self.path == "/stall-body":
            self._stall_body(receiver.slow_seconds)
            return
        status = {"/fail": 500, "/redirect": 302}.get(self.path, 200)
        try:
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client already gave up, as the timeout test makes it do

    # Sends 200 headers promising a 1 MB body, then waits without sending it,
    # like a callback server that streams a huge or endless response.
    def _stall_body(self, seconds):
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(1024 * 1024))
            self.end_headers()
            self.wfile.flush()
            time.sleep(seconds)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client closed the connection without reading the body, as it should

    # Silences the default one-line-per-request log; Receiver(echo=True) prints instead.
    def log_message(self, format, *args):
        pass


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with a longer queue of waiting connections."""

    # The default of 5 refuses connections when 20 sends arrive at once.
    request_queue_size = 128


class Receiver:
    """Runs the server on 127.0.0.1 in a background thread and keeps every request it gets."""

    # Starts serving right away. Port 0 lets the OS pick a free port.
    def __init__(self, port: int = 0, slow_seconds: float = 2.0, echo: bool = False):
        self.slow_seconds = slow_seconds
        self.echo = echo
        self._requests: list[Received] = []
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.receiver = self
        # A short poll interval makes stop() return quickly (serve_forever checks for it this often).
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()

    # The full URL of a path on this server, e.g. url("/fail").
    def url(self, path: str) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{path}"

    # A copy of every request received so far, oldest first.
    @property
    def requests(self) -> list[Received]:
        with self._lock:
            return list(self._requests)

    # Saves one request. Called from the server's handler threads, hence the lock.
    def record(self, request: Received) -> None:
        with self._lock:
            self._requests.append(request)
        if self.echo:
            print(request.method, request.path, request.body.decode(errors="replace"), flush=True)

    # Stops the server and frees its port.
    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


# Calls `predicate` every `interval` seconds until it returns true. Fails the
# test (AssertionError) if that has not happened within `timeout` seconds.
def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(interval)


# Manual mode: serves on the port given on the command line and prints each request.
if __name__ == "__main__":
    receiver = Receiver(port=int(sys.argv[1]), echo=True)
    print(f"Listening on {receiver.url('/')} - press Ctrl+C to stop", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        receiver.stop()
