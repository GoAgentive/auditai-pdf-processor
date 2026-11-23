"""Input download and per-page output writes, for S3 or Azure Blob Storage.

The Lambda reads `s3://bucket/key` and writes manifest-mode pages to S3 with
boto3. The in-cluster service (server.py) is handed a presigned GET URL
(`input_url`) instead, and on Azure writes pages to Blob Storage with the
pod's Workload Identity — stdlib only, so the image needs no Azure SDK.

The output store is chosen by STORAGE_PROVIDER ("s3", the default, or
"azure_blob"), matching the app's setting of the same name.
"""

import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request

DOWNLOAD_TIMEOUT_SECONDS = 120
BLOB_API_VERSION = "2022-11-02"
STORAGE_SCOPE = "https://storage.azure.com/.default"
# Refresh an Entra token this long before it expires.
TOKEN_REFRESH_MARGIN_SECONDS = 300


def download_url(url: str, local_path: str) -> None:
    """Stream a presigned GET URL to `local_path`."""
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response, open(
        local_path, "wb"
    ) as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)


def key_from_url(url: str) -> str:
    """The object path of a presigned URL, used to name temp files and hash
    output prefixes the way the Lambda does with its S3 key."""
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.lstrip("/"))


_s3_client = None


def s3_client():
    """boto3's S3 client, created on first use so an Azure pod never needs AWS config."""
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client("s3")
    return _s3_client


def output_client():
    """The client manifest-mode writes go through: boto3 on S3, `AzureBlobWriter`
    on Azure. Both answer `put_object(Bucket=, Key=, Body=, ContentType=)` and
    carry a `uri_scheme` for the manifest key they report."""
    if os.environ.get("STORAGE_PROVIDER", "s3") == "azure_blob":
        return AzureBlobWriter()
    return s3_client()


class _EntraToken:
    """Workload Identity: exchange the projected service-account token for an
    Entra access token (client-credentials grant with a JWT assertion)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}

    def get(self, scope: str) -> str:
        with self._lock:
            token, expires_at = self._cache.get(scope, (None, 0))
            if token and expires_at - TOKEN_REFRESH_MARGIN_SECONDS > time.time():
                return token

            with open(os.environ["AZURE_FEDERATED_TOKEN_FILE"]) as f:
                assertion = f.read().strip()

            authority = os.environ.get(
                "AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/"
            ).rstrip("/")
            form = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": os.environ["AZURE_CLIENT_ID"],
                    "scope": scope,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                }
            ).encode()
            request = urllib.request.Request(
                f"{authority}/{os.environ['AZURE_TENANT_ID']}/oauth2/v2.0/token", data=form
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)

            token = body["access_token"]
            self._cache[scope] = (token, time.time() + int(body.get("expires_in", 3600)))
            return token


_entra = _EntraToken()


class AzureBlobWriter:
    """Writes blobs with a Workload Identity bearer token.

    `Bucket` is a container in AZURE_STORAGE_ACCOUNT, or `<account>/<container>`
    — the same convention as the app's Agentive.Storage.AzureBlob.
    """

    uri_scheme = "azblob"

    def __init__(self):
        self.account = os.environ["AZURE_STORAGE_ACCOUNT"]
        self.endpoint = os.environ.get("AZURE_STORAGE_BLOB_ENDPOINT", "").rstrip("/")

    def _base(self, account: str) -> str:
        if account == self.account and self.endpoint:
            return self.endpoint
        return f"https://{account}.blob.core.windows.net"

    def put_object(self, Bucket: str, Key: str, Body, ContentType: str = "application/octet-stream"):
        account, _, container = Bucket.rpartition("/")
        account = account or self.account
        body = Body.encode() if isinstance(Body, str) else Body
        url = f"{self._base(account)}/{container}/{urllib.parse.quote(Key, safe='/')}"
        request = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Bearer {_entra.get(STORAGE_SCOPE)}",
                "x-ms-version": BLOB_API_VERSION,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-blob-content-type": ContentType,
                "Content-Type": ContentType,
                "Content-Length": str(len(body)),
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
