import asyncio
import logging
from typing import Optional, List, Dict

from base_client import BaseBotClient

logger = logging.getLogger(__name__)


class WebBotClient(BaseBotClient):
    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}

    def get_or_create_queue(self, session_id: str) -> asyncio.Queue:
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue()
        return self._queues[session_id]

    def delete_queue(self, session_id: str):
        self._queues.pop(session_id, None)

    async def send_message(
        self,
        user_id: str,
        text: str,
        keyboard: Optional[List[List[Dict]]] = None,
        attachments: Optional[List[Dict]] = None
    ) -> Optional[dict]:
        q = self.get_or_create_queue(str(user_id))
        msg: dict = {"type": "text", "text": text or ""}
        if keyboard:
            buttons = []
            for row in keyboard:
                for btn in row:
                    if btn.get("type") == "request_contact":
                        continue
                    buttons.append({
                        "text": btn.get("text", ""),
                        "payload": btn.get("callback_data", btn.get("payload", ""))
                    })
            if buttons:
                msg["buttons"] = buttons
        logger.info(f"📥 [WebClient] PUT to queue session={user_id} msg={msg} queue_size_after_put будет {q.qsize()+1}")
        await q.put(msg)
        return {"ok": True}

    async def send_typing(self, chat_id) -> None:
        sid = str(chat_id)
        if sid in self._queues:
            await self._queues[sid].put({"type": "typing_on"})

    async def send_close(self, session_id: str) -> None:
        q = self.get_or_create_queue(str(session_id))
        await q.put({"type": "chat_close"})

    async def send_alert_to_group(self, group_id, text) -> None:
        logger.info(f"WebBotClient: alert ignored. group_id={group_id}")
        return None

    async def remove_inline_keyboard(self, message_id, original_text) -> bool:
        return True

    def supports_contact_button(self) -> bool:
        return False

    def supports_reminders(self) -> bool:
        return False

    def supports_keyboard_removal(self) -> bool:
        return False
