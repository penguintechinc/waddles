from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListBindingsRequest(_message.Message):
    __slots__ = ("provider", "capability", "after", "limit")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    CAPABILITY_FIELD_NUMBER: _ClassVar[int]
    AFTER_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    provider: str
    capability: str
    after: int
    limit: int
    def __init__(self, provider: _Optional[str] = ..., capability: _Optional[str] = ..., after: _Optional[int] = ..., limit: _Optional[int] = ...) -> None: ...

class ListBindingsResponse(_message.Message):
    __slots__ = ("items", "next_after")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_AFTER_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[ConnectionBinding]
    next_after: int
    def __init__(self, items: _Optional[_Iterable[_Union[ConnectionBinding, _Mapping]]] = ..., next_after: _Optional[int] = ...) -> None: ...

class ConnectionBinding(_message.Message):
    __slots__ = ("grant_id", "connection_id", "community_id", "tenant_id", "provider", "resource_type", "resource_id", "capabilities", "expires_at", "updated_at", "metadata", "kind", "secret_version")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    GRANT_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    SECRET_VERSION_FIELD_NUMBER: _ClassVar[int]
    grant_id: int
    connection_id: int
    community_id: int
    tenant_id: int
    provider: str
    resource_type: str
    resource_id: str
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    expires_at: str
    updated_at: str
    metadata: _containers.ScalarMap[str, str]
    kind: int
    secret_version: int
    def __init__(self, grant_id: _Optional[int] = ..., connection_id: _Optional[int] = ..., community_id: _Optional[int] = ..., tenant_id: _Optional[int] = ..., provider: _Optional[str] = ..., resource_type: _Optional[str] = ..., resource_id: _Optional[str] = ..., capabilities: _Optional[_Iterable[str]] = ..., expires_at: _Optional[str] = ..., updated_at: _Optional[str] = ..., metadata: _Optional[_Mapping[str, str]] = ..., kind: _Optional[int] = ..., secret_version: _Optional[int] = ...) -> None: ...

class ResolveConnectionRequest(_message.Message):
    __slots__ = ("community_id", "connection_id")
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    community_id: int
    connection_id: int
    def __init__(self, community_id: _Optional[int] = ..., connection_id: _Optional[int] = ...) -> None: ...

class ResolveConnectionResponse(_message.Message):
    __slots__ = ("community_id", "connection_id", "provider", "kind", "access_token", "expires_at", "endpoint", "secret", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    ACCESS_TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    SECRET_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    community_id: int
    connection_id: int
    provider: str
    kind: int
    access_token: str
    expires_at: str
    endpoint: ResolveEndpoint
    secret: ResolveSecret
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, community_id: _Optional[int] = ..., connection_id: _Optional[int] = ..., provider: _Optional[str] = ..., kind: _Optional[int] = ..., access_token: _Optional[str] = ..., expires_at: _Optional[str] = ..., endpoint: _Optional[_Union[ResolveEndpoint, _Mapping]] = ..., secret: _Optional[_Union[ResolveSecret, _Mapping]] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ResolveEndpoint(_message.Message):
    __slots__ = ("url", "region", "function", "project")
    URL_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_FIELD_NUMBER: _ClassVar[int]
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    url: str
    region: str
    function: str
    project: str
    def __init__(self, url: _Optional[str] = ..., region: _Optional[str] = ..., function: _Optional[str] = ..., project: _Optional[str] = ...) -> None: ...

class ResolveSecret(_message.Message):
    __slots__ = ("scheme", "fields")
    class FieldsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    SCHEME_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    scheme: int
    fields: _containers.ScalarMap[str, str]
    def __init__(self, scheme: _Optional[int] = ..., fields: _Optional[_Mapping[str, str]] = ...) -> None: ...
