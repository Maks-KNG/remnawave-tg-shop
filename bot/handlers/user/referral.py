import logging
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from typing import Optional, Union
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from bot.services.referral_service import ReferralService
from bot.keyboards.inline.user_keyboards import (
    get_back_to_main_menu_markup,
    get_referral_link_keyboard
)
from bot.middlewares.i18n import JsonI18n
from bot.utils.message_cleaner import send_clean

router = Router(name="user_referral_router")


async def referral_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    settings: Settings,
    i18n_data: dict,
    referral_service: ReferralService,
    bot: Bot,
    session: AsyncSession
):
    """Показать реферальную информацию пользователю."""

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (
        (lambda key, **kw: i18n.gettext(current_lang, key, **kw))
        if i18n else (lambda key, **kw: key)
    )

    # Определяем, что редактировать — сообщение или создавать новое
    target = event.message if isinstance(event, types.CallbackQuery) else event
    if not target:
        logging.error("referral_command_handler: target message is None")
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(get_text("error_generating_referral_link"), show_alert=True)
            except Exception:
                pass
        return

    # i18n / service check
    if not i18n or not referral_service:
        logging.error("Referral dependencies missing")
        try:
            await target.answer(get_text("error_generating_referral_link"))
        except Exception:
            pass
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return

    # Получаем имя бота
    try:
        bot_info = await bot.get_me()
        bot_username = bot_info.username
    except Exception as e:
        logging.error(f"Failed to get bot username: {e}")
        await send_clean(bot, target.chat.id, get_text("error_generating_referral_link"))
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return

    if not bot_username:
        logging.error("Bot username is None — Telegram username not set")
        await send_clean(bot, target.chat.id, get_text("error_generating_referral_link"))
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return

    user_id = event.from_user.id

    # Генерация реферальной ссылки
    referral_link = await referral_service.generate_referral_link(
        session, bot_username, user_id
    )

    if not referral_link:
        logging.error(f"Failed generating referral link for user {user_id}")
        await send_clean(bot, target.chat.id, get_text("error_generating_referral_link"))
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except Exception:
                pass
        return

    # Сбор бонусов
    bonus_lines = []
    for months, _amount in sorted(settings.subscription_options.items()):
        inviter_bonus = settings.referral_bonus_inviter.get(months)
        referee_bonus = settings.referral_bonus_referee.get(months)

        if inviter_bonus is not None or referee_bonus is not None:
            bonus_lines.append(
                get_text(
                    "referral_bonus_per_period",
                    months=months,
                    inviter_bonus_days=inviter_bonus
                    if inviter_bonus is not None
                    else get_text("no_bonus_placeholder"),
                    referee_bonus_days=referee_bonus
                    if referee_bonus is not None
                    else get_text("no_bonus_placeholder"),
                )
            )

    bonus_text = (
        "\n".join(bonus_lines)
        if bonus_lines
        else get_text("referral_no_bonuses_configured")
    )

    # Статистика
    stats = await referral_service.get_referral_stats(session, user_id)

    text = get_text(
        "referral_program_info_new",
        referral_link=referral_link,
        bonus_details=bonus_text,
        invited_count=stats["invited_count"],
        purchased_count=stats["purchased_count"],
    )

    reply_markup = get_referral_link_keyboard(current_lang, i18n)

    # Ответ
    if isinstance(event, types.Message):
        await send_clean(
            bot,
            target.chat.id,
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    else:
        try:
            await target.edit_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        except Exception:
            await send_clean(
                bot,
                target.chat.id,
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        try:
            await event.answer()
        except Exception:
            pass


@router.callback_query(F.data.startswith("referral_action:"))
async def referral_action_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    referral_service: ReferralService,
    bot: Bot,
    session: AsyncSession
):
    """Обработка inline-действий: отправка сообщения для друзей и т.п."""

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = (
        (lambda key, **kw: i18n.gettext(current_lang, key, **kw))
        if i18n else (lambda key, **kw: key)
    )

    action = callback.data.split(":", 1)[1]

    if action == "share_message":
        try:
            bot_info = await bot.get_me()
            bot_username = bot_info.username
        except Exception:
            await callback.answer(get_text("error_generating_referral_link"), show_alert=True)
            return

        if not bot_username:
            await callback.answer(get_text("error_generating_referral_link"), show_alert=True)
            return

        user_id = callback.from_user.id

        referral_link = await referral_service.generate_referral_link(
            session, bot_username, user_id
        )

        if not referral_link:
            await callback.answer(get_text("error_generating_referral_link"), show_alert=True)
            return

        friend_text = get_text("referral_friend_message", referral_link=referral_link)

        try:
            await callback.message.answer(friend_text, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Failed to send friend referral message: {e}")
            await callback.answer(get_text("error_generating_referral_link"), show_alert=True)
            return

    # Always acknowledge callback
    try:
        await callback.answer()
    except Exception:
        pass