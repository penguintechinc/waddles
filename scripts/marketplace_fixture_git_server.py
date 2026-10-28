#!/usr/bin/env python3
"""Fixture git (smart-HTTP over TLS) server for the marketplace-sources flag-on E2E.

Serves throwaway bare repos -- one valid prebuilt bundle repo, one valid sandbox bundle repo and a
set of hostile repos -- and exposes a small bearer-protected admin API so the E2E can drive the
auto-update and history-rewrite paths against a REAL remote. It reuses the Task 3 builders
(`hub_api/tests/malicious_source_fixtures.py`) rather than re-implementing them, so it must run
under a Python that has the hub_api deps (cryptography, PyYAML) with `hub_api/` importable.

How it attaches to the egress-proxy allowlist (see docs/guides/marketplace-sources-e2e.md):
hub-api never talks to this server directly -- it clones through the `git-proxy` run mode of the
egress proxy (`MARKETPLACE_GIT_PROXY_URL`), which is CONNECT-only, port 443 only, exact-host
allowlisted (`gitProxy.allowedHosts` -> `GIT_PROXY_ALLOWED_HOSTS` + the CNP toFQDNs) and refuses
EVERY private/loopback/cluster address after resolving DNS itself. Therefore the E2E host MUST:

1. have a DNS name that resolves ONLY to a publicly routable address (an in-cluster Service /
   RFC1918 address is denied by design and the proxy has no test bypass -- none is added);
2. be listed in `gitProxy.allowedHosts` for the E2E release (helm `--set` / values override);
3. present a certificate trusted by the hub-api image (a public CA cert -- `--cert/--key`);
   a self-signed cert is only generated for the standalone smoke (`--self-signed`).

Admin API (all `Authorization: Bearer $FIXTURE_ADMIN_TOKEN`; token read from the ENV, never argv):

    GET  /_admin/repos                          -> {name: {url, head, kind}}
    GET  /_admin/publisher-key                  -> {"public_key": <b64 raw ed25519>}
    POST /_admin/repos/<name>/commit            body {"version": "1.1.0", "sign_for": "<public_id>"}
    POST /_admin/repos/<name>/force-push        rewrites main onto an unrelated root commit
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import ssl
import subprocess  # noqa: S404
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    REPO_ROOT / "hub_api",
    REPO_ROOT / "libs" / "flask_core",
    REPO_ROOT / "libs",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from services.marketplace_publisher_attestation import build_attestation_payload  # noqa: E402
from services.vendor_bundle_authz import source_namespace_prefix  # noqa: E402
from tests.malicious_source_fixtures import (  # noqa: E402
    GIT_ENV,
    add_bundle,
    commit_bare,
    git,
    make_zip_bomb,
    manifest,
)

log = logging.getLogger("marketplace_fixture_git")

PREBUILT_REPOS = ("prebuilt", "autoupdate", "rewrite")
SANDBOX_REPOS = ("sandbox", "sandbox-egress")
HOSTILE_REPOS = (
    "hostile-symlink-escape",
    "hostile-symlink-abs",
    "hostile-zip-bomb",
    "hostile-deep-path",
    "hostile-core-shadow",
    "hostile-other-prefix",
    "hostile-denylisted-dep",
)
BUNDLE_DIR = "hello"
PLACEHOLDER_WASM = b"\0asm\x0d\0\x01\0 fixture placeholder -- NOT an executable component"
SHORT_LEN = 12
_ADMIN_PREFIX = "/_admin/"


class FixtureError(Exception):
    """A fixture-server precondition failed; always surfaced loudly, never swallowed."""


@dataclass(slots=True)
class RepoState:
    """Per-repo bookkeeping: kind, working tree, bare repo and a rewrite counter."""

    name: str
    kind: str
    work: Path
    bare: Path
    rewrites: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class FixtureStore:
    """Builds and mutates the bare repos under `root` (work trees in `root/work`)."""

    def __init__(
        self,
        root: Path,
        *,
        base_url: str,
        wasm: bytes | None,
        entry: str = "e#t",
        sandbox_bundle_dir: Path | None = None,
    ) -> None:
        """Create the store; `wasm=None` is a loud error (use PLACEHOLDER_WASM explicitly)."""
        if wasm is None:
            raise FixtureError(
                "a prebuilt wasm is required (pass --wasm or --allow-placeholder-wasm)"
            )
        self.root = root
        self.base_url = base_url.rstrip("/")
        self.wasm = wasm
        self.entry = entry
        self.sandbox_bundle_dir = sandbox_bundle_dir
        self.repos: dict[str, RepoState] = {}
        self.git_root = root / "git"
        self.git_root.mkdir(parents=True, exist_ok=True)
        self._key = self._load_or_create_key()

    def _load_or_create_key(self) -> Ed25519PrivateKey:
        path = self.root / "publisher.key"
        if path.is_file():
            raw = path.read_bytes()
            return Ed25519PrivateKey.from_private_bytes(raw)
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        return key

    @property
    def publisher_public_key(self) -> str:
        """Base64 of the raw 32-byte Ed25519 public key (the registration format)."""
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw).decode()

    def url(self, name: str) -> str:
        """The https clone URL for repo `name`."""
        return f"{self.base_url}/{name}.git"

    def head(self, name: str) -> str:
        """Current `main` SHA of repo `name`."""
        return git(self._state(name).bare, "rev-parse", "main")

    def _state(self, name: str) -> RepoState:
        try:
            return self.repos[name]
        except KeyError:
            raise FixtureError(f"unknown repo {name!r}") from None

    def _new(self, name: str, kind: str) -> RepoState:
        work = self.root / "work" / name
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        state = RepoState(name, kind, work, self.git_root / f"{name}.git")
        if state.bare.exists():
            shutil.rmtree(state.bare)
        self.repos[name] = state
        return state

    def _prebuilt_manifest(self, version: str, app_id: str | None = None) -> dict[str, object]:
        return manifest(
            app_id or f"acme.{BUNDLE_DIR}",
            version,
            artifact="prebuilt",
            stages={
                "process": {
                    "entry": self.entry,
                    "consumes": [
                        {
                            "platform": "twitch",
                            "event_types": ["chat.message"],
                            "filters": {"command_prefix": ["!hi"]},
                        }
                    ],
                    "produces": [],
                    "config": {},
                }
            },
        )

    def _write_prebuilt(
        self,
        state: RepoState,
        version: str,
        sign_for: str | None,
        app_id: str | None = None,
    ) -> None:
        """(Re)write the prebuilt bundle; signed for `sign_for` source public_id when given."""
        d = add_bundle(state.work, "python", BUNDLE_DIR, self._prebuilt_manifest(version, app_id))
        (d / f"{BUNDLE_DIR}.wasm").write_bytes(self.wasm)
        digest = hashlib.sha256(self.wasm).hexdigest()
        (d / "component.sha256").write_text(digest + "\n")
        sig = d / "component.sig"
        if sign_for is None:
            sig.unlink(missing_ok=True)
            return
        assigned = f"{source_namespace_prefix(uuid.UUID(sign_for).hex[:SHORT_LEN])}{BUNDLE_DIR}"
        payload = build_attestation_payload(app_id=assigned, version=version, digest=digest)
        sig.write_text(base64.b64encode(self._key.sign(payload)).decode())

    def _publish(self, state: RepoState) -> str:
        sha = commit_bare(state.work, state.bare)
        # `git init --bare` defaults HEAD to master; clients must land on `main`.
        git(state.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        return sha

    def build_all(self) -> None:
        """Create every fixture repo (valid prebuilt/sandbox + hostile) as a single root commit."""
        for name in PREBUILT_REPOS:
            state = self._new(name, "prebuilt")
            self._write_prebuilt(state, "1.0.0", None)
            self._publish(state)
        self._build_sandbox("sandbox", egress_probe=False)
        self._build_sandbox("sandbox-egress", egress_probe=True)
        for name in HOSTILE_REPOS:
            self._build_hostile(name)

    def _build_sandbox(self, name: str, *, egress_probe: bool) -> None:
        state = self._new(name, "sandbox")
        man = manifest(artifact="source")
        if self.sandbox_bundle_dir is not None:
            d = add_bundle(state.work, "python", BUNDLE_DIR, man)
            for src in self.sandbox_bundle_dir.rglob("*"):
                if src.is_file() and src.name not in {
                    "hub-manifest.yaml",
                    "bundle.yaml",
                }:
                    dst = d / src.relative_to(self.sandbox_bundle_dir)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
        else:
            d = add_bundle(
                state.work,
                "python",
                BUNDLE_DIR,
                man,
                files={"app.py": "def t(*a):\n    return []\n"},
            )
        if egress_probe:
            # Attempts an outbound connection at build/import time: the in-cluster build Job's
            # CNP must make this fail (the E2E asserts the build either fails loudly or the probe
            # marker reports a blocked connect -- never a successful egress).
            (d / "egress_probe.py").write_text(
                "import socket\n"
                "def probe(host='example.org', port=443):\n"
                "    s = socket.socket()\n"
                "    s.settimeout(3)\n"
                "    s.connect((host, port))\n"
                "probe()\n"
            )
        self._publish(state)

    def _build_hostile(self, name: str) -> None:
        state = self._new(name, "hostile")
        kind = name.removeprefix("hostile-")
        base = self._prebuilt_manifest("1.0.0")
        if kind == "core-shadow":
            base = self._prebuilt_manifest("1.0.0", app_id="waddles.core.hello")
        elif kind == "other-prefix":
            base = self._prebuilt_manifest(
                "1.0.0",
                app_id=f"{source_namespace_prefix('a' * SHORT_LEN)}{BUNDLE_DIR}",
            )
        files = {"requirements.txt": "dashscope==1.0\n"} if kind == "denylisted-dep" else None
        deep = "/".join("d" for _ in range(60)) + "/f.txt"
        d = add_bundle(
            state.work,
            "python",
            BUNDLE_DIR,
            base,
            files={deep: "x"} if kind == "deep-path" else files,
        )
        (d / f"{BUNDLE_DIR}.wasm").write_bytes(self.wasm)
        if kind == "symlink-escape":
            os.symlink("../../../../etc", d / "esc")
        elif kind == "symlink-abs":
            os.symlink("/etc/passwd", d / "abs")
        elif kind == "zip-bomb":
            make_zip_bomb(d / "payload.zip", mb=60)
        self._publish(state)

    def commit(self, name: str, *, version: str, sign_for: str | None) -> str:
        """Add a new commit on `main` (a bumped, optionally publisher-signed bundle)."""
        state = self._state(name)
        if state.kind != "prebuilt":
            raise FixtureError(f"repo {name!r} is {state.kind}; only prebuilt repos take commits")
        with state.lock:
            self._write_prebuilt(state, version, sign_for)
            git(state.work, "add", "-A")
            git(state.work, "commit", "-q", "-m", f"release {version}")
            git(state.work, "push", "-q", str(state.bare), "main")
        return self.head(name)

    def force_push(self, name: str) -> str:
        """Replace `main` with an unrelated root commit (history rewrite); returns the new head."""
        state = self._state(name)
        with state.lock:
            state.rewrites += 1
            (state.work / "REWRITTEN").write_text(f"{state.rewrites}\n")
            git(state.work, "checkout", "-q", "--orphan", f"rw-{state.rewrites}")
            git(state.work, "add", "-A")
            git(state.work, "commit", "-q", "-m", f"rewritten history {state.rewrites}")
            git(state.work, "branch", "-q", "-f", "main", "HEAD")
            git(state.work, "checkout", "-q", "main")
            git(state.work, "push", "-q", "--force", str(state.bare), "main")
        return self.head(name)

    def describe(self) -> dict[str, dict[str, str]]:
        """Index of every repo: clone URL, head SHA, kind."""
        return {
            n: {"url": self.url(n), "head": self.head(n), "kind": s.kind}
            for n, s in sorted(self.repos.items())
        }


def _http_backend() -> str:
    exec_path = subprocess.run(  # noqa: S603
        [shutil.which("git") or "git", "--exec-path"],
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    path = Path(exec_path) / "git-http-backend"
    if not path.is_file():
        raise FixtureError(f"git-http-backend not found at {path}")
    return str(path)


def make_handler(store: FixtureStore, admin_token: str) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class bound to `store` and the admin bearer token."""
    if not admin_token:
        raise FixtureError(
            "an admin token is required (set the env var named by --admin-token-env)"
        )
    backend = _http_backend()

    class Handler(BaseHTTPRequestHandler):
        server_version = "marketplace-fixture-git"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
            log.debug("http " + fmt, *args)

        def _json(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {admin_token}".encode())

        def _admin(self, method: str, path: str) -> None:
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            parts = path.removeprefix(_ADMIN_PREFIX).strip("/").split("/")
            try:
                self._json(*self._route(method, parts, body))
            except FixtureError as exc:
                self._json(404, {"error": str(exc)})
            except (ValueError, subprocess.CalledProcessError) as exc:
                self._json(400, {"error": str(exc)})

        def _route(self, method: str, parts: list[str], body: dict[str, Any]) -> tuple[int, object]:
            if method == "GET" and parts == ["repos"]:
                return 200, store.describe()
            if method == "GET" and parts == ["publisher-key"]:
                return 200, {"public_key": store.publisher_public_key}
            if method == "POST" and len(parts) == 3 and parts[0] == "repos":
                if parts[2] == "commit":
                    sha = store.commit(
                        parts[1],
                        version=str(body.get("version", "")) or _bad("version required"),
                        sign_for=body.get("sign_for"),
                    )
                    return 200, {"head": sha}
                if parts[2] == "force-push":
                    return 200, {"head": store.force_push(parts[1])}
            return 404, {"error": "not found"}

        def _git(self, method: str) -> None:
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "GIT_PROJECT_ROOT": str(store.git_root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "REQUEST_METHOD": method,
                "PATH_INFO": path,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length),
                "REMOTE_ADDR": self.client_address[0],
            }
            if enc := self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = enc
            proc = subprocess.run(  # noqa: S603
                [backend],
                input=self.rfile.read(length),
                env=env,
                capture_output=True,
                check=False,
            )
            head, sep, out = proc.stdout.partition(b"\r\n\r\n")
            if not sep or proc.returncode != 0:
                log.error("git-http-backend failed rc=%s", proc.returncode)
                self._json(502, {"error": "git backend failure"})
                return
            code = 200
            headers: list[tuple[str, str]] = []
            for line in head.decode("latin-1").split("\r\n"):
                key, _, value = line.partition(":")
                if key.lower() == "status":
                    code = int(value.strip().split()[0])
                else:
                    headers.append((key, value.strip()))
            self.send_response(code)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_GET(self) -> None:  # noqa: N802, D102
            if self.path.startswith(_ADMIN_PREFIX):
                self._admin("GET", self.path.partition("?")[0])
            else:
                self._git("GET")

        def do_POST(self) -> None:  # noqa: N802, D102
            if self.path.startswith(_ADMIN_PREFIX):
                self._admin("POST", self.path.partition("?")[0])
            else:
                self._git("POST")

    return Handler


def _bad(message: str) -> Any:
    raise ValueError(message)


def make_server(
    store: FixtureStore,
    host: str,
    port: int,
    *,
    admin_token: str,
    ssl_context: ssl.SSLContext | None,
) -> ThreadingHTTPServer:
    """Create the (optionally TLS-wrapped) server; the caller runs `serve_forever()`."""
    server = ThreadingHTTPServer((host, port), make_handler(store, admin_token))
    if ssl_context is not None:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server


def tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    """TLS >= 1.2 server context from PEM files."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


def self_signed(dest: Path, hostname: str) -> tuple[Path, Path]:
    """Generate a throwaway self-signed cert (standalone smoke only; hub-api will NOT trust it)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        raise FixtureError("openssl is required for --self-signed")
    cert, key = dest / "cert.pem", dest / "key.pem"
    subprocess.run(  # noqa: S603
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            f"/CN={hostname}",
            "-addext",
            f"subjectAltName=DNS:{hostname},DNS:localhost,IP:127.0.0.1",
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return cert, key


def parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI definition (secrets are never accepted as arguments)."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--hostname", required=True, help="public DNS name clients/proxy dial")
    p.add_argument("--host", default="0.0.0.0")  # noqa: S104
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--root", type=Path, default=None, help="state dir (default: fresh temp dir)")
    p.add_argument("--cert", type=Path)
    p.add_argument("--key", type=Path)
    p.add_argument("--self-signed", action="store_true", help="smoke only; hub-api won't trust it")
    p.add_argument("--wasm", type=Path, help="a REAL built component for the valid prebuilt bundle")
    p.add_argument("--allow-placeholder-wasm", action="store_true", help="smoke only")
    p.add_argument("--entry", default="e#t", help="stage entry of the --wasm component")
    p.add_argument("--sandbox-bundle-dir", type=Path)
    p.add_argument("--admin-token-env", default="FIXTURE_ADMIN_TOKEN")
    p.add_argument("--print-index", action="store_true", help="print the repo index and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the fixtures, then serve until interrupted. Non-zero + message on any precondition."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.wasm is not None:
            wasm: bytes | None = args.wasm.read_bytes()
        elif args.allow_placeholder_wasm:
            wasm = PLACEHOLDER_WASM
        else:
            raise FixtureError("pass --wasm <component> (or --allow-placeholder-wasm for a smoke)")
        token = os.environ.get(args.admin_token_env, "")
        root = args.root or Path(tempfile.mkdtemp(prefix="mkt-fixture-git-"))
        root.mkdir(parents=True, exist_ok=True)
        port_suffix = "" if args.port == 443 else f":{args.port}"
        store = FixtureStore(
            root,
            base_url=f"https://{args.hostname}{port_suffix}",
            wasm=wasm,
            entry=args.entry,
            sandbox_bundle_dir=args.sandbox_bundle_dir,
        )
        store.build_all()
        if args.print_index:
            print(json.dumps(store.describe(), indent=2))  # noqa: T201
            return 0
        if args.self_signed:
            cert, key = self_signed(root, args.hostname)
        elif args.cert and args.key:
            cert, key = args.cert, args.key
        else:
            raise FixtureError("pass --cert/--key (or --self-signed for a smoke)")
        server = make_server(
            store,
            args.host,
            args.port,
            admin_token=token,
            ssl_context=tls_context(cert, key),
        )
    except (FixtureError, OSError, subprocess.CalledProcessError) as exc:
        log.error("fixture server cannot start: %s", exc)
        return 2
    log.info(
        "serving %d repos on %s:%d root=%s",
        len(store.repos),
        args.host,
        args.port,
        root,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
