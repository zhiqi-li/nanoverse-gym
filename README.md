# Nanoverse SDK for NeMo Gym

Run a Nanoverse SDK environment through NeMo Gym. Nanoverse owns simulation and native grading; model-generated SDK programs run through TTC.

RoboDojo's default case is qualified on a real Linux x86-64 L40 Docker host: public-source installation, GPT-6.1 Sol reward **1.0**, eight model requests, nine SDK executions, two trajectories, and normal and SIGTERM cleanup. The other eight SDK entries remain gated. See the [qualification record](docs/qualification/robodojo-l40-20261023.json) for the exact scope.

Download the [v0.1.1 source release](https://github.com/zhiqi-li/nanoverse-gym/releases/tag/v0.1.1). Use a Linux x86-64 GPU host with working Docker GPU support, NVIDIA Container Toolkit, a compatible RTX driver, 12 available CPU cores and 64 GiB of available memory. We recommend at least 250 GiB free for Docker storage and preparation caches; this is a capacity recommendation, not a tested minimum. Qualification used one L40 and a 1.92 TB disk. No cluster client or access to our private storage or registry is required.

Create `model.env` from `model.env.example` using your own API endpoint and key. The qualified provider supports the Responses API, images, tool calls, and the model identifier `openai/openai/gpt-6.1-sol`.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

The output directory must be new and its parent must exist. First use downloads the public common component, installs the pinned simulator and assets from official sources, validates the native runtime, saves a local image, then runs evaluation. Cold preparation is substantial; the tested native software inventory was about 64 GB, and saving its Docker image alone took about 35 minutes. Later runs reuse the prepared image.

| Component | Delivery |
| --- | --- |
| Gym, SDK/bridge code, TTC, Python, Node and Codex | Public common package with file identities and included license files |
| CUDA and system dependencies | Built on the pinned public NVIDIA CUDA base |
| Isaac Sim/Kit, RoboDojo and assets | Installed on the user's host from official sources inside preparation containers |
| Model credentials | User supplied; mounted read-only only for evaluation |

Isaac Sim and Kit are not redistributed in our common package or a public native image. `--accept-isaac-terms` accepts the terms for their official installation. Containers use the host Docker daemon through the launcher; no Docker daemon or socket runs inside them.

You can supply a complete predownloaded RoboDojo `Assets` tree with `--assets-cache ./robodojo-data`, or the common archive with `--common-package ./common-runtime-v1.tar.gz`. See [portable runtime details](docs/portable.md) for pins, cache verification and the qualification limits of these optional paths.

Results include `summary.json`, Gym metrics, model-call captures, SDK executions, native trajectories, and cleanup receipts. Owned run containers are removed after normal exit or interruption. Prepared images, download caches and user results are retained for reuse and inspection. Failed or interrupted runs do not become scored successes.

TTC uses `outer_task` with `unsafe_no_host_isolation=true`. Use a dedicated host or trusted single-tenant execution: this configuration does not isolate mutually untrusted tenants.

`./nvgym list` lists exactly nine SDK environments. The NeMo Gym adapters are under `environment_servers/nanoverse_sdk` and `responses_api_models/nanoverse_sdk_openai`; Gym configuration is under `environments/nanoverse_sdk`.
