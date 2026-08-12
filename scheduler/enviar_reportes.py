#!/usr/bin/env python3
"""
Script de envío automático de reportes por Telegram
BioCore Intelligence 2026
"""

import os
import sys
from datetime import datetime, timedelta
import logging

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Importar módulos locales
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supabase_client import SupabaseClient
from telegram_sender import TelegramSender
from reportes_generator import GeneradorReportes


class AutomaticReportScheduler:
    """Orquestador del sistema de reportes automáticos"""
    
    def __init__(self):
        """Inicializar el scheduler"""
        self.supabase = SupabaseClient()
        self.telegram = TelegramSender()
        self.generador = GeneradorReportes(self.supabase)
        self.enviar_todos = os.getenv('ENVIAR_TODOS', 'false').lower() == 'true'
        
    def obtener_clientes_a_reportar(self):
        """
        Obtener los clientes que deben recibir reporte hoy
        
        Lógica:
        - Si enviar_todos=true: enviar a TODOS
        - Si frecuencia='Diario': siempre enviar
        - Si frecuencia='Semanal': enviar solo si pasaron 7 días desde último_envio
        - Considerar hora_reporte si está definida
        """
        try:
            clientes = self.supabase.obtener_todos_usuarios()
            clientes_a_reportar = []
            hoy = datetime.now()
            
            for cliente in clientes:
                if self.enviar_todos:
                    logger.info(f"✅ Modo TODOS: incluyendo {cliente['Proyecto']}")
                    clientes_a_reportar.append(cliente)
                    continue
                
                frecuencia = cliente.get('frecuencia_reporte', '').lower()
                
                # Caso 1: Reportes Diarios
                if frecuencia == 'diario':
                    if self._verificar_horario(cliente.get('hora_reporte')):
                        logger.info(f"✅ {cliente['Proyecto']}: Reporte DIARIO en horario")
                        clientes_a_reportar.append(cliente)
                    else:
                        logger.info(f"⏰ {cliente['Proyecto']}: No es la hora de reporte diario")
                
                # Caso 2: Reportes Semanales
                elif frecuencia == 'semanal':
                    if self._es_dia_reporte_semanal(cliente):
                        logger.info(f"✅ {cliente['Proyecto']}: Reporte SEMANAL programado")
                        clientes_a_reportar.append(cliente)
                    else:
                        logger.info(f"📅 {cliente['Proyecto']}: No es día de reporte semanal")
                
                else:
                    logger.warning(f"⚠️ {cliente['Proyecto']}: Frecuencia desconocida: {frecuencia}")
            
            return clientes_a_reportar
            
        except Exception as e:
            logger.error(f"❌ Error obteniendo clientes: {e}")
            return []
    
    def _verificar_horario(self, hora_reporte):
        """
        Verificar si es la hora correcta para enviar el reporte
        
        Args:
            hora_reporte: string formato "HH:MM" (ej: "09:00")
        
        Returns:
            bool: True si debe enviar, False si no
        """
        if not hora_reporte:
            return True  # Si no tiene hora, enviar siempre
        
        try:
            hora_programada = datetime.strptime(hora_reporte, "%H:%M").time()
            hora_actual = datetime.now().time()
            
            # Dar margen de 1 hora (entre la hora programada y 1 hora después)
            inicio = hora_programada
            fin = (datetime.combine(datetime.today(), hora_programada) + timedelta(hours=1)).time()
            
            return inicio <= hora_actual < fin
        except ValueError:
            logger.warning(f"⚠️ Formato de hora inválido: {hora_reporte}")
            return True
    
    def _es_dia_reporte_semanal(self, cliente):
        """
        Verificar si hoy es día de reporte semanal
        
        Lógica: Si pasaron 7 días desde el último envío
        """
        ultimo_envio_str = cliente.get('ultimo_envio')
        
        if not ultimo_envio_str:
            # Si nunca se envió, enviar hoy
            logger.info(f"  → Primer reporte para {cliente['Proyecto']}")
            return True
        
        try:
            # Parsear fecha del último envío
            if isinstance(ultimo_envio_str, str):
                ultimo_envio = datetime.fromisoformat(ultimo_envio_str.replace('Z', '+00:00')).date()
            else:
                ultimo_envio = ultimo_envio_str
            
            hoy = datetime.now().date()
            dias_transcurridos = (hoy - ultimo_envio).days
            
            logger.info(f"  → {cliente['Proyecto']}: {dias_transcurridos} días desde último envío")
            
            return dias_transcurridos >= 7
            
        except Exception as e:
            logger.warning(f"  → Error procesando último_envio: {e}")
            return True  # Si hay error, enviar para no perder reporte
    
    def enviar_reportes(self):
        """Ejecutar el flujo completo de envío"""
        logger.info("🚀 Iniciando envío de reportes automáticos...")
        
        clientes = self.obtener_clientes_a_reportar()
        
        if not clientes:
            logger.info("ℹ️ No hay clientes para reportar en este momento")
            return
        
        logger.info(f"📊 Enviando reportes a {len(clientes)} cliente(s)")
        
        for cliente in clientes:
            self._procesar_cliente(cliente)
        
        logger.info("✅ Ciclo de reportes completado")
    
    def _procesar_cliente(self, cliente):
        """
        Procesar un cliente individual: generar reporte y enviar
        """
        proyecto = cliente.get('Proyecto', 'Desconocido')
        chat_id = cliente.get('id_telegram')
        
        if not chat_id:
            logger.warning(f"⚠️ {proyecto}: Sin ID de Telegram")
            return
        
        try:
            logger.info(f"📝 Generando reporte para {proyecto}...")
            
            # Generar reporte
            reporte = self.generador.generar_reporte(cliente)
            
            if not reporte:
                logger.error(f"❌ {proyecto}: No se pudo generar reporte")
                return
            
            # Enviar por Telegram
            logger.info(f"📤 Enviando a Telegram ({chat_id})...")
            exito = self.telegram.enviar_reporte(chat_id, reporte)
            
            if exito:
                # Actualizar último_envio en base de datos
                self.supabase.actualizar_ultimo_envio(cliente.get('id'), datetime.now())
                logger.info(f"✅ {proyecto}: Reporte enviado exitosamente")
            else:
                logger.error(f"❌ {proyecto}: Fallo al enviar por Telegram")
        
        except Exception as e:
            logger.error(f"❌ {proyecto}: Error procesando cliente: {e}")


def main():
    """Punto de entrada principal"""
    try:
        scheduler = AutomaticReportScheduler()
        scheduler.enviar_reportes()
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
