from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetActiveContextRequest(_message.Message):
    __slots__ = ("platform", "source_id", "user")
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    platform: str
    source_id: str
    user: str
    def __init__(self, platform: _Optional[str] = ..., source_id: _Optional[str] = ..., user: _Optional[str] = ...) -> None: ...

class SetActiveContextRequest(_message.Message):
    __slots__ = ("platform", "source_id", "user", "community")
    PLATFORM_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    USER_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_FIELD_NUMBER: _ClassVar[int]
    platform: str
    source_id: str
    user: str
    community: str
    def __init__(self, platform: _Optional[str] = ..., source_id: _Optional[str] = ..., user: _Optional[str] = ..., community: _Optional[str] = ...) -> None: ...

class SwitchableCommunity(_message.Message):
    __slots__ = ("public_id", "display_name", "position")
    PUBLIC_ID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    public_id: str
    display_name: str
    position: int
    def __init__(self, public_id: _Optional[str] = ..., display_name: _Optional[str] = ..., position: _Optional[int] = ...) -> None: ...

class GetActiveContextResponse(_message.Message):
    __slots__ = ("context",)
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    context: ActiveContext
    def __init__(self, context: _Optional[_Union[ActiveContext, _Mapping]] = ...) -> None: ...

class SetActiveContextResponse(_message.Message):
    __slots__ = ("context",)
    CONTEXT_FIELD_NUMBER: _ClassVar[int]
    context: ActiveContext
    def __init__(self, context: _Optional[_Union[ActiveContext, _Mapping]] = ...) -> None: ...

class ActiveContext(_message.Message):
    __slots__ = ("community_public_id", "community_id", "community_name", "explicit", "communities")
    COMMUNITY_PUBLIC_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_NAME_FIELD_NUMBER: _ClassVar[int]
    EXPLICIT_FIELD_NUMBER: _ClassVar[int]
    COMMUNITIES_FIELD_NUMBER: _ClassVar[int]
    community_public_id: str
    community_id: int
    community_name: str
    explicit: bool
    communities: _containers.RepeatedCompositeFieldContainer[SwitchableCommunity]
    def __init__(self, community_public_id: _Optional[str] = ..., community_id: _Optional[int] = ..., community_name: _Optional[str] = ..., explicit: _Optional[bool] = ..., communities: _Optional[_Iterable[_Union[SwitchableCommunity, _Mapping]]] = ...) -> None: ...
