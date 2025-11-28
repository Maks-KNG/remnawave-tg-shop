import logging
import re
from aiogram import Router, F, types, Bot
from aiogram.utils.text_decorations import html_decoration as hd
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from typing import Optional, Union
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

from db.dal import user_dal
from db.models import User

from bot.keyboards.inline.user_keyboards import (
    get_main_menu_inline_keyboard,
    get_language_selection_keyboard,
    get_channel_subscription_keyboard,
)
from bot.services.subscription_service import SubscriptionService
from bot.services.panel_api_service import PanelApiService
    from bot.services.referral_service import ReferralService
from bot.services.promo_code_service import PromoCodeService
from config.settings import Settings
from bot.middlewares.i18n import JsonI18n
from bot.utils.text_sanitizer import sanitize_username, sanitize_display_name
from bot.utils.message_cleaner import send_clean

router = Router(name="user_start_router")


async def send_main_menu(target_event: Union[types.Message, types.CallbackQuery],
                         settings: Settings,
                         i18n_data: dict,
                         subscription_service: SubscriptionService,
                         session: AsyncSession,
                         is_edit: bool = False):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")

    user_id = target_event.from_user.id
    user_full_name = hd.quote(target_event.from_user.full_name)

    if not i18n:
        logging.error(
            f"i18n_instance missing in send_main_menu for user {user_id}"
        )
        err_msg_fallback = "Error: Language service unavailable. Please try again later."
        if isinstance(target_event, types.CallbackQuery):
            try:
                await target_event.answer(err_msg_fallback, show_alert=True)
            except Exception:
                pass
        elif isinstance(target_event, types.Message):
            try:
                await target_event.answer(err_msg_fallback)
            except Exception:
                pass
        return

    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    show_trial_button_in_menu = False
    if settings.TRIAL_ENABLED:
        if hasattr(subscription_service, 'has_had_any_subscription'):
            if not await subscription_service.has_had_any_subscription(session, user_id):
                show_trial_button_in_menu = True
        else:
            logging.error(
                "SubscriptionService missing has_had_any_subscription()!"
            )

    text = _(key="main_menu_greeting", user_name=user_full_name)
    reply_markup = get_main_menu_inline_keyboard(
        current_lang, i18n, settings, show_trial_button_in_menu
    )

    target_message_obj: Optional[types.Message] = None
    if isinstance(target_event, types.Message):
        target_message_obj = target_event
    elif isinstance(target_event, types.CallbackQuery) and target_event.message:
        target_message_obj = target_event.message

    if not target_message_obj:
        logging.error(f"send_main_menu: no target_message_obj for user {user_id}")
        if isinstance(target_event, types.CallbackQuery):
            await target_event.answer(_("error_displaying_menu"), show_alert=True)
        return

    try:
        if is_edit:
            await target_message_obj.edit_text(text, reply_markup=reply_markup)
        else:
            await send_clean(
                target_message_obj.bot,
                target_message_obj.chat.id,
                text,
                reply_markup=reply_markup
            )

        if isinstance(target_event, types.CallbackQuery):
            try:
                await target_event.answer()
            except Exception:
                pass
    except Exception as e_send_edit:
        logging.warning(
            f"Failed to send/edit main menu to {user_id}: {e_send_edit}"
        )
        if is_edit and target_message_obj:
            try:
                await target_message_obj.answer(text, reply_markup=reply_markup)
            except Exception:
                pass


async def ensure_required_channel_subscription(
        event: Union[types.Message, types.CallbackQuery],
        settings: Settings,
        i18n: Optional[JsonI18n],
        current_lang: str,
        session: AsyncSession,
        db_user: Optional[User] = None) -> bool:

    required_channel_id = settings.REQUIRED_CHANNEL_ID
    if not required_channel_id:
        return True

    if isinstance(event, types.CallbackQuery):
        user_id = event.from_user.id
        bot_instance = event.message.bot if event.message else None
        message_obj = event.message
    else:
        user_id = event.from_user.id
        bot_instance = event.bot
        message_obj = event

    if bot_instance is None:
        logging.error("Channel subscription check: no bot instance.")
        return False

    if user_id in settings.ADMIN_IDS:
        return True

    if db_user is None:
        try:
            db_user = await user_dal.get_user_by_id(session, user_id)
        except:
            return False

    if not db_user:
        return True

    if db_user.channel_subscription_verified and \
            db_user.channel_subscription_verified_for == required_channel_id:
        return True

    def translate(key: str, **kwargs):
        return i18n.gettext(current_lang, key, **kwargs) if i18n else key

    now = datetime.now(timezone.utc)
    is_member = False

    try:
        member = await bot_instance.get_chat_member(required_channel_id, user_id)
        status = getattr(member, "status", None)
        allowed = {"creator", "administrator", "member", "restricted"}
        if getattr(status, "value", status) in allowed:
            is_member = True
    except TelegramBadRequest:
        pass
    except TelegramForbiddenError:
        err = translate("channel_subscription_check_failed")
        if isinstance(event, types.CallbackQuery):
            await event.answer(err, show_alert=True)
        return False
    except TelegramAPIError:
        err = translate("channel_subscription_check_failed")
        if isinstance(event, types.CallbackQuery):
            await event.answer(err, show_alert=True)
        return False

    await user_dal.update_user(session, user_id, {
        "channel_subscription_checked_at": now,
        "channel_subscription_verified_for": required_channel_id,
        "channel_subscription_verified": is_member,
    })

    if is_member:
        return True

    keyboard = get_channel_subscription_keyboard(
        current_lang, i18n, settings.REQUIRED_CHANNEL_LINK
    ) if i18n else None

    prompt_text = translate("channel_subscription_required")

    if isinstance(event, types.CallbackQuery):
        if message_obj:
            try:
                await message_obj.edit_text(prompt_text, reply_markup=keyboard)
            except:
                pass
        try:
            await event.answer(prompt_text, show_alert=True)
        except:
            pass
    else:
        await event.answer(prompt_text, reply_markup=keyboard)

    return False


@router.message(CommandStart())
@router.message(CommandStart(magic=F.args.regexp(r"^ref_((?:[uU][A-Za-z0-9]{9})|(?:[A-Za-z0-9]{9})|\d+)$").as_("ref_match")))
@router.message(CommandStart(magic=F.args.regexp(r"^promo_(\w+)$").as_("promo_match")))
@router.message(CommandStart(magic=F.args.regexp(r"^(?!ref_|promo_)([A-Za-z0-9_\-]{2,64})$").as_("ad_param_match")))
async def start_command_handler(message: types.Message,
                                state: FSMContext,
                                settings: Settings,
                                i18n_data: dict,
                                subscription_service: SubscriptionService,
                                session: AsyncSession,
                                ref_match=None,
                                promo_match=None,
                                ad_param_match=None):
    await state.clear()
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n: Optional[JsonI18n] = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs) if i18n else key

    user = message.from_user
    user_id = user.id

    referred_by_user_id = None
    promo_code_to_apply = None
    ad_start_param = None

    if ref_match:
        raw = ref_match.group(1)
        if raw.isdigit():
            if settings.LEGACY_REFS:
                pid = int(raw)
                if pid != user_id and await user_dal.get_user_by_id(session, pid):
                    referred_by_user_id = pid
        else:
            normalized = raw.strip()
            if normalized and normalized[0].lower() == "u":
                normalized = normalized[1:]
            ref_user = await user_dal.get_user_by_referral_code(session, normalized)
            if ref_user and ref_user.user_id != user_id:
                referred_by_user_id = ref_user.user_id

    elif promo_match:
        promo_code_to_apply = promo_match.group(1)

    elif ad_param_match:
        ad_start_param = ad_param_match.group(1)

    sanitized_username = sanitize_username(user.username)
    sanitized_first = sanitize_display_name(user.first_name)
    sanitized_last = sanitize_display_name(user.last_name)

    db_user = await user_dal.get_user_by_id(session, user_id)
    if not db_user:
        user_data = {
            "user_id": user_id,
            "username": sanitized_username,
            "first_name": sanitized_first,
            "last_name": sanitized_last,
            "language_code": current_lang,
            "referred_by_id": referred_by_user_id,
            "registration_date": datetime.now(timezone.utc)
        }
        try:
            db_user, created = await user_dal.create_user(session, user_data)
            if created:
                await session.commit()

                # notify admin
                try:
                    from bot.services.notification_service import NotificationService
                    notification_service = NotificationService(message.bot, settings, i18n)
                    await notification_service.notify_new_user_registration(
                        user_id=user_id,
                        username=sanitized_username,
                        first_name=sanitized_first,
                        referred_by_id=referred_by_user_id
                    )
                except:
                    pass

        except Exception as e:
            await message.answer(_("error_occurred_processing_request"))
            return
    else:
        update_payload = {}
        if db_user.language_code != current_lang:
            update_payload["language_code"] = current_lang
        if referred_by_user_id and db_user.referred_by_id is None:
            is_active = False
            try:
                is_active = await subscription_service.has_active_subscription(session, user_id)
            except:
                pass
            if not is_active:
                update_payload["referred_by_id"] = referred_by_user_id
        if sanitized_username != db_user.username:
            update_payload["username"] = sanitized_username
        if sanitized_first != db_user.first_name:
            update_payload["first_name"] = sanitized_first
        if sanitized_last != db_user.last_name:
            update_payload["last_name"] = sanitized_last

        if update_payload:
            await user_dal.update_user(session, user_id, update_payload)

    # ad attribution
    if ad_start_param:
        try:
            from db.dal import ad_dal
            campaign = await ad_dal.get_campaign_by_start_param(session, ad_start_param)
            if campaign and campaign.is_active:
                await ad_dal.ensure_attribution(session, user_id=user_id, campaign_id=campaign.ad_campaign_id)
                await session.commit()
        except:
            await session.rollback()

    if not await ensure_required_channel_subscription(message, settings, i18n, current_lang, session, db_user):
        return

    # welcome — now via send_clean
    if not settings.DISABLE_WELCOME_MESSAGE:
        await send_clean(
            message.bot,
            message.chat.id,
            _(key="welcome", user_name=hd.quote(user.full_name))
        )

    # auto promo
    if promo_code_to_apply:
        try:
            promo_code_service = PromoCodeService(settings, subscription_service, message.bot, i18n)
            success, result = await promo_code_service.apply_promo_code(
                session, user_id, promo_code_to_apply, current_lang
            )
            if success:
                await session.commit()

                active = await subscription_service.get_active_subscription_details(session, user_id)
                config_link = active.get("config_link") if active else None
                config_link = config_link or _("config_link_not_available")

                new_end_date = result if isinstance(result, datetime) else None

                promo_success_text = _(
                    "promo_code_applied_success_full",
                    end_date=(new_end_date.strftime("%d.%m.%Y %H:%M:%S") if new_end_date else "N/A"),
                    config_link=config_link,
                )

                from bot.keyboards.inline.user_keyboards import get_connect_and_main_keyboard
                await send_clean(
                    message.bot,
                    message.chat.id,
                    promo_success_text,
                    reply_markup=get_connect_and_main_keyboard(current_lang, i18n, settings, config_link),
                    parse_mode="HTML"
                )
                return
            else:
                await session.rollback()
        except:
            await session.rollback()

    await send_main_menu(
        message, settings, i18n_data, subscription_service, session, is_edit=False
    )


@router.callback_query(F.data == "channel_subscription:verify")
async def verify_channel_subscription_callback(callback: types.CallbackQuery,
                                               settings: Settings,
                                               i18n_data: dict,
                                               subscription_service: SubscriptionService,
                                               session: AsyncSession):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")

    db_user = await user_dal.get_user_by_id(session, callback.from_user.id)

    verified = await ensure_required_channel_subscription(
        callback, settings, i18n, current_lang, session, db_user
    )
    if not verified:
        return

    if db_user and db_user.language_code:
        i18n_data["current_language"] = db_user.language_code

    if not settings.DISABLE_WELCOME_MESSAGE:
        welcome_text = hd.quote(callback.from_user.full_name)
        try:
            await callback.message.answer(
                i18n.gettext(i18n_data["current_language"], "welcome", user_name=welcome_text)
            )
        except:
            pass

    try:
        await callback.answer(i18n.gettext(i18n_data["current_language"], "channel_subscription_verified_success"), show_alert=True)
    except:
        pass

    await send_main_menu(
        callback, settings, i18n_data, subscription_service, session, is_edit=True
    )


@router.message(Command("language"))
@router.callback_query(F.data == "main_action:language")
async def language_command_handler(event: Union[types.Message, types.CallbackQuery],
                                   i18n_data: dict,
                                   settings: Settings):
    current_lang = i18n_data.get("current_language", settings.DEFAULT_LANGUAGE)
    i18n = i18n_data.get("i18n_instance")
    _ = lambda key, **kwargs: i18n.gettext(current_lang, key, **kwargs)

    text = _(key="choose_language")
    reply_markup = get_language_selection_keyboard(i18n, current_lang)

    target_message_obj = event.message if isinstance(event, types.CallbackQuery) else event

    if isinstance(event, types.CallbackQuery):
        if event.message:
            try:
                await event.message.edit_text(text, reply_markup=reply_markup)
            except:
                await target_message_obj.answer(text, reply_markup=reply_markup)
        await event.answer()
    else:
        await target_message_obj.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.startswith("set_lang_"))
async def select_language_callback_handler(callback: types.CallbackQuery,
                                           i18n_data: dict,
                                           settings: Settings,
                                           subscription_service: SubscriptionService,
                                           session: AsyncSession):
    i18n = i18n_data.get("i18n_instance")
    if not i18n or not callback.message:
        await callback.answer("Service error.", show_alert=True)
        return

    try:
        lang_code = callback.data.split("_")[2]
    except Exception:
        await callback.answer("Error.", show_alert=True)
        return

    try:
        updated = await user_dal.update_user_language(session, callback.from_user.id, lang_code)
    except:
        await callback.answer("Error.", show_alert=True)
        return

    if updated:
        i18n_data["current_language"] = lang_code
        await callback.answer(i18n.gettext(lang_code, "language_set_alert"))
    else:
        await callback.answer("Could not set language.", show_alert=True)
        return

    await send_main_menu(
        callback, settings, i18n_data, subscription_service, session, is_edit=True
    )


@router.callback_query(F.data.startswith("main_action:"))
async def main_action_callback_handler(callback: types.CallbackQuery,
                                       state: FSMContext,
                                       settings: Settings,
                                       i18n_data: dict,
                                       bot: Bot,
                                       subscription_service: SubscriptionService,
                                       referral_service: ReferralService,
                                       panel_service: PanelApiService,
                                       promo_code_service: PromoCodeService,
                                       session: AsyncSession):
    action = callback.data.split(":")[1]

    from . import subscription as subs
    from . import referral as refs
    from . import promo_user as promo
    from . import trial_handler as trial

    if not callback.message:
        await callback.answer("Error.", show_alert=True)
        return

    if action == "subscribe":
        await subs.display_subscription_options(callback, i18n_data, settings, session)

    elif action == "my_subscription":
        await subs.my_subscription_command_handler(callback, i18n_data, settings,
                                                   panel_service, subscription_service,
                                                   session, bot)

    elif action == "my_devices":
        await subs.my_devices_command_handler(callback, i18n_data, settings,
                                              panel_service, subscription_service,
                                              session, bot)

    elif action == "referral":
        await refs.referral_command_handler(callback, settings, i18n_data,
                                            referral_service, bot, session)

    elif action == "apply_promo":
        await promo.prompt_promo_code_input(callback, state, i18n_data, settings, session)

    elif action == "request_trial":
        await trial.request_trial_confirmation_handler(callback, settings, i18n_data,
                                                       subscription_service, session)

    elif action == "language":
        await language_command_handler(callback, i18n_data, settings)

    elif action == "back_to_main":
        await send_main_menu(callback, settings, i18n_data,
                             subscription_service, session, is_edit=True)

    elif action == "back_to_main_keep":
        await send_main_menu(callback, settings, i18n_data,
                             subscription_service, session, is_edit=False)

    else:
        i18n = i18n_data.get("i18n_instance")
        _ = lambda key, **kwargs: i18n.gettext(
            i18n_data.get("current_language"), key, **kwargs
        )
        await callback.answer(_("main_menu_unknown_action"), show_alert=True)