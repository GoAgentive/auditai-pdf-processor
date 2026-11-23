"""HTTP entrypoint for running the pdf-processor as an in-cluster service.

    POST /invoke   body: the Lambda event (JSON), with `input_url` (a presigned
                   GET URL) in place of `s3_path`. Answers with the Lambda
                   result's statusCode and its JSON body.
    GET  /healthz  200 once the module (PyMuPDF included) has loaded.

One document at a time per process, like one Lambda instance: extraction
already fans out across every core with multiprocessing, so concurrent requests
would only contend for them. Scale with replicas behind the Service instead.

Run: `python server.py` (PORT, default 8080). Image: Dockerfile.server.
"""

import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from index import lambda_handler

logger = logging.getLogger("pdf_processor.server")

MAX_BODY_BYTES = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path != "/invoke":
            self._send(404, b'{"error":"not found"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(400, b'{"error":"body must be a JSON event under 1 MB"}')
            return

        try:
            event = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, b'{"error":"body is not JSON"}')
            return

        result = lambda_handler(event, None)
        body = result.get("body", "{}")
        self._send(int(result.get("statusCode", 500)), body.encode() if isinstance(body, str) else body)

    def _send(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.info("%s %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Handler)

    # Kubernetes sends SIGTERM on scale-down; finish the request in flight
    # (the Deployment's grace period covers the 300 s budget), then exit.
    # shutdown() blocks until serve_forever returns, so it can't run on the
    # thread serving.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    logger.info("pdf-processor listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
