#!/bin/sh
set -eu

root=$1
gpu=$2
cache=$3
package=$4
accept=$5
assets_cache=${6:-}
environment=${7:-robodojo}
case "$environment" in
    robodojo|roboforge|robolab|behavior1k) [ "$accept" = yes ] || { echo 'Official preparation requires --accept-isaac-terms' >&2; exit 2; } ;;
    exemplar|libero|robocasa365|robotwin|molmospaces) ;;
    *) echo 'Official preparation is not implemented for this environment' >&2; exit 2 ;;
esac
umask 077
mkdir -p -- "$cache"
cache=$(CDPATH= cd -- "$cache" && pwd)
context=$(mktemp -d "$cache/build.XXXXXX")
owner=nvgym-prepare-$(basename -- "$context")
preparation=
fetch=
download_image=
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    for container in "$preparation" "$fetch"; do
        [ -n "$container" ] || continue
        if details=$(docker inspect --format '{{.Id}} {{index .Config.Labels "com.nvidia.nanoverse-gym.run"}}' "$container" 2>/dev/null); then
            identity=${details%% *}
            label=${details#* }
            if [ "$label" != "$owner" ]; then
                echo 'Preparation container owner mismatch' >&2
                status=1
                continue
            fi
            docker stop --time 420 "$identity" >/dev/null 2>&1 || true
            if [ "$container" = "$preparation" ]; then
                docker logs "$identity" 2>&1 | tail -c 10485760 > "$cache/last-preparation.log" || true
            fi
            docker rm "$identity" >/dev/null || status=1
        fi
    done
    if [ -n "$download_image" ]; then
        docker image rm "$download_image" >/dev/null 2>&1 || true
    fi
    rm -r -- "$context"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -z "$package" ]; then
    package=$cache/common-runtime-v1.tar.gz
    if [ ! -f "$package" ]; then
        # Docker performs the public download; no host Python or curl is required.
        mkdir "$context/download"
        cat > "$context/download/Dockerfile" <<'EOF'
FROM nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04@sha256:61f6c08f2b59036cb935e56d1e31a6b64e3ae2c7ddb86d33fa0b044c7917b719
ADD https://github.com/zhiqi-li/nanoverse-gym/releases/download/v0.1.0/common-runtime-v1.tar.gz /common-runtime-v1.tar.gz
RUN echo 'fb0f8b55a27c041c8a44d90d6f3c848a9548ee8a05556c787292c393f1b12041  /common-runtime-v1.tar.gz' | sha256sum -c -
EOF
        docker build --provenance=false --iidfile "$context/download-image" "$context/download" >&2
        download_image=$(cat "$context/download-image")
        fetch=$owner-download
        docker create --name "$fetch" --label "com.nvidia.nanoverse-gym.run=$owner" "$download_image" /bin/true >/dev/null
        docker cp "$fetch:/common-runtime-v1.tar.gz" "$package"
    fi
fi
[ -f "$package" ] || { echo 'Common runtime package must name a regular file' >&2; exit 2; }
cp -- "$package" "$context/common-runtime-v1.tar.gz"
cp -- "$root/portable/Dockerfile.common" "$context/Dockerfile"
mkdir "$context/app" "$context/app/scripts" "$context/inputs"
for name in portable environment_servers responses_api_models; do
    cp -R -- "$root/$name" "$context/app/$name"
done
cp -- "$root/scripts/sdk_smoke.py" "$context/app/scripts/sdk_smoke.py"
cp -R -- "$root/releases/$environment" "$context/inputs/$environment"
cat > "$context/.dockerignore" <<'EOF'
**/__pycache__
**/*.pyc
**/.pytest_cache
**/.ruff_cache
**/.env
**/.env.*
**/*.env
**/*.pem
**/*.key
EOF
if [ "$environment" = robotwin ]; then
    docker build --provenance=false --build-arg 'PUBLIC_BASE=nvcr.io/nvidia/cuda:12.1.1-cudnn8-devel-ubuntu22.04@sha256:cc55d151af1e8e083f3210af753a5cfbcbc5455421531eb0459887026bb4699f' --iidfile "$context/common-image" "$context" >&2
else
    docker build --provenance=false --iidfile "$context/common-image" "$context" >&2
fi
common_image=$(cat "$context/common-image")
prepared=nanoverse-gym-$environment:${common_image#sha256:}
cache_identity=
if [ "$environment" = molmospaces ] && [ -n "$assets_cache" ]; then
    fetch=$owner-assets-identity
    docker create --name "$fetch" --label "com.nvidia.nanoverse-gym.run=$owner" \
        --mount "type=bind,source=$assets_cache,target=/input-assets,readonly" \
        --entrypoint /opt/nvgym/bootstrap/bin/python "$common_image" \
        -B -m portable.molmo_public /input-assets >/dev/null
    docker start --attach "$fetch" > "$context/assets-identity"
    [ "$(docker inspect "$fetch" --format '{{.State.ExitCode}}')" -eq 0 ] || exit 1
    cache_identity=$(cat "$context/assets-identity")
    case "$cache_identity" in ''|*[!0-9a-f]*) echo 'Invalid MolmoSpaces cache identity' >&2; exit 1 ;; esac
    [ "${#cache_identity}" -eq 64 ] || exit 1
    prepared=$prepared-$(printf '%s' "$cache_identity" | cut -c1-32)
fi
if image=$(docker image inspect "$prepared" --format '{{.Id}} {{index .Config.Labels "com.nvidia.nanoverse-gym.prepared"}}' 2>/dev/null); then
    [ "${image#* }" = "$common_image" ] || { echo 'Prepared image provenance mismatch' >&2; exit 1; }
    if [ -n "$cache_identity" ]; then
        [ "$(docker image inspect "$prepared" --format '{{index .Config.Labels "com.nvidia.nanoverse-gym.assets-cache"}}')" = "$cache_identity" ] || exit 1
    fi
    printf '%s\n' "${image%% *}"
    exit 0
fi
preparation=$owner-runtime
set -- --entrypoint /opt/nvgym/bootstrap/bin/python "$common_image" \
    -B -m portable.prepare_runtime --environment "$environment" --accept-isaac-terms
if [ -n "$assets_cache" ]; then
    set -- --mount "type=bind,source=$assets_cache,target=/input-assets,readonly" \
        "$@" --assets-cache /input-assets
fi
if [ -n "$cache_identity" ]; then
    set -- --env "NVGYM_ASSETS_CACHE_SHA256=$cache_identity" "$@"
fi
docker create --name "$preparation" --init --gpus "device=$gpu" \
    --cpus 12 --memory 64g --pids-limit 4096 --shm-size 2g \
    --log-driver local --log-opt max-size=10m --log-opt max-file=2 \
    --label "com.nvidia.nanoverse-gym.run=$owner" \
    --label com.nvidia.nanoverse-gym.service=official-preparation \
    --env NVIDIA_DRIVER_CAPABILITIES=all "$@" >/dev/null
docker start --attach "$preparation" >&2 &
attached=$!
wait "$attached" || true
[ "$(docker inspect "$preparation" --format '{{.State.Status}}')" = exited ] || exit 1
code=$(docker inspect "$preparation" --format '{{.State.ExitCode}}')
[ "$code" -eq 0 ] || exit "$code"
set -- --change 'ENTRYPOINT ["/opt/nvgym/runtime/python/bin/python3.13", "-B", "-m", "portable.runtime"]' \
    --change 'CMD []' --change "LABEL com.nvidia.nanoverse-gym.prepared=$common_image"
if [ -n "$cache_identity" ]; then
    set -- "$@" --change "LABEL com.nvidia.nanoverse-gym.assets-cache=$cache_identity"
fi
image=$(docker commit "$@" "$preparation" "$prepared")
printf '%s\n' "$image"
