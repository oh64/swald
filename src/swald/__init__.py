import asyncio
import logging
import multiprocessing
import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

from swald.bridge import Bridge
from swald.config import load_config
from swald.database import Database
from swald.patches import apply
from swald.protocol_process import protocol_main

apply()

logger = logging.getLogger(__name__)

PROTOCOL_FACTORIES = {
    "discord": "swald.protocols.discord:DiscordProtocol",
    "fluxer": "swald.protocols.fluxer:FluxerProtocol",
    "spacebar": "swald.protocols.spacebar:SpacebarProtocol",
    "stoat": "swald.protocols.stoat:StoatProtocol",
}


def _import_protocol(protocol_type: str):
    module_path, class_name = PROTOCOL_FACTORIES[protocol_type].rsplit(":", 1)
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


async def main() -> None:
    logger.info("Started")
    logger.info("Initializing swald")

    config = load_config()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_to_file:
        Path("logs").mkdir(exist_ok=True)
        handlers.insert(
            0,
            logging.FileHandler(
                "logs/swald-"
                + datetime.now(tz=UTC).strftime("%Y-%m-%d#%H:%M:%S")
                + ".log"
            ),
        )
    logging.basicConfig(handlers=handlers, level=logging.INFO)

    database = Database.from_config(
        postgres_dsn=config.postgres_dsn,
    )
    await database.connect()
    bridge = Bridge(database, prefix=config.command_prefix)

    shutdown = threading.Event()
    processes: list[multiprocessing.Process] = []

    for conn in config.connections:
        if conn.type not in PROTOCOL_FACTORIES:
            logger.warning(
                f"Unknown protocol type '{conn.type}' for '{conn.name}', skipping"
            )
            continue

        ProtocolClass = _import_protocol(conn.type)

        to_bridge_queue = multiprocessing.Queue()
        from_bridge_queue = multiprocessing.Queue()

        bridge.register_protocol_queue(
            conn.name, to_bridge_queue, from_bridge_queue
        )

        protocol_kwargs = {
            "command_prefix": config.command_prefix,
            "max_attachment_bridge": config.max_attachment_bridge,
        }
        if conn.type == "fluxer" and conn.url:
            protocol_kwargs["url"] = conn.url
        if conn.type == "spacebar":
            if conn.api:
                protocol_kwargs["api"] = conn.api
            if conn.gateway:
                protocol_kwargs["gateway"] = conn.gateway
            if conn.cdn:
                protocol_kwargs["cdn"] = conn.cdn

        p = multiprocessing.Process(
            target=protocol_main,
            args=(
                ProtocolClass,
                conn.name,
                to_bridge_queue,
                from_bridge_queue,
                conn.token,
                protocol_kwargs,
            ),
            daemon=True,
        )
        p.start()
        processes.append(p)
        logger.info(f"[{conn.name}] Started {conn.type} process (pid={p.pid})")

    logger.info(f"All {len(processes)} protocol processes started")

    loop = asyncio.get_event_loop()

    def _bridge_forwarder():
        while not shutdown.is_set():
            for conn in config.connections:
                q = bridge.protocol_queues.get(conn.name)
                if not q:
                    continue
                try:
                    event = q.get(timeout=0.1)
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future, bridge.handle_event(event)
                    )
                except queue.Empty:
                    pass
                except (OSError, EOFError):
                    break

    forwarder = threading.Thread(target=_bridge_forwarder, daemon=True)
    forwarder.start()

    try:
        while not shutdown.is_set():
            for p in processes:
                if not p.is_alive():
                    logger.warning(f"A protocol process died (pid={p.pid})")
                    shutdown.set()
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        shutdown.set()
    finally:
        for p in processes:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
        await database.close()
        logger.info("Shutdown complete")


def run() -> None:
    multiprocessing.set_start_method("fork", force=True)
    asyncio.run(main())


if __name__ == "__main__":
    run()
