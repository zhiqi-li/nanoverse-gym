# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""One Gym episode delegates to one native SDK Harness and its existing grader."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body
from nemo_gym.base_environment_server import BaseEnvironmentServer, BaseEnvironmentServerConfig, CleanupContext
from nemo_gym.config_types import AggregateMetrics, AggregateMetricsRequest, ModelServerRef
from nemo_gym.episode_types import BaseEpisodeRequest, BaseEpisodeResponse, EpisodeFailure
from nemo_gym.reward_profile import compute_aggregate_metrics
from nemo_gym.rollout_observability import AgentObservationBundle
from nemo_gym.server_utils import apply_rollout_prefix, get_server_url, is_nemo_gym_fastapi_entrypoint
from pydantic import BaseModel, ConfigDict, Field, JsonValue, PrivateAttr, model_validator

from environment_servers.nanoverse_sdk.native import (
    BRIDGE_ROOT,
    MODEL_ID,
    NATIVE_ROOT,
    CaseRegistry,
    EpisodePaths,
    EpisodeProcess,
    NativeEvidence,
    NativeFailure,
    native_module,
    observe_actor,
    runtime_closed,
)


class NativeSDKTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str = Field(min_length=1)


class NativeSDKRequest(BaseEpisodeRequest[NativeSDKTaskInput]):
    pass


class NativeScoredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["native"] = "native"
    environment: str
    outcome_class: Literal["scored"] = "scored"
    reward: float = Field(allow_inf_nan=False)
    verification: dict[str, JsonValue]
    native_attempt: bool
    evidence_id: str
    ng_agent_observations: AgentObservationBundle


class NativePolicyLimitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["policy_limit"] = "policy_limit"
    environment: str
    outcome_class: Literal["policy_failure.decision_budget", "policy_failure.deadline", "policy_failure.context"]
    reward: Literal[0.0] = 0.0
    native_attempt: Literal[False] = False
    verification: dict[str, JsonValue] | None
    evidence_id: str
    ng_agent_observations: AgentObservationBundle


class FixtureResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["fixture"] = "fixture"
    environment: Literal["exemplar"] = "exemplar"
    native: None = None
    fixture_passed: bool
    scope: str
    evidence_id: str
    ng_agent_observations: AgentObservationBundle


NativeResult = Annotated[NativeScoredResult | NativePolicyLimitResult | FixtureResult, Field(discriminator="kind")]


class NativeSDKFailure(EpisodeFailure):
    agent_observations: AgentObservationBundle | None = None
    evidence_id: str | None = None


class NativeSDKResponse(BaseEpisodeResponse[NativeResult]):
    failure: NativeSDKFailure | None = None


class NativeSDKConfig(BaseEnvironmentServerConfig):
    default_episode_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    registry_path: Path
    output_root: Path
    model_server: ModelServerRef
    model_id: Literal[MODEL_ID] = MODEL_ID
    token_capture: bool = False
    native_root: Path = NATIVE_ROOT
    bridge_root: Path = BRIDGE_ROOT

    @model_validator(mode="after")
    def local_owner(self):
        if (self.num_workers or 1) != 1:
            raise ValueError("Native runtime ownership requires num_workers=1")
        if not all(path.is_absolute() for path in (self.output_root, self.native_root, self.bridge_root)):
            raise ValueError("Native source and output roots must be absolute paths")
        return self


def project_native_result(
    evidence: NativeEvidence,
    *,
    owner: EpisodeProcess,
    observations: AgentObservationBundle,
) -> NativeResult:
    common = dict(
        environment=owner.case.environment, evidence_id=owner.paths.root.name, ng_agent_observations=observations
    )
    if owner.case.environment == "exemplar":
        return FixtureResult(**common, fixture_passed=True, scope=evidence.result["scope"])
    outcome = evidence.outcome
    native = native_module(owner.native_root, "environment.outcome")
    grading = native_module(owner.native_root, "sdk.grading")
    verification = native.verification(outcome)
    outcome_class = outcome["class"]
    attempt = grading.native_attempt(outcome_class, verification)
    if outcome_class == "scored":
        if outcome.get("readback_verified") is not True or verification is None:
            raise NativeFailure("Native grade has no verified readback")
        return NativeScoredResult(
            **common, reward=verification["env_reward"], verification=verification, native_attempt=attempt
        )
    if outcome_class in ("policy_failure.decision_budget", "policy_failure.deadline", "policy_failure.context"):
        return NativePolicyLimitResult(**common, outcome_class=outcome_class, verification=verification)
    raise NativeFailure(f"Native outcome is {outcome_class}", terminal=outcome_class in ("provider_blocked", "invalid"))


class NativeSDKEnvironmentServer(BaseEnvironmentServer[NativeSDKRequest, NativeSDKResponse]):
    config: NativeSDKConfig
    request_model = NativeSDKRequest
    response_model = NativeSDKResponse
    checkpoint_boundaries = False
    _registry: CaseRegistry = PrivateAttr()
    _allocations: dict[str, EpisodeProcess] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context, /):
        super().model_post_init(context)
        self._registry = CaseRegistry.load(self.config.registry_path)
        self._registry.validate_deployment(self.config.native_root, self.config.bridge_root)
        self.config.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def native_request(self, capture_key: str, bearer: str) -> None:
        evidence_id = hashlib.sha256(capture_key.encode()).hexdigest()[:16]
        owners = [owner for owner in self._allocations.values() if owner.paths.root.name == evidence_id]
        if len(owners) == 1:
            await owners[0].native_request(bearer)

    async def run(self, request: NativeSDKRequest, cleanup: CleanupContext) -> NativeSDKResponse:
        owner = None
        allocation_claim = None
        try:
            case = self._registry.resolve(request.task.task_input.case_id, request.task.task_id)
            key = request.episode_id.capture_key
            path = self.config.output_root / hashlib.sha256(key.encode()).hexdigest()[:16]
            if path.exists():
                raise NativeFailure("Physical attempt identity already exists; use a new attempt", terminal=True)
            previous = self._allocations.get(case.allocation)
            if previous is not None:
                if not previous.closed and (previous.process is None or previous.process.returncode is None):
                    raise NativeFailure("Native allocation is active or quarantined")
                try:
                    await previous.aclose()
                except Exception:
                    raise NativeFailure(
                        "Native allocation cleanup is unconfirmed; runtime reuse is quarantined"
                    ) from None
                del self._allocations[case.allocation]
            claim = self.config.output_root / (
                "allocation-" + hashlib.sha256(case.allocation.encode()).hexdigest()[:16] + ".json"
            )
            if claim.exists():
                saved = json.loads(claim.read_text())
                processes = native_module(self.config.native_root, "environment.processes")
                if not processes.cleanup_receipt_valid(saved["receipt"], saved["identity"]):
                    raise NativeFailure("Native allocation cleanup is unconfirmed; runtime reuse is quarantined")
                if Path(saved["native"]).exists():
                    runtime_closed(Path(saved["native"]), case.evidence, case.environment)
                claim.unlink()
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                raise NativeFailure(
                    "Physical attempt identity already exists; use a new attempt", terminal=True
                ) from None
            (path / "identity.json").write_text(json.dumps(request.model_dump(mode="json")) + "\n")
            owner = EpisodeProcess(case, EpisodePaths(path), self.config.native_root, self.config.bridge_root)
            try:
                with claim.open("x") as stream:
                    json.dump(
                        {
                            "identity": owner.identity,
                            "receipt": str(owner.paths.cleanup_receipt),
                            "native": str(owner.paths.native),
                        },
                        stream,
                    )
            except FileExistsError:
                owner = None
                raise NativeFailure("Native allocation is already claimed") from None
            allocation_claim = claim
            self._allocations[case.allocation] = owner
            handle = cleanup.register_cleanup("native-sdk-owner", owner.aclose)
            model_url = (
                apply_rollout_prefix(
                    get_server_url(self.config.model_server.name),
                    key,
                    token_capture=self.config.token_capture,
                )
                + "/v1"
            )
            await owner.start(model_base_url=model_url, model_id=self.config.model_id)
            evidence = await owner.finish()
            await handle.close()
            allocation_claim.unlink()
            allocation_claim = None
            observations = observe_actor(owner, invocation_id=key, model=self.config.model_server)
            result = project_native_result(evidence, owner=owner, observations=observations)
            return NativeSDKResponse(episode_id=request.episode_id, task_id=request.task.task_id, result=result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            terminal = isinstance(error, (ValueError, KeyError)) or isinstance(error, NativeFailure) and error.terminal
            reason = (
                str(error)
                if isinstance(error, (NativeFailure, ValueError))
                else f"Native evidence unavailable ({type(error).__name__})"
            )
            if owner is not None:
                try:
                    await owner.aclose()
                    if allocation_claim is not None:
                        allocation_claim.unlink(missing_ok=True)
                except Exception:
                    terminal = False
                    reason = "Native cleanup unconfirmed; allocation quarantined"
            observations = (
                observe_actor(owner, invocation_id=request.episode_id.capture_key, model=self.config.model_server)
                if owner
                else None
            )
            return NativeSDKResponse(
                episode_id=request.episode_id,
                task_id=request.task.task_id,
                failure=NativeSDKFailure(
                    failure_reason=reason[:2000],
                    terminal=terminal,
                    agent_observations=observations,
                    evidence_id=owner.paths.root.name if owner else None,
                ),
            )

    async def aggregate_metrics(self, body: AggregateMetricsRequest = Body()) -> AggregateMetrics:
        fixtures = [row for row in body.verify_responses if row.get("kind") == "fixture"]
        native = [row for row in body.verify_responses if row.get("kind") != "fixture"]
        metrics = compute_aggregate_metrics(native)
        extra = {
            "sdk_fixture_count": len(fixtures),
            "sdk_fixture_pass_count": sum(row.get("fixture_passed") is True for row in fixtures),
            "sdk_native_count": len(native),
        }
        metrics.agent_metrics.update(extra)
        metrics.key_metrics.update(extra)
        return metrics


if __name__ == "__main__":
    NativeSDKEnvironmentServer.run_webserver()
elif is_nemo_gym_fastapi_entrypoint(__file__):
    app = NativeSDKEnvironmentServer.run_webserver()
