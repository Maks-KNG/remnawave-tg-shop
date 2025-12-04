import logging
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from typing import Optional, Union
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from bot.keyboards.inline.user_keyboards import (
    get_subscription_options_keyboard,
    get_back_to_main_menu_markup,
    get_autorenew_confirm_keyboard,
)
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
from bot.middlewares.i18n import JsonI18n
from db.dal import subscription_dal, user_billing_dal
from db.models import Subscription
from bot.utils.message_cleaner import send_clean

router = Router(name="user_subscription_core_router")


# -----------------------------
#  SUBSCRIPTION OPTIONS SCREEN
# -----------------------------
async def display_subscription_options(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    get_text = lambda key, **kwargs: (
        i18n.gettext(current_lang, key, **kwargs) if i18n else key
    )

    if not i18n:
        err_msg = "Language service error."
        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer(err_msg, show_alert=True)
            except: pass
        else:
            await event.answer(err_msg)
        return

    currency_symbol_val = settings.DEFAULT_CURRENCY_SYMBOL

    text_content = (
        get_text("select_subscription_period")
        if settings.subscription_options
        else get_text("no_subscription_options_available")
    )

    reply_markup = (
        get_subscription_options_keyboard(
            settings.subscription_options,
            currency_symbol_val,
            current_lang,
            i18n
        )
        if settings.subscription_options
        else get_back_to_main_menu_markup(current_lang, i18n)
    )

    target_message_obj = (
        event.message if isinstance(event, types.CallbackQuery) else event
    )

    if isinstance(event, types.CallbackQuery):
        try:
            await target_message_obj.edit_text(text_content, reply_markup=reply_markup)
        except:
            await target_message_obj.answer(text_content, reply_markup=reply_markup)
        try:
            await event.answer()
        except:
            pass
    else:
        await send_clean(
            target_message_obj.bot,
            target_message_obj.chat.id,
            text_content,
            reply_markup=reply_markup
        )


@router.callback_query(F.data == "main_action:subscribe")
async def reshow_subscription_options_callback(
    callback: types.CallbackQuery,
    i18n_data: dict,
    settings: Settings,
    session: AsyncSession
):
    await display_subscription_options(callback, i18n_data, settings, session)


# -----------------------------
#  MY SUBSCRIPTION SCREEN
# -----------------------------
async def my_subscription_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot,
):
    target = event.message if isinstance(event, types.CallbackQuery) else event

    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    if not i18n or not target:
        await event.answer(get_text("error_occurred_try_again"))
        return

    # Load active subscription
    active = await subscription_service.get_active_subscription_details(
        session, event.from_user.id
    )

    # ---------------------------------------
    # NO ACTIVE SUBSCRIPTION → SHOW BUY SCREEN
    # ---------------------------------------
    if not active:
        text = get_text("subscription_not_active")

        buy_button = InlineKeyboardButton(
            text=get_text("menu_subscribe_inline", default="Купить"),
            callback_data="main_action:subscribe"
        )
        back_markup = get_back_to_main_menu_markup(current_lang, i18n)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[buy_button], *back_markup.inline_keyboard]
        )

        if isinstance(event, types.CallbackQuery):
            try:
                await event.answer()
            except: pass
            try:
                await event.message.edit_text(text, reply_markup=kb)
            except:
                await event.message.delete()
                await send_clean(target.bot, target.chat.id, text, reply_markup=kb)
        else:
            await event.answer(text, reply_markup=kb)
        return

    # ---------------------------------------
    # ACTIVE SUBSCRIPTION → DETAIL SCREEN
    # ---------------------------------------
    end_date = active.get("end_date")
    days_left = (end_date.date() - datetime.now().date()).days if end_date else 0

    tribute_hint = ""
    local_sub = await subscription_dal.get_active_subscription_by_user_id(
        session, event.from_user.id
    )

    if local_sub and local_sub.provider == "tribute":
        link = settings.tribute_payment_links.get(
            local_sub.duration_months or 1
        ) if hasattr(settings, "tribute_payment_links") else None

        tribute_hint = "\n\n" + (
            get_text("subscription_tribute_notice_with_link", link=link)
            if link else get_text("subscription_tribute_notice")
        )

    text = get_text(
        "my_subscription_details",
        end_date=end_date.strftime("%d.%m.%Y") if end_date else "N/A",
        days_left=max(0, days_left),
        status=active.get("status_from_panel", get_text("status_active")).capitalize(),
        config_link=active.get("config_link") or get_text("config_link_not_available"),
        traffic_limit=(
            f"{active['traffic_limit_bytes'] / 2**30:.2f} GB"
            if active.get("traffic_limit_bytes") else get_text("traffic_unlimited")
        ),
        traffic_used=(
            f"{active['traffic_used_bytes'] / 2**30:.2f} GB"
            if active.get("traffic_used_bytes") is not None else get_text("traffic_na")
        ),
    )

    # -------- Build keyboard layout --------
    base_markup = get_back_to_main_menu_markup(current_lang, i18n)
    kb = base_markup.inline_keyboard
    prepend_rows = []

    # CONNECT BUTTON
    if settings.SUBSCRIPTION_MINI_APP_URL:
        prepend_rows.append([
            InlineKeyboardButton(
                text=get_text("connect_button"),
                web_app=WebAppInfo(url=settings.SUBSCRIPTION_MINI_APP_URL),
            )
        ])
    else:
        cfg = active.get("config_link")
        if cfg:
            prepend_rows.append([
                InlineKeyboardButton(text=get_text("connect_button"), url=cfg)
            ])

    # DEVICES BUTTON
    if settings.MY_DEVICES_SECTION_ENABLED:
        devices_count = "?"
        max_devices = active.get("max_devices")

        try:
            devices = await panel_service.get_user_devices(active.get("user_id"))
            if devices and isinstance(devices.get("devices"), list):
                devices_count = str(len(devices["devices"]))
        except:
            pass

        max_devices_display = (
            str(max_devices) if max_devices not in (None, 0) else get_text("devices_unlimited_label")
        )

        prepend_rows.append([
            InlineKeyboardButton(
                text=get_text("devices_button", current_devices=devices_count, max_devices=max_devices_display),
                callback_data="main_action:my_devices",
            )
        ])

    # AUTO-RENEW TOGGLE
    if local_sub and local_sub.provider != "tribute" and getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        toggle_text = (
            get_text("autorenew_disable_button")
            if local_sub.auto_renew_enabled else get_text("autorenew_enable_button")
        )
        prepend_rows.append([
            InlineKeyboardButton(
                text=toggle_text,
                callback_data=f"toggle_autorenew:{local_sub.subscription_id}:{1 if not local_sub.auto_renew_enabled else 0}",
            )
        ])

    # PAYMENT METHODS
    if getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        prepend_rows.append([
            InlineKeyboardButton(text=get_text("payment_methods_manage_button"), callback_data="pm:manage")
        ])

    if prepend_rows:
        kb = prepend_rows + kb

    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    # SEND OUTPUT
    if isinstance(event, types.CallbackQuery):
        try: await event.answer()
        except: pass

        try:
            await event.message.edit_text(
                text + tribute_hint,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except:
            await bot.send_message(
                target.chat.id,
                text + tribute_hint,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
    else:
        await send_clean(
            target.bot,
            target.chat.id,
            text + tribute_hint,
            reply_markup=markup
        )


# -----------------------------
# MY DEVICES SCREEN
# -----------------------------
@router.callback_query(F.data == "main_action:my_devices")
async def my_devices_command_handler(
    event: Union[types.Message, types.CallbackQuery],
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: JsonI18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    target = event.message if isinstance(event, types.CallbackQuery) else event

    if not settings.MY_DEVICES_SECTION_ENABLED:
        try: await event.answer(get_text("my_devices_feature_disabled"), show_alert=True)
        except: pass
        return

    active = await subscription_service.get_active_subscription_details(
        session, event.from_user.id
    )

    if not active or not active.get("user_id"):
        msg = get_text("subscription_not_active")
        if isinstance(event, types.CallbackQuery):
            try: await event.answer(msg, show_alert=True)
            except: pass
        else:
            await target.answer(msg)
        return

    devices = await panel_service.get_user_devices(active["user_id"])
    max_devices_val = active.get("max_devices")
    max_devices_display = get_text("devices_unlimited_label") if max_devices_val in (None, 0) else str(max_devices_val)

    if not devices or not devices.get("devices"):
        text = get_text("no_devices_details_found_message", max_devices=max_devices_display)
    else:
        devlist = []
        for idx, dev in enumerate(devices["devices"], start=1):
            created_at_str = datetime.fromisoformat(dev["createdAt"]).strftime("%d.%m.%Y %H:%M")
            devlist.append(
                get_text(
                    "device_details",
                    index=idx,
                    device_model=dev.get("deviceModel"),
                    platform=dev.get("platform"),
                    os_version=dev.get("osVersion"),
                    created_at_str=created_at_str,
                    user_agent=dev.get("userAgent"),
                    hwid=dev.get("hwid"),
                )
            )

        text = get_text(
            "my_devices_details",
            devices="\n\n".join(devlist),
            current_devices=len(devlist),
            max_devices=max_devices_display
        )

    # keyboard
    base = get_back_to_main_menu_markup(current_lang, i18n, callback_data="main_action:my_subscription")
    kb = []

    for idx, dev in enumerate(devices.get("devices") or [], start=1):
        hwid = dev.get("hwid")
        kb.append([
            InlineKeyboardButton(
                text=get_text("disconnect_device_button", index=idx, hwid=hwid),
                callback_data=f"disconnect_device:{hwid}"
            )
        ])

    kb += base.inline_keyboard
    markup = InlineKeyboardMarkup(inline_keyboard=kb)

    if isinstance(event, types.CallbackQuery):
        try: await event.answer()
        except: pass

        try:
            await event.message.edit_text(text, reply_markup=markup)
        except:
            await send_clean(event.bot, target.chat.id, text, reply_markup=markup)
    else:
        await send_clean(target.bot, target.chat.id, text, reply_markup=markup)


# -----------------------------
# DISCONNECT DEVICE
# -----------------------------
@router.callback_query(F.data.startswith("disconnect_device:"))
async def disconnect_device_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    try:
        _, hwid = callback.data.split(":", 1)
    except:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    active = await subscription_service.get_active_subscription_details(
        session, callback.from_user.id
    )

    if not active:
        await callback.answer(get_text("subscription_not_active"), show_alert=True)
        return

    ok = await panel_service.disconnect_device(active["user_id"], hwid)

    if not ok:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    await session.commit()

    try: await callback.answer(get_text("device_disconnected"))
    except: pass

    await my_subscription_command_handler(
        callback, i18n_data, settings, panel_service, subscription_service, session, bot
    )


# -----------------------------
# AUTORENEW HANDLERS
# -----------------------------
@router.callback_query(F.data.startswith("toggle_autorenew:"))
async def toggle_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    try:
        _, payload = callback.data.split(":", 1)
        sub_id_str, enable_str = payload.split(":")
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    sub = await session.get(Subscription, sub_id)

    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    if sub.provider == "tribute":
        await callback.answer(get_text("subscription_autorenew_not_supported_for_tribute"), show_alert=True)
        return

    if enable:
        has_card = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not has_card:
            await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            return

    confirm_text = get_text("autorenew_confirm_enable") if enable else get_text("autorenew_confirm_disable")
    kb = get_autorenew_confirm_keyboard(enable, sub.subscription_id, current_lang, i18n)

    try:
        await callback.message.edit_text(confirm_text, reply_markup=kb)
    except:
        await callback.message.answer(confirm_text, reply_markup=kb)

    try: await callback.answer()
    except: pass


@router.callback_query(F.data.startswith("autorenew:confirm:"))
async def confirm_autorenew_handler(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    try:
        _, _, sub_id_str, enable_str = callback.data.split(":", 3)
        sub_id = int(sub_id_str)
        enable = bool(int(enable_str))
    except:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    sub = await session.get(Subscription, sub_id)

    if not sub or sub.user_id != callback.from_user.id:
        await callback.answer(get_text("error_try_again"), show_alert=True)
        return

    if sub.provider == "tribute":
        await callback.answer(get_text("subscription_autorenew_not_supported_for_tribute"), show_alert=True)
        return

    if enable:
        card_ok = await user_billing_dal.user_has_saved_payment_method(session, callback.from_user.id)
        if not card_ok:
            await callback.answer(get_text("autorenew_enable_requires_card"), show_alert=True)
            return

    await subscription_dal.update_subscription(session, sub.subscription_id, {
        "auto_renew_enabled": enable
    })
    await session.commit()

    try: await callback.answer(get_text("subscription_autorenew_updated"))
    except: pass

    await my_subscription_command_handler(
        callback, i18n_data, settings, panel_service, subscription_service, session, bot
    )


@router.callback_query(F.data == "autorenew:cancel")
async def autorenew_cancel_from_webhook_button(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    subscription_service: SubscriptionService,
    panel_service: PanelApiService,
    bot: Bot
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    get_text = lambda key, **kw: i18n.gettext(current_lang, key, **kw)

    sub = await subscription_dal.get_active_subscription_by_user_id(
        session, callback.from_user.id
    )

    if not sub:
        await callback.answer(get_text("subscription_not_active"), show_alert=True)
        return

    if sub.provider == "tribute":
        await callback.answer(get_text("subscription_autorenew_not_supported_for_tribute"), show_alert=True)
        return

    await subscription_dal.update_subscription(session, sub.subscription_id, {
        "auto_renew_enabled": False
    })
    await session.commit()

    try: await callback.answer(get_text("subscription_autorenew_updated"))
    except: pass

    await my_subscription_command_handler(
        callback, i18n_data, settings, panel_service, subscription_service, session, bot
    )


# -----------------------------
# /connect COMMAND
# -----------------------------
@router.message(Command("connect"))
async def connect_command_handler(
    message: types.Message,
    i18n_data: dict,
    settings: Settings,
    panel_service: PanelApiService,
    subscription_service: SubscriptionService,
    session: AsyncSession,
    bot: Bot
):
    logging.info(f"User {message.from_user.id} used /connect command.")
    await my_subscription_command_handler(
        message, i18n_data, settings, panel_service, subscription_service, session, bot
    )