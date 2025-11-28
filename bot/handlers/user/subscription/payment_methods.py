import logging
from typing import Optional, List, Tuple

from aiogram import Router, F, types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from config.settings import Settings
from bot.keyboards.inline.user_keyboards import (
    get_payment_methods_list_keyboard,
    get_payment_method_delete_confirm_keyboard,
    get_payment_method_details_keyboard,
    get_bind_url_keyboard,
)
from bot.services.yookassa_service import YooKassaService
from bot.middlewares.i18n import JsonI18n
from bot.utils.message_cleaner import send_clean
from db.dal import user_billing_dal
from db.models import Payment

router = Router(name="user_subscription_payment_methods_router")


# ------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ------------- #

def _is_yoomoney_network(network: Optional[str]) -> bool:
    s = (network or "").lower()
    return "yoomoney" in s or "yoo money" in s or "yoo-money" in s


def _extract_last4(text: str) -> Optional[str]:
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _format_pm_title(get_text, network: Optional[str], last4: Optional[str]) -> str:
    """
    Унифицированное форматирование названия привязанного способа оплаты.
    """
    if _is_yoomoney_network(network):
        l4 = last4 or _extract_last4(network or "")
        if l4:
            return get_text("payment_method_wallet_title", last4=l4)
        return get_text("payment_method_wallet_title", last4="****")

    if last4:
        network_name = network or get_text("payment_network_card", default="Card")
        return get_text("payment_method_card_title", network=network_name, last4=last4)

    network_name = network or get_text("payment_network_generic", default="Payment method")
    return get_text("payment_method_generic_title", network=network_name)


# ------------- УПРАВЛЕНИЕ СПОСОБАМИ ОПЛАТЫ ------------- #


@router.callback_query(F.data == "pm:manage")
async def payment_methods_manage(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    get_text = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    from db.dal.user_billing_dal import list_user_payment_methods

    methods = await list_user_payment_methods(session, callback.from_user.id)
    cards: List[tuple] = []

    for m in methods:
        title = _format_pm_title(get_text, m.card_network, m.card_last4)
        if m.is_default:
            title = f"⭐ {title}"
        cards.append((str(m.method_id), title))

    text = get_text("payment_methods_title")
    if not cards:
        text += "\n\n" + get_text("payment_method_none")

    try:
        await callback.message.edit_text(
            text,
            reply_markup=get_payment_methods_list_keyboard(
                cards, 0, current_lang, i18n
            ),
        )
    except Exception:
        # Fallback: отправляем новый чистый экран
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            text,
            reply_markup=get_payment_methods_list_keyboard(
                cards, 0, current_lang, i18n
            ),
        )

    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "pm:bind")
async def payment_method_bind(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    yookassa_service: YooKassaService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    _ = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    metadata = {"user_id": str(callback.from_user.id), "bind_only": "1"}

    resp = await yookassa_service.create_payment(
        amount=1.00,
        currency="RUB",
        description="Bind card",
        metadata=metadata,
        receipt_email=settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL,
        save_payment_method=True,
        capture=False,
        bind_only=True,
    )

    if not resp or not resp.get("confirmation_url"):
        try:
            await callback.answer(_("error_payment_gateway"), show_alert=True)
        except Exception:
            pass
        return

    text = _("payment_methods_title")
    keyboard = get_bind_url_keyboard(resp["confirmation_url"], current_lang, i18n)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await send_clean(callback.bot, callback.message.chat.id, text, reply_markup=keyboard)

    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pm:delete_confirm"))
async def payment_method_delete_confirm(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    _ = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    parts = callback.data.split(":", 2)
    pm_id = parts[2] if len(parts) >= 3 else ""

    text = _("payment_method_delete_confirm")
    keyboard = get_payment_method_delete_confirm_keyboard(pm_id, current_lang, i18n)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await send_clean(callback.bot, callback.message.chat.id, text, reply_markup=keyboard)

    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pm:delete"))
async def payment_method_delete(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    _ = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    parts = callback.data.split(":", 2)
    pm_id_raw = parts[2] if len(parts) >= 3 else ""
    deleted = False

    try:
        from db.dal.user_billing_dal import (
            delete_user_payment_method,
            delete_user_payment_method_by_provider_id,
            list_user_payment_methods,
        )

        if pm_id_raw:
            if pm_id_raw.isdigit():
                deleted = await delete_user_payment_method(
                    session, callback.from_user.id, int(pm_id_raw)
                )
            else:
                deleted = await delete_user_payment_method_by_provider_id(
                    session, callback.from_user.id, pm_id_raw
                )

        # Легаси-метод: подчистить старый способ хранения
        try:
            legacy_deleted = await user_billing_dal.delete_yk_payment_method(
                session, callback.from_user.id
            )
            deleted = deleted or legacy_deleted
        except Exception:
            pass

        await session.commit()

        methods = await list_user_payment_methods(session, callback.from_user.id)
        cards: List[tuple] = []

        for m in methods:
            title = _format_pm_title(_, m.card_network, m.card_last4)
            if m.is_default:
                title = f"⭐ {title}"
            cards.append((str(m.method_id), title))

        text = _("payment_methods_title")
        if not cards:
            text += "\n\n" + _("payment_method_none")

        msg = _("payment_method_deleted_success") if deleted else _("error_try_again")
        full_text = f"{msg}\n\n{text}"

        keyboard = get_payment_methods_list_keyboard(cards, 0, current_lang, i18n)

        try:
            await callback.message.edit_text(full_text, reply_markup=keyboard)
        except Exception:
            await send_clean(
                callback.bot,
                callback.message.chat.id,
                full_text,
                reply_markup=keyboard,
            )

        try:
            await callback.answer()
        except Exception:
            pass
        return

    except Exception as e:
        logging.exception("Error while deleting payment method: %s", e)
        await session.rollback()
        try:
            await callback.answer(_("error_try_again"), show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("pm:view"))
async def payment_method_view(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    _ = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    billing = await user_billing_dal.get_user_billing(session, callback.from_user.id)

    # Новый режим: несколько методов в собственной таблице
    if not billing or not billing.yookassa_payment_method_id:
        from db.dal.user_billing_dal import list_user_payment_methods

        methods = await list_user_payment_methods(session, callback.from_user.id)
        if not methods:
            try:
                await callback.answer(_("payment_method_none"), show_alert=True)
            except Exception:
                pass
            return

        parts = callback.data.split(":", 2)
        pm_id = parts[2] if len(parts) >= 3 else str(methods[0].method_id)

        sel = next(
            (m for m in methods if str(m.method_id) == pm_id or m.provider_payment_method_id == pm_id),
            methods[0],
        )

        title = _format_pm_title(_, sel.card_network, sel.card_last4)
        added_at = (
            sel.created_at.strftime("%Y-%m-%d")
            if getattr(sel, "created_at", None)
            else "—"
        )

        last_tx = "—"
        try:
            stmt = (
                select(Payment)
                .where(
                    Payment.user_id == callback.from_user.id,
                    Payment.status == "succeeded",
                    Payment.provider == "yookassa",
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            lp = result.scalar_one_or_none()
            if lp and lp.created_at:
                last_tx = lp.created_at.strftime("%Y-%m-%d")
        except Exception:
            pass

        details = (
            f"{title}\n"
            f"{_('payment_method_added_at', date=added_at)}\n"
            f"{_('payment_method_last_tx', date=last_tx)}"
        )
        keyboard = get_payment_method_details_keyboard(
            str(sel.method_id), current_lang, i18n
        )

        try:
            await callback.message.edit_text(details, reply_markup=keyboard)
        except Exception:
            await send_clean(
                callback.bot,
                callback.message.chat.id,
                details,
                reply_markup=keyboard,
            )

        try:
            await callback.answer()
        except Exception:
            pass
        return

    # Старый формат (один метод в user_billing)
    added_at = (
        billing.created_at.strftime("%Y-%m-%d")
        if getattr(billing, "created_at", None)
        else "—"
    )
    last_tx = "—"

    try:
        stmt = (
            select(Payment)
            .where(
                Payment.user_id == callback.from_user.id,
                Payment.status == "succeeded",
                Payment.provider == "yookassa",
            )
            .order_by(Payment.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        last_payment = result.scalar_one_or_none()
        if last_payment and last_payment.created_at:
            last_tx = last_payment.created_at.strftime("%Y-%m-%d")
    except Exception:
        pass

    title = _format_pm_title(_, billing.card_network, billing.card_last4)
    details = (
        f"{title}\n"
        f"{_('payment_method_added_at', date=added_at)}\n"
        f"{_('payment_method_last_tx', date=last_tx)}"
    )

    keyboard = get_payment_method_details_keyboard(
        billing.yookassa_payment_method_id, current_lang, i18n
    )

    try:
        await callback.message.edit_text(details, reply_markup=keyboard)
    except Exception:
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            details,
            reply_markup=keyboard,
        )

    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pm:history"))
async def payment_method_history(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
    yookassa_service: YooKassaService,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    if not getattr(settings, "YOOKASSA_AUTOPAYMENTS_ENABLED", False):
        try:
            _ = (
                lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
                if i18n else key
            )
            await callback.answer(_("error_service_unavailable"), show_alert=True)
        except Exception:
            pass
        return

    _ = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    from db.dal import payment_dal

    payments = await payment_dal.get_recent_payment_logs_with_user(
        session, limit=30, offset=0
    )
    user_payments = [p for p in payments if p.user_id == callback.from_user.id]

    selected_pm_provider_id: Optional[str] = None
    pm_filter_requested: bool = False

    # data формат: pm:history[:pm_id]
    try:
        _, _, split_pm_id = callback.data.split(":", 2)
        if split_pm_id:
            pm_filter_requested = True
            if split_pm_id.isdigit():
                from db.dal.user_billing_dal import list_user_payment_methods

                methods = await list_user_payment_methods(
                    session, callback.from_user.id
                )
                sel = next(
                    (m for m in methods if str(m.method_id) == split_pm_id), None
                )
                if sel and sel.provider_payment_method_id:
                    selected_pm_provider_id = sel.provider_payment_method_id
            else:
                selected_pm_provider_id = split_pm_id
    except Exception:
        selected_pm_provider_id = None
        pm_filter_requested = False

    if pm_filter_requested and not selected_pm_provider_id:
        user_payments = []

    if selected_pm_provider_id:
        filtered: List[Payment] = []
        for p in user_payments:
            if p.provider != "yookassa":
                continue
            if p.yookassa_payment_id and yookassa_service:
                try:
                    info = await yookassa_service.get_payment_info(
                        p.yookassa_payment_id
                    )
                    pm = (info or {}).get("payment_method") or {}
                    if pm.get("id") == selected_pm_provider_id:
                        filtered.append(p)
                        continue
                except Exception:
                    pass
        user_payments = filtered

    from bot.keyboards.inline.user_keyboards import (
        get_back_to_payment_method_details_keyboard,
        get_payment_methods_manage_keyboard,
    )

    if not user_payments:
        back_pm_id = ""
        try:
            _, _, back_pm_id = callback.data.split(":", 2)
        except Exception:
            back_pm_id = ""

        back_markup = (
            get_back_to_payment_method_details_keyboard(
                back_pm_id, current_lang, i18n
            )
            if back_pm_id
            else get_payment_methods_manage_keyboard(
                current_lang, i18n, has_card=True
            )
        )

        text = _("payment_method_no_history")
        try:
            await callback.message.edit_text(text, reply_markup=back_markup)
        except Exception:
            await send_clean(
                callback.bot,
                callback.message.chat.id,
                text,
                reply_markup=back_markup,
            )
        return

    def _format_item(p: Payment) -> str:
        title = p.description or _(
            "subscription_purchase_title",
            months=p.subscription_duration_months or 1,
        )
        date_str = p.created_at.strftime("%Y-%m-%d") if p.created_at else "N/A"
        return f"{date_str} — {title} — {p.amount:.2f} {p.currency}"

    lines = [_format_item(p) for p in user_payments]
    text = _("payment_method_tx_history_title") + "\n\n" + "\n".join(lines)

    try:
        _, _, split_pm_id_for_back = callback.data.split(":", 2)
    except Exception:
        split_pm_id_for_back = ""

    back_markup = (
        get_back_to_payment_method_details_keyboard(
            split_pm_id_for_back, current_lang, i18n
        )
        if split_pm_id_for_back
        else get_payment_methods_manage_keyboard(current_lang, i18n, has_card=True)
    )

    try:
        await callback.message.edit_text(text, reply_markup=back_markup)
    except Exception:
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            text,
            reply_markup=back_markup,
        )


@router.callback_query(F.data.startswith("pm:list:"))
async def payment_methods_list(
    callback: types.CallbackQuery,
    settings: Settings,
    i18n_data: dict,
    session: AsyncSession,
):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    get_text = (
        lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)
        if i18n else key
    )

    from db.dal.user_billing_dal import list_user_payment_methods

    methods = await list_user_payment_methods(session, callback.from_user.id)
    cards: List[tuple] = []

    for m in methods:
        title = _format_pm_title(get_text, m.card_network, m.card_last4)
        if m.is_default:
            title = f"⭐ {title}"
        cards.append((str(m.method_id), title))

    try:
        _, _, page_str = callback.data.split(":", 2)
        page = int(page_str)
    except Exception:
        page = 0

    text = get_text("payment_methods_title")
    if not cards:
        text += "\n\n" + get_text("payment_method_none")

    keyboard = get_payment_methods_list_keyboard(
        cards, page, current_lang, i18n
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        await send_clean(
            callback.bot,
            callback.message.chat.id,
            text,
            reply_markup=keyboard,
        )

    try:
        await callback.answer()
    except Exception:
        pass