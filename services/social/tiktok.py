"""TikTok: TikTok's own embed widget first (no login); the owner's logged-in session (cookies) as a fallback."""

import base64
import hashlib
import json
import re
from datetime import datetime, timezone

from services.social.base import HttpFetcher, SocialError, SocialPost

SITE = "https://www.tiktok.com"


def _user(account: str) -> str:
    """«https://www.tiktok.com/@artist», «@artist», «artist» -> «artist»."""
    m = re.search(r"@([\w.]+)", account)
    return m.group(1) if m else account.strip().rstrip("/").split("/")[-1]


def _script_json(page: str, script_id: str):
    m = re.search(rf'<script id="{script_id}"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(m.group(1)) if m else None


def solve_waf(cs: str) -> str:
    """TikTok's anti-bot puzzle (as in RSSHub): find n with sha256(a + n) == c; the answer goes into a cookie."""
    challenge = json.loads(base64.b64decode(cs))
    prefix, want = base64.b64decode(challenge["v"]["a"]), base64.b64decode(challenge["v"]["c"])
    for n in range(1_000_000):
        if hashlib.sha256(prefix + str(n).encode()).digest() == want:
            challenge["d"] = base64.b64encode(str(n).encode()).decode()
            return base64.b64encode(json.dumps(challenge, separators=(",", ":")).encode()).decode()
    raise SocialError("tiktok: anti-bot puzzle not solved")


class TikTokEmbedFetcher(HttpFetcher):
    """tiktok.com/embed/@user — the widget sites use to show a creator's videos; no login needed."""

    name = "tiktok-embed"

    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        user = _user(account)
        path = f"/embed/@{user}"
        page = (await self._get(SITE + path)).text
        m = re.search(r'id="cs"[^>]*class="([^"]+)"|class="([^"]+)"[^>]*id="cs"', page)
        if "_wafchallengeid" in page and m:
            cookie = "_wafchallengeid=" + solve_waf(next(g for g in m.groups() if g))
            page = (await self._get(SITE + path, headers={"Cookie": cookie})).text
        state = _script_json(page, "__FRONTITY_CONNECT_STATE__")
        if not state:
            raise SocialError(f"{self.name}: no profile data on the page")
        data = state.get("source", {}).get("data", {})
        box = data.get(path) or next((v for k, v in data.items() if k.lower() == path.lower()), {})
        if (box.get("userInfo") or {}).get("privateAccount"):
            raise SocialError(f"{self.name}: the account is private")
        posts = []
        for video in (box.get("videoList") or [])[:limit]:
            vid = str(video.get("id") or "")
            if not vid.isdigit():
                continue
            author = video.get("authorUniqueId") or user
            cover = video.get("coverUrl") or video.get("originCoverUrl")
            posts.append(SocialPost(
                id=vid, text=(video.get("desc") or "").strip(), url=f"{SITE}/@{author}/video/{vid}",
                created_at=datetime.fromtimestamp(int(vid) >> 32, timezone.utc),   # time is encoded in the id
                images=(cover,) if cover else (), video_url=video.get("playAddr") or None,
            ))
        if not posts:
            raise SocialError(f"{self.name}: no videos")
        return posts


class TikTokWebFetcher(HttpFetcher):
    """The profile page with the owner's logged-in cookies (TIKTOK_COOKIES): TikTok then embeds the video list in
    the page data. Reads both data formats TikTok has used (SIGI_STATE and __UNIVERSAL_DATA_FOR_REHYDRATION__)."""

    name = "tiktok-web"

    async def fetch(self, account: str, limit: int = 10) -> list[SocialPost]:
        user = _user(account)
        page = (await self._get(f"{SITE}/@{user}")).text
        items = []
        sigi = _script_json(page, "SIGI_STATE")
        if sigi:
            items = list((sigi.get("ItemModule") or {}).values())
        universal = _script_json(page, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
        if not items and universal:
            items = (universal.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}).get("itemList") or [])
        if not items:
            raise SocialError(f"{self.name}: no videos in the page — check TIKTOK_COOKIES (the session may have expired)")
        posts = []
        for item in sorted(items, key=lambda i: int(i.get("createTime") or 0), reverse=True)[:limit]:
            video = item.get("video") or {}
            author = item.get("author") if isinstance(item.get("author"), str) else (item.get("author") or {}).get("uniqueId")
            posts.append(SocialPost(
                id=str(item["id"]), text=(item.get("desc") or "").strip(),
                url=f"{SITE}/@{author or user}/video/{item['id']}",
                created_at=datetime.fromtimestamp(int(item["createTime"]), timezone.utc) if item.get("createTime") else None,
                images=(video["cover"],) if video.get("cover") else (), video_url=video.get("playAddr") or None,
            ))
        return posts
