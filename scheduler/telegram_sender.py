"""BioCore Intelligence - envío Telegram canónico v2."""
import logging
import os
import requests

logger = logging.getLogger(__name__)


class TelegramSender:
    API_URL = "https://api.telegram.org/bot"

    def __init__(self):
        self.token = os.getenv("TELEGRAM_TOKEN")
        if not self.token:
            raise ValueError("TELEGRAM_TOKEN requerido")

    def enviar_reporte(self, chat_id, reporte):
        mensaje = (reporte or {}).get("mensaje_directo")
        if not mensaje:
            logger.error("Reporte sin mensaje_directo canónico")
            return False

        try:
            response = requests.post(
                f"{self.API_URL}{self.token}/sendMessage",
                json={
                    "chat_id": str(chat_id),
                    "text": mensaje,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if response.status_code == 200:
                logger.info("Reporte enviado. analysis_id=%s", reporte.get("analysis_id"))
                return True
            logger.error("Telegram HTTP %s: %s", response.status_code, response.text)
            return False
        except Exception as exc:
            logger.error("Error Telegram: %s", exc, exc_info=True)
            return False
