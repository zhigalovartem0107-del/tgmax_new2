"""Song generation provider client (provider not chosen yet)."""


class MusicService:
    def __init__(self, api_url: str | None, api_key: str | None) -> None:
        self.api_url = api_url
        self.api_key = api_key

    async def generate_song(self, lyrics: str, style: str) -> str:
        """Submit a generation job for these lyrics and return the provider's task id."""
        raise NotImplementedError("Music provider is not configured")
