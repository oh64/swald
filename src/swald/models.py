from dataclasses import dataclass


@dataclass
class User:
    id: str
    name: str
    avatar_url: str | None = None


@dataclass
class Attachment:
    id: str
    filename: str
    url: str
    size: int = 0
    data: bytes | None = None


@dataclass
class MessageReference:
    connection_id: str
    room_id: str
    message_id: str


@dataclass
class Message:
    id: str
    user: User
    reference: MessageReference | None
    content: str
    attachments: list[Attachment]
    room_id: str
    connection_id: str


@dataclass
class Room:
    id: str
