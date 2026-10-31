"""Song generation provider client (provider not chosen yet)."""


class MusicService:
    def __init__(self, api_url: str | None, api_key: str | None) -> None:
        self.api_url = api_url
        self.api_key = api_key

    async def generate_song(self, prompt: str, style: str, custom_lyrics: bool) -> str:
        """Submit a generation job and return the provider's task id.

        prompt is the user's lyrics when custom_lyrics is True, otherwise a
        description the provider writes lyrics from.
        """
        raise NotImplementedError("Music provider is not configured")
