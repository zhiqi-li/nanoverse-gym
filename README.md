# Nanoverse SDK for NeMo Gym

Run a Nanoverse SDK environment through NeMo Gym, with simulation and grading owned by Nanoverse and SDK execution inside TTC.

This is a release candidate. RoboDojo is the first environment under end-to-end qualification. Fresh official installation is being validated on L40; the new real-model run and external Docker-host lifecycle remain unverified. Public release assets have not been published yet. The other eight SDK entries remain disabled until independently qualified.

Use a Linux x86-64 GPU host with Docker, NVIDIA Container Toolkit, a compatible driver, 64 GiB of memory and at least 150 GiB of free installation space. Current GPU qualification uses one L40. The command needs no cluster client or access to our private storage or registry.

Create `model.env` from `model.env.example` using your own API endpoint and key. The provider must support image input, tool calls, and `openai/openai/gpt-6.1-sol`.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

The output directory must be new and its parent must exist. Until the public asset is published, add `--common-package ./common-runtime-v1.tar.gz` using the matching maintainer-provided common package. Its SHA256 is `fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041`.

| Component | Delivery |
| --- | --- |
| Gym, SDK/bridge code, TTC, Python, Node and Codex | Common package, with file identities and included license files |
| CUDA and system dependencies | Built on the fixed public NVIDIA CUDA base |
| Isaac Sim/Kit, RoboDojo and its assets | Downloaded from official sources on the operator's machine |
| Model credentials | Supplied by the operator; mounted read-only only for evaluation |

Preparation installs and validates the software, saves a local image for reuse, and starts a separate evaluation container. Isaac Sim and Kit are not included in the common package. `--accept-isaac-terms` is the operator's explicit acceptance for the official installation. No Docker daemon or socket runs inside these containers.

To use a complete, already downloaded RoboDojo `Assets` tree at revision `43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab`, add `--assets-cache ./robodojo-data`. Preparation mounts it read-only, verifies every file against the pinned Git/LFS records, and copies verified bytes into the installation. Keep model credentials outside this cache. `--gpu` selects a host GPU index and defaults to `0`; `--cache` selects the host preparation cache.

Results include `summary.json`, Gym rollouts and aggregate metrics, model-call captures, SDK executions, trajectories, and simulator/TTC cleanup receipts. The host launcher records container cleanup in `docker-cleanup.json`. A failed or interrupted run does not become a scored success. Prepared local images and user results are retained; owned run containers are removed after exit or interruption.

TTC uses the existing `outer_task` mode with `unsafe_no_host_isolation=true`. This configuration does not provide isolation between mutually untrusted tenants.

`./nvgym list` lists exactly the nine SDK environments. The adapters follow NeMo Gym server structure under `environment_servers/nanoverse_sdk` and `responses_api_models/nanoverse_sdk_openai`; the Gym configuration is under `environments/nanoverse_sdk`. See [portable runtime details](docs/portable.md) for input pins, qualification scope and output layout.
