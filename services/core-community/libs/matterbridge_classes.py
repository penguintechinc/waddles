from dataclasses import dataclass

@dataclass
class matterbridgePayload:
    username: str
    gateway: str
    account: str
    text: str