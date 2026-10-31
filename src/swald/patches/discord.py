from __future__ import annotations

import json
import logging
from typing import Any

import discord
from discord.webhook.async_ import (
    MISSING,
    AsyncWebhookAdapter,
    Webhook,
    async_context,
    handle_message_parameters,
)

logger = logging.getLogger(__name__)


async def patched_execute_webhook(
    self,
    webhook_id: int,
    token: str,
    *,
    session,
    proxy=None,
    proxy_auth=None,
    payload: dict[str, Any] | None = None,
    multipart: list[dict[str, Any]] | None = None,
    files=None,
    thread_id: int | None = None,
    thread_name: str | None = None,
    with_components: bool | None = None,
    wait: bool = False,
    message_reference: dict[str, Any] | None = None,
) -> Any:
    params = {"wait": str(wait).lower()}
    if thread_id:
        params["thread_id"] = thread_id

    if thread_name:
        if multipart:
            for p in multipart:
                if p.get("name") == "payload_json":
                    data = json.loads(p["value"])
                    data["thread_name"] = thread_name
                    p["value"] = json.dumps(data)
                    break
        else:
            if payload is None:
                payload = {}
            payload["thread_name"] = thread_name

    if message_reference is not None:
        if multipart:
            for p in multipart:
                if p.get("name") == "payload_json":
                    data = json.loads(p["value"])
                    data["message_reference"] = message_reference
                    p["value"] = json.dumps(data)
                    break
        else:
            if payload is None:
                payload = {}
            payload["message_reference"] = message_reference

    if with_components is not None:
        params["with_components"] = int(with_components)

    route = discord.http.Route(
        "POST",
        "/webhooks/{webhook_id}/{webhook_token}",
        webhook_id=webhook_id,
        webhook_token=token,
    )
    data = await self.request(
        route,
        session,
        payload=payload,
        proxy=proxy,
        proxy_auth=proxy_auth,
        multipart=multipart,
        files=files,
        params=params,
    )
    if isinstance(data, str):
        # Spacebar responds with e.g. "application/json; charset=utf-8", which py-cord's webhook request does not match
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        logger.warning(
            "Webhook %s returned an unparsable string response: %.200r",
            webhook_id,
            data,
        )
        return None
    return data


async def patched_send(
    self,
    content: str = MISSING,
    *,
    username: str = MISSING,
    avatar_url: Any = MISSING,
    tts: bool = False,
    ephemeral: bool = False,
    file=MISSING,
    files=MISSING,
    embed=MISSING,
    embeds=MISSING,
    allowed_mentions=MISSING,
    view=MISSING,
    poll=MISSING,
    thread=MISSING,
    thread_name: str | None = None,
    applied_tags=MISSING,
    wait: bool = False,
    delete_after: float | None = None,
    silent: bool = False,
    suppress_embeds: bool = False,
    message_reference: dict[str, Any] | None = None,
) -> Any:
    if self.token is None:
        raise ValueError("This webhook does not have a token associated with it")

    previous_mentions = getattr(self._state, "allowed_mentions", None)

    params = handle_message_parameters(
        content=content,
        username=username,
        avatar_url=avatar_url,
        tts=tts,
        ephemeral=ephemeral,
        file=file,
        files=files,
        embed=embed,
        embeds=embeds,
        view=view,
        poll=poll,
        applied_tags=applied_tags,
        allowed_mentions=allowed_mentions,
        previous_allowed_mentions=previous_mentions,
        thread_name=thread_name,
        silent=silent,
        suppress_embeds=suppress_embeds,
    )
    adapter = async_context.get()
    thread_id = None
    if thread is not MISSING:
        thread_id = thread.id

    data = await adapter.execute_webhook(
        self.id,
        self.token,
        session=self.session,
        proxy=self.proxy,
        proxy_auth=self.proxy_auth,
        payload=params.payload,
        multipart=params.multipart,
        files=params.files,
        thread_id=thread_id,
        wait=wait,
        with_components=False,
        message_reference=message_reference,
    )

    msg = None
    if wait:
        if data is not None:
            msg = self._create_message(data)
        else:
            msg = None

    if view and not view.is_finished():
        message_id_val = None if msg is None else msg.id
        view.message = None if msg is None else msg
        if self.parent and not view.parent:
            view.parent = self.parent
        if msg:
            view._refresh(msg.components)
        if view.is_dispatchable():
            self._state.store_view(view, message_id_val)

    return msg


def apply():
    AsyncWebhookAdapter.execute_webhook = patched_execute_webhook
    Webhook.send = patched_send
