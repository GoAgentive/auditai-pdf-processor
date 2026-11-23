"""The in-cluster HTTP entrypoint (server.py) and its storage I/O (storage_io.py).

One local HTTP server plays every remote: it serves the PDF behind `input_url`,
answers the Entra token exchange, and records Blob PUTs — so the Azure path
(Workload Identity token, `x-ms-blob-type`, `azblob://` manifest key) is
checked without a cloud.
"""

import json
import pathlib
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import fitz
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


# Enough words per page to pass the processor's own quality gate (≥ 75).
_PARAGRAPH = " ".join(
    f"Line {n} of the ledger shows revenue of {n},234.56 recognised in the period under audit."
    for n in range(12)
)


def _pdf(pages: int) -> bytes:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 540, 760), f"Page {n + 1}. {_PARAGRAPH}", fontsize=10)
    return doc.tobytes()


class _Remote(BaseHTTPRequestHandler):
    pdf = b""
    puts = []
    token_requests = []

    def do_GET(self):
        self._reply(200, self.pdf, "application/pdf")

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).token_requests.append(body.decode())
        self._reply(200, json.dumps({"access_token": "entra-token", "expires_in": 3599}).encode())

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).puts.append((self.path, headers, body))
        self._reply(201, b"")

    def _reply(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture()
def remote(tmp_path, monkeypatch):
    _Remote.puts, _Remote.token_requests = [], []
    server, base = _serve(_Remote)
    token_file = tmp_path / "token"
    token_file.write_text("k8s-sa-jwt\n")
    monkeypatch.setenv("STORAGE_PROVIDER", "azure_blob")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "stauditaitest")
    monkeypatch.setenv("AZURE_STORAGE_BLOB_ENDPOINT", base)
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-1")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", base)
    yield base
    server.shutdown()


@pytest.fixture()
def service():
    import server as pdf_server

    httpd, base = _serve(pdf_server.Handler)
    yield base
    httpd.shutdown()


def _invoke(service, event):
    request = urllib.request.Request(
        f"{service}/invoke",
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_healthz(service):
    with urllib.request.urlopen(f"{service}/healthz") as response:
        assert response.status == 200


def test_small_document_comes_back_inline(remote, service):
    _Remote.pdf = _pdf(2)

    status, result = _invoke(service, {"input_url": f"{remote}/app/doc.pdf?sig=x", "request_id": "r1"})

    assert status == 200
    assert result["success"] is True
    assert result["document_info"]["page_count"] == 2
    assert result["word_count"] > 0
    assert _Remote.puts == []


def test_large_document_writes_pages_to_blob_with_workload_identity(remote, service):
    _Remote.pdf = _pdf(21)

    status, result = _invoke(
        service,
        {
            "input_url": f"{remote}/app/big.pdf?sig=x",
            "request_id": "r2",
            "output_bucket": "app-bucket",
        },
    )

    assert status == 200
    assert result["output_mode"] == "s3"
    assert result["manifest_key"].startswith("azblob://app-bucket/_lambda_results/r2/")
    assert result["page_count"] == 21

    # 21 pages + the manifest, all bearer-authorized block blobs.
    assert len(_Remote.puts) == 22
    for path, headers, _ in _Remote.puts:
        assert path.startswith("/app-bucket/_lambda_results/r2/")
        assert headers["authorization"] == "Bearer entra-token"
        assert headers["x-ms-blob-type"] == "BlockBlob"

    # One token exchange, presenting the projected service-account token.
    assert len(_Remote.token_requests) == 1
    assert "client_assertion=k8s-sa-jwt" in _Remote.token_requests[0]


def test_rejects_a_non_http_input_url(service):
    status, result = _invoke(service, {"input_url": "file:///etc/passwd"})
    assert status == 400
    assert "input_url" in result["error"]
