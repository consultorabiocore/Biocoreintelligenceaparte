"""
Módulo para envío de reportes por Telegram
"""

import os
import logging
from typing import Dict, Any

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)


class TelegramSender:
    """Encargado de enviar mensajes por Telegram"""
    
    API_URL = "https://api.telegram.org/bot"
    
    def __init__(self):
        """Inicializar con token de Telegram"""
        self.token = os.getenv('TELEGRAM_TOKEN')
        
        if not self.token:
            raise ValueError("❌ TELEGRAM_TOKEN requerido en environment")
        
        logger.info("✅ TelegramSender inicializado")
    
    def enviar_reporte(self, chat_id: str, reporte: Dict[str, Any]) -> bool:
        """
        Enviar reporte formateado por Telegram
        
        Args:
            chat_id: ID del chat de Telegram
            reporte: Dict con estructura:
                {
                    'titulo': str,
                    'fecha': str,
                    'proyecto': str,
                    'resumen': str,
                    'indicadores': dict,
                    'graficos': list (URLs de imágenes),
                    'recomendaciones': str
                }
        
        Returns:
            bool: True si se envió exitosamente
        """
        try:
            mensaje = self._formatear_mensaje(reporte)
            
            # Enviar mensaje de texto
            if not self._enviar_mensaje_texto(chat_id, mensaje):
                return False
            
            # Enviar gráficos si existen
            if reporte.get('graficos'):
                self._enviar_graficos(chat_id, reporte['graficos'])
            
            return True
        
        except Exception as e:
            logger.error(f"❌ Error enviando reporte a {chat_id}: {e}")
            return False
    
    def _formatear_mensaje(self, reporte: Dict[str, Any]) -> str:
        """
        Formatear el reporte en mensaje legible para Telegram
        """
        try:
            lineas = []
            
            # Encabezado
            lineas.append(f"📊 *{reporte.get('titulo', 'Reporte BioCore')}*")
            lineas.append(f"📅 {reporte.get('fecha', 'N/A')}")
            lineas.append(f"🌍 Proyecto: *{reporte.get('proyecto', 'N/A')}*")
            lineas.append("")
            
            # Resumen
            lineas.append(f"📋 *Resumen:*")
            lineas.append(reporte.get('resumen', 'Sin información disponible'))
            lineas.append("")
            
            # Indicadores clave
            if reporte.get('indicadores'):
                lineas.append("📈 *Indicadores Clave:*")
                for clave, valor in reporte['indicadores'].items():
                    lineas.append(f"  • {clave}: {valor}")
                lineas.append("")
            
            # Recomendaciones
            if reporte.get('recomendaciones'):
                lineas.append("💡 *Recomendaciones:*")
                lineas.append(reporte['recomendaciones'])
                lineas.append("")
            
            # Footer
            lineas.append("---")
            lineas.append("🤖 _Reporte automático BioCore Intelligence_")
            
            return "\n".join(lineas)
        
        except Exception as e:
            logger.error(f"❌ Error formateando mensaje: {e}")
            return "❌ Error generando mensaje"
    
    def _enviar_mensaje_texto(self, chat_id: str, mensaje: str) -> bool:
        """
        Enviar mensaje de texto por Telegram
        """
        try:
            url = f"{self.API_URL}{self.token}/sendMessage"
            
            payload = {
                'chat_id': chat_id,
                'text': mensaje,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"✅ Mensaje enviado a {chat_id}")
                return True
            else:
                logger.error(f"❌ Error Telegram {response.status_code}: {response.text}")
                return False
        
        except Exception as e:
            logger.error(f"❌ Error enviando mensaje: {e}")
            return False
    
    def _enviar_graficos(self, chat_id: str, graficos: list) -> bool:
        """
        Enviar imágenes/gráficos por Telegram
        """
        for idx, url_grafico in enumerate(graficos):
            try:
                self._enviar_foto(chat_id, url_grafico)
            except Exception as e:
                logger.warning(f"⚠️ Error enviando gráfico {idx}: {e}")
        
        return True
    
    def _enviar_foto(self, chat_id: str, url_foto: str) -> bool:
        """Enviar una foto por Telegram"""
        try:
            url = f"{self.API_URL}{self.token}/sendPhoto"
            
            payload = {
                'chat_id': chat_id,
                'photo': url_foto,
                'caption': '📊 Gráfico del reporte'
            }
            
            response = requests.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                logger.info(f"✅ Foto enviada a {chat_id}")
                return True
            else:
                logger.error(f"❌ Error enviando foto: {response.text}")
                return False
        
        except Exception as e:
            logger.error(f"❌ Error: {e}")
            return False


class TelegramSenderMock:
    """Mock para testing sin token real"""
    
    def enviar_reporte(self, chat_id: str, reporte: Dict[str, Any]) -> bool:
        logger.info(f"[MOCK] Reporte enviado a {chat_id}: {reporte.get('titulo')}")
        return True
