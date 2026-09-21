import io
import logging
import re
from multiprocessing import Queue
from typing import Any

import aiohttp
import discord

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
    WebhookMessageReceived,
)
from swald.protocols.commands import CommandHandler

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True


class DiscordProtocol(CommandHandler):
    PLATFORM_MAX_ATTACHMENT = 500 * 1024 * 1024

    def __init__(self, token: str, connection_id: str, to_bridge: Queue,
                 command_prefix: str = "!bridge ", max_attachment_bridge: int = 100 * 1024 * 1024):
        self.token = token
        self.connection_id = connection_id
        self.to_bridge = to_bridge
        self.command_prefix = command_prefix
        self.max_attachment_bridge = min(max_attachment_bridge, self.PLATFORM_MAX_ATTACHMENT)

        self.bot = discord.Client(intents=intents)
        self._webhooks: dict[int, discord.Webhook] = {}
        self._webhook_user_ids: set[int] = set()
        self._sent_message_ids: set[int] = set()

        self.bot.event(self.on_ready)
        self.bot.event(self.on_message)
        self.bot.event(self.on_raw_message_delete)
        self.bot.event(self.on_raw_message_edit)
        self.bot.event(self.on_typing)

    async def on_ready(self):
        bot_id = self.bot.user.id
        logger.info("Connected to Discord")
        logger.info(
            f"Invite: https://discord.com/oauth2/authorize"
            f"?client_id={bot_id}&permissions=8831554465295680"
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
        if not message.guild:
            return False
        return (
            message.author.id == message.guild.owner_id
            or message.author.guild_permissions.administrator
        )

    async def _send_dm(self, user, content: str) -> bool:
        try:
            await user.send(content)
            return True
        except discord.Forbidden:
            return False

    def message_parsing(self, content: str) -> str:
        content = re.sub(
            r"<a?:(\w+):(\d+)>",
            lambda match: (
                f"[{match.group(1)}](https://cdn.discordapp.com/emojis/{match.group(2)}.webp?size=32)"
            ),
            content,
        )
        return content

    async def on_typing(self, channel, user, when):
        if user.id in self._webhook_user_ids | {self.bot.user.id}:
            return
        self.to_bridge.put(TypingReceived(
            connection_id=self.connection_id,
            room_id=str(channel.id),
        ))

    async def send_typing(self, room_id):
        channel = self.bot.get_channel(int(room_id))
        if not channel:
            return
        await channel.trigger_typing()

    async def on_raw_message_edit(self, payload):
        if payload.message_id in self._sent_message_ids:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        self.to_bridge.put(MessageEdited(
            connection_id=self.connection_id,
            message_id=str(message.id),
            content=self.message_parsing(message.content),
        ))

    async def edit_message(self, room_id, message_id, content):
        return

    async def on_raw_message_delete(self, payload):
        self.to_bridge.put(MessageDeleted(
            connection_id=self.connection_id,
            message_id=str(payload.message_id),
        ))

    async def delete_message(self, room_id: str, message_id: str):
        channel = self.bot.get_channel(int(room_id))
        if not channel:
            return
        message = await channel.fetch_message(int(message_id))
        await message.delete()

    async def on_message(self, message):
        if message.author.bot or message.author.id in self._webhook_user_ids:
            self._sent_message_ids.add(message.id)
            self.to_bridge.put(WebhookMessageReceived(
                connection_id=self.connection_id,
                message=MessagePayload(
                    id=str(message.id),
                    user=UserPayload(
                        id=str(message.author.id),
                        name=message.author.name,
                        avatar_url=str(message.author.display_avatar.url),
                    ),
                    reference=None,
                    content=self.message_parsing(message.content),
                    attachments=[],
                    room_id=str(message.channel.id),
                    connection_id=self.connection_id,
                ),
            ))
            return

        if (self.is_command(message.content)
                and await self._handle_command(message)):
            return

        reference = None
        if message.reference and message.reference.message_id:
            reference = MessageReferencePayload(
                connection_id=self.connection_id,
                room_id=str(message.channel.id),
                message_id=str(message.reference.message_id),
            )

        self.to_bridge.put(MessageReceived(
            connection_id=self.connection_id,
            message=MessagePayload(
                id=str(message.id),
                user=UserPayload(
                    id=str(message.author.id),
                    name=message.author.name,
                    avatar_url=str(message.author.display_avatar.url),
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

    async def _get_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        if channel.id in self._webhooks:
            return self._webhooks[channel.id]
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.user == self.bot.user:
                self._webhooks[channel.id] = wh
                self._webhook_user_ids.add(wh.user.id)
                return wh
        wh = await channel.create_webhook(name="swald")
        self._webhooks[channel.id] = wh
        self._webhook_user_ids.add(wh.user.id)
        return wh

    async def send_message(self, room_id: str, message, reply_to: str | None = None) -> str | None:
        channel = self.bot.get_channel(int(room_id))
        if not channel:
            raise ValueError(f"Channel {room_id} not found")
        webhook = await self._get_webhook(channel)

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
                    discord.File(io.BytesIO(data), filename=attachment.filename)
                )

        content = message.content or None
        if extra_content_parts:
            tail = "\n".join(extra_content_parts)
            content = f"{content}\n{tail}" if content else tail

        view = None
        if reply_to:
            view = discord.ui.View()
            label = f"{message.user.name} > {message.content[:80]}"
            view.add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.url,
                    label=label,
                    url=f"https://discord.com/channels/@me/{room_id}/{reply_to}",
                )
            )

        send_kwargs: dict[str, Any] = {
            "wait": True,
            "username": message.user.name,
            "avatar_url": message.user.avatar_url,
            "files": files if files else None,
            "view": view,
        }
        if content:
            send_kwargs["content"] = content

        sent_message = await webhook.send(**send_kwargs)
        if sent_message:
            self._sent_message_ids.add(sent_message.id)
        return str(sent_message.id) if sent_message else None

    async def start(self):
        await self.bot.start(self.token)
