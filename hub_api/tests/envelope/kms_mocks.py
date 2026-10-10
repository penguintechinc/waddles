"""Socket-level mock KMS providers (AWS KMS+STS, Google Cloud KMS, Azure Key Vault).

These are real HTTP servers on ``127.0.0.1:<ephemeral>`` -- the adapters under
test speak their genuine wire protocols to them (boto3's SigV4/JSON-1.1/Query
stack for AWS, httpx REST for GCP and Azure), so the *provider API* is the only
thing faked. Everything above it -- the adapters, the envelope service, the key
wrap/unwrap path -- runs for real.

Each mock does real cryptography (AES-GCM with the request's context/AAD for
AWS and GCP, genuine RSA-OAEP-256 for Azure), so "wrong tenant context fails to
unwrap" is proven by the provider math, not asserted by a stub. Failure modes
are switched through the mock's mutable ``Behavior`` and every request is
recorded for assertions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(slots=True)
class MockRequest:
    """One recorded inbound request."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        """Decode the body as JSON."""
        return json.loads(self.body or b"{}")

    def form(self) -> dict[str, str]:
        """Decode the body as ``application/x-www-form-urlencoded``."""
        return {k: v[0] for k, v in parse_qs(self.body.decode()).items()}


@dataclass(slots=True)
class MockResponse:
    """What a mock handler answers with."""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json_body(cls, payload: Any, status: int = 200, **headers: str) -> MockResponse:
        """Build a JSON response."""
        return cls(status=status, body=json.dumps(payload).encode(), headers=dict(headers))


@dataclass(slots=True)
class Behavior:
    """Mutable switches shared by every mock: flip them mid-test to simulate an outage."""

    #: ``None`` = healthy; otherwise answer every key-service request with this HTTP status.
    fail_status: int | None = None
    #: Seconds to stall before answering (drives the adapter's hard timeout).
    delay_s: float = 0.0
    #: Customer revoked the grant (provider-specific "denied" answer).
    deny: bool = False
    #: Provider-specific key state ("Enabled" / "Disabled" ...).
    key_state: str = "Enabled"


class MockServer:
    """A threaded loopback HTTP server dispatching every request to ``handler``."""

    def __init__(self, handler: Callable[[MockRequest], MockResponse]) -> None:
        """Bind to an ephemeral port and start serving on a daemon thread."""
        self.requests: list[MockRequest] = []
        self._lock = threading.Lock()
        #: Optional ``(request, normal_response) -> response`` rewrite applied to every answer --
        #: how tests make a provider misbehave (wrong key echoed, malformed body, ...).
        self.interceptor: Callable[[MockRequest, MockResponse], MockResponse] | None = None
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                parts = urlsplit(self.path)
                request = MockRequest(
                    method=self.command,
                    path=parts.path,
                    query=parse_qs(parts.query),
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=self.rfile.read(length) if length else b"",
                )
                with outer._lock:
                    outer.requests.append(request)
                try:
                    response = handler(request)
                    if outer.interceptor is not None:
                        response = outer.interceptor(request, response)
                except Exception as exc:  # noqa: BLE001 - surface mock bugs as a 500, loudly
                    response = MockResponse.json_body({"mock_error": repr(exc)}, status=599)
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.body)

            do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815

            def log_message(self, *args: Any) -> None:  # silence stderr noise
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>``."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def stop(self) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()

    def calls(self, predicate: Callable[[MockRequest], bool] | None = None) -> list[MockRequest]:
        """Snapshot of recorded requests, optionally filtered."""
        with self._lock:
            snapshot = list(self.requests)
        return [r for r in snapshot if predicate is None or predicate(r)]


_TAG = 16  # bytes of key-id tag at the front of a mock AWS ciphertext blob


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _context_aad(ctx: dict[str, str]) -> bytes:
    return json.dumps(ctx, sort_keys=True, separators=(",", ":")).encode()


@cache
def _service_account_rsa() -> rsa.RSAPrivateKey:
    """One RSA key for every GCP mock in the process (2048-bit keygen is the slow part)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Delayed:
    """Mixin applying ``Behavior.delay_s`` before a handler runs."""

    behavior: Behavior

    def _maybe_delay(self) -> None:
        if self.behavior.delay_s:
            time.sleep(self.behavior.delay_s)


# --------------------------------------------------------------------------- AWS


class MockAwsKms(_Delayed):
    """AWS KMS (JSON 1.1) + STS ``AssumeRole`` (Query/XML) on one endpoint.

    ``trusts`` maps a role ARN to the ExternalId its trust policy requires --
    an ``AssumeRole`` with a different ExternalId is answered ``AccessDenied``
    exactly as real STS would.
    """

    def __init__(self) -> None:
        """Start the server with no keys and no trust relationships."""
        self.behavior = Behavior()
        self.keys: dict[str, bytes] = {}
        self.trusts: dict[str, str] = {}
        self.key_usage: dict[str, str] = {}
        self.expire_session_once = False
        self._issued = 0
        self.server = MockServer(self._handle)

    @property
    def url(self) -> str:
        """Endpoint URL (KMS and STS share it)."""
        return self.server.url

    def add_key(self, arn: str) -> None:
        """Create a symmetric ENCRYPT_DECRYPT key with fresh secret material."""
        self.keys[arn] = os.urandom(32)

    def trust(self, role_arn: str, external_id: str) -> None:
        """Pin ``external_id`` in ``role_arn``'s (virtual) trust policy."""
        self.trusts[role_arn] = external_id

    def _xml_error(self, status: int, code: str) -> MockResponse:
        body = (
            '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error>'
            f"<Type>Sender</Type><Code>{code}</Code><Message>mock</Message></Error>"
            "<RequestId>req-1</RequestId></ErrorResponse>"
        )
        return MockResponse(status=status, body=body.encode(), content_type="text/xml")

    def _json_error(self, status: int, code: str) -> MockResponse:
        return MockResponse.json_body(
            {"__type": code, "message": "mock"}, status=status, **{"x-amzn-ErrorType": code}
        )

    def _assume_role(self, request: MockRequest) -> MockResponse:
        form = request.form()
        role, external = form.get("RoleArn", ""), form.get("ExternalId", "")
        if self.behavior.fail_status:
            return self._xml_error(self.behavior.fail_status, "ServiceUnavailable")
        if self.trusts.get(role) != external:
            return self._xml_error(403, "AccessDenied")
        self._issued += 1
        expires = (datetime.now(UTC) + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = (
            '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
            "<AssumeRoleResult><Credentials>"
            f"<AccessKeyId>ASIAMOCK{self._issued:08d}</AccessKeyId>"
            "<SecretAccessKey>mock-secret</SecretAccessKey>"
            f"<SessionToken>mock-session-{self._issued}</SessionToken>"
            f"<Expiration>{expires}</Expiration></Credentials>"
            "<AssumedRoleUser><AssumedRoleId>AROAMOCK:s</AssumedRoleId>"
            f"<Arn>{role}</Arn></AssumedRoleUser></AssumeRoleResult>"
            "<ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata>"
            "</AssumeRoleResponse>"
        )
        return MockResponse(body=body.encode(), content_type="text/xml")

    def _handle(self, request: MockRequest) -> MockResponse:
        self._maybe_delay()
        target = request.headers.get("x-amz-target", "")
        if not target:
            return self._assume_role(request)
        if self.expire_session_once:
            self.expire_session_once = False
            return self._json_error(400, "ExpiredTokenException")
        if self.behavior.fail_status:
            return self._json_error(self.behavior.fail_status, "KMSInternalException")
        if self.behavior.deny:
            return self._json_error(400, "AccessDeniedException")
        payload = request.json()
        operation = target.split(".", 1)[1]
        arn = payload.get("KeyId", "")
        if operation == "Decrypt" and not arn:
            # Real KMS embeds the key id in the ciphertext blob, so Decrypt needs no KeyId.
            tag = base64.b64decode(payload.get("CiphertextBlob", ""))[:_TAG]
            arn = next((a for a in self.keys if self._tag(a) == tag), "")
        secret = self.keys.get(arn)
        if secret is None:
            return self._json_error(400, "NotFoundException")
        if operation == "DescribeKey":
            return self._describe(arn)
        if self.behavior.key_state != "Enabled":
            return self._json_error(400, "DisabledException")
        if operation == "Encrypt":
            return self._encrypt(arn, secret, payload)
        if operation == "Decrypt":
            return self._decrypt(arn, secret, payload)
        if operation == "GenerateDataKey":
            return self._generate_data_key(arn, secret, payload)
        return self._json_error(400, "UnsupportedOperationException")

    def _describe(self, arn: str) -> MockResponse:
        return MockResponse.json_body(
            {
                "KeyMetadata": {
                    "KeyId": arn.rsplit("/", 1)[-1],
                    "Arn": arn,
                    "KeyState": self.behavior.key_state,
                    "KeyUsage": self.key_usage.get(arn, "ENCRYPT_DECRYPT"),
                    "KeySpec": "SYMMETRIC_DEFAULT",
                    "Enabled": self.behavior.key_state == "Enabled",
                }
            }
        )

    @staticmethod
    def _tag(arn: str) -> bytes:
        """Key-id tag embedded at the front of every ciphertext blob (like real KMS)."""
        return hashlib.sha256(arn.encode()).digest()[:_TAG]

    def _seal(self, arn: str, secret: bytes, plaintext: bytes, ctx: dict[str, str]) -> bytes:
        nonce = os.urandom(12)
        return self._tag(arn) + nonce + AESGCM(secret).encrypt(nonce, plaintext, _context_aad(ctx))

    def _encrypt(self, arn: str, secret: bytes, payload: dict[str, Any]) -> MockResponse:
        blob = self._seal(
            arn,
            secret,
            base64.b64decode(payload["Plaintext"]),
            payload.get("EncryptionContext", {}),
        )
        return MockResponse.json_body(
            {
                "CiphertextBlob": _b64(blob),
                "KeyId": arn,
                "EncryptionAlgorithm": "SYMMETRIC_DEFAULT",
            }
        )

    def _decrypt(self, arn: str, secret: bytes, payload: dict[str, Any]) -> MockResponse:
        blob = base64.b64decode(payload["CiphertextBlob"])
        ctx = payload.get("EncryptionContext", {})
        if blob[:_TAG] != self._tag(arn):
            return self._json_error(400, "IncorrectKeyException")
        body = blob[_TAG:]
        try:
            plaintext = AESGCM(secret).decrypt(body[:12], body[12:], _context_aad(ctx))
        except Exception:  # noqa: BLE001 - any failure is "InvalidCiphertext", like AWS
            return self._json_error(400, "InvalidCiphertextException")
        return MockResponse.json_body(
            {"Plaintext": _b64(plaintext), "KeyId": arn, "EncryptionAlgorithm": "SYMMETRIC_DEFAULT"}
        )

    def _generate_data_key(self, arn: str, secret: bytes, payload: dict[str, Any]) -> MockResponse:
        size = int(payload.get("NumberOfBytes") or 32)
        plaintext = os.urandom(size)
        blob = self._seal(arn, secret, plaintext, payload.get("EncryptionContext", {}))
        return MockResponse.json_body(
            {"CiphertextBlob": _b64(blob), "Plaintext": _b64(plaintext), "KeyId": arn}
        )

    def kms_calls(self, operation: str) -> list[MockRequest]:
        """Recorded KMS requests for ``operation`` (e.g. ``"Decrypt"``)."""
        return self.server.calls(
            lambda r: r.headers.get("x-amz-target", "").endswith(f".{operation}")
        )

    def stop(self) -> None:
        """Stop the server."""
        self.server.stop()


# --------------------------------------------------------------------------- GCP


@dataclass(slots=True)
class GcpKey:
    """One Cloud KMS CryptoKey in the mock."""

    secret: bytes
    labels: dict[str, str] = field(default_factory=dict)
    purpose: str = "ENCRYPT_DECRYPT"
    algorithm: str = "GOOGLE_SYMMETRIC_ENCRYPTION"
    state: str = "ENABLED"


class MockGcpKms(_Delayed):
    """Cloud KMS REST + the Google OAuth token endpoint + the GCE metadata server."""

    def __init__(self) -> None:
        """Generate a service-account keypair and start serving."""
        self.behavior = Behavior()
        self.keys: dict[str, GcpKey] = {}
        self._rsa = _service_account_rsa()
        self.client_email = "waddles-kms@platform-proj.iam.gserviceaccount.com"
        self.reject_next_token_use = 0
        self.token_requests = 0
        self._valid_tokens: set[str] = set()
        self.server = MockServer(self._handle)

    @property
    def url(self) -> str:
        """Base URL."""
        return self.server.url

    @property
    def api_base(self) -> str:
        """The Cloud KMS ``/v1`` base the adapter is pointed at."""
        return f"{self.url}/v1"

    @property
    def token_uri(self) -> str:
        """Token endpoint URL for the service-account JWT-bearer flow."""
        return f"{self.url}/token"

    @property
    def metadata_url(self) -> str:
        """GCE-metadata-server-style token URL."""
        return f"{self.url}/computeMetadata/v1/instance/service-accounts/default/token"

    def service_account_json(self) -> str:
        """A service-account key document whose private key this mock verifies against."""
        pem = self._rsa.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        return json.dumps(
            {
                "type": "service_account",
                "client_email": self.client_email,
                "private_key": pem,
                "private_key_id": "kid-1",
                "token_uri": self.token_uri,
            }
        )

    def add_key(self, name: str, *, labels: dict[str, str] | None = None) -> GcpKey:
        """Create a CryptoKey."""
        key = GcpKey(secret=os.urandom(32), labels=dict(labels or {}))
        self.keys[name] = key
        return key

    def _mint(self) -> MockResponse:
        self.token_requests += 1
        token = f"ya29.mock-{self.token_requests}"
        self._valid_tokens.add(token)
        return MockResponse.json_body(
            {"access_token": token, "expires_in": 3600, "token_type": "Bearer"}
        )

    def _token_endpoint(self, request: MockRequest) -> MockResponse:
        form = request.form()
        try:
            claims = jwt.decode(
                form.get("assertion", ""),
                self._rsa.public_key(),
                algorithms=["RS256"],
                audience=self.token_uri,
            )
        except jwt.PyJWTError:
            return MockResponse.json_body({"error": "invalid_grant"}, status=400)
        if claims.get("iss") != self.client_email or "cloudkms" not in claims.get("scope", ""):
            return MockResponse.json_body({"error": "invalid_grant"}, status=400)
        return self._mint()

    def _error(self, http: int, status: str) -> MockResponse:
        return MockResponse.json_body(
            {"error": {"code": http, "status": status, "message": "m"}}, http
        )

    def _handle(self, request: MockRequest) -> MockResponse:
        self._maybe_delay()
        if request.path == "/token":
            return self._token_endpoint(request)
        if request.path.startswith("/computeMetadata/"):
            if request.headers.get("metadata-flavor") != "Google":
                return MockResponse(status=403, body=b"missing Metadata-Flavor")
            return self._mint()
        auth = request.headers.get("authorization", "")
        if self.reject_next_token_use:
            self.reject_next_token_use -= 1
            self._valid_tokens.clear()
        if not auth.startswith("Bearer ") or auth[7:] not in self._valid_tokens:
            return self._error(401, "UNAUTHENTICATED")
        if self.behavior.fail_status:
            return self._error(self.behavior.fail_status, "UNAVAILABLE")
        if self.behavior.deny:
            return self._error(403, "PERMISSION_DENIED")
        path = request.path.removeprefix("/v1/")
        for suffix in (":encrypt", ":decrypt"):
            if path.endswith(suffix):
                return self._crypt(path.removesuffix(suffix), suffix[1:], request.json())
        key = self.keys.get(path)
        if key is None:
            return self._error(404, "NOT_FOUND")
        return MockResponse.json_body(
            {
                "name": path,
                "purpose": key.purpose,
                "labels": key.labels,
                "primary": {"state": key.state, "algorithm": key.algorithm},
            }
        )

    def _crypt(self, name: str, op: str, body: dict[str, Any]) -> MockResponse:
        key = self.keys.get(name)
        if key is None:
            return self._error(404, "NOT_FOUND")
        if key.state != "ENABLED":
            return self._error(400, "FAILED_PRECONDITION")
        aad = base64.b64decode(body.get("additionalAuthenticatedData", ""))
        if op == "encrypt":
            nonce = os.urandom(12)
            blob = nonce + AESGCM(key.secret).encrypt(
                nonce, base64.b64decode(body["plaintext"]), aad
            )
            return MockResponse.json_body(
                {"name": f"{name}/cryptoKeyVersions/1", "ciphertext": _b64(blob)}
            )
        blob = base64.b64decode(body["ciphertext"])
        try:
            plaintext = AESGCM(key.secret).decrypt(blob[:12], blob[12:], aad)
        except Exception:  # noqa: BLE001
            return self._error(400, "INVALID_ARGUMENT")
        return MockResponse.json_body({"plaintext": _b64(plaintext)})

    def stop(self) -> None:
        """Stop the server."""
        self.server.stop()


# ------------------------------------------------------------------------- Azure


@dataclass(slots=True)
class AzureKeyVersion:
    """One version of an Azure Key Vault RSA key."""

    private: rsa.RSAPrivateKey
    version: str = field(default_factory=lambda: os.urandom(16).hex())


@dataclass(slots=True)
class AzureKey:
    """An Azure Key Vault key: tags, attributes, and every version (newest last)."""

    versions: list[AzureKeyVersion] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    key_ops: list[str] = field(default_factory=lambda: ["wrapKey", "unwrapKey"])
    key_size: int = 2048


class MockAzureKeyVault(_Delayed):
    """Entra token endpoint + Key Vault ``keys`` REST, with genuine RSA-OAEP-256 wrap/unwrap."""

    PLATFORM_SECRET = "platform-app-secret"  # noqa: S105 - test fixture
    PLATFORM_CLIENT_ID = "11111111-2222-3333-4444-555555555555"

    def __init__(self, vault_base: str = "https://contoso-vault.vault.azure.net") -> None:
        """Start serving; ``vault_base`` is the canonical URL the adapter is configured with."""
        self.behavior = Behavior()
        self.vault_base = vault_base
        self.keys: dict[str, AzureKey] = {}
        self.consented: set[str] = set()
        self.token_error: tuple[int, dict[str, Any]] | None = None
        self.reject_next_token_use = 0
        self.token_requests = 0
        self._valid_tokens: set[str] = set()
        self.server = MockServer(self._handle)

    @property
    def url(self) -> str:
        """Base URL."""
        return self.server.url

    def rewrite(self, canonical: str) -> str:
        """Map a canonical vault URL onto this mock (the adapter's test-only URL rewrite)."""
        return canonical.replace(self.vault_base, self.url, 1)

    def add_key(
        self, name: str, *, tags: dict[str, str] | None = None, key_size: int = 2048
    ) -> AzureKey:
        """Create an RSA key with one version."""
        key = AzureKey(tags=dict(tags or {}), key_size=key_size)
        self.rotate_key(name, key)
        self.keys[name] = key
        return key

    def rotate_key(self, name: str, key: AzureKey | None = None) -> None:
        """Add a new key version (old versions stay usable for unwrap, like Key Vault)."""
        target = key or self.keys[name]
        target.versions.append(
            AzureKeyVersion(
                rsa.generate_private_key(public_exponent=65537, key_size=target.key_size)
            )
        )

    def _kid(self, name: str, version: str) -> str:
        return f"{self.vault_base}/keys/{name}/{version}"

    def _token(self, request: MockRequest, directory: str) -> MockResponse:
        self.token_requests += 1
        if self.token_error is not None:
            status, body = self.token_error
            return MockResponse.json_body(body, status=status)
        form = request.form()
        if (
            form.get("client_id") != self.PLATFORM_CLIENT_ID
            or form.get("client_secret") != self.PLATFORM_SECRET
        ):
            return MockResponse.json_body(
                {"error": "invalid_client", "error_codes": [7000215]}, status=401
            )
        if directory not in self.consented:
            return MockResponse.json_body(
                {"error": "unauthorized_client", "error_codes": [700016]}, status=400
            )
        token = f"azt-{directory}-{self.token_requests}"
        self._valid_tokens.add(token)
        return MockResponse.json_body(
            {"access_token": token, "expires_in": 3600, "token_type": "Bearer"}
        )

    def _error(self, status: int, code: str) -> MockResponse:
        return MockResponse.json_body({"error": {"code": code, "message": "m"}}, status)

    def _handle(self, request: MockRequest) -> MockResponse:
        self._maybe_delay()
        token_match = re.fullmatch(r"/([^/]+)/oauth2/v2\.0/token", request.path)
        if token_match:
            return self._token(request, token_match.group(1))
        auth = request.headers.get("authorization", "")
        if self.reject_next_token_use:
            self.reject_next_token_use -= 1
            self._valid_tokens.clear()
        if not auth.startswith("Bearer ") or auth[7:] not in self._valid_tokens:
            return self._error(401, "Unauthorized")
        if self.behavior.fail_status:
            return self._error(self.behavior.fail_status, "ServiceUnavailable")
        if self.behavior.deny:
            return self._error(403, "ForbiddenByRbac")
        match = re.fullmatch(
            r"/keys/(?P<name>[^/]+)(?:/(?P<ver>[0-9a-f]{32}))?(?:/(?P<op>wrapkey|unwrapkey))?",
            request.path,
        )
        if not match or match["name"] not in self.keys:
            return self._error(404, "KeyNotFound")
        key = self.keys[match["name"]]
        if match["op"] is None:
            return self._get(match["name"], key)
        if not key.enabled:
            return self._error(403, "Forbidden")
        return self._wrap_or_unwrap(match["name"], key, match["ver"], match["op"], request.json())

    def _get(self, name: str, key: AzureKey) -> MockResponse:
        newest = key.versions[-1]
        numbers = newest.private.public_key().public_numbers()
        return MockResponse.json_body(
            {
                "key": {
                    "kid": self._kid(name, newest.version),
                    "kty": "RSA",
                    "key_ops": key.key_ops,
                    "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": _b64url(numbers.e.to_bytes(3, "big")),
                },
                "attributes": {"enabled": key.enabled},
                "tags": key.tags,
            }
        )

    def _wrap_or_unwrap(
        self, name: str, key: AzureKey, version: str | None, op: str, body: dict[str, Any]
    ) -> MockResponse:
        if body.get("alg") != "RSA-OAEP-256":
            return self._error(400, "BadParameter")
        oaep = padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        )
        value = _unb64url(body["value"])
        if op == "wrapkey":
            target = key.versions[-1]
            ct = target.private.public_key().encrypt(value, oaep)
            return MockResponse.json_body(
                {"kid": self._kid(name, target.version), "value": _b64url(ct)}
            )
        chosen = next((v for v in key.versions if v.version == version), None)
        if chosen is None:
            return self._error(404, "KeyNotFound")
        try:
            plaintext = chosen.private.decrypt(value, oaep)
        except ValueError:
            return self._error(400, "BadParameter")
        return MockResponse.json_body(
            {"kid": self._kid(name, chosen.version), "value": _b64url(plaintext)}
        )

    def stop(self) -> None:
        """Stop the server."""
        self.server.stop()


def sha256_hex(data: bytes) -> str:
    """Hex SHA-256 helper for assertions."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------- S3


class MockS3:
    """A path-style S3 endpoint that accepts PUTs and records the SSE headers it received."""

    def __init__(self) -> None:
        """Start serving."""
        self.server = MockServer(self._handle)

    @property
    def url(self) -> str:
        """Endpoint URL (what ``S3_ENDPOINT_URL`` points at)."""
        return self.server.url

    def _handle(self, request: MockRequest) -> MockResponse:
        if request.method == "PUT":
            return MockResponse(status=200, headers={"ETag": '"mock-etag"'})
        if request.method == "GET":
            return MockResponse(
                status=404,
                body=b"<Error><Code>NoSuchKey</Code></Error>",
                content_type="application/xml",
            )
        return MockResponse(status=200)

    def puts(self) -> list[MockRequest]:
        """Every PUT received, oldest first."""
        return self.server.calls(lambda r: r.method == "PUT")

    def stop(self) -> None:
        """Stop the server."""
        self.server.stop()
