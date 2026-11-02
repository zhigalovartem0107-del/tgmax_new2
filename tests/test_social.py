"""services/social: VK and TikTok fetchers on mocked HTTP (no network)."""

import base64
import hashlib
import json
from types import SimpleNamespace

import httpx2
import pytest

from services.social import FallbackFetcher, SocialError, SocialFetcher, SocialPost, make_fetcher
from services.social.tiktok import TikTokEmbedFetcher, TikTokWebFetcher
from services.social.vk import VKApiFetcher, VKWebFetcher


def client(handler, cookies=None):
    headers = {"Cookie": cookies} if cookies else None
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers=headers)


class Broken(SocialFetcher):
    name = "broken"

    async def fetch(self, account, limit=10):
        raise SocialError("asks to log in")


class Fixed(SocialFetcher):
    name = "fixed"

    async def fetch(self, account, limit=10):
        return [SocialPost(id="1", text="ok", url="https://x/1")]


async def test_fallback_uses_the_next_source_and_explains_failures():
    assert [p.text for p in await FallbackFetcher([Broken(), Fixed()]).fetch("acc")] == ["ok"]
    with pytest.raises(SocialError) as err:
        await FallbackFetcher([Broken(), Broken()]).fetch("acc")
    assert str(err.value).count("asks to log in") == 2


async def test_vk_api():
    def handler(request):
        assert request.url.path == "/method/wall.get" and request.url.params["domain"] == "durov"
        return httpx2.Response(200, json={"response": {"items": [
            {"id": 5, "owner_id": -1, "date": 1700000000, "text": "Пост [id1|Павел]",
             "attachments": [{"type": "photo", "photo": {"sizes": [{"width": 10, "height": 10, "url": "s"},
                                                                    {"width": 800, "height": 600, "url": "big"}]}}]},
            {"id": 6, "owner_id": -1, "date": 1700000100, "text": "Реклама", "marked_as_ads": 1}]}})
    posts = await VKApiFetcher("token", http_client=client(handler)).fetch("durov")
    assert [(p.id, p.text, p.images) for p in posts] == [("-1_5", "Пост Павел", ("big",))]
    assert posts[0].url == "https://vk.com/wall-1_5" and posts[0].created_at.year == 2023


async def test_vk_api_error():
    fetcher = VKApiFetcher("bad", http_client=client(lambda r: httpx2.Response(
        200, json={"error": {"error_code": 5, "error_msg": "User authorization failed"}})))
    with pytest.raises(SocialError, match="authorization failed"):
        await fetcher.fetch("durov")


VK_HTML = """<html><body>
<div class="wall_item" data-post-id="-123_456"><div class="pi_text">Новый трек<br>слушай &amp; делись</div>
<div class="thumb_map_img" data-src_big="https://sun9.userapi.com/big1.jpg|604|453"></div></div>
<div class="wall_item" data-post-id="-123_455"><div class="pi_text">Старый пост</div></div>
</body></html>"""


async def test_vk_web_with_cookies():
    def handler(request):
        assert request.headers["cookie"] == "remixsid=abc" and request.url.host == "m.vk.com"
        return httpx2.Response(200, text=VK_HTML)
    posts = await VKWebFetcher("remixsid=abc", http_client=client(handler, "remixsid=abc")).fetch("mygroup")
    assert posts[0] == SocialPost(id="-123_456", text="Новый трек\nслушай & делись", url="https://vk.com/wall-123_456",
                                  images=("https://sun9.userapi.com/big1.jpg",))
    assert posts[1].text == "Старый пост" and posts[1].images == ()


async def test_vk_web_login_page_is_an_error():
    fetcher = VKWebFetcher("x", http_client=client(lambda r: httpx2.Response(200, text="<form id='login'>")))
    with pytest.raises(SocialError, match="VK_COOKIES"):
        await fetcher.fetch("mygroup")


def _waf(prefix: bytes, n: int) -> str:
    return base64.b64encode(json.dumps({"v": {"a": base64.b64encode(prefix).decode(), "b": 1,
                                              "c": base64.b64encode(hashlib.sha256(prefix + str(n).encode()).digest()).decode()},
                                        "s": "sig"}).encode()).decode()


async def test_tiktok_embed_solves_challenge():
    vid = str((1761003600 << 32) + 9)
    state = {"source": {"data": {"/embed/@artist": {"userInfo": {"privateAccount": False}, "videoList": [
        {"id": vid, "desc": "Новый клип", "coverUrl": "https://p16/c.jpg", "playAddr": "https://v16/v.mp4",
         "authorUniqueId": "artist"}]}}}}
    cs = _waf(b"prefix-123", 7)

    def handler(request):
        cookie = request.headers.get("cookie", "")
        if "_wafchallengeid=" not in cookie:
            return httpx2.Response(200, text=f'<p id="wci" class="_wafchallengeid"></p><p id="cs" class="{cs}"></p>')
        solved = json.loads(base64.b64decode(cookie.split("_wafchallengeid=")[1]))
        assert base64.b64decode(solved["d"]) == b"7"
        return httpx2.Response(200, text='<script id="__FRONTITY_CONNECT_STATE__" type="application/json">'
                                         + json.dumps(state) + "</script>")
    posts = await TikTokEmbedFetcher(http_client=client(handler)).fetch("@artist")
    assert posts == [SocialPost(id=vid, text="Новый клип", url=f"https://www.tiktok.com/@artist/video/{vid}",
                                created_at=posts[0].created_at, images=("https://p16/c.jpg",),
                                video_url="https://v16/v.mp4")]
    assert int(posts[0].created_at.timestamp()) == 1761003600


@pytest.mark.parametrize("fmt", ["sigi", "universal"])
async def test_tiktok_web_with_cookies(fmt):
    item = {"id": "7400000000000000001", "desc": "Клип", "createTime": 1761000000,
            "video": {"cover": "https://p16/c1.jpg", "playAddr": "https://v16/1.mp4"}, "author": "artist"}
    if fmt == "sigi":
        page = ('<script id="SIGI_STATE" type="application/json">'
                + json.dumps({"ItemModule": {item["id"]: item}}) + "</script>")
    else:
        page = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                + json.dumps({"__DEFAULT_SCOPE__": {"webapp.user-detail": {"itemList": [item]}}}) + "</script>")

    def handler(request):
        assert request.headers["cookie"] == "sessionid=s1" and request.url.path == "/@artist"
        return httpx2.Response(200, text=page)
    posts = await TikTokWebFetcher("sessionid=s1", http_client=client(handler, "sessionid=s1")).fetch("artist")
    assert [(p.id, p.text, p.video_url) for p in posts] == [(item["id"], "Клип", "https://v16/1.mp4")]


def test_make_fetcher_builds_chains_from_settings():
    s = SimpleNamespace(vk_token=None, vk_cookies=None, tiktok_cookies=None)
    assert make_fetcher("vk", s) is None                        # nothing configured for VK
    s.vk_cookies = "remixsid=abc"
    assert [f.name for f in make_fetcher("vk", s).fetchers] == ["vk-web"]
    s.vk_token = "t"
    assert [f.name for f in make_fetcher("vk", s).fetchers] == ["vk-api", "vk-web"]
    assert [f.name for f in make_fetcher("tiktok", s).fetchers] == ["tiktok-embed"]
    s.tiktok_cookies = "sessionid=1"
    assert [f.name for f in make_fetcher("tiktok", s).fetchers] == ["tiktok-embed", "tiktok-web"]
