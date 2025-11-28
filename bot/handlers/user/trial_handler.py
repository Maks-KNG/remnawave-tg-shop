import logging
from aiogram import Router, F, types, Bot
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from config.settings import Settings
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.services.notification_service import NotificationService
from bot.keyboards.inline.user_keyboards import (
    get_main_menu_inline_keyboard,
    get_connect_and_main_keyboard,
)
from bot.middlewares.i18n import JsonI18n
from bot.utils.message_cleaner import send_clean
from .start import send_main_menu

router = Router(name="user_trial_router")


# ================================================================
# 🔥 Общая функция активации триала
# ================================================================
async def _activate_trial(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    *,
    notify_admin: bool = True
):
    """Единая функция обработки активации триала."""

    user_id = callback.from_user.id
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    get_text = (lambda k, **kw: i18n.gettext(current_lang, k, **kw)) if i18n else (lambda k, **kw: k)

    if not i18n or not callback.message:
        try:
            await callback.answer(get_text("error_occurred_try_again"), show_alert=True)
        except Exception:
            pass
        return

    # Проверка: включён ли триал
    if not settings.TRIAL_ENABLED:
        try:
            await callback.answer(get_text("trial_feature_disabled"), show_alert=True)
        except Exception:
            pass
        await send_main_menu(callback, settings, i18n_data, subscription_service, session, is_edit=True)
        return

    # Уже был триал или подписка?
    if await subscription_service.has_had_any_subscription(session, user_id):
        try:
            await callback.answer(get_text("trial_already_had_subscription_or_trial"), show_alert=True)
        except Exception:
            pass
        await send_main_menu(callback, settings, i18n_data, subscription_service, session, is_edit=True)
        return

    # 🔥 Пытаемся активировать триал
    activation_result = await subscription_service.activate_trial_subscription(session, user_id)

    activated = bool(activation_result and activation_result.get("activated"))
    end_date_obj = activation_result.get("end_date") if activation_result else None
    config_link = activation_result.get("subscription_url") if activation_result else None
    config_link = config_link or get_text("config_link_not_available")

    # ------------------------------------------------------------
    # УСПЕШНО
    # ------------------------------------------------------------
    if activated:
        try:
            await callback.answer(get_text("trial_activated_alert"), show_alert=True)
        except Exception:
            pass

        days = activation_result.get("days", settings.TRIAL_DURATION_DAYS)
        traffic = activation_result.get("traffic_gb", settings.TRIAL_TRAFFIC_LIMIT_GB)

        traffic_disp = f"{traffic} GB" if traffic and traffic > 0 else get_text("traffic_unlimited")

        text = get_text(
            "trial_activated_details_message",
            days=days,
            end_date=end_date_obj.strftime("%Y-%m-%d") if isinstance(end_date_obj, datetime) else "N/A",
            config_link=config_link,
            traffic_gb=traffic_disp,
        )

        reply_markup = get_connect_and_main_keyboard(current_lang, i18n, settings, config_link)

        # Отправляем админам уведомление
        if notify_admin:
            try:
                ns = NotificationService(callback.bot, settings, i18n)
                await ns.notify_trial_activation(user_id, end_date_obj)
            except Exception as e:
                logging.error(f"Failed notifying admin about trial: {e}")

        # Помечаем trial → ad attribution
        try:
            from db.dal import ad_dal
            await ad_dal.mark_trial_activated(session, user_id)
            await session.commit()
        except Exception as e:
            await session.rollback()
            logging.error(f"Failed marking ad attribution trial for user {user_id}: {e}")

    # ------------------------------------------------------------
    # ОШИБКА
    # ------------------------------------------------------------
    else:
        msg_key = activation_result.get("message_key", "trial_activation_failed") if activation_result else "trial_activation_failed"
        text = get_text(msg_key)

        try:
            await callback.answer(text, show_alert=True)
        except Exception:
            pass

        show_trial_btn = (
            settings.TRIAL_ENABLED
            and not await subscription_service.has_had_any_subscription(session, user_id)
        )

        reply_markup = get_main_menu_inline_keyboard(current_lang, i18n, settings, show_trial_btn)

    # ------------------------------------------------------------
    # Отправка ответа
    # ------------------------------------------------------------
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except Exception:
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


# ================================================================
# 1) Пользователь нажал кнопку "Получить триал"
# ================================================================
@router.callback_query(F.data == "trial_action:request")
async def request_trial_confirmation_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    session: AsyncSession,
):
    # У тебя в старом коде был вариант "мгновенной активации" → мы сохраняем его
    await _activate_trial(callback, settings, i18n_data, subscription_service, session)


# ================================================================
# 2) (Опционально) подтверждение активации триала
# Если ты решишь вернуть confirm-кнопку в будущем — она готова
# ================================================================
@router.callback_query(F.data == "trial_action:confirm_activate")
async def confirm_activate_trial_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    session: AsyncSession,
):
    await _activate_trial(callback, settings, i18n_data, subscription_service, session)


# ================================================================
# 3) Отмена активации триала
# ================================================================
@router.callback_query(F.data == "main_action:cancel_trial")
async def cancel_trial_activation(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    subscription_service: SubscriptionService,
    session: AsyncSession,
):
    await send_main_menu(callback, settings, i18n_data, subscription_service, session, is_edit=True)