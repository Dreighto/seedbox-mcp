from __future__ import annotations

from typing import Any, cast

import httpx

from seedbox_mcp.errors import UpstreamError


class AppleOcrClient:
    """apple-node's mac-ocr service (macOS Vision framework, port 18772) —
    a drop-in replacement for the decommissioned jetson-ocr, same contract:
    POST /ocr {image_b64} -> {texts:[{text,confidence,bbox}], image_size,
    ocr_latency_ms, model_status}. No auth — tailnet-private."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def ocr(self, image_b64: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{self.base_url}/ocr", json={"image_b64": image_b64})
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable", "apple-node OCR service is unreachable.", {"reason": exc.__class__.__name__}
            ) from exc
        if response.is_error:
            raise UpstreamError(
                "upstream_unreachable",
                "apple-node OCR service returned an error.",
                {"status_code": response.status_code, "body": response.text[:500]},
            )
        return cast(dict[str, Any], response.json())
