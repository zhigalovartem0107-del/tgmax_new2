"""Video generation isn't connected yet: the button says so plainly and never charges."""

from conftest import USER_ID
from sqlalchemy import select

from database import Generation, crud


async def test_video_button_says_in_development_and_charges_nothing(bot, session):
    await bot.start()
    await crud.add_balance(session, USER_ID, 25)
    await session.commit()

    await bot.tap("🎬 Создать видео")

    assert "в разработке" in (bot.last.text or "")  # a message, not a toast that is easy to miss
    assert "не списываются" in bot.last.text
    assert "🎵 Создать песню" in bot.last.buttons  # what works now is one tap away

    session.expunge_all()
    assert (await crud.get_user(session, USER_ID)).balance == 25
    assert (await session.scalars(select(Generation))).all() == []


async def test_video_button_works_mid_song_order(bot):
    """The notice doesn't break an order in progress: the menu still works after it."""
    await bot.start()
    menu = bot.last
    await bot.tap("🎵 Создать песню", menu)

    await bot.tap("🎬 Создать видео", menu)
    assert "в разработке" in (bot.last.text or "")

    await bot.tap("🎵 Создать песню")
    assert bot.last.text.startswith("🎵 Как создадим песню?")
