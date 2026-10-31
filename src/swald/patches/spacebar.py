import logging
from importlib import import_module
from typing import Any

import discord

logger = logging.getLogger(__name__)

_compat_applied = False


def _normalize_roles(roles: Any) -> Any:
    """Spacebar sends member roles as full role objects; py-cord expects IDs."""
    if not isinstance(roles, list):
        return roles
    normalized = []
    for role in roles:
        if isinstance(role, dict):
            role_id = role.get("id")
            if role_id is not None:
                normalized.append(role_id)
        else:
            normalized.append(role)
    return normalized


def _normalize_member(member: Any) -> Any:
    if isinstance(member, dict) and "roles" in member:
        member["roles"] = _normalize_roles(member["roles"])
    return member


def _sanitize_message_data(data: Any) -> Any:
    """Normalize member payloads so py-cord can parse them without crashing."""
    if not isinstance(data, dict):
        return data
    if "member" in data:
        logger.debug("Spacebar compat: normalizing 'member.roles' in message data")
        _normalize_member(data["member"])
    for mention in data.get("mentions") or []:
        if isinstance(mention, dict) and "member" in mention:
            logger.debug(
                "Spacebar compat: normalizing 'member.roles' in mention data"
            )
            _normalize_member(mention["member"])
    if isinstance(data.get("referenced_message"), dict):
        _sanitize_message_data(data["referenced_message"])
    for snapshot in data.get("message_snapshots") or []:
        if isinstance(snapshot, dict) and isinstance(snapshot.get("message"), dict):
            _sanitize_message_data(snapshot["message"])
    return data


def apply_compat_patch():
    global _compat_applied
    if _compat_applied:
        return
    _compat_applied = True

    _orig_webhook_create = discord.Webhook._create_message

    def _patched_webhook_create(self, data):
        if isinstance(data, dict):
            if "pinned" not in data:
                logger.debug("Spacebar compat: injecting missing 'pinned' field")
                data["pinned"] = False
            for field in ("components", "reactions", "sticker_items", "embeds", "attachments"):
                if not data.get(field):
                    logger.debug(f"Spacebar compat: injecting missing '{field}' field")
                    data[field] = []
        elif data is not None:
            logger.warning(
                "Spacebar compat: unexpected non-dict webhook message payload: %.200r",
                data,
            )
            return None
        return _orig_webhook_create(self, data)

    discord.Webhook._create_message = _patched_webhook_create

    _orig_state_create = discord.state.ConnectionState.create_message

    def _patched_state_create(self, *, channel, data):
        if isinstance(data, dict):
            if "pinned" not in data:
                logger.debug("Spacebar compat: injecting missing 'pinned' field in state")
                data["pinned"] = False
            for field in ("components", "reactions", "sticker_items", "embeds", "attachments"):
                if not data.get(field):
                    logger.debug(f"Spacebar compat: injecting missing '{field}' field in state")
                    data[field] = []
        return _orig_state_create(self, channel=channel, data=data)

    discord.state.ConnectionState.create_message = _patched_state_create

    _orig_parse_message_create = discord.state.ConnectionState.parse_message_create
    _orig_parse_message_update = discord.state.ConnectionState.parse_message_update

    def _patched_parse_message_create(self, data):
        _sanitize_message_data(data)
        return _orig_parse_message_create(self, data)

    def _patched_parse_message_update(self, data):
        _sanitize_message_data(data)
        return _orig_parse_message_update(self, data)

    discord.state.ConnectionState.parse_message_create = _patched_parse_message_create
    discord.state.ConnectionState.parse_message_update = _patched_parse_message_update


class SpacebarEndpoints:
    def __init__(
        self,
        api: str | None = None,
        cdn: str | None = None,
        gateway: str | None = None,
    ):
        self.api = api
        self.cdn = cdn
        self.gateway = gateway

        self._http_mod: Any = None
        self._asset_mod: Any = None
        self._original_api_base_url: str | None = None
        self._original_asset_base: str | None = None
        self._original_get_gateway: Any = None
        self._original_get_bot_gateway: Any = None
        self._applied = False

    def _load_modules(self):
        self._http_mod = import_module("discord.http")
        self._asset_mod = import_module("discord.asset")

    def apply(self):
        if self._applied:
            return
        if not self.api and not self.cdn and not self.gateway:
            return

        self._load_modules()

        if self.api:
            self._original_api_base_url = self._http_mod.Route.API_BASE_URL
            self._http_mod.Route.API_BASE_URL = f"{self.api}/api/v{self._http_mod.API_VERSION}"
            logger.info(f"API base: {self.api}")

        if self.cdn:
            self._original_asset_base = self._asset_mod.Asset.BASE
            self._asset_mod.Asset.BASE = self.cdn
            logger.info(f"CDN base: {self.cdn}")

        if self.gateway:
            self._original_get_gateway = self._http_mod.HTTPClient.get_gateway
            self._original_get_bot_gateway = self._http_mod.HTTPClient.get_bot_gateway

            gateway_url = self.gateway
            api_version = self._http_mod.API_VERSION

            async def _get_gateway(
                self, *, encoding: str = "json", zlib: bool = True
            ) -> str:
                if zlib:
                    value = "{0}?encoding={1}&v={2}&compress=zlib-stream"
                else:
                    value = "{0}?encoding={1}&v={2}"
                return value.format(gateway_url, encoding, api_version)

            async def _get_bot_gateway(
                self, *, encoding: str = "json", zlib: bool = True
            ) -> tuple[int, str]:
                shards = 1
                if zlib:
                    value = "{0}?encoding={1}&v={2}&compress=zlib-stream"
                else:
                    value = "{0}?encoding={1}&v={2}"
                return shards, value.format(gateway_url, encoding, api_version)

            self._http_mod.HTTPClient.get_gateway = _get_gateway
            self._http_mod.HTTPClient.get_bot_gateway = _get_bot_gateway
            logger.info(f"Gateway: {self.gateway}")

        self._applied = True

    def restore(self):
        if not self._applied:
            return

        if self._original_api_base_url is not None:
            self._http_mod.Route.API_BASE_URL = self._original_api_base_url
            self._original_api_base_url = None

        if self._original_asset_base is not None:
            self._asset_mod.Asset.BASE = self._original_asset_base
            self._original_asset_base = None

        if self._original_get_gateway is not None:
            self._http_mod.HTTPClient.get_gateway = self._original_get_gateway
            self._original_get_gateway = None

        if self._original_get_bot_gateway is not None:
            self._http_mod.HTTPClient.get_bot_gateway = self._original_get_bot_gateway
            self._original_get_bot_gateway = None

        self._applied = False
        logger.info("Endpoints restored to defaults")
