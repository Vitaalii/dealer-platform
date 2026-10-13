import base64
import logging
from typing import Optional, List, Dict
import aiohttp
from base_client import BaseBotClient

logger = logging.getLogger(__name__)


class TiledeskBotClient(BaseBotClient):

    def __init__(self, base_url: str, project_id: str, bot_id: str, login: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id
        self.bot_id = bot_id
        credentials = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Basic {credentials}"
        }

    def supports_contact_button(self) -> bool:
        return False

    def supports_reminders(self) -> bool:
        return False

    def supports_keyboard_removal(self) -> bool:
        return False

    async def send_message(
        self,
        user_id: str,
        text: str,
        keyboard: Optional[List[List[Dict]]] = None,
        attachments: Optional[List[Dict]] = None
    ) -> Optional[dict]:
        if keyboard:
            logger.warning(f"TiledeskBotClient: keyboard not supported, ignoring for request {user_id}")
        if attachments:
            logger.warning(f"TiledeskBotClient: attachments not supported, ignoring for request {user_id}")

        payload = {"text": text or " ", "type": "text"}

        try:
            url = f"{self.base_url}/{self.project_id}/requests/{user_id}/messages"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=self.headers) as resp:
                    if resp.status in [200, 201]:
                        return await resp.json()
                    else:
                        err = await resp.text()
                        logger.error(f"TiledeskBotClient send_message error {resp.status}: {err}")
                        return None
        except Exception as e:
            logger.error(f"TiledeskBotClient send_message exception: {e}")
            return None

    async def send_typing(self, chat_id: int):
        logger.debug(f"TiledeskBotClient: send_typing no-op for request {chat_id}")

    async def send_alert_to_group(self, group_id: str, text: str) -> Optional[dict]:
        logger.info(f"TiledeskBotClient: send_alert_to_group ignored (no group support). group_id={group_id}")
        return None

    async def remove_inline_keyboard(self, message_id: str, original_text: str) -> bool:
        return True
