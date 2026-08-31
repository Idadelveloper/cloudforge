#!/usr/bin/env bash
# Reset only CloudForge's disposable LocalStack container between evaluation runs.
set -euo pipefail

CONTAINER="cloudforge-localstack"
IMAGE="localstack/localstack:4.6"
HEALTH_URL="http://localhost:4566/_localstack/health"
DOCKER_SOCK="${DOCKER_SOCK:-$HOME/.docker/run/docker.sock}"
[ -S "$DOCKER_SOCK" ] || DOCKER_SOCK=/var/run/docker.sock

# Hang-proof engine probe: plain `docker` CLI calls block forever against a
# wedged Docker Desktop backend (problems log P5, P13).
docker_ping() {
    [ "$(curl -s --max-time 3 --unix-socket "$DOCKER_SOCK" http://localhost/_ping 2>/dev/null)" = "OK" ]
}

if [[ "${1:-}" != "--confirm" || "$#" -ne 1 ]]; then
    echo "This removes and recreates only the '${CONTAINER}' LocalStack container."
    echo "It clears emulated resources so the next benchmark run starts clean."
    echo "Run: ./scripts/reset_localstack.sh --confirm"
    exit 2
fi

if ! docker_ping; then
    echo "The Docker engine is not responding."
    if pgrep -f "com.docker.backend" >/dev/null 2>&1; then
        echo "Docker Desktop is running but wedged. Fix it with:"
        echo "    pkill -f com.docker.backend && open -a Docker"
    else
        echo "Start Docker Desktop, wait for the whale icon, then retry."
    fi
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Pulling ${IMAGE} (~1.9 GB, one-time — progress below)…"
    docker pull "$IMAGE"
fi

if docker ps -aq --filter "name=^/${CONTAINER}$" | grep -q .; then
    docker rm -f "$CONTAINER" >/dev/null
fi

docker run -d --name "$CONTAINER" -p 4566:4566 \
    -v /var/run/docker.sock:/var/run/docker.sock "$IMAGE" >/dev/null

for _ in $(seq 1 45); do
    if curl -s --max-time 2 "$HEALTH_URL" >/dev/null; then
        echo "LocalStack reset and healthy at $HEALTH_URL"
        exit 0
    fi
    sleep 2
done

echo "LocalStack was recreated but did not become healthy within 90 seconds."
echo "Recent container logs:"
docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/    /'
exit 1
