"""AWS KMS adapter -- the Enterprise BYOK provider (customer-managed KEK).

**Trust model (design Sec6: "workload-identity-federated role, no static
customer credentials stored").** The customer owns the KMS key and an IAM
role. That role's trust policy names Waddles' platform principal and
requires a per-tenant ``ExternalId`` that *Waddles generates*; its
permissions allow only ``kms:Encrypt``/``kms:Decrypt``/``kms:DescribeKey``
on that one key. Waddles assumes the role (``sts:AssumeRole`` +
``ExternalId``) with its own ambient AWS identity -- the standard AWS SDK
credential chain (IRSA / EKS Pod Identity / environment), read by boto3,
never hard-coded and never stored per tenant -- and uses the 15-minute
session credentials in memory only.

The config's ``principal`` (the IAM role ARN) is *required*: it is what closes
the confused-deputy hole. If the ambient identity could call KMS directly,
tenant M could point their config at tenant V's key and make Waddles use it.
With assume-role, M's ExternalId can never satisfy V's trust policy.

**The KEK never leaves KMS.** Only ``Encrypt``/``Decrypt`` of the 32-byte
DEK are performed; every call binds ``EncryptionContext`` =
``{waddles_tenant_id, waddles_purpose}``, so a wrapped DEK copied to another
tenant's row fails to unwrap, and customers may pin IAM conditions on the
same keys. Key aliases are rejected (an alias can be repointed to a
different key); only ``key/`` ARNs are accepted. The ``aws-cn`` partition is
rejected (supply-chain policy); ``aws`` and ``aws-us-gov`` are accepted.

**Failure mapping** (the service applies the policy, this module only
classifies): access-denied / disabled / deleted / pending-deletion /
revoked-trust -> :class:`KmsAccessDeniedError`; throttling / 5xx / timeouts
/ network -> :class:`KmsUnavailableError`; everything else ->
:class:`KmsRejectedError`. Only the AWS error *code* is kept -- never the
provider message, which can echo ARNs and principals.

Blocking boto3 calls run on a dedicated, bounded thread pool (never the
event loop, never the shared default executor) under a hard timeout, so a
hung customer KMS cannot starve hub-api or another tenant.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from services.envelope import metrics
from services.envelope.crypto import (
    CONTEXT_PURPOSE,
    CONTEXT_TENANT_ID,
    KEY_LENGTH,
    PURPOSE_VERIFY,
)
from services.envelope.errors import (
    KmsAccessDeniedError,
    KmsConfigError,
    KmsError,
    KmsRejectedError,
    KmsUnavailableError,
)
from services.envelope.kms_adapter import PROVIDER_AWS, AdapterFactory, CanonicalConfig, KmsAdapter
from services.envelope.models import KEK_KIND_CUSTOMER, KmsKeyInfo, TenantKmsConfig

logger = logging.getLogger(__name__)

# botocore writes wire-level detail at DEBUG: `botocore.endpoint` logs every request's
# headers and body (an Encrypt request carries the base64 plaintext DEK; every call carries
# the STS session token), `botocore.parsers` logs every response body (a Decrypt response
# carries the base64 plaintext DEK), `botocore.auth` logs canonical requests and signatures.
# This platform switches DEBUG on deliberately to chase intermittent bugs, so pin these
# loggers above DEBUG: turning verbosity up must never be able to write key material or
# credentials to a log sink. (Tested: tests/envelope/test_service_byok.py,
# test_no_key_material_or_secret_ever_reaches_a_log_line.)
for _noisy in (
    "botocore.endpoint",
    "botocore.parsers",
    "botocore.auth",
    "botocore.credentials",
    "botocore.httpsession",
    "botocore.hooks",
    "botocore.regions",
    "botocore.retries.standard",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

PROVIDER_ID = PROVIDER_AWS

_KEY_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov):kms:(?P<region>[a-z]{2}(?:-gov)?-[a-z]+-\d):"
    r"(?P<account>\d{12}):key/"
    r"(?:mrk-[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_ROLE_ARN = re.compile(
    r"^arn:(?P<partition>aws|aws-us-gov):iam::(?P<account>\d{12}):role/"
    r"[A-Za-z0-9+=,.@_/-]{1,512}$"
)
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")

#: botocore: bounded connect/read time and at most one automatic retry --
#: the service layer owns the outer timeout and backoff policy.
_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"max_attempts": 2, "mode": "standard"},
    user_agent_extra="waddles-hub-api-envelope",
)

#: Codes meaning the customer (or their account) withdrew access to the KEK.
_DENIED_CODES = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "DisabledException",
        "KMSInvalidStateException",
        "NotFoundException",
    }
)
#: Codes that are transient by AWS's own contract.
_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "LimitExceededException",
        "KMSInternalException",
        "KeyUnavailableException",
        "DependencyTimeoutException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalFailure",
        "InternalError",
        "RequestTimeout",
        "RequestTimeoutException",
        # The platform's *own* credentials are bad -- an outage on our side,
        # not a customer revocation, so it must never read as "access denied".
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
    }
)
_EXPIRED_CODES = frozenset({"ExpiredTokenException", "ExpiredToken"})

_executor_lock = threading.Lock()
_default_executor: ThreadPoolExecutor | None = None


def _kms_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded KMS thread pool (created lazily, size from env)."""
    global _default_executor
    with _executor_lock:
        if _default_executor is None:
            workers = max(2, min(64, int(os.environ.get("ENVELOPE_KMS_MAX_WORKERS", "8"))))
            _default_executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="waddles-kms"
            )
        return _default_executor


def _error_code(exc: ClientError) -> str:
    """Extract the AWS error code from a botocore `ClientError` ("" when absent)."""
    return str(exc.response.get("Error", {}).get("Code", ""))


def map_client_error(exc: ClientError) -> KmsError:
    """Classify a botocore `ClientError` into the provider-neutral KMS error types."""
    code = _error_code(exc)
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    if code in _DENIED_CODES:
        return KmsAccessDeniedError(f"AWS KMS denied the request ({code})", code=code)
    if code in _TRANSIENT_CODES or status >= 500:
        return KmsUnavailableError(f"AWS KMS transient error ({code or status})", code=code)
    return KmsRejectedError(f"AWS KMS rejected the request ({code or status})", code=code)


def validate_aws_config(key_ref: str, region: str | None, principal: str | None) -> CanonicalConfig:
    """Validate a tenant-supplied AWS config (`principal` is the IAM role ARN to assume).

    Raises:
        KmsConfigError: malformed ARN, alias instead of key ARN, unsupported
            partition, region mismatch, role/key partition mismatch, or a
            missing `principal` (required -- see the module docstring).
    """
    key = _KEY_ARN.match(key_ref or "")
    if key is None:
        raise KmsConfigError(
            "key_ref must be a KMS key ARN (arn:aws:kms:<region>:<account>:key/<id>); "
            "aliases are not accepted"
        )
    if not principal:
        raise KmsConfigError(
            "principal (the IAM role ARN) is required: Waddles only reaches customer keys "
            "by assuming a role (with ExternalId), never directly"
        )
    role = _ROLE_ARN.match(principal)
    if role is None:
        raise KmsConfigError(
            "principal must be an IAM role ARN (arn:aws:iam::<account>:role/<name>)"
        )
    if role["partition"] != key["partition"]:
        raise KmsConfigError("principal and key_ref must be in the same AWS partition")
    arn_region = key["region"]
    if region is not None and region != "":
        if not _REGION.match(region):
            raise KmsConfigError("region is not a valid AWS region")
        if region != arn_region:
            raise KmsConfigError("region must match the region in key_ref")
    return CanonicalConfig(key_ref=key_ref, region=arn_region, principal=principal)


@dataclass(slots=True, frozen=True)
class AwsKeySpec:
    """Everything needed to reach one customer key: the key, its region, role and ExternalId."""

    tenant_id: int
    key_arn: str
    region: str
    role_arn: str
    external_id: str


class AwsClientProvider:
    """Builds KMS clients by assuming the customer's role; caches them until near expiry.

    Thread-safe (called from the KMS executor threads). Session credentials
    live only in the boto3 client object in memory -- never logged, never
    persisted. The ambient platform identity comes from boto3's default
    credential chain.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        refresh_margin_s: float = 120.0,
        session_duration_s: int = 900,
        kms_endpoint_url: str | None = None,
        sts_endpoint_url: str | None = None,
    ) -> None:
        """Configure the provider.

        `session_factory` is injectable for tests. The endpoint URLs are
        *operator* settings (VPC / FIPS / GovCloud endpoints, or a local mock) --
        never derived from tenant input.
        """
        self._session_factory = session_factory or boto3.session.Session
        self._kms_endpoint_url = kms_endpoint_url
        self._sts_endpoint_url = sts_endpoint_url
        self._session: Any | None = None
        self._clock = clock
        self._margin = refresh_margin_s
        self._duration = session_duration_s
        self._lock = threading.Lock()
        self._clients: dict[AwsKeySpec, tuple[Any, float]] = {}

    def kms_client(self, spec: AwsKeySpec) -> Any:
        """Return a KMS client authenticated as the customer's assumed role."""
        with self._lock:
            cached = self._clients.get(spec)
            if cached is not None and cached[1] - self._margin > self._clock():
                return cached[0]
            if self._session is None:
                self._session = self._session_factory()
            sts = self._session.client(
                "sts",
                region_name=spec.region,
                endpoint_url=self._sts_endpoint_url,
                config=_CLIENT_CONFIG,
            )
            response = sts.assume_role(
                RoleArn=spec.role_arn,
                RoleSessionName=f"waddles-t{spec.tenant_id}",
                ExternalId=spec.external_id,
                DurationSeconds=self._duration,
            )
            creds = response["Credentials"]
            client = self._session.client(
                "kms",
                region_name=spec.region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                endpoint_url=self._kms_endpoint_url,
                config=_CLIENT_CONFIG,
            )
            self._clients[spec] = (client, float(creds["Expiration"].timestamp()))
            logger.debug(
                "envelope.aws.assumed_role",
                extra={"tenant_id": spec.tenant_id, "region": spec.region},
            )
            return client

    def invalidate(self, spec: AwsKeySpec) -> None:
        """Drop the cached client for `spec` (e.g. after an expired-token error)."""
        with self._lock:
            self._clients.pop(spec, None)


class AwsKmsAdapter:
    """:class:`~services.envelope.kms_adapter.KmsAdapter` over one customer AWS KMS key."""

    def __init__(
        self,
        spec: AwsKeySpec,
        *,
        provider: AwsClientProvider | None = None,
        executor: Executor | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        """Bind to one customer key; `provider`/`executor` are injectable for tests."""
        self._spec = spec
        self._provider = provider or AwsClientProvider()
        self._executor = executor or _kms_executor()
        self._timeout_s = timeout_s

    @property
    def kek_kind(self) -> str:
        """Always ``customer_kms``."""
        return KEK_KIND_CUSTOMER

    @property
    def key_ref(self) -> str:
        """The customer's key ARN."""
        return self._spec.key_arn

    def _invoke(self, operation: Callable[[Any], Any]) -> Any:
        """Run `operation(kms_client)` on a worker thread, refreshing once on expired creds."""
        last: ClientError | None = None
        for attempt in (1, 2):
            try:
                return operation(self._provider.kms_client(self._spec))
            except ClientError as exc:
                if _error_code(exc) in _EXPIRED_CODES and attempt == 1:
                    self._provider.invalidate(self._spec)
                    last = exc
                    continue
                raise
        if last is not None:
            raise last
        raise KmsUnavailableError("AWS KMS client could not be established")

    async def _run(self, name: str, operation: Callable[[Any], Any]) -> Any:
        """Execute one KMS operation off-loop with a hard timeout, mapping errors + telemetry."""
        loop = asyncio.get_running_loop()
        started = time.perf_counter()
        outcome = "ok"
        with metrics.tracer.start_as_current_span(f"envelope.kms.{name}") as span:
            span.set_attribute("kms.provider", PROVIDER_ID)
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(self._executor, self._invoke, operation),
                    timeout=self._timeout_s,
                )
            except TimeoutError:
                outcome = "timeout"
                raise KmsUnavailableError("AWS KMS request timed out", code="Timeout") from None
            except ClientError as exc:
                mapped = map_client_error(exc)
                outcome = type(mapped).__name__
                raise mapped from exc
            except ParamValidationError as exc:
                outcome = "KmsRejectedError"
                raise KmsRejectedError("AWS KMS request failed validation") from exc
            except BotoCoreError as exc:
                outcome = "KmsUnavailableError"
                raise KmsUnavailableError(
                    f"AWS KMS unreachable ({type(exc).__name__})", code=type(exc).__name__
                ) from exc
            except KmsError as exc:
                outcome = type(exc).__name__
                raise
            finally:
                metrics.record_kms_call(PROVIDER_ID, name, outcome, time.perf_counter() - started)

    async def wrap(self, plaintext_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``kms:Encrypt`` the DEK under the customer key, bound to `context`."""
        arn = self._spec.key_arn
        ctx = dict(context)

        def operation(client: Any) -> Any:
            return client.encrypt(
                KeyId=arn,
                Plaintext=plaintext_dek,
                EncryptionContext=ctx,
                EncryptionAlgorithm="SYMMETRIC_DEFAULT",
            )

        response = await self._run("wrap", operation)
        if response.get("KeyId") != arn or not response.get("CiphertextBlob"):
            raise KmsRejectedError("AWS KMS encrypted under an unexpected key")
        return bytes(response["CiphertextBlob"])

    async def unwrap(self, wrapped_dek: bytes, *, context: Mapping[str, str]) -> bytes:
        """``kms:Decrypt`` a wrapped DEK, requiring the same `context` used to wrap it."""
        arn = self._spec.key_arn
        ctx = dict(context)

        def operation(client: Any) -> Any:
            return client.decrypt(
                CiphertextBlob=wrapped_dek,
                KeyId=arn,
                EncryptionContext=ctx,
                EncryptionAlgorithm="SYMMETRIC_DEFAULT",
            )

        response = await self._run("unwrap", operation)
        plaintext = bytes(response.get("Plaintext", b""))
        if response.get("KeyId") != arn or len(plaintext) != KEY_LENGTH:
            raise KmsRejectedError("AWS KMS returned an unexpected key or payload")
        return plaintext

    async def verify(self) -> KmsKeyInfo:
        """Preflight a customer key: assume-role, ``DescribeKey`` checks, then a wrap/unwrap probe.

        Raises:
            KmsConfigError: key is not an Enabled symmetric ENCRYPT_DECRYPT key.
            KmsAccessDeniedError / KmsUnavailableError / KmsRejectedError:
                assume-role or a KMS call failed (see module docstring).
        """
        arn = self._spec.key_arn

        def describe(client: Any) -> Any:
            return client.describe_key(KeyId=arn)

        metadata = (await self._run("describe", describe)).get("KeyMetadata", {})
        state = str(metadata.get("KeyState", "Unknown"))
        if metadata.get("Arn") != arn:
            raise KmsConfigError("KMS key ARN does not match the key it resolves to")
        if state != "Enabled":
            raise KmsConfigError(f"KMS key state is {state}; it must be Enabled")
        if metadata.get("KeyUsage") != "ENCRYPT_DECRYPT" or metadata.get("KeySpec") not in (
            None,
            "SYMMETRIC_DEFAULT",
        ):
            raise KmsConfigError("KMS key must be a symmetric ENCRYPT_DECRYPT key")

        probe = os.urandom(KEY_LENGTH)
        context = {
            CONTEXT_PURPOSE: PURPOSE_VERIFY,
            CONTEXT_TENANT_ID: str(self._spec.tenant_id),
        }
        round_trip = await self.unwrap(await self.wrap(probe, context=context), context=context)
        if not hmac.compare_digest(round_trip, probe):
            raise KmsRejectedError("KMS wrap/unwrap probe did not round-trip")
        return KmsKeyInfo(provider=PROVIDER_ID, key_ref=arn, key_state=state)


def aws_adapter_factory(
    *,
    provider: AwsClientProvider | None = None,
    executor: Executor | None = None,
    timeout_s: float = 10.0,
) -> AdapterFactory:
    """Build the registry factory turning a `TenantKmsConfig` into an :class:`AwsKmsAdapter`.

    A shared `provider` keeps the STS session cache warm across adapters;
    `key_ref` overrides the config's key so rows still wrapped under a
    previous key stay readable until re-wrapped.
    """
    shared = provider or AwsClientProvider()

    def factory(config: TenantKmsConfig, key_ref: str | None) -> KmsAdapter:
        canonical = validate_aws_config(key_ref or config.key_ref, None, config.principal)
        spec = AwsKeySpec(
            tenant_id=config.tenant_id,
            key_arn=canonical.key_ref,
            region=canonical.region or "",
            role_arn=canonical.principal or "",
            external_id=config.external_id,
        )
        return AwsKmsAdapter(spec, provider=shared, executor=executor, timeout_s=timeout_s)

    return factory
