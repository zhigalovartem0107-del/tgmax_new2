# MAX song & video bot

A paid bot for the MAX messenger that generates songs and videos.

## Run

Requires Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env   # then set MAX_BOT_TOKEN
python main.py
```

Data is stored in `data/bot.db` (SQLite) unless `DATABASE_URL` points to PostgreSQL.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Layout

```
main.py            entry point: DB init, middlewares, routers, polling
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
  payments.py      balance, top-ups, subscriptions
  song.py          song FSM: mode → style → text → confirm → charge → submit
  video.py         video generation flow
  fallback.py      answers taps on outdated buttons
keyboards/
  main_menu.py     main menu buttons and typed callback payloads
  song.py          song flow buttons and styles
services/
  music.py         song generation API client
  video.py         video generation API client
  yookassa.py      YooKassa payments client
tests/             pytest suite (real dispatcher, stubbed HTTP)
```

Handlers get dependencies by parameter name: `session` (SQLAlchemy `AsyncSession`),
`db_user` (`database.models.User`), `context` (FSM state), `music` (`MusicService`).
