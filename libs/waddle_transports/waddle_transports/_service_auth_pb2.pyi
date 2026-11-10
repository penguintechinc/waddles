from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class MintServiceTokenRequest(_message.Message):
    __slots__ = ("api_version", "scope", "community_id", "connection_id")
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    SCOPE_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    scope: str
    community_id: int
    connection_id: int
    def __init__(self, api_version: _Optional[str] = ..., scope: _Optional[str] = ..., community_id: _Optional[int] = ..., connection_id: _Optional[int] = ...) -> None: ...

class MintServiceTokenResponse(_message.Message):
    __slots__ = ("token", "expires_in")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_IN_FIELD_NUMBER: _ClassVar[int]
    token: str
    expires_in: int
    def __init__(self, token: _Optional[str] = ..., expires_in: _Optional[int] = ...) -> None: ...

class GetServiceJwksRequest(_message.Message):
    __slots__ = ("api_version",)
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    def __init__(self, api_version: _Optional[str] = ...) -> None: ...

class GetServiceJwksResponse(_message.Message):
    __slots__ = ("keys",)
    KEYS_FIELD_NUMBER: _ClassVar[int]
    keys: _containers.RepeatedCompositeFieldContainer[Jwk]
    def __init__(self, keys: _Optional[_Iterable[_Union[Jwk, _Mapping]]] = ...) -> None: ...

class Jwk(_message.Message):
    __slots__ = ("kid", "kty", "crv", "alg", "use", "x", "y")
    KID_FIELD_NUMBER: _ClassVar[int]
    KTY_FIELD_NUMBER: _ClassVar[int]
    CRV_FIELD_NUMBER: _ClassVar[int]
    ALG_FIELD_NUMBER: _ClassVar[int]
    USE_FIELD_NUMBER: _ClassVar[int]
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    kid: str
    kty: str
    crv: str
    alg: str
    use: str
    x: str
    y: str
    def __init__(self, kid: _Optional[str] = ..., kty: _Optional[str] = ..., crv: _Optional[str] = ..., alg: _Optional[str] = ..., use: _Optional[str] = ..., x: _Optional[str] = ..., y: _Optional[str] = ...) -> None: ...
