from __future__ import annotations

from typing import Any, cast

import httpx

from whatbox_media_mcp.errors import UpstreamError


class ArrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        result = await self._request("GET", path, params=params)
        return {} if result is None else result

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._request("POST", path, json=payload)
        return result if isinstance(result, dict) else {"result": result}

    async def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        result = await self._request("DELETE", path, params=params)
        return result if isinstance(result, dict) else None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        clean_path = "/" + path.lstrip("/")
        url = f"{self.base_url}{clean_path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                response = await client.request(method, url, params=params, json=json)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise UpstreamError(
                "upstream_unreachable",
                "Upstream service is unreachable.",
                {"path": clean_path, "reason": exc.__class__.__name__},
            ) from exc

        if response.status_code in {401, 403}:
            raise UpstreamError(
                "upstream_auth",
                "Upstream service rejected credentials.",
                {"path": clean_path, "status_code": response.status_code},
            )
        if response.status_code == 404:
            raise UpstreamError("not_found", "Upstream item was not found.", {"path": clean_path})
        if response.is_error:
            details: dict[str, Any] = {"path": clean_path, "status_code": response.status_code}
            if response.content:
                try:
                    details["body"] = response.json()
                except Exception:
                    details["body"] = response.text[:500]
            raise UpstreamError(
                "upstream_unreachable",
                "Upstream service returned an error.",
                details,
            )
        if response.status_code == 204 or not response.content:
            return None
        return cast(dict[str, Any] | list[Any] | None, response.json())
