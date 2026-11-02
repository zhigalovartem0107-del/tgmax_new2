"""VK: the official API with a service key first; the owner's logged-in session (cookies) as a fallback."""

import html
import re
from datetime import datetime, timezone

from services.social.base import HttpFetcher, SocialError, SocialPost


def _domain(account: str) -> str:
    """«https://vk.com/club_name», «vk.com/club_name», «@club_name» -> «club_name»."""
    return account.strip().rstrip("/").split("/")[-1].lstrip("@")


def _plain(text: str) -> str:
    """[id1|Павел] -> Павел."""
    return re.sub(r"\[(?:id|club|public)\d+\|([^\]]+)\]", r"\1", text or "").strip()


class VKApiFetcher(HttpFetcher):
    """wall.get with a VK service key (free: vk.com/apps → your app → «Сервисный ключ доступа»)."""

    name = "vk-api"
    API = "https://api.vk.com/method/wall.get"

    def __init__(self, token: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.token = token

    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        response = await self._get(
            self.API, params={"domain": _domain(account), "count": limit, "v": "5.199", "access_token": self.token}
        )
        data = response.json()
        if "error" in data:
            raise SocialError(f"{self.name}: {data['error'].get('error_msg')}")
        posts = []
        for item in data.get("response", {}).get("items", []):
            if item.get("marked_as_ads"):
                continue                                       # advertising posts are not content
            images = []
            for attachment in item.get("attachments") or []:
                sizes = (attachment.get("photo") or {}).get("sizes") or []
                if attachment.get("type") == "photo" and sizes:
                    images.append(max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))["url"])
            post_id = f"{item.get('owner_id')}_{item['id']}"
            posts.append(SocialPost(
                id=post_id, text=_plain(item.get("text")), url=f"https://vk.com/wall{post_id}",
                created_at=datetime.fromtimestamp(item["date"], timezone.utc) if item.get("date") else None,
                images=tuple(images),
            ))
        return posts


class VKWebFetcher(HttpFetcher):
    """The mobile site m.vk.com with the owner's logged-in cookies (VK_COOKIES): works without an API key and for
    walls VK hides from anonymous visitors. Parses the page markup, so it may need updating if VK changes it."""

    name = "vk-web"
    POST = re.compile(r'<div class="wall_item"[^>]*data-post-id="(-?\d+_\d+)"')

    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        page = (await self._get(f"https://m.vk.com/{_domain(account)}")).text
        starts = [(m.start(), m.group(1)) for m in self.POST.finditer(page)]
        if not starts:
            raise SocialError(f"{self.name}: no posts on the page — check VK_COOKIES (the session may have expired)")
        posts = []
        for k, (pos, post_id) in enumerate(starts[:limit]):
            chunk = page[pos: starts[k + 1][0] if k + 1 < len(starts) else len(page)]
            m = re.search(r'<div class="pi_text[^"]*">(.*?)</div>', chunk, re.S)
            text = re.sub(r"<br\s*/?>", "\n", m.group(1)) if m else ""
            text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
            images = tuple(src.split("|")[0] for src in re.findall(r'data-src_big="([^"]+)"', chunk))
            posts.append(SocialPost(id=post_id, text=text, url=f"https://vk.com/wall{post_id}", images=images))
        return posts
