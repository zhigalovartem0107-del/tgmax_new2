"""Song flow: mode → style → text → confirm → (AI lyrics drafts) → charge → submit to the provider.

Every button tap is answered at once by removing that message's buttons (its text
stays), so a slow step can't be triggered twice; results arrive as new messages.
Answering the tap and the "typing…" indicator are cosmetic: when MAX fails or is
slow to do them, the flow goes on without them.
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from maxapi import F, Router
from maxapi.context import BaseContext, State, StatesGroup
from maxapi.types import MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import GenerationKind, SongMode, User
from keyboards import (
    CUSTOM_STYLE,
    SONG_STYLES,
    MenuAction,
    MenuCallback,
    SongAction,
    SongCallback,
    cancel_kb,
    confirm_kb,
    insufficient_funds_kb,
    lyrics_kb,
    main_menu_kb,
    mode_kb,
    style_kb,
)
from services import MusicService
from services.ai import AIService, LyricsError, LyricsRateLimited

logger = logging.getLogger(__name__)
router = Router(router_id="song")

STYLE_MAX = 120
LYRICS_MAX = 3000
DESCRIPTION_MAX = 500
PREVIEW_MAX = 300
TYPING_INTERVAL = 4.0  # seconds between "typing…" refreshes; MAX shows one for about 5 s
ANSWER_TIMEOUT = 10.0  # seconds to wait for MAX to take a tap's answer before going on without it

MODE_LABELS = {SongMode.LYRICS: "свой текст", SongMode.DESCRIPTION: "текст от ИИ"}

WRITING = "✍️ Пишу текст песни…"
REWRITING = "✍️ Пишу новый вариант…"
DRAFT_FAILED = "😔 Не получилось написать текст. Попробуй ещё раз."
REDRAFT_FAILED = (
    "😔 Не получилось написать новый текст. "
    "Можно создать песню с прошлым или попробовать ещё раз."
)
LAST_DRAFT = "ℹ️ Это последний вариант для этого заказа: создай песню с ним или начни заказ заново."
BUSY = "⏳ Бесплатная нейросеть сейчас перегружена: слишком много запросов. Попробуй {when}."
USE_LATEST = "Используй кнопки под последним текстом."
SUBMITTED = "🎶 Песня создаётся! Пришлю её сюда, как только будет готова."
SUBMIT_FAILED = "😔 Не удалось запустить генерацию. Средства вернулись на баланс."


class SongForm(StatesGroup):
    mode = State()
    style = State()
    custom_style = State()
    text = State()
    confirm = State()
    lyrics = State()  # reviewing an AI draft


def _message_text(event: MessageCreated) -> str:
    body = event.message.body
    return (body.text or "").strip() if body else ""


def _text_prompt(mode: str) -> str:
    if mode == SongMode.LYRICS:
        return f"✍️ Пришли текст песни одним сообщением (до {LYRICS_MAX} символов)."
    return (
        "💡 Опиши, о чём песня: настроение, сюжет, для кого "
        f"(до {DESCRIPTION_MAX} символов)."
    )


def _summary(data: dict, user: User) -> str:
    text = data["text"]
    preview = text if len(text) <= PREVIEW_MAX else text[:PREVIEW_MAX] + "…"
    label = "Текст" if data["mode"] == SongMode.LYRICS else "Описание"
    return (
        "🎵 Проверь заказ\n\n"
        f"Режим: {MODE_LABELS[data['mode']]}\n"
        f"Стиль: {data['style']}\n"
        f"{label}: {preview}\n\n"
        f"💰 Стоимость: {settings.song_price} (на балансе: {user.balance})"
    )


def _lyrics_text(lyrics: str, last: bool = False) -> str:
    text = f"📝 Текст песни:\n\n{lyrics}\n\n💰 Создание песни: {settings.song_price}"
    return f"{text}\n\n{LAST_DRAFT}" if last else text


def _can_redraft(version: int) -> bool:
    """LYRICS_DRAFTS_MAX drafts per order (0: no limit): each one is a free-tier AI request."""
    return not settings.lyrics_drafts_max or version < settings.lyrics_drafts_max


def _when(seconds: float | None) -> str:
    if seconds is None:
        return "через пару минут"
    minutes = max(1, math.ceil(seconds / 60))
    if minutes == 1:
        return "через минуту"
    if minutes < 60:
        return f"через {minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"через {hours} ч {minutes} мин" if minutes else f"через {hours} ч"


def _is_current(payload: SongCallback, data: dict) -> bool:
    return payload.value == str(data.get("lyrics_version"))


async def _freeze(event: MessageCallback, notification: str | None = None) -> None:
    """Answer the tap: keep the message text, remove its buttons.

    A failure only costs the cosmetics: the FSM state already stops a repeated tap.
    """
    body = event.message.body if event.message else None
    try:
        async with asyncio.timeout(ANSWER_TIMEOUT):
            await event.answer(
                notification=notification,
                new_text=body.text if body else None,
                attachments=[],
                raise_if_not_exists=False,
            )
    except Exception as exc:
        logger.warning("Could not answer the tap, going on: %r", exc)


_typing_tasks: set[asyncio.Task] = set()  # running indicators; asyncio keeps only weak references


@asynccontextmanager
async def _typing(event: MessageCallback) -> AsyncIterator[None]:
    """Show "typing…" while the AI works, from a background task.

    maxapi's event.typing() awaits its MAX requests, so a slow request held up the draft,
    and a failed one ended it, even after the AI had written the lyrics.
    """

    async def keep_typing() -> None:
        while True:
            try:
                await event.action()
            except Exception as exc:
                logger.warning("Typing indicator stopped: %r", exc)
                return
            await asyncio.sleep(TYPING_INTERVAL)

    task = asyncio.create_task(keep_typing())
    _typing_tasks.add(task)
    task.add_done_callback(_typing_tasks.discard)
    try:
        yield
    finally:
        task.cancel()  # not awaited: a hanging request must not delay the answer


async def _send_insufficient_funds(event: MessageCallback, user: User) -> None:
    await event.send(
        text=f"😔 Недостаточно средств: нужно {settings.song_price}, на балансе {user.balance}.",
        attachments=[insufficient_funds_kb()],
    )


async def _draft(event: MessageCallback, ai: AIService, data: dict) -> str:
    """An AI draft that fits the lyrics limit; LyricsError if there is none."""
    async with _typing(event):
        lyrics = await ai.write_lyrics(data["text"], data["style"])
    if len(lyrics) > LYRICS_MAX:
        raise LyricsError(f"AI lyrics draft too long: {len(lyrics)} chars")
    return lyrics


async def _write_lyrics(
    event: MessageCallback,
    context: BaseContext,
    session: AsyncSession,
    ai: AIService,
    data: dict,
) -> None:
    await session.commit()  # no DB transaction stays open while the AI works
    version = data.get("lyrics_version", 0)

    try:
        lyrics = await _draft(event, ai, data)
    except LyricsRateLimited as exc:
        logger.warning("AI lyrics draft: %s", exc)
        failure = BUSY.format(when=_when(exc.retry_after))
    except Exception:
        logger.exception("AI lyrics draft failed")
        failure = REDRAFT_FAILED if version else DRAFT_FAILED
    else:
        failure = None
    if failure is not None:
        # A previous draft stays usable; without one the state stays `confirm`, so "✅ Создать" retries.
        await event.send(text=failure, attachments=[lyrics_kb(version) if version else confirm_kb()])
        return

    version += 1
    await context.update_data(lyrics=lyrics, lyrics_version=version)
    await context.set_state(SongForm.lyrics)
    can_redraft = _can_redraft(version)
    await event.send(
        text=_lyrics_text(lyrics, last=not can_redraft),
        attachments=[lyrics_kb(version, can_regenerate=can_redraft)],
    )


async def _submit_song(
    event: MessageCallback,
    session: AsyncSession,
    db_user: User,
    music: MusicService,
    data: dict,
    lyrics: str,
) -> None:
    """Charge the balance, record the order and hand the lyrics to the music provider."""
    generation = await crud.start_paid_generation(
        session,
        db_user.id,
        GenerationKind.SONG,
        prompt=data["text"],
        cost=settings.song_price,
        mode=data["mode"],
        style=data["style"],
        lyrics=lyrics,
    )
    if generation is None:
        await _send_insufficient_funds(event, db_user)
        return
    await session.commit()  # the charge is stored before the provider is called

    try:
        task_id = await music.generate_song(lyrics=lyrics, style=data["style"])
    except Exception as exc:
        logger.exception("Generation %s: provider call failed", generation.id)
        await crud.fail_generation(session, generation, repr(exc))
        await session.commit()  # the refund is stored before the user is told
        await event.send(text=SUBMIT_FAILED, attachments=[main_menu_kb()])
        return

    if not await crud.mark_processing(session, generation.id, task_id):
        await session.commit()
        logger.warning("Generation %s: refunded while the provider was accepting it; result ignored", generation.id)
        return
    await session.commit()
    await event.send(text=SUBMITTED, attachments=[main_menu_kb()])


@router.message_callback(MenuCallback.filter(F.action == MenuAction.SONG))
async def on_song(event: MessageCallback, context: BaseContext) -> None:
    await context.clear()
    await context.set_state(SongForm.mode)
    await event.answer(
        new_text=(
            "🎵 Как создадим песню?\n\n"
            "✍️ Свой текст — ты присылаешь слова.\n"
            "🤖 Текст от ИИ — описываешь идею, слова пишет нейросеть."
        ),
        attachments=[mode_kb()],
    )


@router.message_callback(SongCallback.filter(F.action == SongAction.MODE), SongForm.mode)
async def on_mode(event: MessageCallback, context: BaseContext, payload: SongCallback) -> None:
    await context.update_data(mode=SongMode(payload.value).value)
    await context.set_state(SongForm.style)
    await event.answer(new_text="🎸 Выбери стиль музыки:", attachments=[style_kb()])


@router.message_callback(SongCallback.filter(F.action == SongAction.STYLE), SongForm.style)
async def on_style(event: MessageCallback, context: BaseContext, payload: SongCallback) -> None:
    if payload.value == CUSTOM_STYLE:
        await context.set_state(SongForm.custom_style)
        await event.answer(
            new_text=(
                f"✏️ Опиши стиль своими словами (до {STYLE_MAX} символов), "
                "например: «инди-рок, женский вокал»."
            ),
            attachments=[cancel_kb()],
        )
        return

    data = await context.update_data(style=SONG_STYLES[payload.value])
    await context.set_state(SongForm.text)
    await event.answer(new_text=_text_prompt(data["mode"]), attachments=[cancel_kb()])


@router.message_created(SongForm.custom_style)
async def on_custom_style(event: MessageCreated, context: BaseContext) -> None:
    style = _message_text(event)
    if not style or len(style) > STYLE_MAX:
        await event.message.answer(
            text=f"Пришли стиль текстом, до {STYLE_MAX} символов.", attachments=[cancel_kb()]
        )
        return

    data = await context.update_data(style=style)
    await context.set_state(SongForm.text)
    await event.message.answer(text=_text_prompt(data["mode"]), attachments=[cancel_kb()])


@router.message_created(SongForm.text)
async def on_text(event: MessageCreated, context: BaseContext, db_user: User) -> None:
    data = await context.get_data()
    limit = LYRICS_MAX if data["mode"] == SongMode.LYRICS else DESCRIPTION_MAX
    text = _message_text(event)
    if not text:
        await event.message.answer(
            text="Пришли текст обычным сообщением.", attachments=[cancel_kb()]
        )
        return
    if len(text) > limit:
        await event.message.answer(
            text=f"Слишком длинно: {len(text)} символов, максимум {limit}. Сократи и пришли ещё раз.",
            attachments=[cancel_kb()],
        )
        return

    data = await context.update_data(text=text)
    await context.set_state(SongForm.confirm)
    await event.message.answer(text=_summary(data, db_user), attachments=[confirm_kb()])


@router.message_callback(SongCallback.filter(F.action == SongAction.CONFIRM), SongForm.confirm)
async def on_confirm(
    event: MessageCallback,
    context: BaseContext,
    session: AsyncSession,
    db_user: User,
    music: MusicService,
    ai: AIService,
) -> None:
    data = await context.get_data()

    if data["mode"] == SongMode.LYRICS:  # the user's own text goes straight to the provider
        await context.clear()
        await _freeze(event)
        await _submit_song(event, session, db_user, music, data, lyrics=data["text"])
        return

    if db_user.balance < settings.song_price:  # no AI drafts for orders that can't be paid
        await context.clear()
        await _freeze(event)
        await _send_insufficient_funds(event, db_user)
        return

    await _freeze(event, notification=WRITING)
    await _write_lyrics(event, context, session, ai, data)


@router.message_callback(SongCallback.filter(F.action == SongAction.REGENERATE), SongForm.lyrics)
async def on_regenerate(
    event: MessageCallback,
    context: BaseContext,
    session: AsyncSession,
    ai: AIService,
    payload: SongCallback,
) -> None:
    data = await context.get_data()
    if not _is_current(payload, data) or not _can_redraft(data["lyrics_version"]):
        await event.answer(notification=USE_LATEST)
        return

    await _freeze(event, notification=REWRITING)
    await _write_lyrics(event, context, session, ai, data)


@router.message_callback(SongCallback.filter(F.action == SongAction.CREATE), SongForm.lyrics)
async def on_create_song(
    event: MessageCallback,
    context: BaseContext,
    session: AsyncSession,
    db_user: User,
    music: MusicService,
    payload: SongCallback,
) -> None:
    data = await context.get_data()
    if not _is_current(payload, data):
        await event.answer(notification=USE_LATEST)
        return

    await context.clear()  # a repeated tap no longer matches this handler
    await _freeze(event)
    await _submit_song(event, session, db_user, music, data, lyrics=data["lyrics"])
