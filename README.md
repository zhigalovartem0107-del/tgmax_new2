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

## Layout

```
main.py            entry point: DB init, middlewares, routers, polling
config.py          settings from environment / .env
database/
  engine.py        async engine, session factory, init_db()
  models.py        ORM models (User)
  crud.py          queries and atomic balance operations
middlewares/
  db.py            one DB session per update, committed on success
  user.py          registers the sender, passes it to handlers as db_user
handlers/
  start.py         /start, Start button, help
  payments.py      balance, top-ups, subscriptions
  generation.py    song / video generation flow
keyboards/
  main_menu.py     main menu buttons and typed callback payloads
services/
  music.py         song generation API client
  video.py         video generation API client
  yookassa.py      YooKassa payments client
```

Handlers get dependencies by parameter name: `session` (SQLAlchemy `AsyncSession`),
`db_user` (`database.models.User`), `context` (FSM state).
