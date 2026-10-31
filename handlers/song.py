"""Song flow: mode → style → text → confirm → charge → submit to the provider."""

import logging

from maxapi import F, Router
from maxapi.context import BaseContext, State, StatesGroup
from maxapi.types import MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import GenerationKind, GenerationStatus, SongMode, User
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
    main_menu_kb,
    mode_kb,
    style_kb,
)
from services import MusicService

logger = logging.getLogger(__name__)
router = Router(router_id="song")

STYLE_MAX = 120
LYRICS_MAX = 3000
DESCRIPTION_MAX = 500
PREVIEW_MAX = 300

MODE_LABELS = {SongMode.LYRICS: "свой текст", SongMode.DESCRIPTION: "текст от ИИ"}


class SongForm(StatesGroup):
    mode = State()
    style = State()
    custom_style = State()
    text = State()
    confirm = State()


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
) -> None:
    data = await context.get_data()
    await context.clear()  # a repeated tap on "Создать" no longer matches this handler

    generation = await crud.start_paid_generation(
        session,
        db_user.id,
        GenerationKind.SONG,
        prompt=data["text"],
        cost=settings.song_price,
        mode=data["mode"],
        style=data["style"],
    )
    if generation is None:
        await event.answer(
            new_text=(
                f"😔 Недостаточно средств: нужно {settings.song_price}, "
                f"на балансе {db_user.balance}."
            ),
            attachments=[insufficient_funds_kb()],
        )
        return
    await session.commit()  # the charge is stored before the provider is called

    try:
        task_id = await music.generate_song(
            prompt=data["text"],
            style=data["style"],
            custom_lyrics=data["mode"] == SongMode.LYRICS,
        )
    except Exception as exc:
        logger.exception("Generation %s: provider call failed", generation.id)
        await crud.fail_generation(session, generation, repr(exc))
        await event.answer(
            new_text="😔 Не удалось запустить генерацию. Средства вернулись на баланс.",
            attachments=[main_menu_kb()],
        )
        return

    generation.status = GenerationStatus.PROCESSING
    generation.provider_task_id = task_id
    await event.answer(
        new_text="🎶 Песня создаётся! Пришлю её сюда, как только будет готова.",
        attachments=[main_menu_kb()],
    )
