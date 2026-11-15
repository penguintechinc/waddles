from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TenantDekPurpose(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TENANT_DEK_PURPOSE_UNSPECIFIED: _ClassVar[TenantDekPurpose]
    TENANT_DEK_PURPOSE_AT_REST: _ClassVar[TenantDekPurpose]
    TENANT_DEK_PURPOSE_INGEST_STREAM: _ClassVar[TenantDekPurpose]

TENANT_DEK_PURPOSE_UNSPECIFIED: TenantDekPurpose
TENANT_DEK_PURPOSE_AT_REST: TenantDekPurpose
TENANT_DEK_PURPOSE_INGEST_STREAM: TenantDekPurpose

class GetTenantDekRequest(_message.Message):
    __slots__ = ("tenant_id", "purpose", "dek_version", "caller_ephemeral_public_key")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PURPOSE_FIELD_NUMBER: _ClassVar[int]
    DEK_VERSION_FIELD_NUMBER: _ClassVar[int]
    CALLER_EPHEMERAL_PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    purpose: TenantDekPurpose
    dek_version: int
    caller_ephemeral_public_key: bytes
    def __init__(
        self,
        tenant_id: _Optional[int] = ...,
        purpose: _Optional[_Union[TenantDekPurpose, str]] = ...,
        dek_version: _Optional[int] = ...,
        caller_ephemeral_public_key: _Optional[bytes] = ...,
    ) -> None: ...

class GetTenantDekResponse(_message.Message):
    __slots__ = ("enc", "ciphertext", "dek_version", "ttl_seconds")
    ENC_FIELD_NUMBER: _ClassVar[int]
    CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    DEK_VERSION_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    enc: bytes
    ciphertext: bytes
    dek_version: int
    ttl_seconds: int
    def __init__(
        self,
        enc: _Optional[bytes] = ...,
        ciphertext: _Optional[bytes] = ...,
        dek_version: _Optional[int] = ...,
        ttl_seconds: _Optional[int] = ...,
    ) -> None: ...

class GetStreamDekRequest(_message.Message):
    __slots__ = ("tenant_id", "purpose", "version", "caller_ephemeral_public_key")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PURPOSE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CALLER_EPHEMERAL_PUBLIC_KEY_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    purpose: str
    version: int
    caller_ephemeral_public_key: bytes
    def __init__(
        self,
        tenant_id: _Optional[str] = ...,
        purpose: _Optional[str] = ...,
        version: _Optional[int] = ...,
        caller_ephemeral_public_key: _Optional[bytes] = ...,
    ) -> None: ...

class GetStreamDekResponse(_message.Message):
    __slots__ = ("sealed_dek", "version", "ttl_seconds")
    SEALED_DEK_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    sealed_dek: bytes
    version: int
    ttl_seconds: int
    def __init__(
        self,
        sealed_dek: _Optional[bytes] = ...,
        version: _Optional[int] = ...,
        ttl_seconds: _Optional[int] = ...,
    ) -> None: ...
