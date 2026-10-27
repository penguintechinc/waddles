from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message

DESCRIPTOR: _descriptor.FileDescriptor

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
    def __init__(self, tenant_id: str | None = ..., purpose: str | None = ..., version: int | None = ..., caller_ephemeral_public_key: bytes | None = ...) -> None: ...

class GetStreamDekResponse(_message.Message):
    __slots__ = ("sealed_dek", "version", "ttl_seconds")
    SEALED_DEK_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    sealed_dek: bytes
    version: int
    ttl_seconds: int
    def __init__(self, sealed_dek: bytes | None = ..., version: int | None = ..., ttl_seconds: int | None = ...) -> None: ...
