from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
import uuid

from swald.database import Database
from swald.ipc import (
    AttachmentPayload,
    ChannelInfo,
    ChannelsList,
    CommandRequest,
    CommandResponse,
    DeleteMessage,
    EditMessage,
    ListChannels,
    MessageDeleted,
    MessageEdited,
    MessageReceived,
    SendMessage,
    SendMessageResult,
    SendTyping,
    TypingReceived,
    WebhookMessageReceived,
)

logger = logging.getLogger(__name__)

CODE_EXPIRY_SECONDS = 300


def _generate_code(length: int = 8) -> str:
    import random
    import string
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


class Bridge:
    def __init__(self, database: Database, prefix: str = "!bridge "):
        self.database = database
        self.prefix = prefix
        self.protocol_queues: dict[str, multiprocessing.Queue] = {}
        self.protocol_from_queues: dict[str, multiprocessing.Queue] = {}
        self._send_counter = 0
        self._pending_sends: dict[int, asyncio.Future] = {}
        self._pending_lists: dict[int, asyncio.Future] = {}
        self._deleted_message_ids: dict[str, float] = {}
        self._DELETED_MAX_SIZE = 10000
        self._DELETED_TTL = 300
        self._pending_autolinks: dict[str, list[tuple[str, str, str, str, str]]] = {}
        self._fuzzy_matches: list[dict] = []
        self._FUZZY_TTL = 100

    def register_protocol_queue(
        self,
        connection_id: str,
        to_bridge: multiprocessing.Queue,
        from_bridge: multiprocessing.Queue,
    ):
        self.protocol_queues[connection_id] = to_bridge
        self.protocol_from_queues[connection_id] = from_bridge

    def _from_queue(self, connection_id: str) -> multiprocessing.Queue | None:
        return self.protocol_from_queues.get(connection_id)

    async def handle_event(self, event):
        if isinstance(event, SendMessageResult):
            self._resolve_send(event)
        elif isinstance(event, ChannelsList):
            self._resolve_list_channels(event)
        elif isinstance(event, WebhookMessageReceived):
            await self._on_webhook_message(event)
        elif isinstance(event, MessageReceived):
            await self._on_message_received(event)
        elif isinstance(event, MessageDeleted):
            await self._on_message_deleted(event)
        elif isinstance(event, MessageEdited):
            await self._on_message_edited(event)
        elif isinstance(event, TypingReceived):
            await self._on_typing_received(event)
        elif isinstance(event, CommandRequest):
            await self._on_command(event)

    def _resolve_send(self, event: SendMessageResult):
        future = self._pending_sends.pop(event.request_id, None)
        if future and not future.done():
            future.set_result(event.message_id)

    def _resolve_list_channels(self, event: ChannelsList):
        future = self._pending_lists.pop(event.request_id, None)
        if future and not future.done():
            future.set_result(event.channels)

    def _cleanup_deleted_cache(self, now: float):
        if len(self._deleted_message_ids) > self._DELETED_MAX_SIZE:
            expired = [
                k for k, t in self._deleted_message_ids.items()
                if now - t > self._DELETED_TTL
            ]
            for k in expired:
                del self._deleted_message_ids[k]

    async def _try_fuzzy_match(self, msg, connection_id: str):
        now = time.time()
        remaining = []

        for pending in self._fuzzy_matches:
            if now - pending["timestamp"] > self._FUZZY_TTL:
                continue

            if pending["target_connection_id"] != connection_id:
                remaining.append(pending)
                continue

            if pending["target_room_id"] != msg.room_id:
                remaining.append(pending)
                continue

            if pending["content"] != msg.content:
                remaining.append(pending)
                continue

            if pending["user_name"] != msg.user.name:
                remaining.append(pending)
                continue

            logger.info(
                f"[bridge] Fuzzy match: {pending['internal_id']} -> "
                f"{connection_id}:{msg.id}"
            )

            await self.database.record_bridged_message(
                pending["source_connection_id"],
                pending["source_message_id"],
                pending["bridge_id"],
                pending["target_connection_id"],
                msg.id,
            )
            continue

        self._fuzzy_matches = remaining

    async def _on_webhook_message(self, event: WebhookMessageReceived):
        await self._try_fuzzy_match(event.message, event.connection_id)

    async def _download_attachments(self, attachments: list[AttachmentPayload]):
        import aiohttp

        async with aiohttp.ClientSession() as session:
            for attachment in attachments:
                if attachment.data is not None:
                    continue
                logger.info(
                    f"[bridge] Downloading {attachment.filename} "
                    f"from {attachment.url}"
                )
                try:
                    async with session.get(attachment.url) as resp:
                        attachment.data = await resp.read()
                except Exception:
                    logger.exception(
                        f"[bridge] Failed to download {attachment.filename}"
                    )

    async def _on_message_received(self, event: MessageReceived):
        msg = event.message

        await self._try_fuzzy_match(msg, event.connection_id)

        logger.info(
            f"[{event.connection_id}] {msg.id} by "
            f"{msg.user.name} in {msg.room_id}"
        )

        if not msg.content and not msg.attachments:
            logger.warning(
                f"[{event.connection_id}] Ignoring empty message "
                f"in room {msg.room_id}"
            )
            return

        if msg.attachments:
            await self._download_attachments(msg.attachments)

        matching_bridges = await self.database.get_matching_bridges(
            event.connection_id, msg.room_id
        )

        for bridge_id in matching_bridges:
            if await self.database.has_been_traversed(
                event.connection_id, msg.id, bridge_id
            ):
                continue

            connections = await self.database.get_bridge_connections(bridge_id)

            for target_conn_id, target_room_id in connections.items():
                if target_conn_id == event.connection_id:
                    continue

                queue = self._from_queue(target_conn_id)
                if not queue:
                    logger.warning(
                        f"[bridge] No from_queue for connection_id={target_conn_id}"
                    )
                    continue

                reply_to = None
                if msg.reference:
                    ref_links = await self.database.get_message_links(
                        msg.reference.connection_id,
                        msg.reference.message_id,
                    )
                    for link in ref_links:
                        if (
                            link["bridge_id"] == bridge_id
                            and link["linked_connection_id"] == target_conn_id
                        ):
                            reply_to = link["linked_message_id"]
                            break

                self._send_counter += 1
                request_id = self._send_counter
                internal_id = uuid.uuid4().hex[:8]
                loop = asyncio.get_event_loop()
                future = loop.create_future()
                self._pending_sends[request_id] = future

                queue.put(SendMessage(
                    request_id=request_id,
                    internal_id=internal_id,
                    room_id=target_room_id,
                    message=msg,
                    reply_to=reply_to,
                ))

                try:
                    sent_message_id = await asyncio.wait_for(
                        future, timeout=15
                    )
                except (TimeoutError, asyncio.CancelledError):
                    self._pending_sends.pop(request_id, None)
                    logger.warning(
                        f"[bridge] Queue {internal_id} "
                        f"{event.connection_id}:{msg.room_id} -> "
                        f"{target_conn_id}:{target_room_id} - Timeout"
                    )
                    sent_message_id = None

                if not sent_message_id:
                    self._fuzzy_matches.append({
                        "source_connection_id": event.connection_id,
                        "source_message_id": msg.id,
                        "bridge_id": bridge_id,
                        "target_connection_id": target_conn_id,
                        "target_room_id": target_room_id,
                        "internal_id": internal_id,
                        "content": msg.content,
                        "user_name": msg.user.name,
                        "timestamp": time.time(),
                    })
                    continue

                logger.info(
                    f"[bridge] Queue {internal_id} "
                    f"{event.connection_id}:{msg.room_id} -> "
                    f"{target_conn_id}:{target_room_id}"
                )

                await self.database.record_bridged_message(
                    event.connection_id,
                    msg.id,
                    bridge_id,
                    target_conn_id,
                    sent_message_id,
                )

    async def _on_message_deleted(self, event: MessageDeleted):
        msg_key = f"{event.connection_id}:{event.message_id}"
        now = time.time()

        self._cleanup_deleted_cache(now)

        if msg_key in self._deleted_message_ids:
            return

        logger.info(
            f"[{event.connection_id}] Message deleted: {event.message_id}"
        )

        links = await self.database.delete_bridged_message(
            event.connection_id, event.message_id
        )

        if not links:
            logger.debug(
                f"[bridge] No links found for delete "
                f"{event.connection_id}:{event.message_id}"
            )
            return

        self._deleted_message_ids[msg_key] = now

        for link in links:
            queue = self._from_queue(link["linked_connection_id"])
            if not queue:
                logger.warning(
                    f"[bridge] No queue for "
                    f"{link['linked_connection_id']}"
                )
                continue
            room_id = await self.database.get_room_id(
                link["bridge_id"], link["linked_connection_id"]
            )
            if room_id:
                logger.info(
                    f"[bridge] Propagating delete to "
                    f"{link['linked_connection_id']}:{room_id} "
                    f"message {link['linked_message_id']}"
                )
                queue.put(DeleteMessage(
                    room_id=room_id,
                    message_id=link["linked_message_id"],
                ))

    async def _on_message_edited(self, event: MessageEdited):
        logger.info(
            f"[{event.connection_id}] Message edited: {event.message_id}"
        )
        links = await self.database.get_message_links(
            event.connection_id, event.message_id
        )
        if not links:
            return

        for link in links:
            queue = self._from_queue(link["linked_connection_id"])
            if not queue:
                continue
            room_id = await self.database.get_room_id(
                link["bridge_id"], link["linked_connection_id"]
            )
            if room_id:
                logger.info(
                    f"[bridge] Propagating edit to "
                    f"{link['linked_connection_id']}:{room_id} "
                    f"message {link['linked_message_id']}"
                )
                queue.put(EditMessage(
                    room_id=room_id,
                    message_id=link["linked_message_id"],
                    content=event.content,
                ))

    async def _on_typing_received(self, event: TypingReceived):
        matching_bridges = await self.database.get_matching_bridges(
            event.connection_id, event.room_id
        )
        for bridge_id in matching_bridges:
            connections = await self.database.get_bridge_connections(bridge_id)
            for target_conn_id, target_room_id in connections.items():
                if target_conn_id == event.connection_id:
                    continue
                queue = self._from_queue(target_conn_id)
                if queue:
                    queue.put(SendTyping(room_id=target_room_id))

    async def _request_list_channels(
        self, connection_id: str
    ) -> list[ChannelInfo]:
        queue = self._from_queue(connection_id)
        if not queue:
            return []

        self._send_counter += 1
        request_id = self._send_counter
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_lists[request_id] = future

        queue.put(ListChannels(request_id=request_id))

        try:
            return await asyncio.wait_for(future, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            self._pending_lists.pop(request_id, None)
            logger.warning(
                f"[bridge] Timeout listing channels on {connection_id}"
            )
            return []

    async def _autolink(self, conn_id: str, bridge_id: str) -> str:
        connections = await self.database.get_bridge_connections(bridge_id)

        all_channels: dict[str, list[ChannelInfo]] = {}
        for conn_id_in_bridge in connections:
            channels = await self._request_list_channels(conn_id_in_bridge)
            all_channels[conn_id_in_bridge] = channels

        proposed: list[tuple[str, str, str, str, str]] = []
        conn_ids = list(all_channels.keys())

        for i, conn_a in enumerate(conn_ids):
            for conn_b in conn_ids[i + 1:]:
                channels_a = all_channels[conn_a]
                channels_b = all_channels[conn_b]

                for ch_a in channels_a:
                    for ch_b in channels_b:
                        if ch_a.name != ch_b.name:
                            continue
                        if ch_a.category and ch_b.category and ch_a.category != ch_b.category:
                            continue

                        existing = await self.database.get_matching_bridges(
                            conn_a, ch_a.id
                        )
                        already_linked = False
                        for eid in existing:
                            econnections = await self.database.get_bridge_connections(
                                eid
                            )
                            if conn_b in econnections and econnections[conn_b] == ch_b.id:
                                already_linked = True
                                break
                        if already_linked:
                            continue

                        proposed.append((
                            bridge_id, conn_a, ch_a.id, conn_b, ch_b.id
                        ))

        if not proposed:
            return "No new channels to link found."

        self._pending_autolinks[bridge_id] = proposed

        lines = []
        for i, (bid, ca, ra, cb, rb) in enumerate(proposed):
            lines.append(
                f"  {i + 1}. `{ca}:{ra}` <-> `{cb}:{rb}`"
            )

        return (
            f"**{len(proposed)} channel(s) to link:**\n"
            + "\n".join(lines)
            + f"\n\nRun `{self.prefix}confirm` to create the links, or `{self.prefix}cancel` to abort."
        )

    async def _confirm_autolink(self, bridge_id: str) -> str:
        proposed = self._pending_autolinks.pop(bridge_id, None)
        if not proposed:
            return "No pending autolink to confirm."

        created = 0
        async with self.database.transaction():
            for bid, conn_a, room_a, conn_b, room_b in proposed:
                existing_a = await self.database.get_matching_bridges(conn_a, room_a)
                existing_b = await self.database.get_matching_bridges(conn_b, room_b)

                if existing_a and existing_b:
                    if existing_a[0] == existing_b[0]:
                        continue
                    continue

                target_bridge = existing_a[0] if existing_a else (
                    existing_b[0] if existing_b else None
                )

                if not target_bridge:
                    target_bridge = str(uuid.uuid4())[:8]
                    await self.database.create_bridge_with_room(
                        target_bridge, conn_a, room_a
                    )

                if not existing_a:
                    await self.database.add_room_to_bridge(target_bridge, conn_a, room_a)
                if not existing_b:
                    await self.database.add_room_to_bridge(target_bridge, conn_b, room_b)
                created += 1

        return f"Linked {created} channel pair(s)."

    async def _on_command(self, event: CommandRequest):
        cmd = event.command
        conn_id = event.connection_id
        room_id = event.room_id
        result = ""

        if cmd == "ping":
            pass

        elif cmd == "list":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge."
            else:
                lines = []
                for bridge_id in bridges:
                    connections = await self.database.get_bridge_connections(bridge_id)
                    members = "\n".join(
                        f"  - `{cid}:{rid}`" for cid, rid in connections.items()
                    )
                    lines.append(f"**{bridge_id}**:\n{members}")
                result = "\n".join(lines)

        elif cmd == "create":
            existing = await self.database.get_matching_bridges(conn_id, room_id)
            if existing:
                result = f"This channel is already in bridge `{existing[0]}`"
            else:
                bridge_id = str(uuid.uuid4())[:8]
                await self.database.create_bridge(bridge_id, conn_id, room_id)
                logger.info(f"[{conn_id}] Bridge created: {bridge_id}")
                result = (
                    f"Bridge `{bridge_id}` created!\n"
                    f"Use `{self.prefix}add` to generate a code, then `{self.prefix}join <code>` on the other channel."
                )

        elif cmd == "add":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge. Use `{self.prefix}create` first."
            else:
                code = event.args[0] if event.args else _generate_code()
                expires_at = time.time() + CODE_EXPIRY_SECONDS
                await self.database.add_temporary_code(
                    code, bridges[0], conn_id, room_id, expires_at
                )
                logger.info(f"[{conn_id}] Temporary code: {code} for bridge {bridges[0]}")
                result = code

        elif cmd == "join":
            if not event.args:
                result = "Usage: `!bridge join <code>`"
            else:
                code = event.args[0]
                code_data = await self.database.get_temporary_code(code)
                if not code_data:
                    result = "Invalid or expired code."
                elif time.time() > code_data["expires_at"]:
                    await self.database.remove_temporary_code(code)
                    result = "Code expired. Request a new one with `{self.prefix}add` on the already bridged channel."
                else:
                    bridge_id = code_data["bridge_id"]
                    existing = await self.database.get_matching_bridges(conn_id, room_id)
                    if existing:
                        result = f"This channel is already in bridge `{existing[0]}`"
                    else:
                        await self.database.join_bridge(
                            bridge_id, code, conn_id, room_id
                        )
                        logger.info(f"[{conn_id}] Joined bridge {bridge_id}")
                        result = f"Joined bridge `{bridge_id}`!"

        elif cmd == "leave":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge."
            else:
                bridge_id = bridges[0]
                deleted = await self.database.leave_bridge(
                    bridge_id, conn_id, room_id
                )
                if deleted:
                    logger.info(f"[{conn_id}] Bridge deleted: {bridge_id}")
                else:
                    logger.info(f"[{conn_id}] Left bridge {bridge_id}")
                result = f"Left bridge `{bridge_id}`."

        elif cmd == "autolink":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge. Create or join one first."
            else:
                bridge_id = bridges[0]
                result = await self._autolink(conn_id, bridge_id)

        elif cmd == "confirm":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge."
            else:
                bridge_id = bridges[0]
                result = await self._confirm_autolink(bridge_id)

        elif cmd == "cancel":
            bridges = await self.database.get_matching_bridges(conn_id, room_id)
            if not bridges:
                result = "This channel is not in any bridge."
            else:
                bridge_id = bridges[0]
                self._pending_autolinks.pop(bridge_id, None)
                result = "Autolink cancelled."

        queue = self._from_queue(conn_id)
        if queue and result:
            queue.put(CommandResponse(
                request_id=event.request_id,
                result=result,
            ))
