from typing import Dict
from aiogram import Bot, types

# Тут храним последнее сообщение для каждого пользователя
_last_messages: Dict[int, int] = {}


async def delete_previous(bot: Bot, chat_id: int):
    """Удаляет предыдущее сообщение, если оно сохранено."""
    msg_id = _last_messages.get(chat_id)
    if msg_id:
        try:
            await bot.delete_message(chat_id, msg_id)
        except:
            pass


async def send_clean(bot: Bot, chat_id: int, text: str, **kwargs) -> types.Message:
    """
    Отправляет новое сообщение и удаляет предыдущее.
    """
    await delete_previous(bot, chat_id)

    msg = await bot.send_message(chat_id, text, **kwargs)
    _last_messages[chat_id] = msg.message_id

    return msg