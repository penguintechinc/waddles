from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class PublishRequest(_message.Message):
    __slots__ = ("api_version", "community_id", "surface_id", "payload_json")
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    SURFACE_ID_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_JSON_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    community_id: int
    surface_id: str
    payload_json: str
    def __init__(
        self,
        api_version: _Optional[str] = ...,
        community_id: _Optional[int] = ...,
        surface_id: _Optional[str] = ...,
        payload_json: _Optional[str] = ...,
    ) -> None: ...

class PublishResponse(_message.Message):
    __slots__ = ("seq",)
    SEQ_FIELD_NUMBER: _ClassVar[int]
    seq: int
    def __init__(self, seq: _Optional[int] = ...) -> None: ...
