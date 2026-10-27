"""Shared hostile-XML fixtures for the XXE regression tests.

Provides a loopback SSRF canary server and a catalogue of XXE / entity-expansion
payloads (external-entity file read, external-entity SSRF, internal entity
expansion, billion laughs, parameter entity, bare DTD).  Used by
``test_caldav_xxe.py`` (Apple CalDAV provider) and
``test_youtube_webhook_xxe.py`` (YouTube PubSubHubbub webhook).
"""

from __future__ import annotations

import http.server
import threading
from dataclasses import dataclass
from typing import Any

ENTITY_NAME = "EVIL_ENTITY_NAME_91c2"

PAYLOAD_IDS = [
    "external-file-read",
    "external-http-ssrf",
    "internal-entity",
    "billion-laughs",
    "parameter-entity",
    "bare-dtd",
]


class Canary:
    """Local HTTP server recording every inbound request (SSRF canary)."""

    def __init__(self) -> None:
        """Start a loopback server on an ephemeral port."""
        hits: list[str] = []
        self.hits = hits

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<!ENTITY ssrf_marker 'reached'>")

            def log_message(self, *args: Any) -> None:
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the server and join its thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@dataclass(slots=True, frozen=True)
class Hostile:
    """A hostile XML prologue plus the text an entity reference would inject."""

    doctype: str
    ref: str
    marker: str


def _billion_laughs() -> str:
    """Return a (size-bounded) nested-entity expansion DOCTYPE."""
    levels = ['<!ENTITY lol0 "lol">']
    for i in range(1, 6):
        prev = f"&lol{i - 1};" * 10
        levels.append(f'<!ENTITY lol{i} "{prev}">')
    return "<!DOCTYPE m [" + "".join(levels) + "]>"


def build_hostile(payload_id: str, canary_url: str) -> Hostile:
    """Build the named hostile payload, aimed at ``canary_url`` where relevant."""
    builders: dict[str, Hostile] = {
        "external-file-read": Hostile(
            f'<!DOCTYPE m [<!ENTITY {ENTITY_NAME} SYSTEM "file:///etc/passwd">]>',
            f"&{ENTITY_NAME};",
            "root:",
        ),
        "external-http-ssrf": Hostile(
            f'<!DOCTYPE m [<!ENTITY {ENTITY_NAME} SYSTEM "{canary_url}/ssrf">]>',
            f"&{ENTITY_NAME};",
            "reached",
        ),
        "internal-entity": Hostile(
            f'<!DOCTYPE m [<!ENTITY {ENTITY_NAME} "EXPANDED_MARKER_7f3a">]>',
            f"&{ENTITY_NAME};",
            "EXPANDED_MARKER_7f3a",
        ),
        "billion-laughs": Hostile(_billion_laughs(), "&lol5;", "lol"),
        "parameter-entity": Hostile(
            f'<!DOCTYPE m [<!ENTITY % {ENTITY_NAME} SYSTEM "{canary_url}/pe"> %{ENTITY_NAME};]>',
            "",
            "reached",
        ),
        "bare-dtd": Hostile("<!DOCTYPE m>", "", "unreachable-marker"),
    }
    return builders[payload_id]
