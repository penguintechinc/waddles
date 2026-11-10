from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ScopeStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SCOPE_STATUS_UNSPECIFIED: _ClassVar[ScopeStatus]
    SCOPE_STATUS_RESOLVED: _ClassVar[ScopeStatus]
    SCOPE_STATUS_NOT_FOUND: _ClassVar[ScopeStatus]
    SCOPE_STATUS_AMBIGUOUS: _ClassVar[ScopeStatus]
    SCOPE_STATUS_INVALID: _ClassVar[ScopeStatus]
SCOPE_STATUS_UNSPECIFIED: ScopeStatus
SCOPE_STATUS_RESOLVED: ScopeStatus
SCOPE_STATUS_NOT_FOUND: ScopeStatus
SCOPE_STATUS_AMBIGUOUS: ScopeStatus
SCOPE_STATUS_INVALID: ScopeStatus

class ResolveScopeRequest(_message.Message):
    __slots__ = ("platform", "source_id")
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    platform: str
    source_id: str
    def __init__(self, platform: _Optional[str] = ..., source_id: _Optional[str] = ...) -> None: ...

class ResolveScopeResponse(_message.Message):
    __slots__ = ("tenant_id", "community_id", "tenant_slug", "community_name")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_SLUG_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_NAME_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    tenant_slug: str
    community_name: str
    def __init__(self, tenant_id: _Optional[int] = ..., community_id: _Optional[int] = ..., tenant_slug: _Optional[str] = ..., community_name: _Optional[str] = ...) -> None: ...

class ResolveScopeBatchRequest(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[ResolveScopeRequest]
    def __init__(self, items: _Optional[_Iterable[_Union[ResolveScopeRequest, _Mapping]]] = ...) -> None: ...

class ScopeResult(_message.Message):
    __slots__ = ("platform", "source_id", "status", "scope")
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    platform: str
    source_id: str
    status: ScopeStatus
    scope: ResolveScopeResponse
    def __init__(self, platform: _Optional[str] = ..., source_id: _Optional[str] = ..., status: _Optional[_Union[ScopeStatus, str]] = ..., scope: _Optional[_Union[ResolveScopeResponse, _Mapping]] = ...) -> None: ...

class ResolveScopeBatchResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[ScopeResult]
    def __init__(self, results: _Optional[_Iterable[_Union[ScopeResult, _Mapping]]] = ...) -> None: ...
