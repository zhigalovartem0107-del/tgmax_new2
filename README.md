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
  models.py        ORM models (User, Generation)
  crud.py          queries, atomic balance operations, paid generations
middlewares/
  db.py            one DB session per update, committed on success
  user.py          registers the sender, passes it to handlers as db_user
  services.py      passes service clients to handlers (music, ...)
handlers/
  start.py         /start, /cancel, Start button, main menu, help
  admin.py         /admin stats, /add_balance (ADMIN_IDS only)
  payments.py      balance, top-ups, subscriptions
  song.py          song FSM: mode → style → text → confirm → AI lyrics → charge → submit
  video.py         video generation flow
  fallback.py      answers taps on outdated buttons
keyboards/
  main_menu.py     main menu buttons and typed callback payloads
  song.py          song flow buttons and styles
services/
  ai.py            OpenRouter client that writes the lyrics
  music.py         fal.ai client for ACE-Step songs
  video.py         video generation API client
  yookassa.py      YooKassa payments client
workers/
  song_delivery.py sends finished songs, refunds failed or stuck orders
tests/             pytest suite (real dispatcher, stubbed HTTP)
```

Handlers get dependencies by parameter name: `session` (SQLAlchemy `AsyncSession`),
`db_user` (`database.models.User`), `context` (FSM state), `music` (`MusicService`),
`ai` (`AIService`).
