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

AI lyrics: the models in `OPENROUTER_MODEL` are tried in order, each for at most
`OPENROUTER_TIMEOUT` seconds (default 30), so an answer or an error comes in about
a minute and a half at worst. When OpenRouter answers 429 (free models: about 20
requests a minute, 50 a day on an account with no credits bought), the user is told
when to try again. The bot itself limits only the drafts per order (`LYRICS_DRAFTS_MAX`).

Video: Magic Hour (key in `MAGIC_HOUR_API_KEY`, from https://magichour.ai/developer). 🎬 asks
for a description and an optional photo for the first frame, charges `VIDEO_PRICE` (default:
`SONG_PRICE`) and starts the video; `workers/video_delivery.py` checks it every 20 seconds,
sends the finished video, and refunds one that failed or took over 30 minutes. The default
is `ltx-2.5` (the fastest), 480p, 4 seconds: 24 credits a second, so 96 credits a video,
which fits the 100 free daily credits (`MAGIC_HOUR_VIDEO_MODEL`, `MAGIC_HOUR_VIDEO_SECONDS`
change it, e.g. `wan-2.2`, `kling-3.0`, `veo3.1`, `sora-2`). A photo is uploaded to Magic
Hour's storage first, since the API takes links only with a file extension. When Magic Hour
answers 429, the order is refunded, the user is told when to try again (Retry-After, else a
minute), and no order is sent before that. Without a key, 🎬 says video is in development
and charges nothing.

## Admin commands

Only for MAX user ids listed in `ADMIN_IDS` (comma-separated). To find your id,
send `/admin` to the bot: without access it replies with your id.

- `/admin`: users, generations and balance totals
- `/admin` → **🔑 API Ключи**: set or remove `ACEMUSIC_API_KEY`, `MAGIC_HOUR_API_KEY`, `VK_TOKEN`,
  `VK_COOKIES`, `TIKTOK_COOKIES`. A key saved there wins over the environment variable and is used at
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
  video.py         video FSM: description → optional photo → confirm → charge → submit
  common.py        answering a tap, "try again in N min" wording
  fallback.py      answers taps on outdated buttons
keyboards/
  admin.py         admin panel buttons
  main_menu.py     main menu buttons and typed callback payloads
  song.py          song flow buttons and styles
  video.py         video flow buttons
services/
  ai.py            OpenRouter client that writes the lyrics
  keys.py          API keys saved in the bot, falling back to the environment
  music.py         acemusic.ai client for ACE-Step songs
  video.py         Magic Hour client for videos
  ratelimit.py     reads how long a 429 answer says to wait
  yookassa.py      YooKassa payments client
workers/
  song_delivery.py  sends finished songs, refunds failed or stuck orders
  video_delivery.py sends finished videos, refunds failed or stuck orders
tests/             pytest suite (real dispatcher, stubbed HTTP)
```

Handlers get dependencies by parameter name: `session` (SQLAlchemy `AsyncSession`),
`db_user` (`database.models.User`), `context` (FSM state), `music` (`MusicService`),
`ai` (`AIService`), `video` (`VideoService`), `keys` (`services.keys.ApiKeys`).
