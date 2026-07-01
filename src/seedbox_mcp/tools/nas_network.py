from __future__ import annotations

import asyncio
import json
from typing import Any

from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool

# The NAS's own internet-facing connection — distinct from ROOM, which is a
# separate box on the home network. speedtest-cli is already installed there;
# this SSHes in and runs the fixed command below, nothing model-supplied ever
# reaches the shell (no interpolation, no arbitrary args), so there's no
# injection surface despite this being the one tool in this repo that reaches
# a remote host via SSH rather than a REST API.
NAS_SSH_HOST = "nas.taila28611.ts.net"
SPEEDTEST_TIMEOUT_S = 45.0  # a real run took ~5s; generous margin for a busy period


async def nas_internet_speed_test() -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                NAS_SSH_HOST,
                "speedtest-cli",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SPEEDTEST_TIMEOUT_S)
        except TimeoutError:
            return ToolResponse.failure(
                "timeout", f"Speed test did not complete within {SPEEDTEST_TIMEOUT_S:.0f}s."
            )
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace")[:500] or "speedtest-cli exited non-zero with no stderr."
            return ToolResponse.failure("speedtest_failed", detail)
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return ToolResponse.failure("bad_output", "speedtest-cli did not return valid JSON.")

        return ToolResponse.success(
            {
                "download_mbps": round(data.get("download", 0) / 1_000_000, 1),
                "upload_mbps": round(data.get("upload", 0) / 1_000_000, 1),
                "ping_ms": round(data.get("ping", 0), 1),
                "isp": data.get("client", {}).get("isp"),
                "test_server": data.get("server", {}).get("name"),
                "tested_from": "the NAS itself, not ROOM — this is the NAS's own internet connection",
            }
        )

    return await safe_tool(run)
