from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetCommunityConfigRequest(_message.Message):
    __slots__ = ("api_version", "tenant_id", "community_id", "app_id")
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    tenant_id: int
    community_id: int
    app_id: str
    def __init__(
        self,
        api_version: _Optional[str] = ...,
        tenant_id: _Optional[int] = ...,
        community_id: _Optional[int] = ...,
        app_id: _Optional[str] = ...,
    ) -> None: ...

class GetCommunityConfigResponse(_message.Message):
    __slots__ = ("tenant_id", "community_id", "app_id", "version", "config_json")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    app_id: str
    version: int
    config_json: bytes
    def __init__(
        self,
        tenant_id: _Optional[int] = ...,
        community_id: _Optional[int] = ...,
        app_id: _Optional[str] = ...,
        version: _Optional[int] = ...,
        config_json: _Optional[bytes] = ...,
    ) -> None: ...

class BatchGetCommunityConfigRequest(_message.Message):
    __slots__ = ("api_version", "scopes")
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCOPES_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    scopes: _containers.RepeatedCompositeFieldContainer[GetCommunityConfigRequest]
    def __init__(
        self,
        api_version: _Optional[str] = ...,
        scopes: _Optional[_Iterable[_Union[GetCommunityConfigRequest, _Mapping]]] = ...,
    ) -> None: ...

class BatchGetCommunityConfigResponse(_message.Message):
    __slots__ = ("configs",)
    CONFIGS_FIELD_NUMBER: _ClassVar[int]
    configs: _containers.RepeatedCompositeFieldContainer[GetCommunityConfigResponse]
    def __init__(
        self, configs: _Optional[_Iterable[_Union[GetCommunityConfigResponse, _Mapping]]] = ...
    ) -> None: ...
