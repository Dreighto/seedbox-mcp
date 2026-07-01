from __future__ import annotations

from typing import Any

from seedbox_mcp.runtime import Services
from seedbox_mcp.schemas import ToolResponse
from seedbox_mcp.tools.common import safe_tool


async def poster_ocr(services: Services, image_b64: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        if not services.apple_ocr:
            return ToolResponse.failure("ocr_unavailable", "apple-node OCR is not configured.")
        result = await services.apple_ocr.ocr(image_b64)
        texts = result.get("texts", []) if isinstance(result, dict) else []

        def _bbox_height(item: dict[str, Any]) -> float:
            bbox = item.get("bbox") or []
            if len(bbox) < 4:
                return 0.0
            ys = [pt[1] for pt in bbox if isinstance(pt, list) and len(pt) == 2]
            return (max(ys) - min(ys)) if ys else 0.0

        # Largest text on a poster is almost always the title — sorting by
        # bbox height (not reading order) gives the model a strong hint
        # about which extracted line to treat as the title candidate,
        # without the model needing to reason about pixel geometry itself.
        ranked = sorted(
            (t for t in texts if isinstance(t, dict) and t.get("text")),
            key=_bbox_height,
            reverse=True,
        )
        return ToolResponse.success(
            {
                "texts_by_prominence": [
                    {"text": t.get("text"), "confidence": t.get("confidence")} for t in ranked
                ],
                "note": "First entry is the largest text on the image — usually the title, "
                "but verify it makes sense as a title rather than assuming.",
            }
        )

    return await safe_tool(run)
