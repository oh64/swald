import logging
import re
from multiprocessing import Queue
from typing import Any

import aiohttp
import stoat

from swald.ipc import (
    AttachmentPayload,
    ChannelInfo,
    MessageDeleted,
    MessageEdited,
    MessagePayload,
    MessageReceived,
    MessageReferencePayload,
    TypingReceived,
    UserPayload,
)
from swald.protocols.commands import CommandHandler

logger = logging.getLogger(__name__)


class StoatProtocol(CommandHandler):
    PLATFORM_MAX_ATTACHMENT = None

    def __init__(self, token: str, connection_id: str, to_bridge: Queue,
                 command_prefix: str = "!bridge ", max_attachment_bridge: int = 100 * 1024 * 1024):
        self.token = token
        self.connection_id = connection_id
        self.to_bridge = to_bridge
        self.command_prefix = command_prefix
        self.max_attachment_bridge = max_attachment_bridge

        self.bot = stoat.Client(token=token)
        self._sent_message_ids: set[str] = set()

        self.bot.on(stoat.MessageCreateEvent)(self._on_message_create)
        self.bot.on(stoat.MessageUpdateEvent)(self._on_message_update)
        self.bot.on(stoat.MessageDeleteEvent)(self._on_message_delete)
        self.bot.on(stoat.ChannelStartTypingEvent)(self._on_typing_start)
        self.bot.on(stoat.ReadyEvent)(self._on_ready)

    async def _on_ready(self, event: stoat.ReadyEvent):
        logger.info("Connected to Stoat")
        logger.info(
            "Go to the Stoat interface to get the bot invite link"
        )

    async def list_channels(self) -> list[ChannelInfo]:
        channels = []
        for channel in self.bot.channels.values():
            if isinstance(channel, stoat.TextChannel):
                category = None
                try:
                    cat = channel.category
                    if cat:
                        category = cat.name
                except stoat.NoData:
                    pass
                channels.append(ChannelInfo(
                    id=str(channel.id),
                    name=channel.name,
                    category=category,
                ))
        return channels

    async def _check_admin(self, message) -> bool:
        try:
            server = message.server
        except stoat.NoData:
            return False
        if not server:
            return False
        try:
            author = message.author
            if str(author.id) == str(server.owner_id):
                return True
            channel = await self.bot.fetch_channel(str(message.channel_id))
            member = await server.fetch_member(author.id)
            perms = channel.permissions_for(member)
            return bool(
                perms & stoat.Permissions.manage_server
                or perms & stoat.Permissions.manage_channel
            )
        except (stoat.NotFound, stoat.HTTPException, stoat.NoData):
            return False

    @property
    def _admin_permission_message(self) -> str:
        return "This command requires **Manage Server** or **Manage Channel** permissions."

    async def _send_dm(self, user, content: str) -> bool:
        try:
            await user.send(content)
            return True
        except (stoat.Forbidden, stoat.HTTPException):
            return False

    def message_parsing(self, content: str) -> str:
        content = re.sub(
            r"<a?:(\w+):(\d+)>",
            lambda match: (
                f"[{match.group(1)}](https://stoatusercontent.com/emojis/{match.group(2)}.webp?size=32)"
            ),
            content,
        )
        return content

    async def _on_typing_start(self, event: stoat.ChannelStartTypingEvent):
        user_id = str(event.user_id)
        if user_id == str(self.bot.user.id):
            return
        self.to_bridge.put(TypingReceived(
            connection_id=self.connection_id,
            room_id=str(event.channel_id),
        ))

    async def send_typing(self, room_id):
        try:
            channel = await self.bot.fetch_channel(room_id)
            await channel.begin_typing()
        except (stoat.NotFound, stoat.HTTPException):
            pass

    async def _on_message_update(self, event: stoat.MessageUpdateEvent):
        message = event.after
        if str(message.id) in self._sent_message_ids:
            return
        if hasattr(message, "webhook") and message.webhook:
            return
        self.to_bridge.put(MessageEdited(
            connection_id=self.connection_id,
            message_id=str(message.id),
            content=self.message_parsing(message.content),
        ))

    async def edit_message(self, room_id, message_id, content):
        try:
            await self.bot.http.edit_message(
                str(room_id), str(message_id), content=content
            )
        except stoat.HTTPException:
            pass

    async def _on_message_delete(self, event: stoat.MessageDeleteEvent):
        self.to_bridge.put(MessageDeleted(
            connection_id=self.connection_id,
            message_id=str(event.message_id),
        ))

    async def delete_message(self, room_id: str, message_id: str):
        try:
            await self.bot.http.delete_message(str(room_id), str(message_id))
        except stoat.HTTPException:
            pass

    async def _on_message_create(self, event: stoat.MessageCreateEvent):
        message = event.message

        if hasattr(message, "webhook") and message.webhook:
            return

        if message.author.bot:
            return

        if (self.is_command(message.content)
                and await self._handle_command(message)):
            return

        reference = None
        if message.replies:
            reference = MessageReferencePayload(
                connection_id=self.connection_id,
                room_id=str(message.channel_id),
                message_id=str(message.replies[0]),
            )

        author = message.author
        avatar_url = ""
        if hasattr(author, "avatar") and author.avatar:
            avatar_url = author.avatar.url()

        try:
            stoat_attachments = message.attachments
        except stoat.NoData:
            stoat_attachments = []

        self.to_bridge.put(MessageReceived(
            connection_id=self.connection_id,
            message=MessagePayload(
                id=str(message.id),
                user=UserPayload(
                    id=str(author.id),
                    name=author.name,
                    avatar_url=avatar_url,
                ),
                reference=reference,
                content=self.message_parsing(message.content),
                attachments=[
                    AttachmentPayload(
                        id=str(a.id),
                        filename=a.filename or "unknown",
                        url=a.url(),
                        size=a.size or 0,
                    )
                    for a in stoat_attachments
                ],
                room_id=str(message.channel_id),
                connection_id=self.connection_id,
            ),
        ))

    async def send_message(self, room_id: str, message, reply_to: str | None = None) -> str | None:
        channel = await self.bot.fetch_channel(room_id)

        attachments = []
        extra_content_parts = []
        async with aiohttp.ClientSession() as session:
            for attachment in message.attachments:
                if (self.max_attachment_bridge
                        and attachment.size > self.max_attachment_bridge):
                    extra_content_parts.append(
                        f"{attachment.url} (File too big for {self.connection_id})"
                    )
                    continue
                if attachment.data is not None:
                    data = attachment.data
                else:
                    logger.info(
                        f"Downloading {attachment.filename} from "
                        f"{message.room_id} on {message.connection_id}"
                    )
                    async with session.get(attachment.url) as resp:
                        data = await resp.read()
                logger.info(
                    f"Uploading {attachment.filename} to "
                    f"{room_id} on {self.connection_id}"
                )
                attachments.append((attachment.filename, data))

        content = message.content or None
        if extra_content_parts:
            tail = "\n".join(extra_content_parts)
            content = f"{content}\n{tail}" if content else tail

        replies = None
        if reply_to:
            replies = [stoat.Reply(id=reply_to, fail_if_not_exists=False)]

        masquerade = stoat.MessageMasquerade(
            name=message.user.name[:32],
            avatar=message.user.avatar_url[:256] if message.user.avatar_url else None,
        )

        send_kwargs: dict[str, Any] = {
            "attachments": attachments if attachments else None,
            "replies": replies,
            "masquerade": masquerade,
        }
        if content:
            send_kwargs["content"] = content

        try:
            sent_message = await channel.send(**send_kwargs)
        except stoat.HTTPException as e:
            logger.warning(
                f"[{self.connection_id}] Masquerade send failed: {e}"
            )
            prefix = "I lack the masquerade permission, sending as myself\n"
            try:
                sent_message = await channel.send(
                    content=prefix + (content or ""),
                    attachments=attachments if attachments else None,
                    replies=replies,
                )
            except stoat.HTTPException as e:
                logger.error(
                    f"[{self.connection_id}] Failed to send to {room_id}: {e}"
                )
                return None
        if sent_message:
            self._sent_message_ids.add(str(sent_message.id))
        return str(sent_message.id) if sent_message else None

    async def start(self):
        await self.bot.start()
