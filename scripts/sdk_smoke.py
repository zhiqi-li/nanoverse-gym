# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run one trusted native SDK case through Gym HTTP and its standard collector."""

import argparse
import asyncio
import json
import os
import socket
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import uvicorn
from dotenv import dotenv_values
from nemo_gym import server_utils
from nemo_gym.rollout_collection import RolloutCollectionConfig, RolloutCollectionHelper
from nemo_gym.server_utils import (
    BaseServerConfig,
    GlobalAIOHTTPAsyncClientConfig,
    HeadServer,
    ServerClient,
    set_global_aiohttp_client,
)
from omegaconf import OmegaConf

from environment_servers.nanoverse_sdk.app import NativeSDKConfig, NativeSDKEnvironmentServer
from environment_servers.nanoverse_sdk.native import CaseRegistry
from responses_api_models.nanoverse_sdk_openai.app import (
    NanoverseSDKOpenAIModelServer,
    SimpleModelServerConfig,
)


def bound_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


class EmbeddedServer(uvicorn.Server):
    """Leave process signals to asyncio.run, which owns all embedded servers."""

    def capture_signals(self):
        return nullcontext()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--bridge-root", type=Path, required=True)
    parser.add_argument("--key-name", default="NV_API_KEY_2")
    parser.add_argument("--base-url-key", default="NV_BASE_URL")
    parser.add_argument("--model", default="openai/openai/gpt-6.1-sol")
    parser.add_argument("--episode-seconds", type=int, default=1500)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output.chmod(0o700)
    registry = CaseRegistry.load(args.registry.resolve())
    case = next(case for case in registry.cases if case.case_id == args.case_id)
    values = dotenv_values(args.credentials_file, interpolate=False)
    api_key = os.environ.get(args.key_name) or values[args.key_name]
    base_url = os.environ.get(args.base_url_key) or values[args.base_url_key]
    if not api_key or not base_url:
        raise ValueError("Selected model credential and base URL must be nonempty")
    sockets = {name: bound_socket() for name in ("head", "model", "sdk")}
    ports = {name: sock.getsockname()[1] for name, sock in sockets.items()}
    common = {
        name: {"name": name, "host": "127.0.0.1", "port": ports[name], "entrypoint": "app.py"}
        for name in ("model", "sdk")
    }
    config = {
        "head_server": {"host": "127.0.0.1", "port": ports["head"]},
        "model": {"responses_api_models": {"nanoverse_sdk_openai": common["model"]}},
        "sdk": {"environment_servers": {"nanoverse_sdk": common["sdk"]}},
        "observability_enabled": True,
        "model_call_capture_dir": str(output / "model-calls"),
    }
    os.environ["NEMO_GYM_CONFIG_DICT"] = json.dumps(config)
    client = ServerClient(
        head_server_config=BaseServerConfig(**config["head_server"]),
        global_config_dict=OmegaConf.create(config),
    )
    model = NanoverseSDKOpenAIModelServer(
        config=SimpleModelServerConfig(
            **common["model"],
            openai_base_url=base_url,
            openai_api_key=api_key,
            openai_model=args.model,
            upstream_max_num_tries=1,
            max_http_attempts=1,
            max_rate_limit_wait_seconds=0,
            upstream_request_timeout_seconds=180,
        ),
        server_client=client,
    )
    environment = NativeSDKEnvironmentServer(
        config=NativeSDKConfig(
            **common["sdk"],
            registry_path=args.registry.resolve(),
            output_root=output / "n",
            native_root=args.native_root.resolve(),
            bridge_root=args.bridge_root.resolve(),
            model_server={"type": "responses_api_models", "name": "model"},
            model_id=args.model,
            token_capture=False,
            max_concurrent_episodes=1,
            queue_timeout_seconds=30,
            default_episode_timeout_seconds=args.episode_seconds,
            cleanup_timeout_seconds=90,
        ),
        server_client=client,
    )
    head = HeadServer(config=BaseServerConfig(**config["head_server"]))
    head.mark_ready()
    apps = {"head": head.setup_webserver()}
    for name, server in (("model", model), ("sdk", environment)):
        app = (
            model.setup_webserver(on_native_request=environment.native_request)
            if name == "model" else server.setup_webserver()
        )
        server.setup_liveness(app)
        server.setup_exception_middleware(app)
        server.setup_cancellation_middleware(app)
        server_utils.install_request_context_isolation(app)
        apps[name] = app
    servers = {
        name: EmbeddedServer(
            uvicorn.Config(app, host="127.0.0.1", port=ports[name], log_level="warning", access_log=False)
        )
        for name, app in apps.items()
    }
    shared = set_global_aiohttp_client(GlobalAIOHTTPAsyncClientConfig())
    tasks = [asyncio.create_task(server.serve(sockets=[sockets[name]])) for name, server in servers.items()]
    try:
        async with asyncio.timeout(30):
            while not all(server.started for server in servers.values()):
                for task in tasks:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.05)
        taskset = f"{case.environment}:sdk-{uuid4().hex[:8]}"
        row = {"task_id": {"taskset": taskset, "task_id": case.case_id}, "task_input": {"case_id": case.case_id}}
        inputs, rollouts = output / "tasks.jsonl", output / "rollouts.jsonl"
        inputs.write_text(json.dumps(row) + "\n")
        await RolloutCollectionHelper().run_from_config(
            RolloutCollectionConfig(
                input_jsonl_fpath=str(inputs),
                output_jsonl_fpath=str(rollouts),
                environment_routing_mode="taskset",
                environment_server_routes={taskset: "sdk"},
                num_samples_in_parallel=1,
                num_repeats=1,
                require_complete=True,
            )
        )
        records = [json.loads(line) for line in rollouts.read_text().splitlines() if line]
        report = {
            "scope": "Gym native SDK Environment HTTP + standard collector",
            "environment": case.environment,
            "records": len(records),
            "reward_present": bool(records and "reward" in records[0]),
            "kind": records[0].get("kind") if records else None,
            "reward": records[0].get("reward") if records else None,
        }
        (output / "collector-summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    finally:
        for server in servers.values():
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await shared.close()
        if server_utils._GLOBAL_AIOHTTP_CONTROL_CLIENT is not None:
            await server_utils._GLOBAL_AIOHTTP_CONTROL_CLIENT.close()
        for sock in sockets.values():
            sock.close()


if __name__ == "__main__":
    asyncio.run(main())
