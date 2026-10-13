import json
import logging
from typing import Optional, List, Dict
import aiohttp
from base_client import BaseBotClient

logger = logging.getLogger(__name__)


class TelegramBotClient(BaseBotClient):

    def __init__(self, token: str):
        self.token = token
        self.BASE_URL = f"https://api.telegram.org/bot{token}"

    def supports_contact_button(self) -> bool:
        return True

    def supports_reminders(self) -> bool:
        return True

    def supports_keyboard_removal(self) -> bool:
        return True

    async def send_message(
        self,
        user_id: str,
        text: str,
        keyboard: Optional[List[List[Dict]]] = None,
        attachments: Optional[List[Dict]] = None
    ) -> Optional[dict]:
        if attachments:
            logger.warning(f"TelegramBotClient: attachments not supported, ignoring for user {user_id}")

        payload: dict = {
            "chat_id": user_id,
            "text": text or " ",
            "parse_mode": "HTML"
        }

        if keyboard:
            # Проверяем наличие кнопки request_contact
            has_contact_btn = any(
                btn.get("type") == "request_contact"
                for row in keyboard
                for btn in row
            )
            if has_contact_btn:
                reply_markup = {
                    "keyboard": [[{"text": "ПОДТВЕРДИТЬ", "request_contact": True}]],
                    "one_time_keyboard": True,
                    "resize_keyboard": True
                }
            else:
                inline_keyboard = []
                for row in keyboard:
                    tg_row = []
                    for btn in row:
                        if btn.get("type") == "request_contact":
                            continue
                        tg_btn = {"text": btn.get("text", "")}
                        if "callback_data" in btn:
                            tg_btn["callback_data"] = str(btn["callback_data"])
                        tg_row.append(tg_btn)
                    if tg_row:
                        inline_keyboard.append(tg_row)
                reply_markup = {"inline_keyboard": inline_keyboard}

            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.BASE_URL}/sendMessage", json=payload) as resp:
                    if resp.status in [200, 201]:
                        return await resp.json()
                    else:
                        err = await resp.text()
                        logger.error(f"TelegramBotClient send_message error {resp.status}: {err}")
                        return None
        except Exception as e:
            logger.error(f"TelegramBotClient send_message exception: {e}")
            return None

    async def send_typing(self, chat_id: int):
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self.BASE_URL}/sendChatAction",
                    json={"chat_id": chat_id, "action": "typing"}
                )
        except Exception as e:
            logger.error(f"TelegramBotClient send_typing error: {e}")

    async def send_alert_to_group(self, group_id: str, text: str) -> Optional[dict]:
        return await self.send_message(group_id, text)

    async def answer_callback(self, callback_query_id: str):
        """Подтверждает callback_query (убирает «часики» у кнопки).

        U4 мультибота: перенос прямого вызова API из main.py — токен берётся
        из клиента бота сессии, а не из env."""
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self.BASE_URL}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id}
                )
        except Exception as e:
            logger.error(f"TelegramBotClient answer_callback error: {e}")

    async def delete_message(self, chat_id: str, message_id: int):
        """Удаляет сообщение из чата (U4: перенос из main.py, токен клиента)."""
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self.BASE_URL}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": message_id}
                )
        except Exception as e:
            logger.error(f"TelegramBotClient delete_message error: {e}")

    async def remove_inline_keyboard(self, message_id: str, original_text: str) -> bool:
        try:
            parts = message_id.split(":", 1)
            if len(parts) != 2:
                logger.error(f"TelegramBotClient: invalid message_id format '{message_id}'")
                return False
            chat_id_part, msg_id_part = parts
            payload = {
                "chat_id": chat_id_part,
                "message_id": int(msg_id_part),
                "reply_markup": json.dumps({"inline_keyboard": []})
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{self.BASE_URL}/editMessageReplyMarkup", json=payload) as resp:
                    if resp.status in [200, 201]:
                        return True
                    err = await resp.text()
                    logger.error(f"TelegramBotClient remove_inline_keyboard error {resp.status}: {err}")
                    return False
        except Exception as e:
            logger.error(f"TelegramBotClient remove_inline_keyboard exception: {e}")
            return False
