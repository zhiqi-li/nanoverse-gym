# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Materialize trusted SDK case IDs for Gym's standard rollout collector."""

import argparse
import json
from pathlib import Path

from nemo_gym.episode_types import TaskId
from omegaconf import OmegaConf

from environment_servers.nanoverse_sdk.native import CaseRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--server", default="nanoverse_sdk")
    args = parser.parse_args()
    if not args.run_id or ":" in args.run_id or any(c.isspace() for c in args.run_id):
        parser.error("run-id must be nonempty and contain no colon or whitespace")
    registry = CaseRegistry.load(args.registry)
    rows, routes = [], {}
    for case in registry.cases:
        task = TaskId(taskset=f"{case.environment}:{args.run_id}", task_id=case.case_id)
        rows.append({"task_id": task.model_dump(), "task_input": {"case_id": case.case_id}})
        routes[task.taskset] = args.server
    for path in (args.output, args.routes):
        if path.exists():
            parser.error(f"output already exists: {path}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.routes.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        for row in rows:
            file.write(json.dumps(row) + "\n")
    with args.routes.open("x") as file:
        file.write(OmegaConf.to_yaml({"environment_routing_mode": "taskset", "environment_server_routes": routes}))
    print(json.dumps({"tasks": len(rows), "tasksets": len(routes)}))


if __name__ == "__main__":
    main()
