import logging
import re
from datetime import datetime
from typing import Optional

from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import hcode
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from bot.states.user_states import UserPromoStates
from bot.services.promo_code_service import PromoCodeService
from bot.services.subscription_service import SubscriptionService
from bot.keyboards.inline.user_keyboards import (
    get_back_to_main_menu_markup,
    get_connect_and_main_keyboard,
)
from bot.middlewares.i18n import JsonI18n
from bot.utils.message_cleaner import send_clean

from .start import send_main_menu

router = Router(name="user_promo_router")

SUSPICIOUS_SQL_KEYWORDS_REGEX = re.compile(
    r"\b(DROP\s*TABLE|DELETE\s*FROM|ALTER\s*TABLE|TRUNCATE\s*TABLE|UNION\s*SELECT|"
    r";\s*SELECT|;\s*INSERT|;\s*UPDATE|;\s*DELETE|xp_cmdshell|sysdatabases|sysobjects|INFORMATION_SCHEMA)\b",
    re.IGNORECASE,
)
SUSPICIOUS_CHARS_REGEX = re.compile(r"(--|#\s|;|\*\/|\/\*)")
MAX_PROMO_CODE_INPUT_LENGTH = 100


async def prompt_promo_code_input(
    callback: types.CallbackQuery,
    state: FSMContext,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession,  # session пока не используем, но сохраняем для совместимости сигнатуры
):
    """
    Показываем пользователю экран ввода промокода и переводим FSM в состояние ожидания ввода.
    """
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n:
        try:
            await callback.answer("Language service error.", show_alert=True)
        except Exception:
            pass
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    if not callback.message:
        logging.error("CallbackQuery has no message in prompt_promo_code_input")
        try:
            await callback.answer(_("error_occurred_processing_request"), show_alert=True)
        except Exception:
            pass
        return

    text = _(key="promo_code_prompt")
    reply_markup = get_back_to_main_menu_markup(current_lang, i18n)

    # Пытаемся отредактировать текущее сообщение, если нельзя — отправляем новый "чистый" экран
    try:
        await callback.message.edit_text(text=text, reply_markup=reply_markup)
    except Exception as e_edit:
        logging.warning(
            f"Failed to edit message for promo prompt: {e_edit}. Sending new one via send_clean."
        )
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            text,
            reply_markup=reply_markup,
        )

    try:
        await callback.answer()
    except Exception:
        pass

    await state.set_state(UserPromoStates.waiting_for_promo_code)
    logging.info(
        f"User {callback.from_user.id} entered state UserPromoStates.waiting_for_promo_code. "
        f"FSM state: {await state.get_state()}"
    )


@router.message(UserPromoStates.waiting_for_promo_code, F.text)
async def process_promo_code_input(
    message: types.Message,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    promo_code_service: PromoCodeService,
    subscription_service: SubscriptionService,
    bot: Bot,
    session: AsyncSession,
):
    """
    Обрабатываем текстовый ввод промокода от пользователя.
    Сообщение пользователя НЕ удаляем (вариант A), отвечаем новым сообщением.
    """
    current_state = await state.get_state()
    logging.info(
        f"Processing promo code input from user {message.from_user.id} in state {current_state}: "
        f"'{message.text}'"
    )

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n or not promo_code_service:
        logging.error(
            "Dependencies (i18n or PromoCodeService) missing in process_promo_code_input"
        )
        try:
            await message.reply("Service error. Please try again later.")
        except Exception:
            pass
        await state.clear()
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    code_input = message.text.strip() if message.text else ""
    user = message.from_user

    # ----------- БАЗОВАЯ АНТИ-SQL / АНТИ-ГАРБЕДЖ ЗАЩИТА ----------- #
    is_suspicious = False
    if not code_input:
        is_suspicious = True
        logging.warning(f"Empty promo code input by user {user.id}.")
    elif (
        len(code_input) > MAX_PROMO_CODE_INPUT_LENGTH
        or SUSPICIOUS_SQL_KEYWORDS_REGEX.search(code_input)
        or SUSPICIOUS_CHARS_REGEX.search(code_input)
    ):
        is_suspicious = True
        logging.warning(
            f"Suspicious input for promo code by user {user.id} "
            f"(len: {len(code_input)}): '{code_input}'"
        )

    # ----------- ОТВЕТ ПОЛЬЗОВАТЕЛЮ ----------- #
    if is_suspicious:
        # Логируем и, при необходимости, шлём нотификацию админу
        if settings.LOG_SUSPICIOUS_ACTIVITY:
            try:
                from bot.services.notification_service import NotificationService

                notification_service = NotificationService(bot, settings, i18n)
                await notification_service.notify_suspicious_promo_attempt(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    suspicious_input=code_input,
                )
            except Exception as e:
                logging.error(f"Failed to send suspicious promo notification: {e}")

        response_to_user_text = _(
            "promo_code_not_found",
            code=hcode(code_input.upper()) if code_input else hcode(""),
        )
        reply_markup = get_back_to_main_menu_markup(current_lang, i18n)

    else:
        # Пытаемся применить промокод
        success, result = await promo_code_service.apply_promo_code(
            session, user.id, code_input, current_lang
        )

        if success:
            await session.commit()
            logging.info(
                f"Promo code '{code_input}' successfully applied for user {user.id}."
            )

            new_end_date = result if isinstance(result, datetime) else None

            active = await subscription_service.get_active_subscription_details(
                session, user.id
            )
            config_link = active.get("config_link") if active else None
            config_link = config_link or _("config_link_not_available")

            response_to_user_text = _(
                "promo_code_applied_success_full",
                end_date=(
                    new_end_date.strftime("%d.%m.%Y %H:%M:%S")
                    if new_end_date
                    else "N/A"
                ),
                config_link=config_link,
            )
            reply_markup = get_connect_and_main_keyboard(
                current_lang, i18n, settings, config_link
            )
        else:
            await session.rollback()
            logging.info(
                f"Promo code '{code_input}' application failed for user {user.id}. "
                f"Reason: {result}"
            )
            # result уже локализован PromoCodeService
            response_to_user_text = result
            reply_markup = get_back_to_main_menu_markup(current_lang, i18n)

    # Сообщение пользователя не трогаем, отвечаем отдельным сообщением
    try:
        await message.answer(
            response_to_user_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except Exception as e_send:
        logging.error(
            f"Failed to send promo result message to user {message.from_user.id}: {e_send}",
            exc_info=True,
        )

    await state.clear()
    logging.info(
        f"Promo code input '{code_input}' processing finished for user "
        f"{message.from_user.id}. State cleared."
    )


@router.callback_query(
    F.data == "main_action:back_to_main",
    UserPromoStates.waiting_for_promo_code,
)
async def cancel_promo_input_via_button(
    callback: types.CallbackQuery,
    state: FSMContext,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    session: AsyncSession,
):
    """
    Пользователь нажал "Назад в главное меню", находясь в состоянии ввода промокода.
    Сбрасываем состояние и возвращаем главное меню.
    """
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not i18n:
        logging.error("i18n missing in cancel_promo_input_via_button")
        try:
            await callback.answer("Language error", show_alert=True)
        except Exception:
            pass
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    prev_state = await state.get_state()
    logging.info(
        f"User {callback.from_user.id} cancelled promo code input via button "
        f"from state {prev_state}. Clearing state."
    )
    await state.clear()

    if callback.message:
        # Возвращаем главное меню через существующую функцию (сама разрулит edit / send)
        await send_main_menu(
            callback,
            settings,
            i18n_data,
            subscription_service,
            session,
            is_edit=True,
        )
    else:
        # На всякий случай fallback, если message потерялся
        try:
            await callback.answer(_("promo_input_cancelled_short"), show_alert=False)
        except Exception:
            pass