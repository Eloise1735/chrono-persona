from __future__ import annotations

import httpx


class WebSearchClient:
    def __init__(self, api_base: str, api_key: str):
        self.api_base = str(api_base or "").rstrip("/")
        self.api_key = str(api_key or "")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, max_results: int = 5) -> list[dict]:
        if not self.api_base or not self.api_key or not str(query or "").strip():
            return []
        base_lower = self.api_base.lower()
        if "tavily" in base_lower:
            return await self._search_tavily(query, max_results=max_results)
        return await self._search_generic(query, max_results=max_results)

    async def _search_tavily(self, query: str, max_results: int = 5) -> list[dict]:
        url = self.api_base
        if not url.endswith("/search"):
            url = f"{url}/search"
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max(1, int(max_results or 5)),
            "search_depth": "advanced",
            "include_answer": True,
            "include_raw_content": True,
        }
        response = await self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("results") or []
        normalized: list[dict] = []
        if isinstance(data.get("answer"), str) and data.get("answer", "").strip():
            normalized.append(
                {
                    "title": "Tavily Answer",
                    "url": "",
                    "snippet": str(data.get("answer") or "").strip(),
                    "content": str(data.get("answer") or "").strip(),
                }
            )
        if not isinstance(items, list):
            return normalized
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("content") or item.get("snippet") or ""),
                    "content": str(item.get("raw_content") or item.get("content") or item.get("snippet") or ""),
                }
            )
        return normalized

    async def _search_generic(self, query: str, max_results: int = 5) -> list[dict]:
        url = self.api_base
        if not url.endswith("/search"):
            url = f"{url}/search"
        payload = {
            "query": query,
            "max_results": max(1, int(max_results or 5)),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response = await self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            items = data.get("results") or data.get("data") or []
        else:
            items = data
        if not isinstance(items, list):
            return []
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("snippet") or item.get("content") or ""),
                    "content": str(item.get("content") or item.get("snippet") or ""),
                }
            )
        return normalized
