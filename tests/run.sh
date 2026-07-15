#!/usr/bin/env bash
# Единственный способ гонять тесты этого проекта.
# Локальный python на macOS отрисует шрифты иначе — эталоны разойдутся.
set -euo pipefail

cd "$(dirname "$0")/.."

docker build --platform=linux/amd64 -f Dockerfile.test -t omanko-test . >/dev/null

exec docker run --rm --platform=linux/amd64 \
    -e PYTHONPATH=/app \
    -e GOLDEN_UPDATE="${GOLDEN_UPDATE:-}" \
    -v "$PWD:/app" \
    -w /app \
    omanko-test \
    python -m pytest tests/ "$@"
