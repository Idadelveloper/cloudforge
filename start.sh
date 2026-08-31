#!/bin/bash
# CloudForge one-command launcher: Docker -> LocalStack -> Streamlit.
# Every Docker interaction is gated on a hang-proof socket probe, because a
# wedged Docker Desktop backend makes plain `docker` CLI calls block forever
# (problems log P5, P13).
set -e
cd "$(dirname "$0")"

CONTAINER=cloudforge-localstack
IMAGE=localstack/localstack:4.6
DOCKER_SOCK="${DOCKER_SOCK:-$HOME/.docker/run/docker.sock}"
[ -S "$DOCKER_SOCK" ] || DOCKER_SOCK=/var/run/docker.sock
ENV_DIR=

# Probe the Docker engine API directly with a hard timeout. Unlike
# `docker info`, this cannot hang when the backend is wedged.
docker_ping() {
    [ "$(curl -s --max-time 3 --unix-socket "$DOCKER_SOCK" http://localhost/_ping 2>/dev/null)" = "OK" ]
}

if [ -x ".venv/bin/streamlit" ]; then
    ENV_DIR=.venv
elif [ -x "venv/bin/streamlit" ]; then
    ENV_DIR=venv
else
    echo "✗ Missing project virtual environment."
    echo "  Create it with:"
    echo "  python3.13 -m venv .venv"
    echo "  source .venv/bin/activate"
    echo "  python -m pip install -r requirements.txt"
    exit 1
fi

# 1. Docker daemon
if ! docker_ping; then
    echo "▶ Starting Docker Desktop…"
    open -a Docker
    for i in $(seq 1 45); do
        docker_ping && break
        sleep 2
        [ $((i % 10)) -eq 0 ] && echo "  … still waiting for the Docker engine (${i}x2s)"
    done
    if ! docker_ping; then
        echo "✗ The Docker engine did not respond within 90 seconds."
        if pgrep -f "com.docker.backend" >/dev/null 2>&1; then
            echo "  Docker Desktop is running but its engine socket is not answering"
            echo "  (a wedged backend). Fix it with:"
            echo "      pkill -f com.docker.backend && open -a Docker"
            echo "  then rerun ./start.sh"
        else
            echo "  Open Docker Desktop manually, wait for the whale icon, then rerun ./start.sh"
        fi
        exit 1
    fi
fi
echo "✓ Docker engine is responding"

# 2. LocalStack container (create on first use, start thereafter)
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "✓ ${CONTAINER} already running"
elif docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    echo "▶ Starting existing ${CONTAINER} container…"
    docker start "$CONTAINER" >/dev/null
else
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "▶ Pulling ${IMAGE} (~1.9 GB, one-time — progress below)…"
        docker pull "$IMAGE"
    fi
    echo "▶ Creating ${CONTAINER} container…"
    docker run -d --name "$CONTAINER" -p 4566:4566 \
        -v /var/run/docker.sock:/var/run/docker.sock "$IMAGE" >/dev/null
fi

printf "▶ Waiting for LocalStack health"
HEALTHY=0
for _ in $(seq 1 45); do
    if curl -s --max-time 2 http://localhost:4566/_localstack/health >/dev/null; then
        HEALTHY=1
        break
    fi
    printf "."
    sleep 2
done
echo
if [ "$HEALTHY" = "1" ]; then
    echo "✓ LocalStack is healthy on http://localhost:4566"
else
    echo "⚠ LocalStack did not become healthy within 90 seconds."
    echo "  Recent container logs:"
    docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/    /'
    echo "  The app will show LocalStack as offline until it recovers."
fi

# 3. CloudForge
echo "▶ Launching CloudForge…"
exec "$ENV_DIR/bin/streamlit" run app.py
