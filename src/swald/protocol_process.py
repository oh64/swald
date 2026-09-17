import asyncio
import itertools
import logging
import multiprocessing
import queue
import threading
from typing import Any

from swald.ipc import (
    ChannelsList,
    CommandRequest,
    CommandResponse,
    DeleteMessage,
    EditMessage,
    ListChannels,
    MessagePayload,
    SendMessage,
    SendMessageResult,
    SendTyping,
)

logger = logging.getLogger(__name__)

_request_counter = itertools.count()


class _ConnectionFilter(logging.Filter):
    def __init__(self, connection_id: str):
        super().__init__()
        self.connection_id = connection_id

    def filter(self, record: logging.LogRecord) -> bool:
        prefix = f"[{self.connection_id}] "
        if not isinstance(record.msg, str) or not record.msg.startswith(prefix):
            record.msg = f"{prefix}{record.msg}"
        return True


async def send_command(
    to_bridge: multiprocessing.Queue,
    command: str,
    connection_id: str,
    room_id: str,
    args: list[str] | None = None,
    timeout: float = 5.0,
) -> str:
    request_id = next(_request_counter)
    loop = asyncio.get_event_loop()
    future: asyncio.Future[str] = loop.create_future()
    pending[request_id] = future

    to_bridge.put(CommandRequest(
        request_id=request_id,
        command=command,
        connection_id=connection_id,
        room_id=room_id,
        args=args or [],
    ))

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        return ""
    finally:
        pending.pop(request_id, None)


pending: dict[int, asyncio.Future[str]] = {}


def protocol_main(
    protocol_class: type,
    connection_id: str,
    to_bridge: multiprocessing.Queue,
    from_bridge: multiprocessing.Queue,
    token: str,
    protocol_kwargs: dict[str, Any],
) -> None:
    asyncio.run(
        _run_protocol(
            protocol_class,
            connection_id,
            to_bridge,
            from_bridge,
            token,
            protocol_kwargs,
        )
    )


async def _run_protocol(
    protocol_class: type,
    connection_id: str,
    to_bridge: multiprocessing.Queue,
    from_bridge: multiprocessing.Queue,
    token: str,
    protocol_kwargs: dict[str, Any],
) -> None:
    log_filter = _ConnectionFilter(connection_id)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(log_filter)

    protocol = protocol_class(
        token,
        connection_id,
        to_bridge=to_bridge,
        **protocol_kwargs,
    )

    bridge_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _forward_to_async():
        while True:
            try:
                item = from_bridge.get(timeout=0.1)
                loop.call_soon_threadsafe(bridge_queue.put_nowait, item)
            except queue.Empty:
                pass
            except (OSError, EOFError):
                break

    forwarder = threading.Thread(target=_forward_to_async, daemon=True)
    forwarder.start()

    listen_task = asyncio.create_task(
        _listen_bridge(protocol, bridge_queue, connection_id)
    )

    logger.info(f"Starting {protocol_class.__name__}...")
    try:
        await protocol.start()
    except Exception:
        logger.exception("Protocol crashed")
    finally:
        listen_task.cancel()
        for handler in root_logger.handlers:
            handler.removeFilter(log_filter)


async def _listen_bridge(
    protocol, bridge_queue: asyncio.Queue, connection_id: str
):
    while True:
        item = await bridge_queue.get()
        try:
            if isinstance(item, SendMessage):
                msg = _payload_to_message(item.message)
                sent_id = await protocol.send_message(
                    item.room_id, msg, item.reply_to
                )
                if sent_id:
                    logger.info(
                        f"[{connection_id}] Sent {item.internal_id} "
                        f"- Success {sent_id}"
                    )
                else:
                    logger.warning(
                        f"[{connection_id}] Sent {item.internal_id} "
                        f"- Error, no message saved"
                    )
                protocol.to_bridge.put(SendMessageResult(
                    request_id=item.request_id,
                    connection_id=connection_id,
                    internal_id=item.internal_id,
                    message_id=str(sent_id) if sent_id else None,
                ))

            elif isinstance(item, SendTyping):
                await protocol.send_typing(item.room_id)

            elif isinstance(item, EditMessage):
                await protocol.edit_message(
                    item.room_id, item.message_id, item.content
                )

            elif isinstance(item, DeleteMessage):
                await protocol.delete_message(item.room_id, item.message_id)

            elif isinstance(item, ListChannels):
                channels = await protocol.list_channels()
                protocol.to_bridge.put(ChannelsList(
                    request_id=item.request_id,
                    connection_id=connection_id,
                    channels=channels,
                ))

            elif isinstance(item, CommandResponse):
                future = pending.get(item.request_id)
                if future and not future.done():
                    future.set_result(item.result)
        except Exception:
            logger.exception(
                f"[{connection_id}] Error handling bridge message {item}"
            )


def _payload_to_message(payload: MessagePayload):
    from swald.models import Attachment, Message, MessageReference, User

    reference = None
    if payload.reference:
        reference = MessageReference(
            connection_id=payload.reference.connection_id,
            room_id=payload.reference.room_id,
            message_id=payload.reference.message_id,
        )

    return Message(
        id=payload.id,
        user=User(
            id=payload.user.id,
            name=payload.user.name,
            avatar_url=payload.user.avatar_url,
        ),
        reference=reference,
        content=payload.content,
        attachments=[
            Attachment(
                id=a.id,
                filename=a.filename,
                url=a.url,
                size=a.size,
                data=a.data,
            )
            for a in payload.attachments
        ],
        room_id=payload.room_id,
        connection_id=payload.connection_id,
    )
