from abc import ABC, abstractmethod
from typing import Optional, List, Dict

class BaseBotClient(ABC):

    @abstractmethod
    async def send_message(
        self,
        user_id: str,
        text: str,
        keyboard: Optional[List[List[Dict]]] = None,
        attachments: Optional[List[Dict]] = None
    ) -> Optional[dict]:
        pass

    @abstractmethod
    async def send_typing(self, chat_id: int):
        pass

    @abstractmethod
    async def send_alert_to_group(self, group_id: str, text: str) -> Optional[dict]:
        pass

    @abstractmethod
    async def remove_inline_keyboard(self, message_id: str, original_text: str) -> bool:
        pass

    def supports_contact_button(self) -> bool:
        """Поддерживает ли канал нативную кнопку передачи номера телефона."""
        return False

    def supports_reminders(self) -> bool:
        """Можно ли писать клиенту первым (для reminder_worker и sync_worker farewell)."""
        return True

    def supports_keyboard_removal(self) -> bool:
        """Нужно ли убирать кнопки после клика."""
        return True
