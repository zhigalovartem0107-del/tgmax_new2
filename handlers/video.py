"""Video flow: description → optional first frame (a photo) → confirm → charge → submit to Magic Hour.

Without MAGIC_HOUR_API_KEY the button says video is in development and charges nothing.
The video arrives later: workers/video_delivery.py sends it, or refunds a failed order.
An order Magic Hour refuses with "too many requests" is refunded and kept, so
"✅ Создать видео" can be tapped again after the wait the message names.
"""

import logging

from maxapi import F, Router
from maxapi.context import BaseContext, State, StatesGroup
from maxapi.enums.attachment import AttachmentType
from maxapi.types import MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import GenerationKind, User, VideoMode
from handlers.common import freeze, when
from keyboards import MenuAction, MenuCallback, cancel_kb, insufficient_funds_kb, main_menu_kb
from keyboards.video import VideoAction, VideoCallback, image_kb, video_confirm_kb
from services.video import VideoRateLimited, VideoService

logger = logging.getLogger(__name__)
router = Router(router_id="video")

PROMPT_MAX = 1000
PREVIEW_MAX = 300

IN_DEVELOPMENT = (
    "🎬 Создание видео пока в разработке — скоро появится!\n\n"
    "Деньги за видео не списываются. А пока можно создать песню 🎵"
)
ASK_PROMPT = (
    "🎬 Опиши видео: что происходит в кадре, где, какое настроение, как движется камера "
    f"(до {PROMPT_MAX} символов).\n\n"
    "Например: «кот идёт по пляжу на закате, мягкие волны, тёплый свет»."
)
ASK_IMAGE = (
    "🖼 Хочешь оживить свою картинку? Пришли фото — видео начнётся с него.\n"
    "Или нажми «Без картинки»."
)
NOT_A_PHOTO = "Пришли фото (как картинку, не файлом) или нажми «Без картинки»."
BUSY = "⏳ Видео-сервис сейчас перегружен: слишком много запросов. Попробуй {when}."
REFUNDED = "Средства вернулись на баланс."
SUBMITTED = "🎬 Видео создаётся! Пришлю его сюда, как только будет готово — обычно это несколько минут."
SUBMIT_FAILED = f"😔 Не удалось запустить создание видео. {REFUNDED}"


class VideoForm(StatesGroup):
    prompt = State()
    image = State()  # waiting for a photo, or "Без картинки"
    confirm = State()


def _message_text(event: MessageCreated) -> str:
    body = event.message.body
    return (body.text or "").strip() if body else ""


def _photo_url(event: MessageCreated) -> str | None:
    """The link of the first photo in the message: MAX serves it publicly, so the bot can fetch it."""
    body = event.message.body
    for attachment in (body.attachments or []) if body else []:
        url = getattr(attachment.payload, "url", None)
        if attachment.type == AttachmentType.IMAGE and isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _summary(data: dict, user: User) -> str:
    prompt = data["prompt"]
    preview = prompt if len(prompt) <= PREVIEW_MAX else prompt[:PREVIEW_MAX] + "…"
    image = "твоё фото — видео начнётся с него" if data.get("image_url") else "нет"
    return (
        "🎬 Проверь заказ\n\n"
        f"Описание: {preview}\n"
        f"Картинка: {image}\n\n"
        f"💰 Стоимость: {settings.video_cost} (на балансе: {user.balance})"
    )


async def _keep_for_retry(context: BaseContext, data: dict) -> None:
    await context.set_state(VideoForm.confirm)
    await context.update_data(**data)


@router.message_callback(MenuCallback.filter(F.action == MenuAction.VIDEO))
async def on_video(event: MessageCallback, context: BaseContext, video: VideoService) -> None:
    if not video.configured:
        # The message itself, not only a toast: MAX shows a toast briefly, or not at all.
        await event.answer(notification="🚧 В разработке", new_text=IN_DEVELOPMENT, attachments=[main_menu_kb()])
        return
    await context.clear()
    await context.set_state(VideoForm.prompt)
    await event.answer(new_text=ASK_PROMPT, attachments=[cancel_kb()])


@router.message_created(VideoForm.prompt)
async def on_prompt(event: MessageCreated, context: BaseContext) -> None:
    prompt = _message_text(event)
    if not prompt:
        await event.message.answer(text="Пришли описание видео обычным сообщением.", attachments=[cancel_kb()])
        return
    if len(prompt) > PROMPT_MAX:
        await event.message.answer(
            text=f"Слишком длинно: {len(prompt)} символов, максимум {PROMPT_MAX}. Сократи и пришли ещё раз.",
            attachments=[cancel_kb()],
        )
        return

    await context.update_data(prompt=prompt, image_url=None)
    await context.set_state(VideoForm.image)
    await event.message.answer(text=ASK_IMAGE, attachments=[image_kb()])


@router.message_created(VideoForm.image)
async def on_image(event: MessageCreated, context: BaseContext, db_user: User) -> None:
    image_url = _photo_url(event)
    if image_url is None:
        await event.message.answer(text=NOT_A_PHOTO, attachments=[image_kb()])
        return

    data = await context.update_data(image_url=image_url)
    await context.set_state(VideoForm.confirm)
    await event.message.answer(text=_summary(data, db_user), attachments=[video_confirm_kb()])


@router.message_callback(VideoCallback.filter(F.action == VideoAction.NO_IMAGE), VideoForm.image)
async def on_no_image(event: MessageCallback, context: BaseContext, db_user: User) -> None:
    data = await context.update_data(image_url=None)
    await context.set_state(VideoForm.confirm)
    await event.answer(new_text=_summary(data, db_user), attachments=[video_confirm_kb()])


@router.message_callback(VideoCallback.filter(F.action == VideoAction.CONFIRM), VideoForm.confirm)
async def on_confirm(
    event: MessageCallback,
    context: BaseContext,
    session: AsyncSession,
    db_user: User,
    video: VideoService,
) -> None:
    data = await context.get_data()
    await context.clear()  # a repeated tap no longer matches this handler
    await freeze(event)

    if wait := video.wait_before_next():  # sure to be refused now: don't charge
        await _keep_for_retry(context, data)
        await event.send(text=BUSY.format(when=when(wait)), attachments=[video_confirm_kb()])
        return

    image_url = data.get("image_url")
    generation = await crud.start_paid_generation(
        session,
        db_user.id,
        GenerationKind.VIDEO,
        prompt=data["prompt"],
        cost=settings.video_cost,
        mode=VideoMode.IMAGE if image_url else VideoMode.TEXT,
    )
    if generation is None:
        await event.send(
            text=f"😔 Недостаточно средств: нужно {settings.video_cost}, на балансе {db_user.balance}.",
            attachments=[insufficient_funds_kb()],
        )
        return
    await session.commit()  # the charge is stored before Magic Hour is called

    try:
        task_id = await video.generate_video(data["prompt"], image_url=image_url)
    except VideoRateLimited as exc:
        await crud.fail_generation(session, generation, str(exc))
        await session.commit()  # the refund is stored before the user is told
        await _keep_for_retry(context, data)
        await event.send(
            text=f"{BUSY.format(when=when(exc.retry_after))} {REFUNDED}", attachments=[video_confirm_kb()]
        )
        return
    except Exception as exc:
        logger.exception("Video %s: Magic Hour call failed", generation.id)
        await crud.fail_generation(session, generation, repr(exc))
        await session.commit()  # the refund is stored before the user is told
        await event.send(text=SUBMIT_FAILED, attachments=[main_menu_kb()])
        return

    if not await crud.mark_processing(session, generation.id, task_id):
        await session.commit()
        logger.warning("Video %s: refunded while Magic Hour was accepting it; result ignored", generation.id)
        return
    await session.commit()
    await event.send(text=SUBMITTED, attachments=[main_menu_kb()])
