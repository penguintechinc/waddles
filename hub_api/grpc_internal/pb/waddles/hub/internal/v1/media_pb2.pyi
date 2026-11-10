from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class MediaTier(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MEDIA_TIER_UNSPECIFIED: _ClassVar[MediaTier]
    MEDIA_TIER_FREE: _ClassVar[MediaTier]
    MEDIA_TIER_PROFESSIONAL: _ClassVar[MediaTier]
    MEDIA_TIER_ENTERPRISE: _ClassVar[MediaTier]

class ByokStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    BYOK_STATUS_UNSPECIFIED: _ClassVar[ByokStatus]
    BYOK_STATUS_OK: _ClassVar[ByokStatus]
    BYOK_STATUS_NOT_CONFIGURED: _ClassVar[ByokStatus]
    BYOK_STATUS_TIER_REQUIRED: _ClassVar[ByokStatus]
MEDIA_TIER_UNSPECIFIED: MediaTier
MEDIA_TIER_FREE: MediaTier
MEDIA_TIER_PROFESSIONAL: MediaTier
MEDIA_TIER_ENTERPRISE: MediaTier
BYOK_STATUS_UNSPECIFIED: ByokStatus
BYOK_STATUS_OK: ByokStatus
BYOK_STATUS_NOT_CONFIGURED: ByokStatus
BYOK_STATUS_TIER_REQUIRED: ByokStatus

class ResolveByokRequest(_message.Message):
    __slots__ = ("tenant_id", "community_id", "provider")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    provider: str
    def __init__(self, tenant_id: _Optional[int] = ..., community_id: _Optional[int] = ..., provider: _Optional[str] = ...) -> None: ...

class ResolveByokResponse(_message.Message):
    __slots__ = ("status", "tier", "api_key")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TIER_FIELD_NUMBER: _ClassVar[int]
    API_KEY_FIELD_NUMBER: _ClassVar[int]
    status: ByokStatus
    tier: MediaTier
    api_key: str
    def __init__(self, status: _Optional[_Union[ByokStatus, str]] = ..., tier: _Optional[_Union[MediaTier, str]] = ..., api_key: _Optional[str] = ...) -> None: ...

class GetEffectiveTierRequest(_message.Message):
    __slots__ = ("tenant_id", "community_id")
    TENANT_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    tenant_id: int
    community_id: int
    def __init__(self, tenant_id: _Optional[int] = ..., community_id: _Optional[int] = ...) -> None: ...

class GetEffectiveTierResponse(_message.Message):
    __slots__ = ("tier",)
    TIER_FIELD_NUMBER: _ClassVar[int]
    tier: MediaTier
    def __init__(self, tier: _Optional[_Union[MediaTier, str]] = ...) -> None: ...
