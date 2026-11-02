"""Social network sources (VK, TikTok) behind one interface.

make_fetcher(network, settings) builds a chain: the official/anonymous source first, then the owner's
logged-in session (cookies from VK_COOKIES / TIKTOK_COOKIES) as a fallback. Using a personal session to read
pages may break the site's terms — use an account you can afford to lose.
"""

from services.social.base import FallbackFetcher, HttpFetcher, SocialError, SocialFetcher, SocialPost
from services.social.tiktok import TikTokEmbedFetcher, TikTokWebFetcher
from services.social.vk import VKApiFetcher, VKWebFetcher

__all__ = [
    "FallbackFetcher",
    "HttpFetcher",
    "SocialError",
    "SocialFetcher",
    "SocialPost",
    "TikTokEmbedFetcher",
    "TikTokWebFetcher",
    "VKApiFetcher",
    "VKWebFetcher",
    "make_fetcher",
]


def _secret(value) -> str | None:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def make_fetcher(network: str, settings) -> FallbackFetcher | None:
    """The source chain for "vk" or "tiktok", from settings. None — nothing configured for that network."""
    chain = []
    if network == "vk":
        if _secret(settings.vk_token):
            chain.append(VKApiFetcher(_secret(settings.vk_token)))
        if _secret(settings.vk_cookies):
            chain.append(VKWebFetcher(_secret(settings.vk_cookies)))
    elif network == "tiktok":
        chain.append(TikTokEmbedFetcher())
        if _secret(settings.tiktok_cookies):
            chain.append(TikTokWebFetcher(_secret(settings.tiktok_cookies)))
    else:
        raise ValueError(f"unknown network {network!r}")
    return FallbackFetcher(chain) if chain else None
