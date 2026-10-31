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

    # --- Generation providers ---
    music_api_url: str | None = None
    music_api_key: SecretStr | None = None
    video_api_url: str | None = None
    video_api_key: SecretStr | None = None

    # --- Pricing (balance units) ---
    song_price: int = Field(default=1, ge=1)

    log_level: str = "INFO"

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
