# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Qualify native RoboDojo SDK + TTC through the stock Gym model HTTP server.

This exercises the native SDK actor, not Gym's single_agent_turn environment adapter.
Run with the Gym dependencies used by scripts/smoke.py. All runtime paths are explicit.
"""

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import signal
import socket
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values
from nemo_gym import server_utils
from nemo_gym.server_utils import (
    BaseServerConfig,
    GlobalAIOHTTPAsyncClientConfig,
    HeadServer,
    ServerClient,
    set_global_aiohttp_client,
)
from omegaconf import OmegaConf
from responses_api_models.openai_model.app import SimpleModelServer, SimpleModelServerConfig


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


class Capture:
    def __init__(self, app, output):
        self.app, self.output = app, output
        self.requests, self.results = [], []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].endswith("/responses"):
            return await self.app(scope, receive, send)
        request, response = bytearray(), bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            request.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            payload = json.loads(request)
        except (ValueError, UnicodeDecodeError):
            payload = None
        forwarded = copy.deepcopy(payload)
        items = forwarded.get("input") if isinstance(forwarded, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part.setdefault("detail", "auto")
        body = json.dumps(forwarded).encode() if payload is not None else bytes(request)
        scope = scope | {
            "headers": [(key, value) for key, value in scope.get("headers", []) if key != b"content-length"]
            + [(b"content-length", str(len(body)).encode())]
        }
        status = None
        delivered = False

        async def received():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def sent(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                response.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, received, sent)
        finally:
            index = len(self.requests)
            self.requests.append(payload or {})
            with (self.output / "model-requests.jsonl").open("a") as file:
                file.write(json.dumps(payload) + "\n")
            with (self.output / "model-forwarded-requests.jsonl").open("a") as file:
                file.write(json.dumps(forwarded) + "\n")
            (self.output / f"model-response-{index:03d}.body").write_bytes(response)
            candidates = []
            try:
                candidates.append(json.loads(response))
            except ValueError:
                for line in response.splitlines():
                    if line.startswith(b"data: ") and line != b"data: [DONE]":
                        candidates.append(json.loads(line[6:]))
            metadata = [
                {key: value[key] for key in ("id", "model", "status", "usage", "error") if key in value}
                for event in candidates
                if isinstance(event, dict)
                for value in [event.get("response", event)]
                if isinstance(value, dict)
            ]
            self.results.append({"request": index, "http_status": status, "responses": metadata})
            write_json(self.output / "model-results.json", self.results)


def bound_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def summarize(output, capture, returncode, timed_out, error):
    native = output / "native"
    evidence = {}
    for name, pattern in (
        ("result", "result.json"),
        ("outcome", "outcome.json"),
        ("grading", "grading.json"),
        ("sdk", "sdk/summary.json"),
        ("ttc", "**/ttc-runtime.json"),
        ("codex", "**/session-config.json"),
        ("actor", "actor/receipt.json"),
    ):
        paths = sorted(native.glob(pattern))
        evidence[name] = json.loads(paths[0].read_text()) if paths else {}
    ready = []
    if (native / "simulator.log").exists():
        for line in (native / "simulator.log").read_text(errors="replace").splitlines():
            if line.startswith("{") and '"ready"' in line:
                try:
                    ready.append(json.loads(line))
                except ValueError:
                    pass
    pngs, invalid_images = [], 0
    for payload in capture.requests:
        for item in payload.get("input", []):
            content = item.get("content", []) if isinstance(item, dict) else []
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "input_image":
                    try:
                        data = base64.b64decode(part["image_url"].split(",", 1)[1], validate=True)
                        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("Image is not PNG")
                        pngs.append(hashlib.sha256(data).hexdigest())
                    except (KeyError, ValueError, IndexError):
                        invalid_images += 1
    result, outcome, grading = (evidence[key] for key in ("result", "outcome", "grading"))
    sdk, ttc, codex = (evidence[key] for key in ("sdk", "ttc", "codex"))
    steps = result.get("steps", (outcome.get("native") or {}).get("steps", 0))
    reward = (outcome.get("verification") or {}).get("env_reward")
    if type(reward) not in (int, float) or not math.isfinite(reward):
        reward = None
    failures = []
    for passed, message in (
        (not error, error),
        (not timed_out, "Runner deadline exceeded"),
        (
            outcome.get("class") in ("scored", "policy_failure.decision_budget", "policy_failure.deadline"),
            "Native outcome is missing or dropped",
        ),
        (
            returncode == 0
            or outcome.get("class") in ("policy_failure.decision_budget", "policy_failure.deadline")
            and returncode == 1,
            "Native launcher failed",
        ),
        (reward is not None and bool(grading), "Bounded episode did not reach a verified terminal grade"),
        (
            not outcome.get("unknown_outcome") and not outcome.get("policy_faults") and not sdk.get("faults"),
            "Native SDK reported an infrastructure fault",
        ),
        (bool(ready), "Native simulator readiness is missing"),
        (isinstance(steps, int) and steps > 0 and sdk.get("executions", 0) > 0, "No SDK controls executed"),
        (sdk.get("closed") is True, "SDK host did not close"),
        (
            ttc.get("state") == "closed"
            and bool(ttc.get("sdk_probe"))
            and not any(key in ttc for key in ("fault", "cleanup_error", "daemon_cleanup_error", "export_error")),
            "TTC readiness or cleanup failed",
        ),
        ((codex.get("execution_environment") or {}).get("runtime") == "ttc", "Codex did not execute in TTC"),
        (
            bool(capture.requests)
            and any(
                item["http_status"] == 200 and any(response.get("model") for response in item["responses"])
                for item in capture.results
            ),
            "No real model response through Gym",
        ),
        (bool(pngs) and not invalid_images, "Real PNG model inputs are missing or invalid"),
        (not grading or grading.get("commit_control") == steps, "SDK steps disagree with terminal grade"),
    ):
        if not passed:
            failures.append(message)
    return {
        "scope": "native RoboDojo SDK + TTC + Gym model transport",
        "accepted": not failures,
        "failures": failures,
        "return_codes": {"native_launcher": returncode},
        "timed_out": timed_out,
        "native_outcome_class": outcome.get("class"),
        "terminal_reward": reward,
        "episode_terminal": bool(grading),
        "sdk_control_steps": steps,
        "grading_commit_control": grading.get("commit_control"),
        "model_requests": len(capture.requests),
        "png_parts_received": len(pngs),
        "distinct_png_frames": len(set(pngs)),
        "native_ready": ready,
        "evidence": evidence,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "nanoverse-root",
        "ember-root",
        "simulator-root",
        "node",
        "ttc-manifest",
        "guest-programs",
        "codex",
        "credentials-file",
        "output-dir",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--node-root", type=Path, help="Compiled @nanoverse/environments package; defaults to Ember's dependency"
    )
    parser.add_argument("--key-name", default="NV_API_KEY_2")
    parser.add_argument("--base-url-key", default="NV_BASE_URL")
    parser.add_argument("--model", default="openai/openai/gpt-6.1-sol")
    parser.add_argument("--model-calls", type=int, default=20)
    parser.add_argument("--episode-seconds", type=int, default=1800)
    args = parser.parse_args()
    if min(args.model_calls, args.episode_seconds) < 1:
        parser.error("Budgets must be positive")
    output = args.output_dir.resolve()
    if "general_pickup" in str(output).lower() or len(str(output / "native/host.sock").encode()) >= 104:
        parser.error("Output path must be short and must not contain the task name")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output.chmod(0o700)
    native, ember, simulator = (path.resolve() for path in (args.nanoverse_root, args.ember_root, args.simulator_root))
    values = dotenv_values(args.credentials_file, interpolate=False)
    api_key = os.environ.get(args.key_name) or values[args.key_name]
    base_url = os.environ.get(args.base_url_key) or values[args.base_url_key]
    if not api_key or not base_url:
        raise ValueError("Selected model credential and base URL must be nonempty")
    sockets = {name: bound_socket() for name in ("head", "model")}
    ports = {name: sock.getsockname()[1] for name, sock in sockets.items()}
    common = {"name": "model", "host": "127.0.0.1", "port": ports["model"], "entrypoint": "app.py"}
    config = {
        "head_server": {"host": "127.0.0.1", "port": ports["head"]},
        "model": {"responses_api_models": {"openai_model": common}},
    }
    os.environ["NEMO_GYM_CONFIG_DICT"] = json.dumps(config)
    client = ServerClient(
        head_server_config=BaseServerConfig(**config["head_server"]), global_config_dict=OmegaConf.create(config)
    )
    model = SimpleModelServer(
        config=SimpleModelServerConfig(
            **common,
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
    head = HeadServer(config=BaseServerConfig(**config["head_server"]))
    head.mark_ready()
    capture = Capture(model.setup_webserver(), output)
    servers = {
        name: uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=ports[name], log_level="warning", access_log=False)
        )
        for name, app in (("head", head.setup_webserver()), ("model", capture))
    }
    shared = set_global_aiohttp_client(GlobalAIOHTTPAsyncClientConfig())
    tasks = [asyncio.create_task(server.serve(sockets=[sockets[name]])) for name, server in servers.items()]
    process, timed_out, error = None, False, None
    try:
        async with asyncio.timeout(30):
            while not all(server.started for server in servers.values()):
                for task in tasks:
                    if task.done():
                        task.result()
                await asyncio.sleep(0.05)
        native_config = json.loads((ember / "examples/robodojo/codex-sdk.json").read_text())
        native_config.update(
            python=str(simulator / "venv/bin/python"),
            robodojo=str(simulator / "RoboDojo"),
            assets=str(simulator / "data"),
            home_robot_config=str(simulator / "data/Assets/Robots/x5/curobo_tmp.yml"),
            policies=[],
            episode_seconds=args.episode_seconds,
            decision_limit=args.model_calls,
            request_timeout_seconds=180,
            simulator_command=[
                str(simulator / "venv/bin/python"),
                "-X",
                "faulthandler",
                "-u",
                "-m",
                "robodojo_env.native.server",
                "--kit_args",
                "--/app/updateOrder/checkForHydraRenderComplete=1000 "
                "--/app/renderer/waitIdle=true --/app/hydraEngine/waitIdle=true",
            ],
            native_rpc="unix_socket",
            kit_root=str(output / "kit"),
            harness_command=[
                str(args.node.resolve()),
                str(ember / "harnesses/codex/main.mts"),
                "--codex",
                str(args.codex.resolve()),
            ],
        )
        native_config["model"] = {
            "id": args.model,
            "profile": "codex_chatgpt",
            "reasoning": "xhigh",
            "base_url": f"http://127.0.0.1:{ports['model']}/v1",
        }
        native_config["sdk"] = {
            "execution": "ttc",
            "python": "/usr/bin/python3",
            "call_cap": args.model_calls,
            "profile": {"action_space": "free", "feedback": "auto"},
        }
        native_config["roles"]["actor"]["harness_options"] = {"tools": "native"}
        write_json(output / "native-config.json", native_config)
        env = {
            key: value
            for key, value in os.environ.items()
            if "API_KEY" not in key and key not in (args.key_name, args.base_url_key, "NV_BASE_URL")
        }
        env.update(
            PYTHONPATH=os.pathsep.join((str(native), str(native / "envs"))),
            PATH=str(args.node.resolve().parent) + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
            NANOVERSE_NODE_ROOT=str((args.node_root or ember / "node_modules/@nanoverse/environments").resolve()),
            NANOVERSE_TTC_MANIFEST=str(args.ttc_manifest.resolve()),
            NANOVERSE_GUEST_PROGRAMS=str(args.guest_programs.resolve()),
            NV_API_KEY="local-gym-only",
        )
        policy = ember / "examples/robodojo/task-policies/general_pickup"
        if not policy.is_dir():
            raise ValueError(f"Native task policy is missing: {policy}")
        command = (
            "from pathlib import Path; import sys; from robodojo_env.launch import launch; "
            "sys.exit(launch(Path(sys.argv[1]), Path(sys.argv[2]), policy=Path(sys.argv[3])))"
        )
        with (output / "native-launcher.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                command,
                str(output / "native-config.json"),
                str(output / "native"),
                str(policy),
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), args.episode_seconds + 300)
            except TimeoutError:
                timed_out = True
    except Exception as exception:
        error = f"{type(exception).__name__}: {exception}"
    finally:
        if process is not None and process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 45)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(process.wait(), 10)
        for server in servers.values():
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await shared.close()
        if server_utils._GLOBAL_AIOHTTP_CONTROL_CLIENT is not None:
            await server_utils._GLOBAL_AIOHTTP_CONTROL_CLIENT.close()
        for sock in sockets.values():
            sock.close()
        summary = summarize(output, capture, None if process is None else process.returncode, timed_out, error)
        write_json(output / "summary.json", summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "evidence"}), flush=True)
    return 0 if summary["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
