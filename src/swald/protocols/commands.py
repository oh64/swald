import logging

from swald.protocol_process import send_command

logger = logging.getLogger(__name__)


class CommandHandler:
    async def _cmd_help(self) -> str:
        p = self.command_prefix
        return (
            "**Available commands:**\n"
            f"`{p}ping` - Pong?\n"
            f"`{p}create` - Create a new bridge\n"
            f"`{p}add` - Generate a temporary code (DM)\n"
            f"`{p}join <code>` - Join a bridge with a code\n"
            f"`{p}list` - List bridges for this channel\n"
            f"`{p}leave` - Leave the bridge for this channel\n"
            f"`{p}autolink` - Auto-link channels by name across platforms\n"
            f"`{p}confirm` - Confirm pending action\n"
            f"`{p}cancel` - Cancel pending action\n"
            f"`{p}help` - Show this help"
        )

    async def _cmd_list(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "list", self.connection_id, room_id
        )

    async def _cmd_create(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "create", self.connection_id, room_id
        )

    async def _cmd_add(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "add", self.connection_id, room_id
        )

    async def _cmd_join(self, code: str, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "join", self.connection_id, room_id, [code]
        )

    async def _cmd_leave(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "leave", self.connection_id, room_id
        )

    async def _cmd_autolink(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "autolink", self.connection_id, room_id
        )

    async def _cmd_confirm(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "confirm", self.connection_id, room_id
        )

    async def _cmd_cancel(self, room_id: str) -> str:
        return await send_command(
            self.to_bridge, "cancel", self.connection_id, room_id
        )

    def is_command(self, content: str) -> bool:
        stripped = content.strip()
        if stripped == self.command_prefix.rstrip():
            return True
        return content.startswith(self.command_prefix)

    async def _send_response(self, message, content: str):
        try:
            await message.channel.send(content)
        except Exception:
            logger.exception(f"[{self.connection_id}] Failed to send response")

    async def _handle_command(self, message) -> bool:
        content = message.content
        if not self.is_command(content):
            return False

        stripped = content.strip()
        bare_prefix = self.command_prefix.rstrip()
        if stripped == bare_prefix:
            await self._send_response(message, await self._cmd_help())
            return True

        args = content[len(self.command_prefix):].strip().split()
        if not args:
            await self._send_response(message, await self._cmd_help())
            return True

        cmd = args[0].lower()
        room_id = str(message.channel.id)

        if cmd == "ping":
            result = await send_command(
                self.to_bridge, "ping", self.connection_id, room_id
            )
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "help":
            await self._send_response(message, await self._cmd_help())
            return True

        if cmd == "list":
            result = await self._cmd_list(room_id)
            if result:
                await self._send_response(message, result)
            return True

        if not await self._check_admin(message):
            await self._send_response(message, self._admin_permission_message)
            return True

        if cmd == "create":
            result = await self._cmd_create(room_id)
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "add":
            code = await self._cmd_add(room_id)
            if code:
                sent = await self._send_dm(
                    message.author,
                    f"Your bridge code: `{code}`\n"
                    f"Use `{self.command_prefix}join {code}` on the other channel.",
                )
                if sent:
                    await self._send_response(message, "Code sent via DM!")
                else:
                    await self._send_response(
                        message,
                        f"I can't send you a DM. Here's your code: `{code}`"
                    )
            else:
                await self._send_response(message, code)
            return True

        if cmd == "join":
            if len(args) < 2:
                await self._send_response(
                    message,
                    f"Usage: `{self.command_prefix}join <code>`"
                )
                return True
            result = await self._cmd_join(args[1], room_id)
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "leave":
            result = await self._cmd_leave(room_id)
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "autolink":
            result = await self._cmd_autolink(room_id)
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "confirm":
            result = await self._cmd_confirm(room_id)
            if result:
                await self._send_response(message, result)
            return True

        if cmd == "cancel":
            result = await self._cmd_cancel(room_id)
            if result:
                await self._send_response(message, result)
            return True

        return False

    async def _send_dm(self, user, content: str) -> bool:
        try:
            await user.send(content)
            return True
        except Exception:
            return False

    async def _check_admin(self, message) -> bool:
        raise NotImplementedError

    @property
    def _admin_permission_message(self) -> str:
        return "This command requires **Administrator** permissions."
