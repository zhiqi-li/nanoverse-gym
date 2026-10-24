# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Trusted native cases, descendant ownership, and native evidence readback."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import os
import secrets
import signal
import sys
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nemo_gym.config_types import ModelServerRef
from nemo_gym.episode_types import TaskId
from nemo_gym.rollout_observability import (
    AgentInvocation,
    AgentObservationBundle,
    ModelCallRef,
    ObservationGap,
    ToolCallObservation,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    PrivateAttr,
    field_validator,
    model_validator,
)

from environment_servers.nanoverse_sdk.stop import HarnessStop

SUPPORTED_ENVIRONMENTS = (
    "behavior1k",
    "exemplar",
    "libero",
    "molmospaces",
    "robocasa365",
    "robodojo",
    "roboforge",
    "robolab",
    "robotwin",
)
REPOSITORY = Path(__file__).resolve().parents[2]
NATIVE_ROOT = REPOSITORY / ".cache/ttc-pair/nanoverse"
BRIDGE_ROOT = REPOSITORY / ".cache/ttc-pair/ember_bridge"
MODEL_ID = "openai/openai/gpt-6.1-sol"


def native_module(root: Path, name: str):
    """Import pinned stdlib evidence readers without installing simulator dependencies."""
    prefix = "_gym_native_" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]
    parts = name.split(".")
    for index in range(len(parts)):
        package = ".".join([prefix, *parts[:index]])
        if package not in sys.modules:
            module = types.ModuleType(package)
            module.__path__ = [str(root / "nanoverse" / Path(*parts[:index]))]
            sys.modules[package] = module
    qualified = prefix + "." + name
    if qualified not in sys.modules:
        spec = importlib.util.spec_from_file_location(qualified, root / "nanoverse" / (name.replace(".", "/") + ".py"))
        if spec is None or spec.loader is None:
            raise ValueError(f"Missing pinned native module {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
    return sys.modules[qualified]


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Native evidence must be an object: {path.name}")
    return value


def read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError(f"Native log contains a non-object: {path.name}")
    return records


def credential_key(key: str) -> bool:
    key = key.lower()
    return any(part in key for part in ("api_key", "password", "secret", "authorization", "credential")) or (
        key in ("token", "nv_base_url") or key.endswith("_token") or key.endswith("_token_file")
    )


def scrub_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: scrub_config(item) for key, item in value.items() if not credential_key(key)}
    if isinstance(value, list):
        return [scrub_config(item) for item in value]
    return value


def runtime_closed(root: Path, contract: EvidenceContract, environment: str) -> tuple[dict, dict]:
    result_path = root / contract.result_path
    result = read_json(result_path) if result_path.exists() else {}
    sdk = result.get("sdk_summary")
    if not isinstance(sdk, dict):
        sdk_path = contract.sdk_summary_path or (
            "native/sdk/summary.json" if environment in ("exemplar", "robolab") else "sdk/summary.json"
        )
        sdk = read_json(root / sdk_path)
    ttc = read_json(root / contract.ttc_path)
    if sdk.get("closed") is not True or sdk.get("faults"):
        raise NativeFailure("Native SDK closure is unconfirmed")
    if (
        ttc.get("state") != "closed"
        or not ttc.get("sdk_probe")
        or any(name in ttc for name in ("fault", "cleanup_error", "daemon_cleanup_error", "export_error"))
    ):
        raise NativeFailure("Native TTC closure is unconfirmed")
    return sdk, ttc


class EvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_path: str = "result.json"
    outcome_path: str = "outcome.json"
    sdk_summary_path: str | None = None
    ttc_path: str = "actor/ttc-runtime.json"
    actor_path: str = "actor/receipt.json"
    fixture_audit_path: str = "sdk-audit.json"

    @field_validator("result_path", "outcome_path", "sdk_summary_path", "ttc_path", "actor_path", "fixture_audit_path")
    @classmethod
    def relative_evidence(cls, value):
        if value is not None and (Path(value).is_absolute() or ".." in Path(value).parts or not value):
            raise ValueError("Evidence paths must be nonempty paths within the native output")
        return value


class OperatorCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    environment: str
    python: Path
    config_path: Path
    policy_path: Path | None = None
    runtime_env: dict[str, str] = Field(default_factory=dict)
    native_seconds: PositiveFloat = Field(default=600, allow_inf_nan=False)
    model_calls: PositiveInt = 40
    cleanup_seconds: PositiveFloat = Field(default=60, allow_inf_nan=False)
    allocation_id: str | None = None
    evidence: EvidenceContract = Field(default_factory=EvidenceContract)
    _template: dict[str, Any] | None = PrivateAttr(default=None)

    @field_validator("environment")
    @classmethod
    def supported(cls, value):
        if value not in SUPPORTED_ENVIRONMENTS:
            raise ValueError(f"Unsupported SDK environment {value!r}")
        return value

    @field_validator("runtime_env")
    @classmethod
    def credentials_stay_in_gym(cls, value):
        if any(credential_key(key) for key in value):
            raise ValueError("Native runtime_env must not contain provider credentials or model URL overrides")
        return value

    @model_validator(mode="after")
    def paths_exist(self):
        if not self.python.is_absolute() or not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise ValueError("python must name an existing executable by absolute path")
        if not self.config_path.is_absolute() or not self.config_path.is_file():
            raise ValueError("config_path must name an existing absolute template")
        if self.policy_path is not None and (not self.policy_path.is_absolute() or not self.policy_path.is_dir()):
            raise ValueError("policy_path must name an existing absolute policy directory")
        return self

    @property
    def allocation(self) -> str:
        return self.allocation_id or self.environment


class CaseRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: tuple[OperatorCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_cases(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate operator case_id")
        return self

    @classmethod
    def load(cls, path: Path) -> CaseRegistry:
        return cls.model_validate_json(Path(path).read_text())

    def resolve(self, case_id: str, task_id: TaskId) -> OperatorCase:
        case = next((case for case in self.cases if case.case_id == case_id), None)
        if case is None:
            raise ValueError("Unknown operator case_id")
        environment, separator, run_id = task_id.taskset.partition(":")
        if environment != case.environment or not separator or not run_id or task_id.task_id != case.case_id:
            raise ValueError("Task identity does not match its registered case and run-qualified taskset")
        return case

    def validate_deployment(self, native_root: Path, bridge_root: Path) -> None:
        catalog = native_module(native_root, "catalog").load_catalog(native_root)
        registry = read_json(bridge_root / "harnesses/registry.json")
        codex_entry = (bridge_root / registry["codex"]["entry"]).resolve()
        for case in self.cases:
            spec = catalog.get(case.environment)
            if spec is None or spec.profile != "sdk" or not spec.sdk or not spec.launcher:
                raise ValueError(f"Pinned catalog has no native SDK launcher for {case.environment}")
            config = native_module(native_root, "environment.config").load_config(case.config_path)
            if config.get("integration") != case.environment:
                raise ValueError("Native template integration does not match the registered environment")
            if (config.get("sdk") or {}).get("execution") != "ttc":
                raise ValueError("Native SDK execution must be ttc")
            model = config.get("model") or {}
            if (
                model.get("id") != MODEL_ID
                or model.get("profile") != "codex_chatgpt"
                or model.get("reasoning") != "xhigh"
            ):
                raise ValueError("Native actor must use GPT-6.1 Sol, codex_chatgpt, xhigh")
            command = config.get("harness_command") or []
            if not isinstance(command, list) or sum(Path(part).resolve() == codex_entry for part in command) != 1:
                raise ValueError("Native actor must use the pinned registered Codex Harness")
            roles = config.get("roles") or {}
            if set(roles) != {"actor"}:
                raise ValueError("Native-only model attribution requires exactly one actor role")
            actor = roles["actor"]
            options = actor.get("harness_options", config.get("harness_options")) or {}
            if options.get("tools") != "native" or "execution" in options:
                raise ValueError("Native actor must use sole native tools in TTC")
            if (
                actor.get("model")
                or actor.get("tools")
                or actor.get("skills")
                or actor.get("harness_tools")
                or config.get("harness_tools")
            ):
                raise ValueError("Actor model overrides and model-capable helper tools are disabled")
            sdk = config["sdk"]
            if sdk.get("review_policies") or sdk.get("s1_policy") or config.get("policies"):
                raise ValueError("Model-capable native helper policies must be disabled")
            if config.get("provider_auth", "api_key") != "api_key":
                raise ValueError("Provider authentication belongs to Gym's model server")
            if config.get("smoke"):
                raise ValueError("Native smoke helpers are disabled on the sole actor model route")
            for name in ("NANOVERSE_TTC_MANIFEST", "NANOVERSE_GUEST_PROGRAMS"):
                if not Path(case.runtime_env.get(name, "")).is_file():
                    raise ValueError(f"Native runtime_env must supply an existing {name}")
            case._template = config


@dataclass(frozen=True)
class EpisodePaths:
    root: Path

    @property
    def native(self) -> Path:
        return self.root / "n"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def cleanup_receipt(self) -> Path:
        return self.root / "cleanup.json"


class NativeFailure(Exception):
    def __init__(self, reason: str, *, terminal: bool = False):
        super().__init__(reason)
        self.terminal = terminal


@dataclass
class NativeEvidence:
    result: dict[str, Any]
    outcome: dict[str, Any] | None
    actor: dict[str, Any]
    sdk: dict[str, Any]
    ttc: dict[str, Any]
    fixture_audit: dict[str, Any] | None = None


@dataclass
class EpisodeProcess:
    case: OperatorCase
    paths: EpisodePaths
    native_root: Path
    bridge_root: Path
    identity: str = field(default_factory=lambda: uuid.uuid4().hex)
    process: asyncio.subprocess.Process | None = None
    closed: bool = False
    _log: Any = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _spawning: asyncio.Task | None = None
    _capability: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    _harness_stop: HarnessStop | None = field(default=None, repr=False)
    _driver_command: tuple[str, ...] = field(default=(), repr=False)
    _cli_command: tuple[str, ...] = field(default=(), repr=False)
    _request_seen: bool = False
    _closing: bool = False
    _cleanup_deadline: float | None = None
    _fallback_sent: bool = False

    async def native_request(self, bearer: str) -> bool:
        if self._closing or self.closed:
            return False
        async with self._lock:
            if (
                self.closed or self._closing or not self._capability
                or not secrets.compare_digest(bearer, self._capability)
                or self.process is None or self.process.returncode is not None
            ):
                return False
            if not self._request_seen:
                self._harness_stop = HarnessStop.bind(
                    self.process.pid, self._driver_command, self._cli_command,
                    (*self.case._template["harness_command"], str(self.paths.native / "actor/input.json")),
                )
                self._request_seen = True
            return True

    def retire_stop(self) -> None:
        self._capability = ""
        if self._harness_stop is not None:
            self._harness_stop.close()
            self._harness_stop = None

    def environment(self) -> dict[str, str]:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(self.bridge_root / "src"), str(self.native_root), str(self.native_root / "envs"))
            ),
        }
        env.update(self.case.runtime_env)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["NV_API_KEY"] = self._capability
        return env

    async def start(self, *, model_base_url: str, model_id: str) -> None:
        if self.case._template is None:
            raise ValueError("Native case must be validated before launch")
        config = scrub_config(self.case._template)
        config["model"].update(id=model_id, base_url=model_base_url)
        config.update(episode_seconds=self.case.native_seconds, decision_limit=self.case.model_calls)
        actor = config["roles"]["actor"]
        actor["limits"] = {"seconds": self.case.native_seconds, "model_calls": self.case.model_calls}
        config["sdk"]["call_cap"] = self.case.model_calls
        config["sdk"].pop("code_root", None)
        if "kit_root" in config:
            config["kit_root"] = str(self.paths.root / "kit")
        self.paths.config.write_text(json.dumps(config, allow_nan=False) + "\n")
        self.paths.config.chmod(0o600)
        self._log = (self.paths.root / "launcher.log").open("wb")
        command = [str(self.case.python), "-m", "ember_bridge.cli"]
        run = [*command, "run", "--config", str(self.paths.config), "--output", str(self.paths.native)]
        if self.case.policy_path is not None:
            run.extend(("--policy", str(self.case.policy_path)))
        validate = [*command, "validate", "--config", str(self.paths.config)]
        driver = (
            "import json,pathlib,subprocess,sys; "
            "code=subprocess.call(sys.argv[2:sys.argv.index('--next')]); "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'returncode':code})); "
            "sys.exit(code) if code else None; "
            "sys.exit(subprocess.call(sys.argv[sys.argv.index('--next')+1:]))"
        )
        self._cli_command = tuple(run)
        self._driver_command = (
            str(self.case.python), "-c", driver, str(self.paths.root / "validation.json"),
            *validate, "--next", *run,
        )
        owner = native_module(self.native_root, "environment.processes")
        self._spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(self.case.python),
                str(self.native_root / "nanoverse/environment/supervise.py"),
                "--cleanup-receipt",
                str(self.paths.cleanup_receipt),
                "--cleanup-id",
                self.identity,
                "--",
                *self._driver_command,
                env=self.environment(),
                cwd=self.bridge_root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=self._log,
                stderr=self._log,
                preexec_fn=owner.parent_death_signal(signal.SIGTERM),
                start_new_session=True,
            )
        )
        try:
            self.process = await asyncio.shield(self._spawning)
        except asyncio.CancelledError:
            self.process = await asyncio.shield(self._spawning)
            raise

    async def finish(self) -> NativeEvidence:
        if self.process is None:
            raise NativeFailure("Native process did not start")
        await self.process.wait()
        if read_json(self.paths.root / "validation.json").get("returncode") != 0:
            raise NativeFailure("Native configuration validation failed; see launcher.log", terminal=True)
        return self.read_evidence()

    def read_evidence(self) -> NativeEvidence:
        root, contract = self.paths.native, self.case.evidence
        result = read_json(root / contract.result_path)
        actor = read_json(root / contract.actor_path)
        sdk, ttc = runtime_closed(root, contract, self.case.environment)
        if actor.get("role", "actor") != "actor":
            raise NativeFailure("Native evidence violates sole actor attribution", terminal=True)
        if self.case.environment == "exemplar":
            audit = read_json(root / contract.fixture_audit_path)
            if result.get("native", "missing") is not None or audit.get("ok") is not True:
                raise NativeFailure("Exemplar fixture audit failed")
            if (
                self.process is None
                or self.process.returncode != 0
                or actor.get("finished") is not True
                or result.get("harness_error")
            ):
                raise NativeFailure("Exemplar actor did not complete")
            if not isinstance(audit.get("executions"), int) or audit["executions"] < 1:
                raise NativeFailure("Exemplar fixture performed no audited SDK operations")
            if audit.get("native_actions", 0) < 1 or sdk.get("executions", 0) < 1:
                raise NativeFailure("Exemplar fixture performed no audited native controls")
            return NativeEvidence(result, None, actor, sdk, ttc, audit)
        outcome = native_module(self.native_root, "environment.outcome").read(root / contract.outcome_path)
        if outcome is None:
            raise NativeFailure("Native outcome is missing or corrupt")
        return NativeEvidence(result, outcome, actor, sdk, ttc)

    async def aclose(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self._closing = True
            if self._cleanup_deadline is None:
                self._cleanup_deadline = asyncio.get_running_loop().time() + float(self.case.cleanup_seconds)
            try:
                await self._close_native()
            finally:
                self.retire_stop()

    async def _close_native(self) -> None:
        try:
            async with asyncio.timeout_at(self._cleanup_deadline):
                if self._spawning is not None and self.process is None:
                    self.process = await asyncio.shield(self._spawning)
                if self.process is None:
                    self.closed = True
                    if self._log is not None:
                        self._log.close()
                    return
                remaining = self._cleanup_deadline - asyncio.get_running_loop().time()
                if remaining <= 0 and self.process.returncode is None:
                    raise TimeoutError
                if self.process.returncode is None and self._harness_stop is not None:
                    if self._harness_stop.request():
                        try:
                            reserve = min(5.0, float(self.case.cleanup_seconds) / 3)
                            async with asyncio.timeout(max(0.0, remaining - reserve)):
                                await self.process.wait()
                        except TimeoutError:
                            pass
                # The native supervisor must install its cleanup handler before fallback.
                while self.process.returncode is None and not self._fallback_sent:
                    if asyncio.get_running_loop().time() >= self._cleanup_deadline:
                        raise TimeoutError
                    try:
                        status = Path(f"/proc/{self.process.pid}/status").read_text()
                    except FileNotFoundError:
                        await asyncio.sleep(0.01)
                        continue
                    caught = next(line.split()[1] for line in status.splitlines() if line.startswith("SigCgt:"))
                    if int(caught, 16) & (1 << (signal.SIGTERM - 1)):
                        try:
                            self.process.send_signal(signal.SIGTERM)
                            self._fallback_sent = True
                        except ProcessLookupError:
                            pass
                        break
                    await asyncio.sleep(0.01)
                await self.process.wait()
        except TimeoutError:
            raise NativeFailure("Native cleanup unconfirmed; allocation quarantined") from None
        owner = native_module(self.native_root, "environment.processes")
        if not owner.cleanup_receipt_valid(self.paths.cleanup_receipt, self.identity):
            raise NativeFailure("Native cleanup receipt identity is missing or invalid; allocation quarantined")
        if self.paths.native.exists():
            runtime_closed(self.paths.native, self.case.evidence, self.case.environment)
        self.closed = True
        if self._log is not None:
            self._log.close()


def observe_actor(owner: EpisodeProcess, *, invocation_id: str, model: ModelServerRef) -> AgentObservationBundle:
    directory = owner.paths.native / "actor"
    gaps = [
        ObservationGap(code="assistant_message_identity_unavailable", invocation_id=invocation_id),
        ObservationGap(code="sandbox_resource_usage_unavailable", invocation_id=invocation_id),
        ObservationGap(code="compaction_association_unavailable", invocation_id=invocation_id),
    ]
    try:
        receipt = read_json(directory / "receipt.json")
    except (OSError, ValueError):
        receipt = {}
        gaps.append(ObservationGap(code="actor_receipt_unavailable", invocation_id=invocation_id))
    refs = {}
    try:
        for event in read_records(directory / "provider-responses.jsonl"):
            body = event.get("body")
            candidates = []
            if isinstance(body, str):
                try:
                    candidates = [json.loads(body)]
                except ValueError:
                    candidates = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: {")]
            for candidate in candidates:
                response = candidate.get("response", candidate)
                response_id = response.get("id")
                if isinstance(response_id, str) and response_id:
                    refs[response_id] = ModelCallRef(response_id=response_id, model_ref=model)
    except (OSError, ValueError, AttributeError):
        gaps.append(ObservationGap(code="provider_response_identity_unavailable", invocation_id=invocation_id))
    if not refs:
        gaps.append(ObservationGap(code="model_call_reference_unavailable", invocation_id=invocation_id))
    seconds = receipt.get("seconds")
    invocation = AgentInvocation(
        invocation_id=invocation_id,
        status="completed" if receipt.get("finished") is True else "failed" if receipt.get("error") else "unknown",
        model_calls=list(refs.values()),
        duration_ms=seconds * 1000
        if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0
        else None,
    )
    tools = {}
    try:
        for event in read_records(directory / "timeline.jsonl"):
            call_id = event.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if event.get("type") == "tool_execution_start":
                tools[call_id] = ToolCallObservation(
                    invocation_id=invocation_id,
                    tool_call_id=call_id,
                    tool_name=event.get("tool"),
                    started_at=event["time"] / 1000,
                    timing_source="harness",
                    status="incomplete",
                )
            elif event.get("type") == "tool_execution_end" and call_id in tools:
                tool = tools[call_id]
                tool.completed_at = event["time"] / 1000
                tool.duration_ms = max(0, (tool.completed_at - tool.started_at) * 1000)
                tool.status = "failed" if event.get("is_error") else "completed"
    except (OSError, ValueError, TypeError, KeyError):
        gaps.append(ObservationGap(code="tool_timing_unavailable", invocation_id=invocation_id))
    if not tools:
        gaps.append(ObservationGap(code="tool_timing_unavailable", invocation_id=invocation_id))
    return AgentObservationBundle(source="nanoverse_sdk.native", records=[invocation, *tools.values()], gaps=gaps)
