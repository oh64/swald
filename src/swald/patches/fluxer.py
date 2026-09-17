import json as json_mod
from typing import Any

import aiohttp
import fluxer


async def execute_webhook(
    self,
    webhook_id: int | str,
    token: str,
    *,
    content: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
    username: str | None = None,
    avatar_url: str | None = None,
    wait: bool = False,
    files: list[dict[str, Any]] | None = None,
    message_reference: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    route = self._route(
        "POST",
        "/webhooks/{webhook_id}/{token}",
        webhook_id=webhook_id,
        token=token,
    )

    payload: dict[str, Any] = {}

    if content is not None:
        payload["content"] = content
    if embeds is not None:
        payload["embeds"] = embeds
    if username is not None:
        payload["username"] = username
    if avatar_url is not None:
        payload["avatar_url"] = avatar_url
    if message_reference is not None:
        payload["message_reference"] = message_reference

    params = {"wait": "true"} if wait else None

    if files:
        form = aiohttp.FormData()

        payload["attachments"] = [
            {"id": i, "filename": file["filename"]} for i, file in enumerate(files)
        ]

        form.add_field(
            "payload_json",
            json_mod.dumps(payload),
            content_type="application/json",
        )

        for i, file in enumerate(files):
            form.add_field(
                f"files[{i}]",
                file["data"],
                filename=file["filename"],
            )

        return await self.request(route, data=form, params=params)

    return await self.request(
        route,
        json=payload,
        params=params,
    )


async def webhook_send(
    self,
    content: str | None = None,
    *,
    embeds: list[dict[str, Any]] | None = None,
    username: str | None = None,
    avatar_url: str | None = None,
    file: fluxer.File | None = None,
    files: list[fluxer.File] | None = None,
    wait: bool = False,
    message_reference: dict[str, Any] | None = None,
) -> fluxer.Message | None:
    if not self._http:
        raise RuntimeError("Cannot send with webhook without HTTPClient")

    file_list: list[dict[str, Any]] | None = None

    if file is not None:
        file_list = [file.to_dict()]
    elif files is not None:
        file_list = [f.to_dict() for f in files]

    data = await self._http.execute_webhook(
        self.id,
        self.token,
        content=content,
        embeds=embeds,
        username=username,
        avatar_url=avatar_url,
        wait=wait,
        files=file_list,
        message_reference=message_reference,
    )

    if data is not None:
        return fluxer.Message.from_data(data, self._http)

    return None


async def edit_webhook_message(
    self,
    webhook_id: int | str,
    token: str,
    message_id: int | str,
    *,
    content: str | None = None,
) -> dict[str, Any]:
    route = self._route(
        "PATCH",
        "/webhooks/{webhook_id}/{token}/messages/{message_id}",
        webhook_id=webhook_id,
        token=token,
        message_id=message_id,
    )
    payload: dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    return await self.request(route, json=payload)


async def webhook_edit_message(
    self,
    message_id: int | str,
    *,
    content: str | None = None,
) -> fluxer.Message | None:
    if not self._http:
        raise RuntimeError("Cannot edit message with webhook without HTTPClient")

    data = await self._http.edit_webhook_message(
        self.id,
        self.token,
        message_id,
        content=content,
    )

    if data is not None:
        return fluxer.Message.from_data(data, self._http)

    return None


def apply():
    fluxer.HTTPClient.execute_webhook = execute_webhook
    fluxer.HTTPClient.edit_webhook_message = edit_webhook_message
    fluxer.Webhook.send = webhook_send
    fluxer.Webhook.edit_message = webhook_edit_message
