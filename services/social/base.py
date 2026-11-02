"""Common interface for social network sources: a fetcher returns an account's latest posts."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import httpx2

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")


@dataclass(frozen=True)
class SocialPost:
    id: str
    text: str
    url: str
    created_at: datetime | None = None
    images: tuple[str, ...] = ()
    video_url: str | None = None


class SocialError(Exception):
    """The source could not give the posts (blocked, needs login, changed its page, network)."""


class SocialFetcher(ABC):
    name = "base"

    @abstractmethod
    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        """Latest posts of `account` (username, @username or link), newest first."""


class HttpFetcher(SocialFetcher):
    """A fetcher over HTTP. `cookies` is the owner's own logged-in browser session ("userbot" mode):
    the site then serves pages as to that logged-in user, which works where anonymous access is refused."""

    def __init__(self, cookies: str | None = None, http_client: httpx2.AsyncClient | None = None) -> None:
        headers = {"User-Agent": BROWSER_UA, "Accept-Language": "ru-RU,ru;q=0.9"}
        if cookies:
            headers["Cookie"] = cookies
        self._http = http_client or httpx2.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True)

    async def _get(self, url: str, **kwargs) -> httpx2.Response:
        try:
            response = await self._http.get(url, **kwargs)
        except httpx2.HTTPError as exc:
            raise SocialError(f"{self.name}: no connection ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise SocialError(f"{self.name}: answered {response.status_code}")
        return response

    async def close(self) -> None:
        await self._http.aclose()


class FallbackFetcher(SocialFetcher):
    """Tries sources in order; the first that returns posts wins. All failed → SocialError with every reason."""

    name = "fallback"

    def __init__(self, fetchers: list[SocialFetcher]) -> None:
        self.fetchers = fetchers

    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        reasons = []
        for fetcher in self.fetchers:
            try:
                return await fetcher.fetch(account, limit)
            except SocialError as exc:
                reasons.append(str(exc))
        raise SocialError("; ".join(reasons) or "no sources configured")

    async def close(self) -> None:
        for fetcher in self.fetchers:
            if hasattr(fetcher, "close"):
                await fetcher.close()
