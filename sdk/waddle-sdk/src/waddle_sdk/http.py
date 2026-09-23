"""``waddle_transports``-compatible HTTP client over the WIT ``http`` import.

Same call shape as ``waddle_transports.transports.http``'s transport; raises
the same two exception names ``waddle_transports.base`` defines so a
bundle's existing ``except RetryableTransportError`` handling is unchanged.

Binding shapes confirmed via ``componentize-py bindings`` against the
committed ``wit/waddle-bundle/stage.wit``: ``wit_world.imports.http.Request``
(``method, url, headers: list[Header], body: bytes | None, secret_refs: list[tuple[str, str]]``),
``Response`` (``status, headers, body: bytes, truncated``), and ``send(req) ->
Response`` raising the generated ``Err`` (a frozen-dataclass ``Exception``
with one attribute, ``value``, holding the ``Error`` union: ``Error_Denied``,
``Error_Timeout``, ``Error_TooLarge``, ``Error_RateLimited``,
``Error_Transport``, each a ``@dataclass``). This module classifies the error
structurally by class name rather than importing
``wit_world.imports.http.Error_*`` at module scope, since ``wit_world`` is
only importable inside a real component.
"""

from __future__ import annotations

from typing import Any


class RetryableTransportError(Exception):
    """Matches ``waddle_transports.base.RetryableTransportError``."""


class NonRetryableTransportError(Exception):
    """Matches ``waddle_transports.base.NonRetryableTransportError``."""


class SecretRef:
    """An opaque reference to a secret the STAGE resolves and injects as a header.

    The value never enters this component (spec Sec8.3).
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        """Wrap the secret's reference name -- never the secret value itself."""
        self.name = name

    def __repr__(self) -> str:
        """Return a debug-friendly representation."""
        return f"SecretRef({self.name!r})"


def resolve_secret(env_var_name: str) -> SecretRef:
    """Matches ``waddle_transports.signing.resolve_secret``'s name/contract.

    Returns an opaque reference, never a value.
    """
    return SecretRef(env_var_name)


def _classify_http_error(exc: Exception, url: str) -> Exception:
    """Map the WIT ``http.Error`` union, by class name, onto the two transport exceptions."""
    wit_error = getattr(exc, "value", exc)
    case_name = type(wit_error).__name__
    detail = getattr(wit_error, "value", None)
    if case_name == "Error_Timeout":
        return RetryableTransportError(f"timeout calling {url}")
    if case_name == "Error_RateLimited":
        return RetryableTransportError(f"rate limited calling {url}, retry after {detail}ms")
    if case_name == "Error_Denied":
        return NonRetryableTransportError(f"egress denied for {url}: {detail}")
    if case_name == "Error_Transport":
        return RetryableTransportError(f"transport error calling {url}: {detail}")
    if case_name == "Error_TooLarge":
        return NonRetryableTransportError(f"response too large from {url}: {detail} bytes")
    return NonRetryableTransportError(f"unclassified http error calling {url}: {exc}")


class HttpClient:
    """Drop-in for ``waddle_transports.transports.http``'s client shape."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        secret_refs: dict[str, SecretRef] | None = None,
    ) -> dict[str, Any]:
        """Send one HTTP request over the WIT ``http`` import and return a plain dict response."""
        import wit_world

        http_mod = wit_world.imports.http
        header_list = [http_mod.Header(name=k, value=v) for k, v in (headers or {}).items()]
        secret_ref_list = [(k, v.name) for k, v in (secret_refs or {}).items()]
        request = http_mod.Request(
            method=method,
            url=url,
            headers=header_list,
            body=body,
            secret_refs=secret_ref_list,
        )
        try:
            response = http_mod.send(request)
        except Exception as exc:  # noqa: BLE001 - structurally classified, see module docstring
            raise _classify_http_error(exc, url) from exc
        return {
            "status": response.status,
            "headers": {h.name: h.value for h in response.headers},
            "body": bytes(response.body),
            "truncated": response.truncated,
        }

    async def get(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Send a GET request."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        """Send a POST request."""
        return await self.request("POST", url, **kwargs)
