# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the Gym protocol with a controlled CLI under the actual native supervisor."""

import asyncio
import json
import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from nemo_gym.rollout_collection import _episode_record
from nemo_gym.server_utils import ServerClient

from environment_servers.nanoverse_sdk import app as sdk_app
from environment_servers.nanoverse_sdk.app import NativeSDKConfig, NativeSDKEnvironmentServer, NativeSDKRequest
from environment_servers.nanoverse_sdk.native import (
    BRIDGE_ROOT,
    MODEL_ID,
    NATIVE_ROOT,
    SUPPORTED_ENVIRONMENTS,
    CaseRegistry,
    native_module,
)

CLI = r"""
import json, os, pathlib, signal, subprocess, sys, time
args=sys.argv
config=json.loads(pathlib.Path(args[args.index('--config')+1]).read_text())
mode=os.environ.get('FIXTURE_MODE','scored')
if args[1]=='validate':
    if mode=='validation_failed': sys.exit(1)
    if mode=='validation_hang':
        pathlib.Path(os.environ['READY_FILE']).write_text(str(os.getpid()))
        time.sleep(600)
    sys.exit(0)
root=pathlib.Path(args[args.index('--output')+1]); root.mkdir(parents=True)
def write(name,value):
    path=root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value))
env=config['integration']
write('result.json', {'integration':env,'native':None,'scope':'CPU contract fixture; not a benchmark result'})
write('actor/receipt.json', {'role':'actor','finished':True,'seconds':0.2,'calls':1,
 'summary_calls':int(mode=='compacted')})
write('actor/ttc-runtime.json', {'state':'closed','sdk_probe':'controlled readiness'})
write('native/sdk/summary.json' if env=='exemplar' else 'sdk/summary.json', {'closed':True,'executions':1})
write('sdk-audit.json', {'ok':True,'executions':1,'native_actions':1})
(root/'actor/provider-responses.jsonl').write_text(json.dumps({'status':200,'body':json.dumps({'id':'resp-exact','usage':{'total_tokens':17}})})+'\n')
(root/'actor/timeline.jsonl').write_text('\n'.join(json.dumps(row) for row in [
 {'type':'tool_execution_start','tool_call_id':'call-exact','tool':'exec_command','time':1000},
 {'type':'tool_execution_end','tool_call_id':'call-exact','is_error':False,'time':1100}])+'\n')
write('received-config.json',config)
write('received-env.json',dict(os.environ))
outcome={'schema':'ember_bridge.outcome.v1',
 'class':mode if mode in ('provider_blocked','infrastructure','policy_failure.deadline') else 'scored',
 'readback_verified':True,'verification':{'env_reward':0.75,'done':True,'metrics':{'termination':'native','success_once':True}}}
write('outcome.json',outcome)
if mode=='missing': (root/'outcome.json').unlink()
if mode=='corrupt': (root/'outcome.json').write_text('{broken')
if mode=='ttc_unclosed': write('actor/ttc-runtime.json',{'state':'ready','sdk_probe':'controlled readiness'})
if mode=='empty_fixture': write('sdk-audit.json',{'ok':True,'executions':1,'native_actions':0})
if mode=='archive':
    result=json.loads((root/'result.json').read_text())
    result['sdk_summary']=json.loads((root/'sdk/summary.json').read_text())
    write('result.json',result)
    (root/'sdk/summary.json').unlink()
if mode=='hanging':
    child=subprocess.Popen([sys.executable,'-c','import os,time;os.setsid();time.sleep(600)'])
    pathlib.Path(os.environ['READY_FILE']).write_text(str(child.pid))
    time.sleep(600)
sys.exit(1 if mode=='policy_failure.deadline' else 0)
"""


@pytest.fixture
def server_factory(tmp_path, monkeypatch):
    package = tmp_path / "fake" / "ember_bridge"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(CLI)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    monkeypatch.setattr(sdk_app, "get_server_url", lambda name: "http://gym-model.invalid:1234")
    count = 0

    def create(environment="robodojo", mode="scored", **overrides):
        nonlocal count
        count += 1
        template = tmp_path / f"config-{count}.json"
        template.write_text(
            json.dumps(
                {
                    "integration": environment,
                    "model": {"id": MODEL_ID, "profile": "codex_chatgpt", "reasoning": "xhigh", "api_key": "secret"},
                    "harness_command": ["/usr/bin/node", str(BRIDGE_ROOT / "harnesses/codex/main.mts")],
                    "roles": {"actor": {"tools": [], "skills": [], "harness_options": {"tools": "native"}}},
                    "sdk": {"execution": "ttc"},
                    "provider_auth": "api_key",
                }
            )
        )
        registry_path = tmp_path / f"registry-{count}.json"
        registry_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "case-one",
                            "environment": environment,
                            "python": sys.executable,
                            "config_path": str(template),
                            "native_seconds": 2,
                            "cleanup_seconds": 3,
                            "runtime_env": {
                                "PYTHONPATH": str(package.parent),
                                "FIXTURE_MODE": mode,
                                "READY_FILE": str(tmp_path / f"ready-{count}"),
                                "NANOVERSE_TTC_MANIFEST": str(manifest),
                                "NANOVERSE_GUEST_PROGRAMS": str(manifest),
                            },
                        }
                    ]
                }
            )
        )
        client = MagicMock(spec=ServerClient)
        client.global_config_dict = {}
        config = NativeSDKConfig(
            **(
                {
                    "name": "nanoverse_sdk",
                    "host": "127.0.0.1",
                    "port": 12345,
                    "entrypoint": "app.py",
                    "registry_path": registry_path,
                    "output_root": tmp_path / f"episodes-{count}",
                    "model_server": {"type": "responses_api_models", "name": "policy_model"},
                    "cleanup_timeout_seconds": 5,
                    "default_episode_timeout_seconds": 20,
                }
                | overrides
            )
        )
        return NativeSDKEnvironmentServer(config=config, server_client=client)

    return create


def request(environment="robodojo", attempt=0, **overrides):
    return {
        "episode_id": {"rollout_id": "rollout", "attempt": attempt},
        "task": {
            "task_id": {"taskset": f"{environment}:run-one", "task_id": "case-one"},
            "task_input": {"case_id": "case-one"},
        },
    } | overrides


async def post(server, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.setup_webserver()), base_url="http://gym"
    ) as client:
        result = await client.post("/run", json=body)
        assert result.status_code == 200, result.text
        return result.json()


async def wait_ready(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.02)


async def test_native_grade_and_real_collector_projection(server_factory, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-stay-in-gym")
    server = server_factory(token_capture=True)
    wire = await post(server, request(attempt=2))
    assert wire["failure"] is None, wire
    result = _episode_record(wire)
    assert result["reward"] == result["verification"]["env_reward"] == 0.75
    assert result["native_attempt"] is True
    owner = next(iter(server._allocations.values()))
    recorded = json.loads((owner.paths.native / "received-config.json").read_text())
    assert "/ng-rollout/rollout-a2/training-token-capture/" in recorded["model"]["base_url"]
    assert recorded["model"]["base_url"].endswith("/v1")
    assert "api_key" not in recorded["model"]
    env = json.loads((owner.paths.native / "received-env.json").read_text())
    assert "OPENAI_API_KEY" not in env
    assert len(env["NV_API_KEY"]) >= 40
    assert env["NV_API_KEY"] not in ("local-gym-only", "must-stay-in-gym")
    assert not owner._capability
    assert env["NV_API_KEY"] not in repr(owner)
    observations = result["ng_agent_observations"]
    actor, tool = observations["records"]
    assert actor["invocation_id"] == "rollout-a2"
    assert actor["model_calls"][0]["response_id"] == "resp-exact"
    assert tool["tool_call_id"] == "call-exact"
    assert tool["duration_ms"] == pytest.approx(100)
    assert owner.closed and owner.process.returncode == 0
    assert native_module(NATIVE_ROOT, "environment.processes").cleanup_receipt_valid(
        owner.paths.cleanup_receipt, owner.identity
    )


async def test_native_actor_compaction_preserves_its_verified_grade(server_factory):
    wire = await post(server_factory(mode="compacted"), request())
    assert wire["failure"] is None, wire
    result = _episode_record(wire)
    assert result["reward"] == result["verification"]["env_reward"] == 0.75
    assert result["ng_agent_observations"]["records"][0]["invocation_id"] == "rollout"


@pytest.mark.parametrize("actor_options,accepted", [({}, False), ({"tools": "native"}, True)])
def test_actor_harness_options_replace_global_options(server_factory, actor_options, accepted):
    server = server_factory()
    case = server._registry.cases[0]
    config = json.loads(case.config_path.read_text())
    config["harness_options"] = {"tools": "native", **({"execution": "host"} if accepted else {})}
    config["roles"]["actor"]["harness_options"] = actor_options
    case.config_path.write_text(json.dumps(config))
    registry = CaseRegistry.load(server.config.registry_path)
    if accepted:
        registry.validate_deployment(NATIVE_ROOT, BRIDGE_ROOT)
    else:
        with pytest.raises(ValueError, match="sole native tools"):
            registry.validate_deployment(NATIVE_ROOT, BRIDGE_ROOT)


async def test_fixture_stays_reward_free_in_stock_collector(server_factory):
    wire = await post(server_factory(environment="exemplar"), request("exemplar"))
    result = _episode_record(wire)
    assert wire["failure"] is None, wire
    assert result["kind"] == "fixture" and result["fixture_passed"] is True
    assert "reward" not in result and result["native"] is None


async def test_native_policy_limit_is_analysis_zero(server_factory):
    wire = await post(server_factory(mode="policy_failure.deadline"), request())
    result = _episode_record(wire)
    assert result["outcome_class"] == "policy_failure.deadline"
    assert result["native_attempt"] is False and result["reward"] == 0


async def test_outer_gym_deadline_closes_native_startup(server_factory):
    server = server_factory(mode="validation_hang", default_episode_timeout_seconds=0.3)
    wire = await post(server, request())
    assert wire["result"] is None and "reward" not in _episode_record(wire)
    owner = next(iter(server._allocations.values()))
    assert owner.closed and owner.process.returncode is not None
    assert native_module(NATIVE_ROOT, "environment.processes").cleanup_receipt_valid(
        owner.paths.cleanup_receipt, owner.identity
    )


@pytest.mark.parametrize("deadline", [None, float("inf")])
def test_gym_episode_requires_a_finite_deadline(server_factory, deadline):
    with pytest.raises(ValueError):
        server_factory(default_episode_timeout_seconds=deadline)


async def test_fixture_cannot_pass_without_real_audited_controls(server_factory):
    wire = await post(server_factory(environment="exemplar", mode="empty_fixture"), request("exemplar"))
    assert wire["result"] is None and "reward" not in _episode_record(wire)


async def test_archive_uses_recorded_native_sdk_summary(server_factory):
    wire = await post(server_factory(environment="libero", mode="archive"), request("libero"))
    assert wire["failure"] is None, wire
    assert wire["result"]["reward"] == 0.75


async def test_native_preflight_config_failure_is_terminal_and_unscored(server_factory):
    wire = await post(server_factory(mode="validation_failed"), request())
    assert wire["result"] is None and wire["failure"]["terminal"] is True
    assert "configuration validation failed" in wire["failure"]["failure_reason"]
    assert "reward" not in _episode_record(wire)


async def test_operator_template_is_pinned_at_deployment_load(server_factory):
    server = server_factory()
    server._registry.cases[0].config_path.write_text('{"untrusted_late_replacement":true}')
    wire = await post(server, request())
    assert wire["failure"] is None, wire


@pytest.mark.parametrize(
    "mode,terminal", [("provider_blocked", True), ("infrastructure", False), ("missing", False), ("corrupt", False)]
)
async def test_unscored_failures_retain_actual_observations(server_factory, mode, terminal):
    wire = await post(server_factory(mode=mode), request())
    assert wire["result"] is None
    assert wire["failure"]["terminal"] is terminal
    record = _episode_record(wire)
    assert "reward" not in record
    partial = record["_ng_failure_partial_response"]
    assert partial == wire["failure"]["partial_response"]
    assert "reward" not in partial
    assert partial["ng_agent_observations"]["records"][0]["model_calls"][0]["response_id"] == "resp-exact"


async def test_conflicting_attempt_cannot_launch_twice(server_factory):
    server = server_factory()
    first = await post(server, request())
    duplicate = await post(server, request())
    assert first["result"] is not None
    assert duplicate["failure"]["terminal"] is True
    assert "identity" in duplicate["failure"]["failure_reason"]
    retry = await post(server, request(attempt=1))
    assert retry["result"] is not None
    assert retry["result"]["evidence_id"] != first["result"]["evidence_id"]


async def test_cleanup_identity_failure_quarantines_and_can_recover(server_factory, monkeypatch):
    server = server_factory()
    processes = native_module(NATIVE_ROOT, "environment.processes")
    valid = processes.cleanup_receipt_valid
    monkeypatch.setattr(processes, "cleanup_receipt_valid", lambda path, identity: False)
    wire = await post(server, request())
    assert wire["result"] is None and "quarantined" in wire["failure"]["failure_reason"]
    blocked = await post(server, request(attempt=1))
    assert blocked["result"] is None
    assert len(list(server.config.output_root.glob("*/identity.json"))) == 1
    monkeypatch.setattr(processes, "cleanup_receipt_valid", valid)
    recovered = await post(server, request(attempt=1))
    assert recovered["result"] is not None, recovered


async def test_runtime_closure_failure_survives_server_restart(server_factory):
    server = server_factory(mode="ttc_unclosed")
    wire = await post(server, request())
    assert wire["result"] is None and "quarantined" in wire["failure"]["failure_reason"]
    restarted = NativeSDKEnvironmentServer(config=server.config, server_client=server.server_client)
    blocked = await post(restarted, request(attempt=1))
    assert blocked["result"] is None
    assert len(list(server.config.output_root.glob("*/identity.json"))) == 1


@pytest.mark.parametrize("mode", ["validation_hang", "hanging"])
async def test_cancellation_closes_owned_descendants(server_factory, mode):
    server = server_factory(mode=mode)
    case = server._registry.cases[0]
    task = asyncio.create_task(server.run_request(NativeSDKRequest.model_validate(request())))
    await wait_ready(Path(case.runtime_env["READY_FILE"]))
    child = int(Path(case.runtime_env["READY_FILE"]).read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner = next(iter(server._allocations.values()))
    assert owner.closed
    assert owner.process.returncode is not None
    assert not Path(f"/proc/{child}").exists()
    await owner.aclose()


async def test_cancel_during_process_creation_keeps_owner(server_factory, monkeypatch):
    original = asyncio.create_subprocess_exec
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    server = server_factory()
    task = asyncio.create_task(server.run_request(NativeSDKRequest.model_validate(request())))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    owner = next(iter(server._allocations.values()))
    assert owner.closed and owner.process.returncode is not None


async def test_supervisor_survives_parent_loss_until_descendants_close(tmp_path):
    receipt, ready = tmp_path / "cleanup.json", tmp_path / "child.pid"
    child_code = (
        "import os,pathlib,sys,time;os.setsid();pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(600)"
    )
    parent_code = (
        "import importlib.util,pathlib,signal,subprocess,sys,time;"
        "spec=importlib.util.spec_from_file_location('owner',sys.argv[1]);"
        "owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner);"
        "subprocess.Popen([sys.executable,sys.argv[2],'--cleanup-receipt',sys.argv[3],"
        "'--cleanup-id','parent-loss','--',sys.executable,'-c',sys.argv[4],sys.argv[5]],"
        "preexec_fn=owner.parent_death_signal(signal.SIGTERM));time.sleep(600)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        str(NATIVE_ROOT / "nanoverse/environment/processes.py"),
        str(NATIVE_ROOT / "nanoverse/environment/supervise.py"),
        str(receipt),
        child_code,
        str(ready),
    )
    try:
        await wait_ready(ready)
        child_pid = int(ready.read_text())
        process.send_signal(signal.SIGKILL)
        await process.wait()
        await wait_ready(receipt)
        assert native_module(NATIVE_ROOT, "environment.processes").cleanup_receipt_valid(receipt, "parent-loss")
        assert not Path(f"/proc/{child_pid}").exists()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def test_http_rejects_runtime_overrides_and_wrong_case_binding(server_factory):
    server = server_factory()
    body = request()
    body["task"]["task_input"]["command"] = ["untrusted"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.setup_webserver()), base_url="http://gym"
    ) as client:
        result = await client.post("/run", json=body)
    assert result.status_code == 422
    mismatch = await post(server, request("exemplar"))
    assert mismatch["failure"]["terminal"] is True


def test_registry_subset_and_one_supported_environment_set(server_factory):
    server = server_factory(environment="exemplar")
    registry = CaseRegistry.load(server.config.registry_path)
    assert tuple(case.environment for case in registry.cases) == ("exemplar",)
    assert len(SUPPORTED_ENVIRONMENTS) == 9
    value = json.loads(server.config.registry_path.read_text())
    value["cases"][0]["runtime_env"]["OPENAI_API_KEY"] = "forbidden"
    with pytest.raises(ValueError, match="credentials"):
        CaseRegistry.model_validate(value)


async def test_aggregation_partitions_fixture_and_preserves_rows(server_factory, monkeypatch):
    server = server_factory()
    rows = [
        {"kind": "native", "reward": 0.75, "task_index": 3, "repeat_index": 5},
        {"kind": "fixture", "fixture_passed": True},
    ]
    seen = []
    compute = sdk_app.compute_aggregate_metrics

    def capture(native):
        seen.extend(native)
        return compute(native)

    monkeypatch.setattr(sdk_app, "compute_aggregate_metrics", capture)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.setup_webserver()), base_url="http://gym"
    ) as client:
        reply = await client.post("/aggregate_metrics", json={"verify_responses": rows})
        fixture_only = await client.post("/aggregate_metrics", json={"verify_responses": rows[1:]})
    assert reply.status_code == 200, reply.text
    assert seen == rows[:1]
    assert reply.json()["agent_metrics"]["sdk_fixture_pass_count"] == 1
    assert not any("mean" in key for key in fixture_only.json()["agent_metrics"])


async def test_stale_rollout_cannot_reach_current_owner_callback(server_factory):
    import hashlib
    from types import SimpleNamespace

    server = server_factory()
    calls = []

    async def observed(bearer):
        calls.append(bearer)

    key = 'current-rollout'
    path = server.config.output_root / hashlib.sha256(key.encode()).hexdigest()[:16]
    owner = SimpleNamespace(paths=SimpleNamespace(root=path), native_request=observed)
    server._allocations['controlled'] = owner
    await server.native_request('stale-rollout', 'any-token')
    assert calls == []
    await server.native_request(key, 'current-local-token')
    assert calls == ['current-local-token']
