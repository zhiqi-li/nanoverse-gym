# Portable SDK runtime

The portable runtime packages Gym, the paired Nanoverse SDK and bridge, TTC,
the TTC Python guest, Node, Codex, and one simulator runtime. The host launcher
selects an environment image and runs its container entrypoint.

Public images are not published. All nine entries in `portable/images.tsv`
currently have image `-`. The default launcher refuses these entries before
starting Docker or spending model API budget. An explicit `--image` selects
a maintainer-built candidate. The image compiler accepts exact cases for all nine
SDK environments. Checked-in release inputs cover all nine SDK environments. They retain the
recovered accepted registry and configuration bytes where available. Each
final environment image still requires independent qualification.

## Public distribution boundary

The current seed-based candidate images are for private internal qualification.
A software-only extraction excludes licensed datasets and keys, but can still
contain NVIDIA Kit binaries. The [Isaac Sim 5.1 license overview](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/common/licenses-isaac-sim.html)
separates the Apache-licensed source from other components. Section 2.2 of the
[additional software terms](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/common/license-isaac-sim-additional.html)
restricts redistribution. Covered public redistribution authorization has not
been established for this project. The candidate images remain private.

The external preparation path packages the adapter, supporting runtime, and
preparation recipe separately from simulator components. The user's machine
obtains simulator components from their official origins. The `--prepare`
launcher creates a GPU preparation container, saves its completed installation
as a user-owned local image, and launches a separate evaluation container.
Preparation receives no model credential file. This implementation remains
under qualification: the [fixed common package is a public prerelease](https://github.com/zhiqi-li/nanoverse-gym/releases/tag/v0.1.0),
and the complete Docker host lifecycle has not passed acceptance. No Docker
daemon or socket runs inside the preparation container or an OSMO task.

The nine accepted cases have separate public-input gates. RoboDojo's official-source
installation and model-free native scene check have passed on L40. Its production
handoff, final Gym evaluation and external Docker lifecycle remain under qualification.
The v24 handoff rejected two Kit extension links into the earlier installer's
temporary home. The v25 preparation recipe places the installer home inside
the software payload and requires all native Python dependencies to resolve
inside that payload before sealing. This updated installation still needs
fresh remote qualification.
The v25 cold attempt also exposed missing local Git LFS filter configuration
after asset objects had downloaded. The current recovery recipe initializes
these filters locally, without global Git settings or repository hooks. The
old implementation reproduced the pointer-file failure in a real Git LFS
regression check; the fix and 49 related cases passed. A development continuation
materialized and verified all 15,365 pinned asset files. This does not qualify
the rejected v25 cold attempt or the complete Docker host path.
RoboForge additionally needs its authored task package.
LIBERO and RoboTwin have public-origin installer recipes to qualify. RoboLab
must retain version 0.2.1 and Isaac Sim 5.0. RoboCasa365's five frozen asset packs
have verified official download sources, but native installation remains unqualified.
MolmoSpaces' staged source, wheelhouse, and data need exact official or
operator-downloaded replacements before public availability.
BEHAVIOR needs a software-only owner operation because its
existing full installer copies the dataset key into the installation root.
The proposed image preparation must keep that key and dataset external.
Exemplar remains a deterministic conformance fixture.

## Execution boundary

An external user runs `nvgym` on a machine with Docker, NVIDIA Container Toolkit,
a compatible GPU driver, and model API credentials. Docker starts the selected
runtime. No cluster client or shared filesystem is required by the runtime.

An OSMO task already supplies the GPU container. Its command runs
`portable.runtime` directly. It does not start Docker inside the task.
Both paths use the same runtime entrypoint and native SDK owner. Gym owns
collection and model transport. Nanoverse owns simulation, grading, and TTC.

The direct L40 acceptance checks the container runtime. It does not establish
that the final Docker image builds or that the host launcher's Docker lifecycle
works on an external user's machine.

## Host launcher

The launcher accepts exactly the nine SDK environments listed by
`./nvgym list`. A candidate invocation has this form.

```sh
./nvgym run robodojo --image "$CANDIDATE_IMAGE" \
	--credentials ./model.env --output ./run-001
```

The output parent must exist. The output directory must be new. `--gpu` selects
a host GPU index and defaults to `0`. The launcher pulls an absent image and
runs the resolved image ID. A published release must replace each available
image entry with a tested digest reference.

The official-source preparation candidate accepts this command. The fixed
common package downloads anonymously from its public prerelease. An optional
`--common-package` uses a previously downloaded copy with the same SHA256.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --credentials ./model.env --output ./run-001
```

`--cache DIRECTORY` selects the host preparation cache, which retains the
prepared local image for reuse. Preparation obtains the pinned Isaac Sim,
RoboDojo sources and assets from official origins before SDK admission. The
common package contains no Isaac Sim or Kit installation. Accepting the terms
is an explicit operator choice. A failed or interrupted preparation cannot be
used as an evaluation image.

The pinned installer requires at least 150 GiB of free space in its installation
filesystem before starting. The verified native payload alone contains about
64.3 GB, including about 41.3 GB of assets. Preparation downloads and builds
additional temporary files. Current GPU qualification uses one L40; other GPU
architectures have not been validated by this release.

An operator can provide a complete, previously downloaded asset tree with
`--assets-cache DIRECTORY`. It must contain regular files under `Assets/`
from the pinned [RoboDojo dataset revision](https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo/tree/43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab/Assets).
Preparation mounts that directory read-only, fetches the pinned Git metadata,
checks every asset's size and SHA256, and copies verified files into the native
installation. It generates robot configurations in the installation and keeps
the downloaded originals unchanged. An incomplete or changed cache fails
preparation. Keep the model credential file outside this directory.

```sh
./nvgym run robodojo --prepare --accept-isaac-terms \
    --common-package ./common-runtime-v1.tar.gz --assets-cache ./robodojo-data \
    --credentials ./model.env --output ./run-001
```

The asset-cache implementation has passed remote source checks. Full simulator
and Docker host qualification of this path remains open. `--data` separately
selects a runtime asset override, as described below.

The credential file supplies these names. Values stay in the user's file.

```dotenv
NV_BASE_URL=https://your-provider.example/v1
NV_API_KEY_2=your-api-key
```

The current candidate requests `openai/openai/gpt-6.1-sol`. The provider must
serve that model identifier and support image input and tool calls. Model
selection is fixed for this qualification candidate. The launcher does not
send credentials to the native actor or TTC guest. It mounts the file read-only
for Gym's Model Server.

The container receives one GPU, 12 CPUs, 64 GiB of memory, a 4096-process limit,
and 2 GiB of shared memory. Docker logs retain at most two 10 MiB files.
No Docker socket or host network port is mounted. TTC uses the existing
`outer_task` resource mode with `unsafe_no_host_isolation=true`. These limits
apply to the whole container; this configuration does not provide isolation
between mutually untrusted tenants.

The launcher stops and removes only its owned container after completion or
interruption. It retains the downloaded image and user results for reuse.
`docker-cleanup.json` records removal or an unresolved Docker error.

## User-supplied RoboDojo data

RoboDojo accepts `--data DIRECTORY`. The directory contains an `Assets` tree,
including `Assets/Robots/x5/curobo_tmp.yml`. Docker mounts the directory read-only
at `/data`, and the runtime passes that asset root to RoboDojo.

```sh
./nvgym run robodojo --image "$CANDIDATE_IMAGE" \
	--credentials ./model.env --output ./run-002 --data ./robodojo-data
```

The seed asset snapshot comes from the public
[RoboDojo dataset](https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo)
at revision `43dacb13d3051a9ccd421f4e7e08e4e3eb29dfab`. The current argument
checks the required file layout. It does not hash every user-supplied asset or
download missing files. The candidate image already includes assets, so this
option changes which asset tree is used without reducing that image's size.

## User-supplied BEHAVIOR data

BEHAVIOR requires `--data DIRECTORY`. Obtain and extract the official resources
through the official [installation and data acquisition guide](https://behavior.stanford.edu/getting_started/installation.html)
and [dataset download entrypoint](https://behavior.stanford.edu/reference/utils/asset_utils.html).
Complete the dataset license process yourself. The directory contains this layout.

```text
behavior-data/
  behavior-1k-assets/VERSION
  omnigibson-robot-assets/VERSION
  2026-challenge-task-instances/metadata/
    task.jsonl
    available_tasks.yaml
    B100_task_misc.csv
  omnigibson.key
```

The two asset versions must be `3.9.0` and `3.8.2`, respectively. The full
resource trees must be present, beyond the identity files shown here. The
launcher requires an explicit existing data directory before Docker pulls the
image. Docker mounts this directory read-only at `/data`.

```sh
./nvgym run behavior1k --image "$CANDIDATE_IMAGE" \
    --credentials ./model.env --data ./behavior-data --output ./run-003
```

Runtime admission checks the four resources, contained paths, both versions,
three challenge metadata files, and a readable nonempty bounded key before
resolving model credentials or starting Gym. The runtime records hashes of the
five nonsecret identity files in `summary.json`. It records neither key contents
nor a key hash. These are identities of the supplied resources. Historical
extracted metadata hashes have not been recovered, so this check does not claim
historical metadata byte equality. The native BEHAVIOR owner validates the
selected task, instance, encrypted assets, and scene at startup.

The accepted case remains `turning_on_radio`, instance `0`, with `compact_64k`,
policy control, empty policies, 400 model calls, a 900-second native startup
limit, and 4200-second actor and native limits. The outer release limit is 5400
seconds. Writable simulator appdata lives under the selected output directory.
The data mount is never a TTC guest input. The image does not download missing
data or accept its license for the operator.

## Existing GPU container

The release layout places application files under `/opt/nvgym`. The container
environment sets `PYTHONPATH` to the packaged application and Gym directories.
Given an empty `/output` directory and a credential file, the entrypoint is
equivalent to this command.

```sh
/opt/nvgym/runtime/python/bin/python3.13 -m portable.runtime robodojo \
	--output-dir /output --credentials-file /run/secrets/model.env
```

For RoboDojo, optional `--data-root /data` exercises the same data selection as
the host launcher's `--data`. BEHAVIOR requires `--data-root /data` and its
user-supplied resources. Existing containers are managed by their outer scheduler.
The runtime closes its episode processes and TTC guest. It leaves the enclosing
container to that scheduler.

## Outputs

`summary.json` records the environment, release provenance, exit code, native
cleanup confirmation, and score. A fixture receives status `fixture` instead
of an invented reward. A failed or interrupted episode receives the corresponding
status and does not become a scored success.

`episode/rollouts.jsonl` and `episode/rollouts_aggregate_metrics.json` are the
official Gym collector outputs. `episode/model-calls` contains model captures.
The per-episode directory under `episode/n` contains the native outcome,
SDK execution and trajectory records, TTC receipt, and simulator logs.
The runtime preserves the output directory's original owner when running as
container root. For RoboDojo, it also passes that owner through the native
`kit_trusted_owners` setting so the simulator can start below a user-owned
output mount. Native directory permission checks remain active.

The L40 RoboDojo qualification on 2026-10-22 produced one native rollout with
reward `1.0`, eight GPT-6.1 Sol calls, 49 native controls, six SDK executions,
and two trajectories. The runtime exited `0`, native cleanup was complete,
and TTC reported `closed`. The 43 SDK interface and HTTP interruption tests also passed
on that L40. This is one task completion and does not estimate benchmark-wide
model quality. The final public image and Docker host launcher remain unqualified.

A second run selected a separate data directory backed by the existing public
asset snapshot. It reached model execution, but the provider returned HTTP 429
with empty response bodies. The run was interrupted and produced no score.
This checks asset-path initialization, not a complete data-root evaluation or
a fresh internet download. Its process cleanup receipt completed and TTC closed.

That interruption exposed Uvicorn's process signal handlers overriding the
collector's asyncio owner. Embedded HTTP servers now leave signal handling to
the collector, and the wrapper forwards one SIGINT during cleanup. The L40
regression uses a real active HTTP request to confirm cancellation and closure
of all three servers. It does not call a model API.

The packaged runtime also passed interruption after the native simulator process
started below an output directory owned by UID 23301. It exited without forcing
the collector to stop, and that unprivileged UID could read all generated files.
This test used a loopback-only model endpoint and created no TTC guest. Process
cleanup completed. Because interruption preceded SDK startup, SDK closure evidence
was absent and the adapter retained its conservative quarantine diagnostic.
This is not evidence for interruption of a live TTC guest or for a Docker host.

The private Exemplar v21 image subsequently passed normal execution, live TTC
guest interruption, and interruption during an active SDK script. Those checks
used a loopback provider and do not establish a real model score.

The private RoboDojo v21 image reached a live TTC guest and closed SDK, TTC,
watchdog, and simulator processes after interruption. Its final native inventory
failed, so the image remains rejected. An isolated experiment reproduced
cuRobo's version discovery stripping `GIT_OPTIONAL_LOCKS` and refreshing a Git
index. The v24 native host enforces `git --no-optional-locks` through its owned
Git entrypoint. The mechanism and affected source checks passed; a new runtime
still needs complete lifecycle acceptance.

## Image factory inputs

`python -m portable.build_context --release-input releases/robodojo/release.json`
selects a maintainer's exact case and native image input. Additional required
arguments supply source paths and the output directory. The compiler does not
resolve private sources for end users. Its required
inputs include the paired Nanoverse and bridge source, a fresh Gym installation,
the TTC binary and guest manifests, Node with its license, and the Codex program
manifest with its license. Every copied payload file or symlink enters the
release inventory. The runtime verifies this inventory before model execution.

The release input binds original configuration and registry bytes by SHA256.
It selects one existing case. Declared path replacements must name existing
JSON fields and match their original values. Control modes, omitted policies,
task settings, budgets, and evidence paths pass through without reconstruction.
The RoboDojo example is derived from accepted runtime metadata. It does not
claim to contain the original historical registry bytes.

Native inputs select a fixture image, an expanded seed image, or a BEHAVIOR
`software_archive`. Every source image and final base is pinned by digest. The
previous `archive_cache` release variant is rejected.
Expanded seeds retain their original userspace and baked payload. The runtime
creates the original absolute Python prefix as a link to that payload, including
when a scheduler mounts `/osmo`. This link is inside the container and requires
no original host directory. Conflicting existing installations are rejected.

Build admission validates the native configuration, paired source, TTC deployment,
and isolated SDK interpreter. Expanded seeds and derived BEHAVIOR software receive a full inventory check
before and after admission. Admission does not install source into the native
Python environment. The runtime checks the admission receipt and initial native
inventory before model execution. These new image admission paths still require
remote validation against the final image.

The native actor uses a separate image-owned interpreter at
`/opt/nvgym/native-host/bin/python`. The admitted original interpreter constructs
it, and its inventoried startup policy loads the original native package hooks
and the packaged paired source. Actor and watchdog startup therefore work
without an inherited `PYTHONPATH`. The SDK remains a separate interpreter with
no native private modules. Original and derived interpreters must have the same
Python version, ABI, and resolved base directory.

After collector shutdown and child cleanup, the runtime verifies the complete
native, actor-host, and paired-source inventories again. A mutation makes the
run fail even if the simulator returned a reward. These checks also apply to
interruption. Native subprocesses disable bytecode writes and Git's optional
index refresh at their actual launch boundary.

The clean SDK has its own complete admission inventory. Its configured Python
entrypoint inserts `-B` before interpreter startup, including when the native
staging owner supplies only PATH and PYTHONPATH. The receipt distinguishes
that entrypoint from its copied backend. Runtime verification executes neither
file. It checks their exact bytes, aliases, packages and modes before collection
and after cleanup. Direct backend reentry does not inherit the entrypoint's
policy; any resulting mutation still fails the full inventory check.

The embedded collector can request cooperative shutdown after an authenticated
model request from the current native launch. It signals only that retained
Harness leader, leaving native SDK and TTC finalizers alive. The original
supervisor remains the bounded hard fallback. Cancellation before the first
request, or deployments with separate server processes, may lack semantic
closure evidence and remain quarantined. None of these paths turns interrupted
or mutated output into a score.

The BEHAVIOR preparation stage reads the existing private cache image only as a
build input. It verifies the cache descriptor and exactly the pinned native
archive and Warp wheel. Its safe extractor admits the observed standalone
Python tree, native venv, and public BEHAVIOR source tree. It excludes dataset
roots, identity-copy directories, Git state, environment files, and the dataset
key. Python modules such as `torchvision/datasets` remain software. No dataset
object or key object is opened, extracted, or copied.

The final BEHAVIOR stage starts from an independent public Ubuntu 22.04 CUDA
12.8 base. It installs the reviewed runtime system libraries, then copies only
the prepared native software and the common `/opt/nvgym` payload. It never
inherits private cache layers. A new `nvgym.native-software.v1` descriptor binds
the preparation recipe, both input objects, source commit, Python distribution
metadata, software notice paths, and complete output inventories. Build
admission records actual final system package versions. This derived identity
does not claim to be a historical expanded seed.

Build preparation records a native authority with the derived descriptor path
and hash, payload, original prefix, and Python executable. Runtime compares that
authority with its admission receipt, verifies every immutable software entry,
and recreates the original `/osmo` prefix after scheduler mounts. The expanded
seed verifier still records dangling historical absolute links as diagnostics;
its full snapshot hash, configured interpreter, and native admission remain
mandatory.

The BEHAVIOR external-data contract is authored from the paired installer and
cells module, then compared with those actual modules using the isolated native
interpreter during build admission. Admission checks its configured Python,
required GLU, Xt, Vulkan, and EGL libraries, installed simulator and Warp
versions, paired source, native configuration, and SDK isolation. These checks
remain separate from a real native scene startup.

Software-only packaging does not establish redistribution rights for all
simulator dependencies. Before publication, review the copied software terms
and notices and inspect every final image layer. BEHAVIOR's public catalog entry
stays unavailable until the exact final image passes clean-base GPU startup,
external-data failure cases, native scene observation and cleanup, the accepted
model case with usable budget, and a public pull plus host Docker lifecycle.
No final-image BEHAVIOR qualification is claimed by this source change.
RoboForge also requires its exact authored task package and manifest identities.

Before publishing a release, the maintainer still needs to build and qualify
the final image, publish all required runtime artifacts,
validate the Docker host lifecycle, and set verified public image digests in
`portable/images.tsv`. Each remaining native simulator image requires real final-image runtime
acceptance with its checked-in release inputs. Historical SDK adapter checks
for all nine do not establish portable image availability.

## Maintainer verification

Gym uses Python 3.13. Native interpreter integration tests use a separate
standalone Python 3.11, matching the native environments. Set
`NVGYM_TEST_NATIVE_PYTHON` to that interpreter when running
`tests/test_native_host.py`. The fixture copies the standalone base into its
own temporary native payload and qualifies startup before recording the seed.
The test then requires unchanged native, host, and source identities after
independently sanitized actor and child startup. An absent interpreter skips
these native integration tests.

The 2026-10-22 frozen v18 remote source checks passed all 149 tests, Ruff,
and shell syntax. These include cold SDK startup through symlink, copied and
indirect interpreter layouts, the actual registered staging owner, authenticated
shutdown and scored-result mutation rejection. A real uv-created native venv
regression failed before the canonical base-directory correction and passed
afterward.

The private Exemplar v18 image has digest
`sha256:5d03770c593e8b6cf0c25a7124309837162283cc62299a726380a6af3768c840`.
A fresh container passed outer-entrypoint SIGINT with a live TTC guest and held
model request. SDK, TTC and watchdog closed, all original/base/host/source/SDK
identities stayed equal, and UID 23301 could read all 78 generated files. A
second test executed a real SDK control and retained a running guest command
session before the same outer SIGINT; all closure and identity checks passed.
Neither test called an external model endpoint or produced a score. They qualify
these interruption paths in this conformance fixture, not a simulator score,
a public pull, the external Docker host wrapper, or the other eight images.
A scripted normal-completion test also passed through the exact runtime and
collector. It produced 200 native controls, 200 SDK executions and one trajectory,
with an audited fixture result, exit code 0, complete closure and equal
inventories. Its single model-protocol response came from a synthetic endpoint;
it is not a GPT-6.1 Sol evaluation.
Real software-only preparation verified 139,904 BEHAVIOR software files from
the pinned native archive and Warp wheel. This does not qualify the final
public CUDA image, simulator startup, public pulls, or Docker host lifecycle.
The real RoboDojo scene exposed writes to native bytecode and the cuRobo Git
index. The outer native launcher sets both policy variables, but the frozen
simulator owner filters them out before starting its child. A fresh v18 L40
container reproduced this failure despite complete SDK, TTC, watchdog and
native cleanup. It added 34 paired-source bytecode files and failed the full
native inventory. This image is not accepted for evaluation. An owned early
interpreter entry is integrated in v19. Its independent source review and
160 remote tests passed, including the frozen simulator owner's actual child
launch with a sanitized environment and a recursive empty-environment guard.
The entry applies bytecode and Git policy before Python starts. Pristine-image
startup and post-cleanup inventory checks remain required before publishing a
runtime digest.

The v19 Exemplar image then failed initial admission because OSMO tightened
the container root from build mode `0755` to runtime mode `0555`. Bash bytes,
its link, and the other ancestor identities stayed equal. The v20 native-host
v3 receipt retains the build observation and permits only that root owner-write
removal or an unchanged root mode. A root recorded as `0555` cannot become
`0755`. All other helper metadata and protected inventories remain exact.
Independent review, 204 remote tests, Ruff, and shell syntax passed for v20.
The v21 shutdown binding also recognizes the exact leading `-B` inserted by the
admitted entrypoint; 31 affected remote regressions passed. The actual v21 Exemplar
image passed normal completion with 200 controls, interruption with a live TTC
guest and held model request, and interruption during active SDK work. All three
closed SDK/TTC/watchdog resources and retained identical immutable inventories.
The 1,451 exported evidence files matched their sizes and hashes. These runs used
a synthetic loopback provider, not GPT-6.1 Sol or a native benchmark score.

The official-origin preparation candidate separately passed independent review
and 19 remote source tests with NeMo Gym installed. Those tests establish
namespace compatibility and the preparation verifier's behavior. They mock
native installation and do not qualify a cold install, a simulator episode,
or the host Docker lifecycle.

A separate fresh public CUDA container subsequently installed RoboDojo from its
official sources without a private native seed or model credentials. Anonymous
Git LFS supplied all 15,365 assets at the accepted dataset revision, with
41,269,513,111 bytes checked against the pinned Git objects' sizes and SHA256s.
The unchanged native installer completed after verified Hub cache prefill. Its
native qualifier passed three RGB cameras, metric depth and calibration checks,
read-only observations, two controls and recording. The 37 exported native
evidence files matched their sizes and hashes.

`portable.robodojo_assets` implements this recovery transport for an interrupted,
unsealed assets phase. `portable.official_install` runs the frozen owner, handles
the observed anonymous Hub rate-limit failure through that transport, verifies
the native scene and creates an independent `installed_software` descriptor.
It refuses to replace a sealed descriptor. The release loader and native admission
recognize this authority at its canonical prefix. Eleven asset-transport tests,
32 preparation/release checks and Ruff passed remotely; the transport's manifest
also matched the actual complete pinned Git tree. A subsequent live check exposed
writable installer locks and two Kit appdata directories. Preparation now discards
only its completed download caches and managed-Python lock, and protects those
two directories before native qualification. Sixteen affected owner checks and
Ruff passed after this fix.

The actual L40 preparation then passed native qualification and sealed all
214,553 files, totaling 64,254,378,440 bytes. A second full inventory comparison
passed. Original software and asset bytes remain included in that authority;
installer cache cleanup and initial Kit directory modes are recorded explicitly.
Native-host/SDK admission, Gym/TTC evaluation and the Docker host lifecycle still
require acceptance for this official-source installation. The host launcher does
not yet automatically run this preparation path.

## Reference distribution boundary

The reference is [Embodied First Exam at e695c327](https://github.com/mll-lab-nu/embodied-first-exam/tree/e695c327deacb346fb468564a5cf40b72569cd34).
Its recipes distinguish public images from `BUILD_ONLY` images. A user can
download software into an image they build locally even when maintainers do not
publish that resulting image. The reference's private images serve as optional
build caches; external users build from public sources without pulling them.
See its [base-image contract](https://github.com/mll-lab-nu/embodied-first-exam/blob/e695c327deacb346fb468564a5cf40b72569cd34/docs/protocol/running.md#base-images).

For this integration, the operator selected official-origin acquisition for
Isaac Sim and Kit. The public artifact contains the permitted common runtime,
SDK tools, recipes and download validators. Preparation downloads vendor
software directly into the operator's own image. Model credentials enter only
the later evaluation container.

| Our SDK environment | Distribution plan | User-supplied inputs |
| --- | --- | --- |
| exemplar | Common runtime and fixture in a public image after publication checks. | Model API credentials. |
| robodojo, roboforge | Public recipe installs Isaac and pinned native software and assets into an operator-owned image. RoboForge retains its exact authored task package. | Model API credentials; accept applicable installation terms. |
| robolab | Same operator-owned image boundary, retaining RoboLab 0.2.1 and Isaac Sim 5.0. | Model API credentials; accept applicable installation terms. |
| behavior1k | Prepare software separately; mount licensed data read-only and appdata writable. | Model API credentials and the operator's licensed dataset and key. |
| robocasa365 | Download the five exact asset packs from official public origins during operator preparation and verify their hashes. Do not add the reference's sixth pack. | Model API credentials; an already downloaded matching data cache is optional. |
| molmospaces | Use official resource acquisition for the accepted case, retaining the classic and separate Filament runtime identities. The existing full snapshot is not a public asset bundle. | Model API credentials; matching operator-downloaded resources may be supplied. |
| libero, robotwin | Package permitted software; obtain the exact accepted assets from official sources during operator preparation until their redistribution scope is established. | Model API credentials; matching operator-downloaded resources may be supplied. |

On 2026-10-22, a remote CPU-only probe anonymously downloaded all five original
RoboCasa365 archives from the official Box endpoints. All 5,213,425,799 bytes
matched the frozen owner's five SHA-256 values. The probe deleted each ZIP
after verification and retained its report. This establishes exact public
acquisition for those resources; native installation and Gym runtime acceptance
remain separate gates. The frozen native owner can consume hash-verified files
from its resource cache without reading S3 credentials.

These are delivery decisions, not completed availability claims. The public
catalog remains unavailable until each environment passes its own gates.
The reference's RoboLab 0.3.1, LIBERO-MAX event layer, RoboDojo revisions and
MolmoSpaces eight-instance asset subset are different from our accepted inputs.
Its images cannot replace those inputs without a new explicit qualification.
The original configuration and registry bytes remain the source of task
semantics; checked path bindings handle deployment paths.

The reference provides concrete examples of [RoboDojo build-only acquisition](https://github.com/mll-lab-nu/embodied-first-exam/blob/e695c327deacb346fb468564a5cf40b72569cd34/images/robodojo/BUILD_ONLY),
[BEHAVIOR external licensed data](https://github.com/mll-lab-nu/embodied-first-exam/blob/e695c327deacb346fb468564a5cf40b72569cd34/images/behavior/BUILD_ONLY),
[RoboCasa asset fetching](https://github.com/mll-lab-nu/embodied-first-exam/blob/e695c327deacb346fb468564a5cf40b72569cd34/images/robocasa365/fetch_assets.sh)
and [MolmoSpaces asset preparation](https://github.com/mll-lab-nu/embodied-first-exam/blob/e695c327deacb346fb468564a5cf40b72569cd34/images/molmospaces/prepare_assets.sh).
Its repository license does not replace the component and asset licenses.
