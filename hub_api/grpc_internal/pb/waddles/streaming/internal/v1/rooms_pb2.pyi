from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class RoomRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ROOM_ROLE_UNSPECIFIED: _ClassVar[RoomRole]
    ROOM_ROLE_GUEST: _ClassVar[RoomRole]
    ROOM_ROLE_MEMBER: _ClassVar[RoomRole]
    ROOM_ROLE_MODERATOR: _ClassVar[RoomRole]
    ROOM_ROLE_ADMIN: _ClassVar[RoomRole]

ROOM_ROLE_UNSPECIFIED: RoomRole
ROOM_ROLE_GUEST: RoomRole
ROOM_ROLE_MEMBER: RoomRole
ROOM_ROLE_MODERATOR: RoomRole
ROOM_ROLE_ADMIN: RoomRole

class Room(_message.Message):
    __slots__ = (
        "room_id",
        "community_id",
        "label",
        "max_publishers",
        "created_at",
        "publisher_ids",
        "subscriber_count",
        "locked",
    )
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    MAX_PUBLISHERS_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_IDS_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIBER_COUNT_FIELD_NUMBER: _ClassVar[int]
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    community_id: str
    label: str
    max_publishers: int
    created_at: int
    publisher_ids: _containers.RepeatedScalarFieldContainer[str]
    subscriber_count: int
    locked: bool
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        community_id: _Optional[str] = ...,
        label: _Optional[str] = ...,
        max_publishers: _Optional[int] = ...,
        created_at: _Optional[int] = ...,
        publisher_ids: _Optional[_Iterable[str]] = ...,
        subscriber_count: _Optional[int] = ...,
        locked: _Optional[bool] = ...,
    ) -> None: ...

class Participant(_message.Message):
    __slots__ = ("participant_id", "publisher", "subscriber", "muted")
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIBER_FIELD_NUMBER: _ClassVar[int]
    MUTED_FIELD_NUMBER: _ClassVar[int]
    participant_id: str
    publisher: bool
    subscriber: bool
    muted: bool
    def __init__(
        self,
        participant_id: _Optional[str] = ...,
        publisher: _Optional[bool] = ...,
        subscriber: _Optional[bool] = ...,
        muted: _Optional[bool] = ...,
    ) -> None: ...

class ListRoomsRequest(_message.Message):
    __slots__ = ("community_id",)
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    def __init__(self, community_id: _Optional[str] = ...) -> None: ...

class ListRoomsResponse(_message.Message):
    __slots__ = ("rooms",)
    ROOMS_FIELD_NUMBER: _ClassVar[int]
    rooms: _containers.RepeatedCompositeFieldContainer[Room]
    def __init__(self, rooms: _Optional[_Iterable[_Union[Room, _Mapping]]] = ...) -> None: ...

class GetRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    def __init__(
        self, community_id: _Optional[str] = ..., room_id: _Optional[str] = ...
    ) -> None: ...

class GetRoomResponse(_message.Message):
    __slots__ = ("room",)
    ROOM_FIELD_NUMBER: _ClassVar[int]
    room: Room
    def __init__(self, room: _Optional[_Union[Room, _Mapping]] = ...) -> None: ...

class ListParticipantsRequest(_message.Message):
    __slots__ = ("community_id", "room_id")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    def __init__(
        self, community_id: _Optional[str] = ..., room_id: _Optional[str] = ...
    ) -> None: ...

class ListParticipantsResponse(_message.Message):
    __slots__ = ("room_id", "locked", "participants")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANTS_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    locked: bool
    participants: _containers.RepeatedCompositeFieldContainer[Participant]
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        locked: _Optional[bool] = ...,
        participants: _Optional[_Iterable[_Union[Participant, _Mapping]]] = ...,
    ) -> None: ...

class CreateRoomRequest(_message.Message):
    __slots__ = ("community_id", "label", "max_publishers")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    MAX_PUBLISHERS_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    label: str
    max_publishers: int
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        label: _Optional[str] = ...,
        max_publishers: _Optional[int] = ...,
    ) -> None: ...

class CreateRoomResponse(_message.Message):
    __slots__ = ("room",)
    ROOM_FIELD_NUMBER: _ClassVar[int]
    room: Room
    def __init__(self, room: _Optional[_Union[Room, _Mapping]] = ...) -> None: ...

class DeleteRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    def __init__(
        self, community_id: _Optional[str] = ..., room_id: _Optional[str] = ...
    ) -> None: ...

class DeleteRoomResponse(_message.Message):
    __slots__ = ("room",)
    ROOM_FIELD_NUMBER: _ClassVar[int]
    room: Room
    def __init__(self, room: _Optional[_Union[Room, _Mapping]] = ...) -> None: ...

class JoinRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "participant_id", "publish", "subscribe", "ttl_secs")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    PUBLISH_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIBE_FIELD_NUMBER: _ClassVar[int]
    TTL_SECS_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    participant_id: str
    publish: bool
    subscribe: bool
    ttl_secs: int
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        publish: _Optional[bool] = ...,
        subscribe: _Optional[bool] = ...,
        ttl_secs: _Optional[int] = ...,
    ) -> None: ...

class JoinRoomResponse(_message.Message):
    __slots__ = ("token", "token_id", "participant_id", "expires_at", "whip_url", "whep_url")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOKEN_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    WHIP_URL_FIELD_NUMBER: _ClassVar[int]
    WHEP_URL_FIELD_NUMBER: _ClassVar[int]
    token: str
    token_id: str
    participant_id: str
    expires_at: int
    whip_url: str
    whep_url: str
    def __init__(
        self,
        token: _Optional[str] = ...,
        token_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        expires_at: _Optional[int] = ...,
        whip_url: _Optional[str] = ...,
        whep_url: _Optional[str] = ...,
    ) -> None: ...

class LeaveRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "participant_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    participant_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class LeaveRoomResponse(_message.Message):
    __slots__ = ("room_id", "participant_id", "sessions_closed")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSIONS_CLOSED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    participant_id: str
    sessions_closed: int
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        sessions_closed: _Optional[int] = ...,
    ) -> None: ...

class KickParticipantRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "participant_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    participant_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class KickParticipantResponse(_message.Message):
    __slots__ = ("room_id", "participant_id", "sessions_closed")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    SESSIONS_CLOSED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    participant_id: str
    sessions_closed: int
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        sessions_closed: _Optional[int] = ...,
    ) -> None: ...

class MuteParticipantRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "participant_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    participant_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class MuteParticipantResponse(_message.Message):
    __slots__ = ("room_id", "participant_id", "muted")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    MUTED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    participant_id: str
    muted: bool
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        muted: _Optional[bool] = ...,
    ) -> None: ...

class UnmuteParticipantRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "participant_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    participant_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class UnmuteParticipantResponse(_message.Message):
    __slots__ = ("room_id", "participant_id", "muted")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    MUTED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    participant_id: str
    muted: bool
    def __init__(
        self,
        room_id: _Optional[str] = ...,
        participant_id: _Optional[str] = ...,
        muted: _Optional[bool] = ...,
    ) -> None: ...

class MuteAllRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class MuteAllResponse(_message.Message):
    __slots__ = ("room_id", "muted")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    MUTED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    muted: int
    def __init__(self, room_id: _Optional[str] = ..., muted: _Optional[int] = ...) -> None: ...

class LockRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class LockRoomResponse(_message.Message):
    __slots__ = ("room_id", "locked")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    locked: bool
    def __init__(self, room_id: _Optional[str] = ..., locked: _Optional[bool] = ...) -> None: ...

class UnlockRoomRequest(_message.Message):
    __slots__ = ("community_id", "room_id", "actor_id", "actor_role")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ID_FIELD_NUMBER: _ClassVar[int]
    ACTOR_ROLE_FIELD_NUMBER: _ClassVar[int]
    community_id: str
    room_id: str
    actor_id: str
    actor_role: RoomRole
    def __init__(
        self,
        community_id: _Optional[str] = ...,
        room_id: _Optional[str] = ...,
        actor_id: _Optional[str] = ...,
        actor_role: _Optional[_Union[RoomRole, str]] = ...,
    ) -> None: ...

class UnlockRoomResponse(_message.Message):
    __slots__ = ("room_id", "locked")
    ROOM_ID_FIELD_NUMBER: _ClassVar[int]
    LOCKED_FIELD_NUMBER: _ClassVar[int]
    room_id: str
    locked: bool
    def __init__(self, room_id: _Optional[str] = ..., locked: _Optional[bool] = ...) -> None: ...
