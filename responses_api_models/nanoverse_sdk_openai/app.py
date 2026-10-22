# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ingress for a dedicated native SDK root actor with all model-capable helpers disabled.

The inherited server owns provider calls, capture and synthesized downstream SSE.
The rollout prefix is explicit correlation, not authentication.
"""

import json
import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from nemo_gym.episode_types import EpisodeId
from nemo_gym.server_utils import ROLLOUT_PATH_PREFIX, TOKEN_CAPTURE_PATH_SEGMENT
from responses_api_models.openai_model.app import SimpleModelServer, SimpleModelServerConfig
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_ROLLOUT_ROUTE = re.compile(
    rf"^/{re.escape(ROLLOUT_PATH_PREFIX)}/(?P<key>[^/]+)"
    rf"(?:/{re.escape(TOKEN_CAPTURE_PATH_SEGMENT)})?(?P<route>/.*)$"
)
NativeRequest = Callable[[str, str], Awaitable[None]]

_MODEL_ROUTES = frozenset({"/v1/responses", "/v1/chat/completions", "/v1/messages"})


class NativeSDKIngress:
    def __init__(self, app: ASGIApp, on_native_request: NativeRequest | None = None):
        self.app = app
        self.on_native_request = on_native_request

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        match = _ROLLOUT_ROUTE.fullmatch(path)
        route = match["route"] if match else path
        if route not in _MODEL_ROUTES:
            await self.app(scope, receive, send)
            return
        try:
            if match is None:
                raise ValueError("missing rollout route")
            capture_key = EpisodeId.from_capture_key(match["key"]).capture_key
        except ValueError:
            await JSONResponse(
                {"detail": "A valid explicit /ng-rollout/<EpisodeId.capture_key> model route is required"},
                status_code=400,
            )(scope, receive, send)
            return

        authorization = [value for key, value in scope.get("headers", []) if key.lower() == b"authorization"]
        if self.on_native_request is not None and len(authorization) == 1:
            value = authorization[0]
            if value.startswith(b"Bearer "):
                try:
                    bearer = value[7:].decode("ascii")
                except UnicodeDecodeError:
                    bearer = ""
                if bearer:
                    await self.on_native_request(capture_key, bearer)
        headers = [
            (key, value) for key, value in scope.get("headers", [])
            if key.lower() not in (b"x-session-id", b"authorization")
        ]
        headers.append((b"x-session-id", capture_key.encode("ascii")))
        scope = scope | {"headers": headers}
        if route != "/v1/responses":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        changed = False
        items = payload.get("input") if isinstance(payload, dict) else None
        for item in items if isinstance(items, list) else []:
            content = item.get("content") if isinstance(item, dict) else None
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "input_image" and "detail" not in part:
                    part["detail"] = "auto"
                    changed = True
        if changed:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            scope = scope | {
                "headers": [(key, value) for key, value in headers if key.lower() != b"content-length"]
                + [(b"content-length", str(len(body)).encode("ascii"))]
            }

        delivered = False

        async def receive_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, receive_body, send)


class NanoverseSDKOpenAIModelServer(SimpleModelServer):
    config: SimpleModelServerConfig

    def setup_webserver(self, *, on_native_request: NativeRequest | None = None) -> FastAPI:
        app = super().setup_webserver()
        app.add_middleware(NativeSDKIngress, on_native_request=on_native_request)
        return app


if __name__ == "__main__":
    NanoverseSDKOpenAIModelServer.run_webserver()
