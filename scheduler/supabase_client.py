"""
Cliente para conectar con Supabase (PostgreSQL)
"""

import os
import logging
from datetime import datetime
import json

try:
    from supabase import create_client, Client
except ImportError:
    Client = None

logger = logging.getLogger(__name__)


class SupabaseClient:
    """Cliente para interactuar con base de datos Supabase"""
    
    def __init__(self):
        """Inicializar cliente Supabase"""
        self.url = os.getenv('SUPABASE_URL')
        self.key = os.getenv('SUPABASE_KEY')
        
        if not self.url or not self.key:
            raise ValueError("❌ SUPABASE_URL y SUPABASE_KEY requeridos en environment")
        
        try:
            self.client = create_client(self.url, self.key)
            logger.info(f"✅ Conectado a Supabase: {self.url}")
        except Exception as e:
            logger.error(f"❌ Error conectando a Supabase: {e}")
            raise
    
    def obtener_todos_usuarios(self):
        """
        Obtener lista de todos los usuarios/clientes
        
        Retorna: List[dict] con estructura:
        {
            'id': str,
            'Proyecto': str,
            'Tipo': str,
            'id_telegram': str,
            'frecuencia_reporte': str ('Diario' | 'Semanal'),
            'hora_reporte': str ('HH:MM'),
            'ultimo_envio': str (ISO datetime),
        }
        """
        try:
            response = self.client.table('usuarios').select('*').execute()
            datos = response.data
            logger.info(f"✅ Obtenidos {len(datos)} usuarios de Supabase")
            return datos
        except Exception as e:
            logger.error(f"❌ Error obteniendo usuarios: {e}")
            return []
    
    def obtener_usuario_por_id(self, usuario_id):
        """Obtener un usuario específico por ID"""
        try:
            response = self.client.table('usuarios').select('*').eq('id', usuario_id).execute()
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"❌ Error obteniendo usuario {usuario_id}: {e}")
            return None
    
    def obtener_historial_reportes(self, proyecto_id):
        """
        Obtener histórico de reportes de un proyecto
        
        Retorna: List[dict] con índices de biodiversidad y datos satelitales
        """
        try:
            response = self.client.table('historial_reportes').select('*').eq(
                'proyecto_id', proyecto_id
            ).order('fecha_reporte', desc=True).limit(10).execute()
            
            return response.data
        except Exception as e:
            logger.error(f"❌ Error obteniendo historial {proyecto_id}: {e}")
            return []
    
    def obtener_ultimo_reporte(self, proyecto_id):
        """Obtener el último reporte de un proyecto"""
        try:
            response = self.client.table('historial_reportes').select('*').eq(
                'proyecto_id', proyecto_id
            ).order('fecha_reporte', desc=True).limit(1).execute()
            
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"❌ Error obteniendo último reporte: {e}")
            return None
    
    def actualizar_ultimo_envio(self, usuario_id, fecha_envio):
        """
        Actualizar la columna 'ultimo_envio' en la tabla usuarios
        
        Args:
            usuario_id: ID del usuario en Supabase
            fecha_envio: datetime object o string ISO
        """
        try:
            # Convertir a ISO string si es datetime
            if isinstance(fecha_envio, datetime):
                fecha_str = fecha_envio.isoformat()
            else:
                fecha_str = fecha_envio
            
            response = self.client.table('usuarios').update({
                'ultimo_envio': fecha_str
            }).eq('id', usuario_id).execute()
            
            logger.info(f"✅ Actualizado último_envio para usuario {usuario_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Error actualizando último_envio: {e}")
            return False
    
    def registrar_envio(self, usuario_id, estado, detalles=None):
        """
        Registrar un intento de envío en tabla de auditoría (opcional)
        
        Args:
            usuario_id: ID del usuario
            estado: 'exitoso' | 'fallido'
            detalles: string con información adicional
        """
        try:
            registro = {
                'usuario_id': usuario_id,
                'fecha_envio': datetime.now().isoformat(),
                'estado': estado,
                'detalles': detalles or ''
            }
            
            # Crear tabla si existe
            response = self.client.table('auditoria_envios').insert([registro]).execute()
            return True
        except Exception as e:
            logger.warning(f"⚠️ No se pudo registrar auditoría: {e}")
            return False


class SupabaseClientMock:
    """Mock para testing sin conexión real a Supabase"""
    
    def obtener_todos_usuarios(self):
        return [
            {
                'id': '1',
                'Proyecto': 'Minera Los Andes',
                'Tipo': 'MINERIA',
                'id_telegram': '123456789',
                'frecuencia_reporte': 'Diario',
                'hora_reporte': '09:00',
                'ultimo_envio': None
            }
        ]
    
    def obtener_historial_reportes(self, proyecto_id):
        return [{
            'fecha_reporte': datetime.now().isoformat(),
            'indice_ndvi': 0.75,
            'indice_evi': 0.65,
            'cobertura_vegetal': 45.2
        }]
    
    def actualizar_ultimo_envio(self, usuario_id, fecha_envio):
        logger.info(f"[MOCK] Actualizado último_envio: {usuario_id}")
        return True
