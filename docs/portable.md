# Portable SDK evaluation

The launcher prepares a selected environment on a Docker GPU host, then runs the actual NeMo Gym collector, model endpoint, TTC SDK and native grader. It requires no OSMO, Colossus, shared filesystem or private software seed. The [per-environment qualification record](qualification/sdk-nine-l40-20261024.json) is the authority for tested cases, data modes and outcomes. Its pending entries are not qualified.

## Host and model requirements

Use Linux x86-64 with working Docker GPU support and NVIDIA Container Toolkit. Tested native cases use a Production L40. Each preparation/evaluation container receives one selected GPU, 12 CPUs, 64 GiB RAM, 4,096 processes and 2 GiB shared memory. Exemplar's environment logic is a CPU fixture; it uses the same host launcher.

The acceptance host runs Ubuntu 22.04, kernel 5.15.0-177-generic, Docker 29.9.2 and driver 580.178.04, with 256 GB RAM and a 2.9 TB NVMe filesystem. It has two L40 GPUs; each case selects one. That host uses `NVreg_EnableGpuFirmware=0`. This is its tested configuration, not a driver change made by this launcher or a universal prerequisite. Consult the official [Container Toolkit guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) and [Isaac Sim requirements](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/requirements.html) for your host.

The largest tested native software inventories are about 64 GB. Installer staging, saved/unpacked Docker layers, user datasets and results need additional capacity. The RoboDojo installer requires 150 GiB free in its installation filesystem. Allow at least 250 GiB for one environment's preparation and Docker storage, plus user-supplied datasets; this is capacity guidance, not a qualified minimum. Retaining several environments consumes more space. ARM/GB300 and other GPU architectures have not been qualified.

Create `model.env` using your own provider:

```dotenv
NV_BASE_URL=https://your-provider.example/v1
NV_API_KEY_2=replace-with-your-own-key
```

`NV_API_KEY_2` is the runtime's credential variable name. The endpoint must support image input and tool calls. Keep credentials outside `releases/`, application package directories, datasets and asset caches, and restrict access to the file. The top-level `model.env` shown here is excluded from source packaging. Preparation does not mount credentials; evaluation mounts them read-only and preserves the file.

| Model profile | Requested model ID | Upstream API |
| --- | --- | --- |
| `gpt-6.1-sol` (default) | `openai/openai/gpt-6.1-sol` | Responses |
| `nemotron-3.5-super-vl` | `nvidia/nvidia/nemotron-3.5-super-vl` | Chat Completions through Gym's official converter |
| `nemotron-3.5-super-vl-nim` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | Optional official hosted identity; not covered by the gateway model's results |

For example, add `--model-profile nemotron-3.5-super-vl` to an evaluation command. The gateway and official hosted identities are separate profiles; the launcher does not silently rewrite one to the other. Model API and conversion checks establish transport behavior, not native task success.

For the Chat Completions profiles, the adapter removes the Responses-only encrypted reasoning state request and reasoning-summary display hint before Gym converts the request. It preserves reasoning effort, instructions, images and tool arguments. Responses-native profiles retain those hints. A successful HTTP status alone is insufficient: a streamed `response.failed` is a failed model call.

## Run an environment

Download the [source release](https://github.com/zhiqi-li/nanoverse-gym/releases/latest) and work from its extracted root. For a public-data environment:

```sh
./nvgym run libero --prepare --credentials ./model.env --output ./run-001
```

For an Isaac-based environment, explicitly accept the terms:

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

The same form supports `roboforge` and `robolab`. BEHAVIOR additionally requires its data root; MolmoSpaces' qualified path uses a data cache, as described below. `./nvgym list` lists exactly nine SDK environments.

The output directory must be new and its parent must exist. `--gpu 0` selects the host GPU index and is the default. `--cache /path/to/cache` selects preparation storage; its default is `${XDG_CACHE_HOME:-$HOME/.cache}/nanoverse-gym`.

Preparation verifies the common archive, builds the public base, installs official native sources and dependencies, prepares assets and validates software admission. Evaluation starts in a separate container. Original native software, SDK, host bridge and release identities are checked before and after it. Normal execution and interruption must preserve them.

First use is substantial: downloads, installation, native probes and saving a large Docker image can take tens of minutes. A prepared image is reused when its common-image provenance matches. MolmoSpaces additionally binds its supplied cache identity into the prepared-image cache key. A native installation/reset probe establishes readiness; the separate real-model run establishes scoring and trajectories.

## Packaging and official software

The public common package contains SDK/bridge code, TTC and guest programs, Python/Node/Codex runtimes and included license files. Preparation installs `nemo-gym==0.7.0` from PyPI in a separate Python 3.13.14 evaluator environment. The evaluator checks both distribution version and module location, so the legacy Gym tree in the original common archive cannot shadow the installed release. CUDA comes from a pinned public NVIDIA base. Native code and dependencies are installed from public GitHub/PyPI/NVIDIA sources. RoboCasa365 uses the official public Box asset packs with the accepted full-file checksums; no S3 credentials are needed by the product.

Isaac Sim/Kit, licensed BEHAVIOR data and prepared native images are absent from the common archive. Isaac components are obtained from official sources after `--accept-isaac-terms`. The prepared image stays local. Consult upstream licenses before redistributing it.

BEHAVIOR uses an explicitly recorded SDK-only dependency profile: upstream remote-policy and dataset-conversion tools are optional; the local policy implementation and physics source are unchanged. The profile aligns dependencies with the pinned Isaac runtime and records patched source identities. RoboLab uses Kit's portable-root configuration so runtime caches/logs stay outside immutable software. BEHAVIOR and LIBERO route Numba's compilation cache outside immutable software. These adaptations are part of the installed provenance; they are not ignored inventory differences.

To supply the public common archive yourself, add `--common-package ./common-runtime-v1.tar.gz`. Download it from the [common component release](https://github.com/zhiqi-li/nanoverse-gym/releases/tag/v0.1.0); its SHA256 is `fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041`.

## BEHAVIOR data

Obtain and unpack the data under the applicable upstream terms using the [official installation/data guide](https://behavior.stanford.edu/getting_started/installation.html). The accepted version/layout is:

```text
behavior-data/
  behavior-1k-assets/VERSION                 # 3.9.0
  omnigibson-robot-assets/VERSION             # 3.8.2
  2026-challenge-task-instances/metadata/
    task.jsonl
    available_tasks.yaml
    B100_task_misc.csv
  omnigibson.key
```

The directories and identity files must resolve inside this data root. Preserve access to the data and restrict the key, for example with `chmod 600 behavior-data/omnigibson.key`. Then run:

```sh
./nvgym run behavior1k --prepare --accept-isaac-terms \
    --data ./behavior-data --credentials ./model.env --output ./behavior-run
```

Software preparation needs no dataset or key. Evaluation mounts the data read-only at `/data` and writes app data under the output. Admission validates the two versions and the five non-secret identity files; it checks that the key is readable without hashing its contents. Native task fingerprints and readback are still checked during evaluation. The project does not download a license key, accept the data terms for you or put the data/key into a software image.

## MolmoSpaces data

The qualified scope is `close_v1-890` with the classic MuJoCo renderer. Filament and other task families are not covered by this release's case qualification. Software comes from the pinned public [MolmoSpaces source](https://github.com/allenai/molmospaces/tree/735138876887eb64650742631b8dda841dd264bf) and version-pinned public packages.

The tested path supplies official data in advance:

```sh
./nvgym run molmospaces --prepare --assets-cache ./molmo-data \
    --credentials ./model.env --output ./molmo-run
```

Use the data versions of that source, including benchmark v1 `20260408` and v2 `20260415`; an older mirror is not a substitute. Follow the pinned upstream [resource-manager/data instructions](https://github.com/allenai/molmospaces/blob/735138876887eb64650742631b8dda841dd264bf/docs/assets.md) when downloading. The cache directory must contain only `assets/`, `cache/` and `nltk_data/`. When downloading, map upstream `MLSPACES_ASSETS_DIR` to `downloaded-cache/assets` and `MLSPACES_CACHE_DIR` to `downloaded-cache/cache`; put the NLTK `wordnet` and `wordnet2022` resources under `downloaded-cache/nltk_data`. Include the complete scene, object and grasp closure for `close_v1-890`. Links must be relative, resolve inside the cache and not dangle. To materialize a downloaded cache on the download machine using standard Linux tools:

```sh
mkdir ./molmo-data
cp -aL ./downloaded-cache/assets ./downloaded-cache/cache \
    ./downloaded-cache/nltk_data ./molmo-data/
```

An accepted data-only archive is also supported: a directory containing only `molmospaces-assets.tar.zst`, 8,406,823,113 bytes, SHA256 `09916c2c8b0f00fb814f094125c5b3561a2168df89a07b1ac349b73771b657b9`. It contains data, not a native runtime or wheelhouse. The importer verifies it, rewrites its original absolute data links into relative links, and checks file/link counts and layout. This optional archive identity does not require access to our storage; a portable official-data directory is supported independently.

Preparation mounts the input read-only and copies verified data into the local installation; native qualification and evaluation run offline. Omitting `--assets-cache` enables the official ResourceManager/R2 download path. That automatic data path was blocked by certificate/network policy on the acceptance host and was not fully qualified; the documented command above uses the qualified supplied-data path. The public Hugging Face mirror lacked required benchmark versions. We did not downgrade the data or disable TLS verification.

## RoboCasa365 model budget

The standalone `robocasa365-sdk-native64` profile allows 64 actor model calls within the unchanged 1,200-second actor limit. Its task remains `OpenDrawer`, seed 1, with the native task horizon of 750 controls. The original 40-request config, registry and release descriptor are retained beside it as provenance. Two 40-request trials stopped before the native horizon and returned policy-limit penalties; they are recorded separately. The 64-request profile is a different model-budget condition, so its policy results are not a direct comparison with those trials.

## Optional RoboDojo asset cache

The default path downloads public assets automatically. To reuse a complete predownloaded tree, add `--assets-cache ./robodojo-data`. The directory must contain `Assets/`, with materialized contents at Hugging Face revision `43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab`, not Git LFS pointer files. Each file is verified against pinned Git/LFS records before copying. The optional cache transport/full-file checks are exercised; it is not a separate complete Docker-host model qualification. The qualified default RoboDojo run uses official downloads with no supplied asset cache.

## Results and cleanup

| Artifact | Purpose |
| --- | --- |
| `summary.json` | Status, reward when scored, cleanup confirmation and initial/final software identities |
| `episode/rollouts.jsonl` and aggregate metrics | Standard NeMo Gym results and grading |
| Files under `episode/` | Model captures, SDK executions, native records and trajectories |
| `image-id`, `container-id`, `container-name` | Exact local execution identity |
| `docker-cleanup.json` | Owned run-container removal |

A completed native episode can fail with verified reward zero. Declared decision/deadline/context limits instead produce a `policy_limit` penalty of zero, with `native_attempt=false`; that is not a completed native grade. The qualification record distinguishes these outcomes and task success. Provider failures, unknown outcomes, failed native cleanup and software-identity violations are not successful scored evaluations. Exemplar reports CPU conformance evidence without a native benchmark reward.

Normal exit and tested live SIGTERM shut down native processes, SDK/TTC and the watchdog, and remove the owned run container. The interruption tests pin the exact normal-run image, hold a bridge-local upstream request with TTC live, make no external model calls, and require an unscored interrupted result. SIGKILL, host power loss and Docker daemon failure are outside the graceful-shutdown qualification.

Images, caches, user data, credentials and results are retained. The launcher never globally prunes Docker. Docker logs rotate at 10 MB with two files; the retained preparation log is capped at 10 MB. Native/Gym evidence stays in the chosen output. Results may contain task images, prompts and licensed data; keep and share them according to your data terms.

Retries use a new output directory. Fix a rejected provider credential before retrying; an already prepared image can be reused. No model credential is included in preparation images or public qualification summaries.

## Isolation

Each run has its own container, bridge network, output mount and selected GPU. Network cleanup is recorded in `docker-network-cleanup.json`; the launcher checks ownership before removal. No Docker socket is exposed inside a run. Assign different GPUs explicitly for concurrent GPU tenants; `--gpu` selects a device and does not reserve it against other host users.

The default `--isolation trusted` uses TTC's existing `outer_task` envelope with `unsafe_no_host_isolation=true`. Use a dedicated or trusted host in that mode.

`--isolation strict` omits the unsafe TTC flag, uses the supplied seccomp profile and provides an 8 GiB `/tmp` tmpfs for TTC guest storage. Guest storage counts toward the container's memory limit. On an AppArmor host, an operator must first load the supplied profile:

```sh
sudo apparmor_parser -r portable/security/ttc.apparmor
./nvgym run libero --prepare --isolation strict \
    --model-profile nemotron-3.5-super-vl \
    --credentials ./model.env --output ./strict-run
```

The host must permit unprivileged user namespaces. The tested policy host is Ubuntu 22.04/kernel 5.15; other host policies may reject nested namespace creation. A strict image is required for strict execution, and neither policy errors nor unsupported host features trigger an unsafe fallback. See [policy provenance and the TTC runtime child profile](../portable/security/NOTICE.md).

The native actor receives a random episode capability for its local Gym model endpoint, not the provider key. The embedded driver binds capability authorization to the owning live episode; wrong, foreign and revoked capabilities are rejected before provider forwarding. Model adapters launched without that binding fail closed. This authority binding is supplied by `scripts.sdk_smoke`, not by an independent model-server process.

The rootless probes in `tests/probes/ttc_isolation.py` test two live TTC guests, host-file/proc access, network reachability, process limits and stopping one guest while its peer continues. They use synthetic canaries and no model credentials. These checks do not establish VM isolation or protection from users who control the host Docker daemon. Native case qualification and model success remain separate checks.

## Pins and scope

| Input | Identity |
| --- | --- |
| SDK/bridge pair | `sha256:d8836d1569a505d4635ee367159c2621930e3f2b55335e73bc9a1b777a5f09a0` |
| NeMo Gym | Published `nemo-gym==0.7.0`; package version and import location checked |
| Default public CUDA base | `12.8.1-cudnn-devel-ubuntu22.04`, digest `61f6c08f2b59036cb935e56d1e31a6b64e3ae2c7ddb86d33fa0b044c7917b719` |
| RoboTwin public CUDA base | `12.1.1-cudnn8-devel-ubuntu22.04`, digest `cc55d151af1e8e083f3210af753a5cfbcbc5455421531eb0459887026bb4699f` |
| Common archive | SHA256 `fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041` |

Each environment preserves the accepted task, seed, layout, SDK roles and native control budget. RoboCasa365 uses the explicitly versioned 64-call actor profile described above; its original 40-call inputs remain as provenance. Installed software and runtime/source identities are recorded in outputs. The [current qualification record](qualification/sdk-nine-l40-20261024.json) states exact measured outcomes and retained evidence identities. The [earlier RoboDojo record](qualification/robodojo-l40-20261023.json) remains a historical result. One default-case result per environment is not a benchmark-wide success-rate estimate.

This repository integrates with NeMo Gym through its environment-server and compatible model-server contracts. Native grading remains owned by Nanoverse. It does not claim inclusion in NVIDIA-NeMo/Gym upstream or substitute fixtures for native simulators. Exemplar is explicitly a CPU conformance fixture.
