# Portable RoboDojo evaluation

The qualified path runs RoboDojo's `general_pickup` case through NeMo Gym, using GPT-6.1 Sol and the SDK in TTC. It runs on the user's Docker GPU host and downloads its simulator and assets from official sources. It needs no OSMO, Colossus, private registry or shared filesystem access.

## Prepare the host

Use Linux x86-64, a supported RTX GPU, working Docker GPU support and NVIDIA Container Toolkit. The launcher limits each preparation/evaluation container to one selected GPU, 12 CPUs, 64 GiB RAM, 4,096 processes and 2 GiB shared memory. Allow that capacity on the host.

The installer requires 150 GiB free in its installation filesystem. Docker layers, temporary downloads, asset checkout and the saved native image require additional space. We recommend at least 250 GiB free across Docker storage and preparation caches; the minimum total capacity has not been qualified. The test host had 128 GB RAM and a 1.92 TB NVMe disk.

Qualification used Ubuntu 22.04.5, kernel 5.15.0-177-generic, Docker 29.1.3, NVIDIA Container Toolkit 1.20.1-1, driver 580.178.04 and a Production L40 with 46,068 MiB reported memory. That host required `NVreg_EnableGpuFirmware=0` before its GPU became usable. This describes the tested configuration; it is not an automatic driver change made by the launcher or a requirement established for all hosts. See NVIDIA's [Container Toolkit installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) and [Isaac Sim requirements](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/requirements.html) when preparing a host.

ARM/GB300 and other GPU architectures have not been qualified.

## Run a case

Download and extract the [v0.1.1 source release](https://github.com/zhiqi-li/nanoverse-gym/releases/tag/v0.1.1), then work from its root. Copy `model.env.example` to `model.env` and fill in your own values:

```dotenv
NV_BASE_URL=https://your-provider.example/v1
NV_API_KEY_2=replace-with-your-own-key
```

`NV_API_KEY_2` is the runtime's variable name; the key belongs to your provider account. The qualified model profile requests `openai/openai/gpt-6.1-sol`. Its endpoint must support the Responses API, image input and tool calls. Check that the key has sufficient inference budget. Other model profiles are not qualified by this release.

Keep credentials outside packaged source directories and asset caches. Restrict access to `model.env`; the launcher does not delete your supplied credential file.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

The output directory must not exist; its parent must exist. `--gpu 0` selects a host GPU index and is the default. `--cache /path/to/cache` selects the preparation cache; its default is `${XDG_CACHE_HOME:-$HOME/.cache}/nanoverse-gym`.

Preparation downloads and checks the common package, builds the common image, installs the pinned native software and complete assets, runs a native admission probe, then saves a local prepared image. The probe checks reset, RGB-D/calibration, read-only observation, two hold controls and recording; it does not claim task success. Evaluation starts in a separate container and mounts your credentials read-only. The native payload, SDK and release identities are checked before and after evaluation.

First use can take a long time. The tested native inventory was 64,415,024,769 bytes across 218,595 files, including 41,269,513,111 bytes of verified assets. Saving the prepared Docker image took about 35 minutes on the acceptance host. Later invocations reuse the prepared image when its common-image provenance matches.

`--accept-isaac-terms` is explicit acceptance for the official installation of Isaac Sim/Kit. Our common package contains neither Isaac Sim/Kit nor the RoboDojo native installation or assets. We do not publish the prepared native image. Consult the upstream licenses before redistributing any locally prepared image.

## Supply downloads yourself

To reuse the public common component:

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --common-package ./common-runtime-v1.tar.gz \
    --credentials ./model.env --output ./run-002
```

Download it from the [common component release](https://github.com/zhiqi-li/nanoverse-gym/releases/tag/v0.1.0). The archive is checked against SHA256 `fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041`.

To supply RoboDojo assets, use the full `Assets` tree at Hugging Face revision `43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab`. The directory passed below must contain `Assets/`, with materialized file contents rather than Git LFS pointer files:

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --assets-cache ./robodojo-data \
    --credentials ./model.env --output ./run-003
```

Preparation mounts this input read-only, verifies every file against pinned Git/LFS records and copies verified bytes into the installation. Keep model credentials outside it. The asset transport and full-file verification were exercised; this optional cache mode has not had a separate complete Docker-host model evaluation. The qualified Docker-host run used the default public download path with no supplied asset cache.

The legacy `--data` runtime override and maintainer `--image` path are outside the public default-path qualification.

## Inspect results and cleanup

The output contains:

| Artifact | Purpose |
| --- | --- |
| `summary.json` | Runtime status, reward when scored, cleanup confirmation and initial/final identities |
| `episode/rollouts.jsonl` and aggregate metrics | NeMo Gym rollouts and grading |
| Files under `episode/` | Model requests/responses, SDK executions, trajectories and native simulator records |
| `image-id`, `container-id`, `container-name` | Local execution identity |
| `docker-cleanup.json` | Whether the launcher removed its owned container |

Results may contain images, prompts and task data; treat them as your evaluation data. They do not need to be uploaded to us.

Normal exit and SIGTERM remove the owned run container after shutting down the simulator, SDK/TTC and watchdog. SIGTERM to the live host launcher returned 143 and completed cleanup in about 80 seconds in qualification. Interrupting while a model request is outstanding does not produce a reward. SIGKILL, host power loss and Docker daemon failure were not qualified as graceful shutdown paths.

Images, download caches and result directories remain for reuse and review. The launcher does not globally prune Docker or delete your supplied data or credentials. Docker logs rotate at 10 MB with two files; the last preparation log is capped at 10 MB in the cache. Native/Gym evidence remains in the selected output directory.

A failed run needs a new output directory for a retry. For example, if a provider rejects the key for exhausted budget, replace the key in your credential file and repeat with `--output ./run-004`. Completed preparation is reused; a provider error is recorded as failure rather than reward zero or success.

## Qualified scope

The [2026-10-23 qualification record](qualification/robodojo-l40-20261023.json) covers one default case, `general_pickup`, seed 0, layout 0, on an actual Colossus baremetal L40 Docker host. Colossus supplied the ordinary GPU host; the product has no Colossus dependency.

Cold preparation started with an empty product cache, downloaded official sources and passed native admission without a private seed, a private image, supplied data or model credentials in the preparation container. The first model attempt was rejected because its key had exhausted budget. A later invocation with a valid key reused the exact prepared image and scored 1.0: eight real model requests (all HTTP 200), nine SDK executions, two trajectories and 65 native control steps. This establishes cold preparation and successful cached evaluation separately; it does not claim the initial cold invocation scored.

A separate SIGTERM test interrupted the host launcher with TTC live and a bridge-local upstream request held open. It made zero external model requests, produced no reward and confirmed native/TTC/container cleanup. Both cases preserved software identities and left their results readable by the invoking host user. These checks cover the default case and lifecycle; they are not a benchmark-wide success-rate estimate.

The catalog contains exactly `behavior1k`, `exemplar`, `libero`, `molmospaces`, `robocasa365`, `robodojo`, `roboforge`, `robolab` and `robotwin`. Only RoboDojo supports qualified public `--prepare` in this release. The other eight entries remain gated pending independent installation and end-to-end qualification.

TTC uses the existing `outer_task` mode with `unsafe_no_host_isolation=true`. The host Docker container provides the outer execution boundary; this configuration does not isolate mutually untrusted tenants. Use dedicated or trusted single-tenant hosts.

## Fixed inputs and NeMo Gym integration

| Input | Pin |
| --- | --- |
| Tested runtime source | `fb6ede5b852bd229c3457b23dfd1adfc57a91c34` (`v0.1.1-rc.2`); v0.1.1 changes only documentation and qualification metadata |
| Public CUDA base | `nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:61f6c08f2b59036cb935e56d1e31a6b64e3ae2c7ddb86d33fa0b044c7917b719` |
| Common package | `common-runtime-v1.tar.gz`, SHA256 `fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041` |
| SDK/bridge pair | `sha256:d8836d1569a505d4635ee367159c2621930e3f2b55335e73bc9a1b777a5f09a0` |
| NeMo Gym | `69c2ff3ebd6e7b6c2106f5f8cb1ae5c0f912b8bb` |
| RoboDojo source | `726e9aabfaa642203722eb126f5eaf0f37f3e1ad` |
| RoboDojo assets | `43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab` |

`source-manifest.json` records source file hashes. Runtime provenance and installed-software inventory hashes are written into each run's summary. The common package includes Gym/SDK/bridge code, TTC and guest programs, Python/Node/Codex runtimes and included license files; native simulator components are installed separately from official sources.

This is a standalone integration repository. It does not claim inclusion in NVIDIA-NeMo/Gym upstream. `environment_servers/nanoverse_sdk` implements Gym's environment-server contract and delegates simulation and grading to Nanoverse. `responses_api_models/nanoverse_sdk_openai` handles the compatible model endpoint; `environments/nanoverse_sdk` contains the Gym configuration. The accepted result has one native attempt and zero SDK fixture attempts.
