from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DistributionStage(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DISTRIBUTION_STAGE_UNSPECIFIED: _ClassVar[DistributionStage]
    DISTRIBUTION_STAGE_INGEST: _ClassVar[DistributionStage]
    DISTRIBUTION_STAGE_PROCESS: _ClassVar[DistributionStage]
    DISTRIBUTION_STAGE_ACTION: _ClassVar[DistributionStage]
DISTRIBUTION_STAGE_UNSPECIFIED: DistributionStage
DISTRIBUTION_STAGE_INGEST: DistributionStage
DISTRIBUTION_STAGE_PROCESS: DistributionStage
DISTRIBUTION_STAGE_ACTION: DistributionStage

class ListBundlesRequest(_message.Message):
    __slots__ = ("stage", "community_id")
    STAGE_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    stage: DistributionStage
    community_id: int
    def __init__(self, stage: _Optional[_Union[DistributionStage, str]] = ..., community_id: _Optional[int] = ...) -> None: ...

class ListBundlesResponse(_message.Message):
    __slots__ = ("bundles",)
    BUNDLES_FIELD_NUMBER: _ClassVar[int]
    bundles: _containers.RepeatedCompositeFieldContainer[DistributionBundle]
    def __init__(self, bundles: _Optional[_Iterable[_Union[DistributionBundle, _Mapping]]] = ...) -> None: ...

class DistributionBundle(_message.Message):
    __slots__ = ("app_id", "community_id", "entrypoint", "spec_json", "config_json")
    APP_ID_FIELD_NUMBER: _ClassVar[int]
    COMMUNITY_ID_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_FIELD_NUMBER: _ClassVar[int]
    SPEC_JSON_FIELD_NUMBER: _ClassVar[int]
    CONFIG_JSON_FIELD_NUMBER: _ClassVar[int]
    app_id: str
    community_id: int
    entrypoint: str
    spec_json: bytes
    config_json: bytes
    def __init__(self, app_id: _Optional[str] = ..., community_id: _Optional[int] = ..., entrypoint: _Optional[str] = ..., spec_json: _Optional[bytes] = ..., config_json: _Optional[bytes] = ...) -> None: ...
