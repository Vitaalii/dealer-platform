import os
import aiohttp
import logging
import json
from typing import Optional, List, Dict
from base_client import BaseBotClient

logger = logging.getLogger(__name__)

# Класс отказа MAX при отправке клиенту (план 2026-10-12-002, KTD-C1):
# permanent — диалог клиента закрыт (403 chat.denied / error.dialog.suspended,
# 404): повтор бессмыслен до нового сообщения клиента; content — 400, ошибка
# содержимого: тот же текст повторять бессмысленно, клиента не помечаем;
# temporary — 429, 5xx, сеть и таймаут.
MAX_ERR_PERMANENT = "permanent"
MAX_ERR_CONTENT = "content"
MAX_ERR_TEMPORARY = "temporary"


def classify_max_error(status: Optional[int], body=None) -> str:
    """Пометка «диалог закрыт» — только недоступность клиента: 404 и 403 с
    chat.denied / error.dialog.suspended. Прочие 403 (права бота, токен) —
    временные: общий сбой не должен глушить живых клиентов (ревью v3.4.3)."""
    if status == 404:
        return MAX_ERR_PERMANENT
    if status == 403:
        code = body.get("code") if isinstance(body, dict) else None
        message = str(body.get("message") or "") if isinstance(body, dict) else ""
        if code == "chat.denied" or "error.dialog.suspended" in message:
            return MAX_ERR_PERMANENT
        return MAX_ERR_TEMPORARY
    if status == 400:
        return MAX_ERR_CONTENT
    return MAX_ERR_TEMPORARY

class MaxBotClient(BaseBotClient):
    def __init__(self, token: str):
        self.token = token
        self.base_url = os.getenv("MAX_API_URL", "https://platform-api.max.ru") 
        self.headers = {
            "Authorization": self.token,
            "Content-Type": "application/json"
        }

    async def send_message(self, user_id: str, text: str, keyboard: Optional[List[List[Dict]]] = None, attachments: Optional[List[Dict]] = None) -> Optional[dict]:
        """Отправляет сообщение пользователю. Все параметры согласно мануалу MAX API."""
        result, _err = await self.send_message_detailed(user_id, text, keyboard=keyboard,
                                                        attachments=attachments)
        return result

    async def send_message_detailed(self, user_id: str, text: str, keyboard: Optional[List[List[Dict]]] = None,
                                    attachments: Optional[List[Dict]] = None):
        """send_message с причиной отказа: (ответ, None) или (None, {"kind", "status", "code"}).
        Клиент общий для всех пользователей — причина возвращается, а не хранится."""
        # 1. Принудительно приводим к числу для логики, если это возможно
        try:
            clean_user_id = int(user_id)
        except ValueError:
            clean_user_id = user_id 

        # Параметры получателя передаются в URL (Query String)
        url = f"{self.base_url}/messages?user_id={str(clean_user_id)}"
        
        payload = {
            "text": text,
            "format": "html" 
        }

        # --- НОВОЕ: Общий массив для любых вложений (картинки, файлы, кнопки) ---
        final_attachments = []

        if attachments:
            final_attachments.extend(attachments)

        if keyboard:
            max_buttons = []
            for row in keyboard:
                max_row = []
                for btn in row:
                    button_type = btn.get("type", "link" if "url" in btn else "callback")
                    
                    btn_data = {
                        "type": button_type,
                        "text": btn.get("text", "Нажать") 
                    }
                    
                    if button_type == "link":
                        btn_data["url"] = btn.get("url")
                    elif button_type == "callback":
                        btn_data["payload"] = str(btn.get("callback_data", "default_payload"))
                    
                    max_row.append(btn_data)
                max_buttons.append(max_row)

            # Кнопки в MAX тоже передаются как attachment
            final_attachments.append({
                "type": "inline_keyboard",
                "payload": {"buttons": max_buttons}
            })

        # Если есть хотя бы одно вложение (картинка или кнопки), добавляем в payload
        if final_attachments:
            payload["attachments"] = final_attachments
        # -------------------------------------------------------------------------

        # Используем json.dumps для красивого вывода лога
        print(f"📡 ОТПРАВКА В MAX: {url} | Payload: {json.dumps(payload, ensure_ascii=False)}")

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.post(url, json=payload, headers=self.headers) as resp:
                    try:
                        res_data = await resp.json()
                    except:
                        res_data = await resp.text()

                    if resp.status in [200, 201]:
                        print(f"✅ УСПЕШНЫЙ ОТВЕТ ОТ API: {res_data}")
                        return res_data, None
                    else:
                        # [U2 план 2026-09-08-002] Фактический статус и тело —
                        # грепается MAX_API_ERROR (раньше «Возможно 503» было
                        # единственным следом отказа)
                        logger.error("❌ MAX_API_ERROR send_message user=%s status=%s body=%s",
                                     clean_user_id, resp.status, res_data)
                        code = res_data.get("code") if isinstance(res_data, dict) else None
                        return None, {"kind": classify_max_error(resp.status, res_data),
                                      "status": resp.status, "code": code}
        except Exception as e:
            logger.error("💥 MAX_API_ERROR send_message user=%s exception=%s: %s",
                         clean_user_id, type(e).__name__, e)
            return None, {"kind": MAX_ERR_TEMPORARY, "status": None, "code": type(e).__name__}

    async def send_alert_to_group(self, group_id: str, text: str) -> Optional[dict]:
        """Отправляет уведомление в группу (использует chat_id в URL)."""
        url = f"{self.base_url}/messages?chat_id={str(group_id)}"
        payload = {"text": text, "format": "html"}
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=self.headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        text_err = await resp.text()
                        logger.error("❌ MAX_API_ERROR send_alert group=%s status=%s body=%s",
                                     group_id, resp.status, text_err)
                        return None
        except Exception as e:
            logger.error("💥 MAX_API_ERROR send_alert group=%s exception=%s: %s",
                         group_id, type(e).__name__, e)
            return None

    def supports_contact_button(self) -> bool:
        return True

    async def send_typing(self, chat_id: int):
        """Отправляет индикатор активности в чат MAX."""
        url = f"{self.base_url}/chats/{chat_id}/actions"
        payload = {"action": "TYPING_ON"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=self.headers) as resp:
                    if resp.status not in [200, 201]:
                        error_text = await resp.text()
                        logger.warning(f"⚠️ Не удалось включить тайпинг для chat_id {chat_id}. Статус: {resp.status} | Ответ сервера: {error_text}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка сети при отправке тайпинга: {e}")

    async def remove_inline_keyboard(self, message_id: str, original_text: str) -> bool:
        """Удаляет inline-кнопки у сообщения, заменяя их пустым массивом."""
        
        # Строго по мануалу: ID передается как параметр строки запроса
        url = f"{self.base_url}/messages?message_id={message_id}"
        
        # Отправляем оригинальный текст и пустой массив attachments для удаления кнопок
        payload = {
            "text": original_text,
            "format": "html",
            "attachments": [] 
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                # Мануал требует использовать метод PUT
                async with session.put(url, json=payload, headers=self.headers) as resp:
                    if resp.status == 200:
                        print(f"✅ [MAX API] Кнопки успешно скрыты для сообщения {message_id}")
                        return True
                    else:
                        err_text = await resp.text()
                        print(f"❌ [MAX API] Ошибка скрытия кнопок {resp.status}: {err_text}")
                        return False
        except Exception as e:
            print(f"💥 Ошибка удаления кнопок: {e}")
            return False