#!/usr/bin/env bash
set -euo pipefail

HOST="${MCP_HOST:-127.0.0.1}"
PORT="${MCP_PORT:-17432}"

python - <<PY
import json
import urllib.error
import urllib.request

url = "http://${HOST}:${PORT}/health"
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        print(json.dumps({"url": url, "status": response.status}))
        raise SystemExit(0 if response.status == 200 else 1)
except urllib.error.URLError as exc:
    print(json.dumps({"url": url, "error": str(exc)}))
    raise SystemExit(1)
PY

