# MAX song & video bot

A paid bot for the MAX messenger that generates songs and videos.

## Run

Requires Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env   # then set MAX_BOT_TOKEN and OPENROUTER_API_KEY
python main.py
```

Data is stored in `data/bot.db` (SQLite) unless `DATABASE_URL` points to PostgreSQL.
Logs, including startup errors, are also written to `data/bot.log`: open it in the
host's file manager when the host's log viewer shows nothing.

## Admin commands

Only for MAX user ids listed in `ADMIN_IDS` (comma-separated). To find your id,
send `/admin` to the bot: without access it replies with your id.

- `/admin`: users, generations and balance totals
- `/admin` → **🔑 API Ключи**: set or remove `ACEMUSIC_API_KEY`, `VK_TOKEN`, `VK_COOKIES`,
  `TIKTOK_COOKIES`. A key saved there wins over the environment variable and is used at
  once, without a restart; 🗑 goes back to the environment variable. (The VK/TikTok keys are
  kept for the social sources, which aren't connected to the bot yet.) Keys are shown masked
  (last 4 characters), and the message with a key is deleted from the chat. They are stored
  in the `api_keys` table of the database (`data/bot.db`), unencrypted, like the host's
  environment variables, so keep the database private.
- `/add_balance <amount>` (or `/add_credits`): top up your own balance

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Layout

```
main.py            entry point: logging (console + data/bot.log), then starts app
app.py             the bot: dispatcher, services, command menu, polling
config.py          settings from environment / .env
database/
  engine.py        async engine, session factory, init_db()
  models.py        ORM models (User, Payment, Generation, ApiKey)
  crud.py          queries, atomic balance operations, paid generations
middlewares/
  db.py            one DB session per update, committed on success
  user.py          registers the sender, passes it to handlers as db_user
  services.py      passes service clients to handlers (music, ...)
handlers/
  start.py         /start, /cancel, Start button, main menu, help
  admin.py         /admin stats and API keys, /add_balance (ADMIN_IDS only)
  payments.py      balance, top-ups, subscriptions
  song.py          song FSM: mode → style → text → confirm → AI lyrics → charge → submit
  video.py         video generation flow
  fallback.py      answers taps on outdated buttons
keyboards/
  admin.py         admin panel buttons
  main_menu.py     main menu buttons and typed callback payloads
  song.py          song flow buttons and styles
services/
  ai.py            OpenRouter client that writes the lyrics
  keys.py          API keys saved in the bot, falling back to the environment
  music.py         acemusic.ai client for ACE-Step songs
  video.py         video generation API client
  yookassa.py      YooKassa payments client
workers/
  song_delivery.py sends finished songs, refunds failed or stuck orders
tests/             pytest suite (real dispatcher, stubbed HTTP)
```

Handlers get dependencies by parameter name: `session` (SQLAlchemy `AsyncSession`),
`db_user` (`database.models.User`), `context` (FSM state), `music` (`MusicService`),
`ai` (`AIService`), `keys` (`services.keys.ApiKeys`).
