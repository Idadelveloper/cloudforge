#!/bin/bash
# CloudForge one-command launcher: Docker -> LocalStack -> Streamlit.
set -e
cd "$(dirname "$0")"

CONTAINER=cloudforge-localstack
IMAGE=localstack/localstack:4.6
ENV_DIR=

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
if ! docker info >/dev/null 2>&1; then
    echo "▶ Starting Docker Desktop…"
    open -a Docker
    for _ in $(seq 1 60); do
        docker info >/dev/null 2>&1 && break
        sleep 1
    done
    if ! docker info >/dev/null 2>&1; then
        echo "✗ Docker did not start. Open Docker Desktop manually, then rerun ./start.sh"
        exit 1
    fi
fi
echo "✓ Docker is running"

# 2. LocalStack container (create on first use, start thereafter)
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    :
elif docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    docker start "$CONTAINER" >/dev/null
else
    echo "▶ Creating LocalStack container…"
    docker run -d --name "$CONTAINER" -p 4566:4566 \
        -v /var/run/docker.sock:/var/run/docker.sock "$IMAGE" >/dev/null
fi

for _ in $(seq 1 30); do
    curl -s --max-time 2 http://localhost:4566/_localstack/health >/dev/null && break
    sleep 2
done
if curl -s --max-time 2 http://localhost:4566/_localstack/health >/dev/null; then
    echo "✓ LocalStack is healthy on http://localhost:4566"
else
    echo "⚠ LocalStack container started but not healthy yet — the app will show it as offline until it is."
fi

# 3. CloudForge
echo "▶ Launching CloudForge…"
exec "$ENV_DIR/bin/streamlit" run app.py
