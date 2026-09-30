from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf.internal import containers as _containers

DESCRIPTOR: _descriptor.FileDescriptor

class MintEphemeralPseudonymRequest(_message.Message):
    __slots__ = ("tenant_id", "platform", "platform_user_id", "handle")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_USER_ID_FIELD_NUMBER: _ClassVar[int]
    HANDLE_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    platform: str
    platform_user_id: str
    handle: str
    def __init__(self, tenant_id: str | None = ..., platform: str | None = ..., platform_user_id: str | None = ..., handle: str | None = ...) -> None: ...

class MintEphemeralPseudonymsRequest(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[MintEphemeralPseudonymRequest]
    def __init__(self, items: _Iterable[MintEphemeralPseudonymRequest | _Mapping] | None = ...) -> None: ...

class EphemeralPseudonym(_message.Message):
    __slots__ = ("platform_user_id", "pseudonym")
    PLATFORM_USER_ID_FIELD_NUMBER: _ClassVar[int]
    PSEUDONYM_FIELD_NUMBER: _ClassVar[int]
    platform_user_id: str
    pseudonym: str
    def __init__(self, platform_user_id: str | None = ..., pseudonym: str | None = ...) -> None: ...

class MintEphemeralPseudonymsResponse(_message.Message):
    __slots__ = ("pseudonyms",)
    PSEUDONYMS_FIELD_NUMBER: _ClassVar[int]
    pseudonyms: _containers.RepeatedCompositeFieldContainer[EphemeralPseudonym]
    def __init__(self, pseudonyms: _Iterable[EphemeralPseudonym | _Mapping] | None = ...) -> None: ...

class ResolveDisplayNamesRequest(_message.Message):
    __slots__ = ("tenant_id", "uuids")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    UUIDS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    uuids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, tenant_id: str | None = ..., uuids: _Iterable[str] | None = ...) -> None: ...

class ResolvedDisplayName(_message.Message):
    __slots__ = ("uuid", "display_name", "is_hub_user")
    UUID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    IS_HUB_USER_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    display_name: str
    is_hub_user: bool
    def __init__(self, uuid: str | None = ..., display_name: str | None = ..., is_hub_user: bool | None = ...) -> None: ...

class ResolveDisplayNamesResponse(_message.Message):
    __slots__ = ("names", "unresolved_uuids")
    NAMES_FIELD_NUMBER: _ClassVar[int]
    UNRESOLVED_UUIDS_FIELD_NUMBER: _ClassVar[int]
    names: _containers.RepeatedCompositeFieldContainer[ResolvedDisplayName]
    unresolved_uuids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, names: _Iterable[ResolvedDisplayName | _Mapping] | None = ..., unresolved_uuids: _Iterable[str] | None = ...) -> None: ...
