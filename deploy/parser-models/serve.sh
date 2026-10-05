#!/usr/bin/env bash
# Start one parser worker container per backend against the shared /models root.
# Usage: serve.sh <backend> <port> [gpu]
set -euo pipefail
backend="${1:?backend required}"
port="${2:?port required}"
gpu="${3:-0}"
root="/home/t-wanghai/models/openviking-parsers"

if [ ! -f "$root/secrets/parser.key" ]; then
  mkdir -p "$root/secrets"
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$root/secrets/parser.key"
  chmod 600 "$root/secrets/parser.key"
fi

name="ov-parser-worker-$backend"
docker rm "$name" >/dev/null 2>&1 || true

engine_key=""
if [ -f "$root/secrets/parser.key" ]; then
  engine_key="$(cat "$root/secrets/parser.key")"
fi

exec docker run -d --name "$name" \
  --gpus "device=$gpu" \
  --restart unless-stopped \
  -e PARSER_ROOT=/models \
  -e PARSER_BACKEND="$backend" \
  -e PARSER_MINERU_ENGINE_KEY="$engine_key" \
  -p "127.0.0.1:$port:9100" \
  -v "$root:/models" \
  -w /models \
  --entrypoint /models/envs/http/bin/python \
  vllm/vllm-openai:v0.27.1 \
  -m uvicorn worker:app --host 0.0.0.0 --port 9100
