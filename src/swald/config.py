import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTACHMENT_BRIDGE = 100 * 1024 * 1024  # 100 MB


@dataclass
class ConnectionConfig:
    name: str
    type: str
    token: str
    url: str | None = None
    api: str | None = None
    gateway: str | None = None
    cdn: str | None = None


@dataclass
class Config:
    command_prefix: str = "!bridge "
    max_attachment_bridge: int = DEFAULT_MAX_ATTACHMENT_BRIDGE
    log_to_file: bool = True
    postgres_dsn: str | None = None
    connections: list[ConnectionConfig] = field(default_factory=list)


def load_config(path: str = "swald.yml") -> Config:
    config_path = Path(path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {path}\n"
            "Create a swald.yml file. See swald.yml.example for reference."
        )

    with open(config_path) as f:
        data = yaml.safe_load(f)

    max_attach = data.get("maxAttachmentBridge", "100MB")
    if isinstance(max_attach, str):
        max_attach = max_attach.upper().strip()
        if max_attach.endswith("MB"):
            max_attachment_bytes = int(max_attach[:-2]) * 1024 * 1024
        elif max_attach.endswith("GB"):
            max_attachment_bytes = int(max_attach[:-2]) * 1024 * 1024 * 1024
        elif max_attach.endswith("B"):
            max_attachment_bytes = int(max_attach[:-1])
        else:
            max_attachment_bytes = int(max_attach)
    else:
        max_attachment_bytes = int(max_attach)

    config = Config(
        command_prefix=data.get("command-prefix", "!bridge "),
        max_attachment_bridge=max_attachment_bytes,
        log_to_file=data.get("logToFile", True),
        postgres_dsn=data.get("postgres") or os.environ.get("POSTGRESS_CONN"),
    )

    for name, conn_data in data.get("connections", {}).items():
        if "type" not in conn_data:
            logger.warning(f"Connection '{name}' missing 'type' field, skipping")
            continue
        if "token" not in conn_data:
            logger.warning(f"Connection '{name}' missing 'token' field, skipping")
            continue

        config.connections.append(
            ConnectionConfig(
                name=name,
                type=conn_data["type"],
                token=conn_data["token"],
                url=conn_data.get("url"),
                api=conn_data.get("api"),
                gateway=conn_data.get("gateway"),
                cdn=conn_data.get("cdn"),
            )
        )

    logger.info(
        f"Loaded config: {len(config.connections)} connection(s) "
        f"with prefix '{config.command_prefix}' "
        f"and max attachment size {max_attachment_bytes // (1024 * 1024)}MB"
    )
    return config
