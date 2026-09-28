from dataclasses import dataclass
from datetime import datetime


# Use the below for any database (web or direct) info being passed around
@dataclass
class dbinfo:
    host: str
    port: int
    user: str
    password: str
    database: str
    table: str


@dataclass
class event:
    activity: str
    amount: float
    namespace: str
    platform: str
    interface: str
    subInterface: str
    rawText: str


@dataclass
class retvars:
    msg: str
    media: str
    stdout: str


@dataclass
class dbquery:
    columns: list
    table: str
    queryColumn: str
    queryValue: str

# ===============================================
# Below are all the dataclasses for the WaddleDBM
# ===============================================

@dataclass
class module_commands:
    module_id: int
    command_name: str
    action_url: str
    description: str
    request_method: str
    request_parameters: list
    payload_keys: list
    req_priv_list: list


@dataclass
class module:
    name: str
    description: str
    gateway_url: str
    module_type_id: int
    metadata: dict

@dataclass
class module_command_metadata:
    action: str
    description: str
    method: str
    parameters: list
    payload_keys: list 
    req_priv_list: list

@dataclass
class identity:
    name: str
    country: str
    ip_address: str
    browser_fingerprints: list

@dataclass
class community:
    community_name: str
    community_description: str

@dataclass
class identity_label:
    identity_id: int
    community_id: int
    label: str

@dataclass
class community_module:
    module_id: int
    community_id: int
    enabled: bool
    priv_list: list

@dataclass
class role:
    name: str
    community_id: int
    description: str
    priv_list: list
    requirements: list

@dataclass
class community_member:
    community_id: int
    identity_id: int
    role_id: int

@dataclass
class reputation:
    identity_id: int
    community_id: int
    amount: int

@dataclass
class currency:
    community_id: int
    identity_id: int
    amount: int

@dataclass
class gateway_server_type:
    type_name: str
    description: str

@dataclass
class gateway_server:
    name: str
    server_id: int
    server_nick: str
    server_type: int
    protocol: str

@dataclass
class routing:
    channel: str
    community_id: int
    routing_gateway_ids: list
    aliases: list

@dataclass  
class context:
    identity_id: int
    community_id: int

@dataclass
class account_type:
    type_name: str
    description: str

@dataclass
class gateway_account:
    account_name: str
    account_type: int
    is_default: bool

@dataclass
class gateway_type:
    type_name: str
    description: str

@dataclass
class routing_gateway:
    gateway_server: int
    channel_id: str
    gateway_type: int
    activation_key: str
    is_active: bool

@dataclass
class calender:
    community_id: int
    event_name: str
    event_description: str
    event_start: datetime
    event_end: datetime
    not_start_sent: bool
    not_end_sent: bool

@dataclass
class admin_context:
    identity_id: int
    community_id: int
    session_token: str
    session_expires: datetime

@dataclass
class text_response:
    community_id: int
    text_val: str
    response_val: str

@dataclass
class prize_status:
    status_name: str
    description: str

@dataclass
class prize:
    community_id: int
    prize_guid: str
    prize_name: str
    prize_description: str
    winner_identity_id: int
    prize_status: int
    timeout: int

@dataclass
class prize_entry:
    prize_id: int
    identity_id: int

@dataclass
class alias_command:
    community_id: int
    alias_val: str
    command_val: str


# ===================================================================
# Below are the dataclasses for the ouputs of the dbm action calls
# ===================================================================

@dataclass
class message_output:
    msg: str

@dataclass
class data_output:
    data: list
