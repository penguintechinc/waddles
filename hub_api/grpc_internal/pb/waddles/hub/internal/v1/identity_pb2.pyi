from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class MatchKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MATCH_KIND_UNSPECIFIED: _ClassVar[MatchKind]
    MATCH_KIND_MENTION: _ClassVar[MatchKind]
    MATCH_KIND_HANDLE: _ClassVar[MatchKind]

MATCH_KIND_UNSPECIFIED: MatchKind
MATCH_KIND_MENTION: MatchKind
MATCH_KIND_HANDLE: MatchKind

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
    def __init__(
        self,
        tenant_id: _Optional[str] = ...,
        platform: _Optional[str] = ...,
        platform_user_id: _Optional[str] = ...,
        handle: _Optional[str] = ...,
    ) -> None: ...

class MintEphemeralPseudonymsRequest(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[MintEphemeralPseudonymRequest]
    def __init__(
        self, items: _Optional[_Iterable[_Union[MintEphemeralPseudonymRequest, _Mapping]]] = ...
    ) -> None: ...

class EphemeralPseudonym(_message.Message):
    __slots__ = ("platform_user_id", "pseudonym")
    PLATFORM_USER_ID_FIELD_NUMBER: _ClassVar[int]
    PSEUDONYM_FIELD_NUMBER: _ClassVar[int]
    platform_user_id: str
    pseudonym: str
    def __init__(
        self, platform_user_id: _Optional[str] = ..., pseudonym: _Optional[str] = ...
    ) -> None: ...

class MintEphemeralPseudonymsResponse(_message.Message):
    __slots__ = ("pseudonyms",)
    PSEUDONYMS_FIELD_NUMBER: _ClassVar[int]
    pseudonyms: _containers.RepeatedCompositeFieldContainer[EphemeralPseudonym]
    def __init__(
        self, pseudonyms: _Optional[_Iterable[_Union[EphemeralPseudonym, _Mapping]]] = ...
    ) -> None: ...

class ResolveDisplayNamesRequest(_message.Message):
    __slots__ = ("tenant_id", "uuids")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    UUIDS_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    uuids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self, tenant_id: _Optional[str] = ..., uuids: _Optional[_Iterable[str]] = ...
    ) -> None: ...

class ResolvedDisplayName(_message.Message):
    __slots__ = ("uuid", "display_name", "is_hub_user")
    UUID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    IS_HUB_USER_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    display_name: str
    is_hub_user: bool
    def __init__(
        self,
        uuid: _Optional[str] = ...,
        display_name: _Optional[str] = ...,
        is_hub_user: _Optional[bool] = ...,
    ) -> None: ...

class ResolveDisplayNamesResponse(_message.Message):
    __slots__ = ("names", "unresolved_uuids")
    NAMES_FIELD_NUMBER: _ClassVar[int]
    UNRESOLVED_UUIDS_FIELD_NUMBER: _ClassVar[int]
    names: _containers.RepeatedCompositeFieldContainer[ResolvedDisplayName]
    unresolved_uuids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        names: _Optional[_Iterable[_Union[ResolvedDisplayName, _Mapping]]] = ...,
        unresolved_uuids: _Optional[_Iterable[str]] = ...,
    ) -> None: ...

class ResolveHandleRequest(_message.Message):
    __slots__ = ("tenant_id", "platform", "target")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    tenant_id: str
    platform: str
    target: str
    def __init__(
        self,
        tenant_id: _Optional[str] = ...,
        platform: _Optional[str] = ...,
        target: _Optional[str] = ...,
    ) -> None: ...

class ResolveHandleResponse(_message.Message):
    __slots__ = ("uuid", "match_kind")
    UUID_FIELD_NUMBER: _ClassVar[int]
    MATCH_KIND_FIELD_NUMBER: _ClassVar[int]
    uuid: str
    match_kind: MatchKind
    def __init__(
        self, uuid: _Optional[str] = ..., match_kind: _Optional[_Union[MatchKind, str]] = ...
    ) -> None: ...
