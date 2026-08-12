"""
Generador de reportes con datos de biodiversidad e índices satelitales
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class GeneradorReportes:
    """Generador de reportes para cada cliente"""
    
    def __init__(self, supabase_client=None):
        """
        Inicializar generador de reportes
        
        Args:
            supabase_client: Cliente Supabase (opcional para inyección de dependencia)
        """
        self.supabase = supabase_client
    
    def generar_reporte(self, cliente: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generar reporte completo para un cliente
        
        Args:
            cliente: Dict con datos del cliente
        
        Returns:
            Dict con estructura de reporte o None si hay error
        """
        try:
            proyecto = cliente.get('Proyecto', 'Desconocido')
            proyecto_id = cliente.get('id')
            
            logger.info(f"📝 Generando reporte para {proyecto}...")
            
            # Obtener datos del último reporte guardado
            datos_actuales = self._obtener_datos_proyecto(proyecto_id)
            
            if not datos_actuales:
                logger.warning(f"⚠️ Sin datos disponibles para {proyecto}")
                datos_actuales = self._datos_default()
            
            # Construir reporte
            reporte = {
                'titulo': f'Reporte de Biodiversidad - {proyecto}',
                'fecha': datetime.now().strftime('%d/%m/%Y %H:%M'),
                'proyecto': proyecto,
                'resumen': self._generar_resumen(datos_actuales),
                'indicadores': self._extraer_indicadores(datos_actuales),
                'graficos': self._obtener_urls_graficos(proyecto_id),
                'recomendaciones': self._generar_recomendaciones(datos_actuales)
            }
            
            logger.info(f"✅ Reporte generado para {proyecto}")
            return reporte
        
        except Exception as e:
            logger.error(f"❌ Error generando reporte: {e}")
            return None
    
    def _obtener_datos_proyecto(self, proyecto_id: str) -> Optional[Dict[str, Any]]:
        """Obtener últimos datos del proyecto desde Supabase"""
        if not self.supabase:
            return None
        
        try:
            reporte = self.supabase.obtener_ultimo_reporte(proyecto_id)
            return reporte
        except Exception as e:
            logger.warning(f"⚠️ Error obteniendo datos: {e}")
            return None
    
    def _datos_default(self) -> Dict[str, Any]:
        """Retornar datos por defecto si no hay datos reales"""
        return {
            'indice_ndvi': 0.65,
            'indice_evi': 0.55,
            'cobertura_vegetal': 42.5,
            'temperatura_superficie': 22.3,
            'humedad_suelo': 0.35,
            'estado_vegetacion': 'Normal',
            'fecha_reporte': datetime.now().isoformat()
        }
    
    def _generar_resumen(self, datos: Dict[str, Any]) -> str:
        """
        Generar un resumen textual del estado actual
        """
        ndvi = datos.get('indice_ndvi', 0.65)
        cobertura = datos.get('cobertura_vegetal', 42.5)
        estado = datos.get('estado_vegetacion', 'Normal')
        
        # Evaluar estado
        if ndvi > 0.7:
            salud = "Buena salud vegetal 🟢"
        elif ndvi > 0.5:
            salud = "Salud vegetal moderada 🟡"
        else:
            salud = "Salud vegetal baja 🔴"
        
        resumen = f"""
El área monitoreada presenta {salud}.

Cobertura vegetal: {cobertura:.1f}%
Índice NDVI: {ndvi:.2f} (rango 0-1)
Estado actual: {estado}

Los cambios se están monitoreando constantemente.
        """.strip()
        
        return resumen
    
    def _extraer_indicadores(self, datos: Dict[str, Any]) -> Dict[str, str]:
        """Extraer indicadores clave para mostrar"""
        return {
            'NDVI': f"{datos.get('indice_ndvi', 0):.2f}",
            'EVI': f"{datos.get('indice_evi', 0):.2f}",
            'Cobertura Vegetal': f"{datos.get('cobertura_vegetal', 0):.1f}%",
            'Temperatura': f"{datos.get('temperatura_superficie', 0):.1f}°C",
            'Humedad del Suelo': f"{datos.get('humedad_suelo', 0):.2f}",
        }
    
    def _generar_recomendaciones(self, datos: Dict[str, Any]) -> str:
        """
        Generar recomendaciones basadas en los datos
        """
        ndvi = datos.get('indice_ndvi', 0.65)
        cobertura = datos.get('cobertura_vegetal', 42.5)
        
        recomendaciones = []
        
        if ndvi < 0.5:
            recomendaciones.append("⚠️ Considere acciones de recuperación vegetal")
        
        if cobertura < 30:
            recomendaciones.append("📍 La cobertura vegetal está por debajo de lo óptimo")
        
        if ndvi > 0.7:
            recomendaciones.append("✅ Mantener monitoreo regular del área")
        
        if not recomendaciones:
            recomendaciones.append("📊 El área se encuentra en condiciones normales")
        
        return "\n".join(recomendaciones)
    
    def _obtener_urls_graficos(self, proyecto_id: str) -> list:
        """
        Obtener URLs de gráficos generados (si existen en Supabase storage)
        
        Retorna: lista de URLs de imágenes
        """
        # Placeholder - implementar si se almacenan gráficos en Supabase Storage
        return []


class GeneradorReportesMock:
    """Mock para testing"""
    
    def generar_reporte(self, cliente: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'titulo': f"Reporte - {cliente.get('Proyecto')}",
            'fecha': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'proyecto': cliente.get('Proyecto'),
            'resumen': 'Resumen de prueba',
            'indicadores': {'NDVI': '0.75', 'Cobertura': '45%'},
            'graficos': [],
            'recomendaciones': 'Mantener monitoreo'
      }
