from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListBundleSchedulesRequest(_message.Message):
    __slots__ = ("tenant_id", "community_id", "app_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    app_id: str
    def __init__(self, tenant_id: _Optional[int] = ..., community_id: _Optional[int] = ..., app_id: _Optional[str] = ...) -> None: ...

class BundleScheduleTarget(_message.Message):
    __slots__ = ("tenant_id", "community_id", "tenant_slug", "community_name", "app_id", "interval_s", "jitter")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_SLUG_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_NAME_FIELD_NUMBER: _ClassVar[int]
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_S_FIELD_NUMBER: _ClassVar[int]
    JITTER_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    tenant_slug: str
    community_name: str
    app_id: str
    interval_s: int
    jitter: float
    def __init__(self, tenant_id: _Optional[int] = ..., community_id: _Optional[int] = ..., tenant_slug: _Optional[str] = ..., community_name: _Optional[str] = ..., app_id: _Optional[str] = ..., interval_s: _Optional[int] = ..., jitter: _Optional[float] = ...) -> None: ...

class ListBundleSchedulesResponse(_message.Message):
    __slots__ = ("targets",)
    TARGETS_FIELD_NUMBER: _ClassVar[int]
    targets: _containers.RepeatedCompositeFieldContainer[BundleScheduleTarget]
    def __init__(self, targets: _Optional[_Iterable[_Union[BundleScheduleTarget, _Mapping]]] = ...) -> None: ...
