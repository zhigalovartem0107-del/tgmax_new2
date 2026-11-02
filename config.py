"""Application settings, loaded from environment variables and an optional .env file."""

from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- MAX bot ---
    # Accept the names the old bot and the hosting panel already use.
    bot_token: SecretStr = Field(
        validation_alias=AliasChoices("MAX_BOT_TOKEN", "BOT_TOKEN", "MAX_TOKEN"),
    )
    # MAX user ids with access to admin features. Comma-separated: "123,456".
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Database ---
    # SQLite by default; for PostgreSQL use postgresql+asyncpg://user:pass@host:5432/db
    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR / 'bot.db'}"
    database_echo: bool = False

    # --- Payments (YooKassa) ---
    yookassa_shop_id: str | None = None
    yookassa_secret_key: SecretStr | None = None
    # The shop's own email for the 54-FZ receipt when the buyer has no email (optional).
    yookassa_receipt_email: str | None = None
    # Legal documents shown before payment.
    terms_url: str = ""
    privacy_url: str = ""
    # Balance packages for /shop: "units:rubles,units:rubles".
    shop_packages: str = "5:199,15:499,40:990"

    # --- Generation providers ---
    music_api_url: str | None = None
    music_api_key: SecretStr | None = None
    video_api_url: str | None = None
    video_api_key: SecretStr | None = None

    # --- AI lyrics (OpenRouter) ---
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "openrouter/free"

    # --- Pricing (balance units) ---
    song_price: int = Field(default=1, ge=1)

    log_level: str = "INFO"

    @property
    def packages(self) -> list[tuple[int, int]]:
        """[(balance units, price in rubles)] from SHOP_PACKAGES."""
        result = []
        for part in self.shop_packages.replace(";", ",").split(","):
            if ":" in part:
                units, rubles = part.split(":", 1)
                result.append((int(units), int(rubles)))
        return result

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value


try:
    settings = Settings()
except ValidationError as exc:
    raise SystemExit(
        "Configuration error. Set MAX_BOT_TOKEN in the environment or in .env "
        f"(see .env.example).\n{exc}"
    ) from exc
