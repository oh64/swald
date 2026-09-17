import io
import logging
import re
from multiprocessing import Queue
from typing import Any

import aiohttp
import fluxer

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


class FluxerProtocol(CommandHandler):
    PLATFORM_MAX_ATTACHMENT = 500 * 1024 * 1024

    def __init__(self, token: str, connection_id: str, to_bridge: Queue,
                 command_prefix: str = "!bridge ", max_attachment_bridge: int = 100 * 1024 * 1024,
                 url: str | None = None):
        self.token = token
        self.connection_id = connection_id
        self.to_bridge = to_bridge
        self.command_prefix = command_prefix
        self.max_attachment_bridge = min(max_attachment_bridge, self.PLATFORM_MAX_ATTACHMENT)

        bot_kwargs = {"command_prefix": "!", "intents": fluxer.Intents.default() | fluxer.Intents.MESSAGE_CONTENT}
        if url:
            bot_kwargs["api_url"] = url
        self.bot = fluxer.Bot(**bot_kwargs)
        self._webhooks: dict[str, fluxer.Webhook] = {}
        self._webhook_user_ids: set[int] = set()
        self._sent_message_ids: set[int] = set()

        self.bot.event(self.on_ready)
        self.bot.event(self.on_message)
        self.bot.event(self.on_message_delete)
        self.bot.event(self.on_message_edit)
        self.bot.event(self.on_typing_start)

    async def on_ready(self):
        bot_id = self.bot.user.id
        logger.info("Connected to Fluxer")
        logger.info(
            f"Invite: https://web.canary.fluxer.app/oauth2/authorize"
            f"?client_id={bot_id}&scope=bot&permissions=8831554465295680"
        )

    async def list_channels(self) -> list[ChannelInfo]:
        channels = []
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                channels.append(ChannelInfo(
                    id=str(channel.id),
                    name=channel.name,
                    category=channel.category.name if channel.category else None,
                ))
        return channels

    async def _check_admin(self, message) -> bool:
        if not hasattr(message, "guild") or not message.guild:
            return False
        if message.author.id == message.guild.owner_id:
            return True
        try:
            member = await message.guild.fetch_member(message.author.id)
            roles = await message.guild.fetch_roles()
            role_perms = 0
            member_role_ids = set(member.roles)
            for role in roles:
                if role.id == message.guild.id or role.id in member_role_ids:
                    role_perms |= role.permissions
            return bool(role_perms & int(fluxer.Permissions.ADMINISTRATOR))
        except (fluxer.NotFound, fluxer.Forbidden):
            return False

    async def _send_dm(self, user, content: str) -> bool:
        try:
            await user.send(content)
            return True
        except (fluxer.Forbidden, fluxer.HTTPException):
            return False

    def message_parsing(self, content: str) -> str:
        content = re.sub(
            r"<a?:(\w+):(\d+)>",
            lambda match: (
                f"[{match.group(1)}](https://fluxerusercontent.com/emojis/{match.group(2)}.webp?size=32)"
            ),
            content,
        )
        return content

    async def on_typing_start(self, data):
        if int(data["user_id"]) in self._webhook_user_ids | {self.bot.user.id}:
            return
        self.to_bridge.put(TypingReceived(
            connection_id=self.connection_id,
            room_id=str(data["channel_id"]),
        ))

    async def send_typing(self, room_id):
        channel = await self.bot.fetch_channel(int(room_id))
        if not channel:
            return
        await channel.trigger_typing()

    async def on_message_edit(self, message):
        if message.id in self._sent_message_ids:
            return
        if message.author.bot or message.author.id in self._webhook_user_ids:
            return
        self.to_bridge.put(MessageEdited(
            connection_id=self.connection_id,
            message_id=str(message.id),
            content=self.message_parsing(message.content),
        ))

    async def edit_message(self, room_id, message_id, content):
        webhook = await self._get_webhook(room_id)
        if not webhook:
            return
        await webhook.edit_message(int(message_id), content=content)

    async def on_message_delete(self, data):
        self.to_bridge.put(MessageDeleted(
            connection_id=self.connection_id,
            message_id=str(data["id"]),
        ))

    async def delete_message(self, room_id: str, message_id: str):
        await self.bot.delete_message(int(room_id), int(message_id))

    async def on_message(self, message):
        if message.author.bot or message.author.id in self._webhook_user_ids:
            return

        if (self.is_command(message.content)
                and await self._handle_command(message)):
            return

        reference = None
        if message.referenced_message:
            reference = MessageReferencePayload(
                connection_id=self.connection_id,
                room_id=str(message.channel.id),
                message_id=str(message.referenced_message.id),
            )

        self.to_bridge.put(MessageReceived(
            connection_id=self.connection_id,
            message=MessagePayload(
                id=str(message.id),
                user=UserPayload(
                    id=str(message.author.id),
                    name=message.author.username,
                    avatar_url=message.author.avatar_url,
                ),
                reference=reference,
                content=self.message_parsing(message.content),
                attachments=[
                    AttachmentPayload(
                        id=str(a.id),
                        filename=a.filename,
                        url=a.url,
                        size=a.size,
                    )
                    for a in message.attachments
                ],
                room_id=str(message.channel.id),
                connection_id=self.connection_id,
            ),
        ))

    async def _get_webhook(self, channel_id: str) -> fluxer.Webhook | None:
        if channel_id in self._webhooks:
            return self._webhooks[channel_id]
        webhooks = await self.bot.fetch_channel_webhooks(channel_id)
        for wh in webhooks:
            if wh.user.id == self.bot.user.id:
                self._webhooks[channel_id] = wh
                self._webhook_user_ids.add(wh.user.id)
                return wh
        wh = await self.bot.create_webhook(channel_id, name="swald")
        self._webhooks[channel_id] = wh
        self._webhook_user_ids.add(wh.user.id)
        return wh

    async def send_message(self, room_id: str, message, reply_to: str | None = None) -> str | None:
        webhook = await self._get_webhook(room_id)
        if not webhook:
            raise ValueError(f"No webhook for channel {room_id}")

        files = []
        extra_content_parts = []
        async with aiohttp.ClientSession() as session:
            for attachment in message.attachments:
                if attachment.size > self.max_attachment_bridge:
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
                files.append(
                    fluxer.File(io.BytesIO(data), filename=attachment.filename)
                )

        content = message.content or None
        if extra_content_parts:
            tail = "\n".join(extra_content_parts)
            content = f"{content}\n{tail}" if content else tail

        send_kwargs: dict[str, Any] = {
            "username": message.user.name,
            "avatar_url": message.user.avatar_url,
            "files": files if files else None,
            "message_reference": {
                "message_id": reply_to,
                "channel_id": room_id,
            } if reply_to else None,
            "wait": True,
        }
        if content:
            send_kwargs["content"] = content

        sent_message = await webhook.send(**send_kwargs)
        if sent_message:
            self._sent_message_ids.add(sent_message.id)
        return str(sent_message.id) if sent_message else None

    async def start(self):
        await self.bot.start(self.token)
