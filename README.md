# Nanoverse SDK for NeMo Gym

Run nine Nanoverse SDK environments through NeMo Gym. Nanoverse owns simulation and native grading; the selected model writes SDK programs that execute through TTC. The [nine-case qualification record](docs/qualification/sdk-nine-l40-20261024.json) preserves the original GPT-6.1 Sol baseline, including its Gym source identity and normal/SIGTERM evidence. Preparation or an import check alone is not qualification.

The runtime now installs the published **NeMo Gym 0.7.0** package with Python 3.13.14. Admission checks the installed package version and import location; it does not require a Gym Git commit. Native SDK and simulator identities remain recorded for reproducibility.

The [Gym 0.7.0, model and isolation results](docs/qualification/gym-0.7.0-model-isolation-20261024.json) separate completed native grades, model budget failures and lifecycle checks. This upgrade reran native RoboDojo and LIBERO plus the Exemplar CPU fixture; the historical nine-environment baseline remains unchanged.

Use a Linux x86-64 NVIDIA GPU host with working Docker GPU support and NVIDIA Container Toolkit. Qualification uses Production L40 GPUs, one selected GPU per container, 12 CPU cores and a 64 GiB memory limit. Allow space for simulator downloads, assets, Docker layers and retained results; the tested host has 256 GB RAM and a 2.9 TB disk. Other GPU architectures and a minimum total disk capacity have not been qualified.

Download the [source release](https://github.com/zhiqi-li/nanoverse-gym/releases/latest), extract it, and copy `model.env.example` to `model.env`. Supply your own endpoint and key, supporting images and tool calls. The default model is `openai/openai/gpt-6.1-sol` through Responses; `--model-profile nemotron-3.5-super-vl` selects the exact Chat Completions model ID `nvidia/nvidia/nemotron-3.5-super-vl`. Gym's official converter handles its Responses interface and streaming. No host Python, cluster client, private registry or access to our filesystem is required.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

Choose an environment below. Each command uses `--prepare --credentials ./model.env --output NEW_DIRECTORY`; add the flags shown in the last column. The output parent must exist. First use downloads and installs software inside preparation containers, checks its identity, saves a local image and runs evaluation. Later runs reuse that prepared image. Cold preparation can take tens of minutes; saving a large Docker image is part of that time.

| Environment | Evaluation scope | Additional flags / data |
| --- | --- | --- |
| `behavior1k` | Native `turning_on_radio` | `--accept-isaac-terms --data ./behavior-data`; obtain the licensed official data and key yourself |
| `exemplar` | CPU SDK conformance fixture; no native benchmark score | None |
| `libero` | Native `libero_goal/put_the_bowl_on_the_plate` | None; official assets prepared automatically |
| `molmospaces` | Native `close_v1-890`, classic renderer | Qualified data path: `--assets-cache ./molmo-data`; see [data requirements](docs/portable.md#molmospaces-data) |
| `robocasa365` | Native `OpenDrawer`; 64 actor calls, 750-control horizon | None; pinned official Box assets prepared automatically |
| `robodojo` | Native `general_pickup` task | `--accept-isaac-terms` |
| `roboforge` | Native authored `align_blocks` task | `--accept-isaac-terms`; the pinned task package is included |
| `robolab` | Native `BlocksInBinTask` | `--accept-isaac-terms` |
| `robotwin` | Native `stack_blocks_two` task | None; its pinned CUDA 12.1 base is selected automatically |

A completed native episode can receive a verified score of zero. Model budget/context limits instead produce a policy-limit penalty of zero; that does not establish completed native grading. The report separates these outcomes and task success. Exemplar remains a fixture and is never counted as a native benchmark attempt. Results cover one accepted default case per environment, not benchmark-wide success rates.

| Component | Delivery |
| --- | --- |
| Gym, SDK/bridge, TTC and language runtimes | Public common package with file identities and included license files |
| CUDA, native simulator code and dependencies | Pinned public sources installed on the user's host inside Docker |
| Public simulation assets | Official downloads, or a supported user-supplied data cache |
| BEHAVIOR data and `omnigibson.key` | User supplied, mounted read-only for evaluation; absent from software images |
| Model credentials | User supplied, mounted read-only only for evaluation |

Isaac Sim/Kit and prepared native images are not redistributed by this project. `--accept-isaac-terms` accepts the terms for their official installation. Docker runs on the host; containers contain no nested Docker daemon or host Docker socket.

Results include `summary.json`, NeMo Gym metrics, model-call captures, SDK executions, native trajectories and cleanup receipts. Normal exit and live SIGTERM shut down native/TTC processes and remove the owned run container. Prepared images, download caches, user data and results remain available for reuse. Provider errors, unknown native outcomes and failed software checks do not become scored successes.

Each evaluation receives its own Docker bridge network and removes it during cleanup. The default `--isolation trusted` retains TTC's `outer_task` mode with `unsafe_no_host_isolation=true` and requires a trusted host. Optional `--isolation strict` uses rootless TTC, the supplied seccomp/AppArmor policies and an 8 GiB temporary filesystem for guest storage. It requires host user-namespace support and one-time AppArmor policy installation on AppArmor hosts; see [isolation setup and limits](docs/portable.md#isolation). A strict request fails rather than switching to a permissive policy.

See [installation, data layouts and qualification limits](docs/portable.md). The environment adapter lives in `environment_servers/nanoverse_sdk`; model adapters are under `responses_api_models`. The configuration schema reference is under `environments/nanoverse_sdk`. The embedded `scripts.sdk_smoke` driver binds live episode capabilities and runs Gym's standard collector. Independently launched model adapters reject requests without that binding. This is a standalone integration repository, not a claim of inclusion in NVIDIA-NeMo/Gym upstream.
