from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UserPayload:
    id: str
    name: str
    avatar_url: str | None = None


@dataclass
class AttachmentPayload:
    id: str
    filename: str
    url: str
    size: int = 0
    data: bytes | None = None


@dataclass
class MessageReferencePayload:
    connection_id: str
    room_id: str
    message_id: str


@dataclass
class MessagePayload:
    id: str
    user: UserPayload
    reference: MessageReferencePayload | None
    content: str
    attachments: list[AttachmentPayload]
    room_id: str
    connection_id: str


# Protocol -> Bridge
@dataclass
class MessageReceived:
    connection_id: str
    message: MessagePayload


@dataclass
class WebhookMessageReceived:
    connection_id: str
    message: MessagePayload


@dataclass
class MessageDeleted:
    connection_id: str
    message_id: str


@dataclass
class MessageEdited:
    connection_id: str
    message_id: str
    content: str


@dataclass
class TypingReceived:
    connection_id: str
    room_id: str


@dataclass
class CommandRequest:
    request_id: int
    command: str
    connection_id: str
    room_id: str
    args: list[str] = field(default_factory=list)


# Bridge -> Protocol
@dataclass
class SendMessage:
    request_id: int
    internal_id: str
    room_id: str
    message: MessagePayload
    reply_to: str | None = None


# Protocol -> Bridge
@dataclass
class SendMessageResult:
    request_id: int
    connection_id: str
    internal_id: str
    message_id: str | None


@dataclass
class SendTyping:
    room_id: str


@dataclass
class EditMessage:
    room_id: str
    message_id: str
    content: str


@dataclass
class DeleteMessage:
    room_id: str
    message_id: str


@dataclass
class CommandResponse:
    request_id: int
    result: str


@dataclass
class ChannelInfo:
    id: str
    name: str
    category: str | None = None


# Bridge -> Protocol (request channel list)
@dataclass
class ListChannels:
    request_id: int


# Protocol -> Bridge (response with channel list)
@dataclass
class ChannelsList:
    request_id: int
    connection_id: str
    channels: list[ChannelInfo]
