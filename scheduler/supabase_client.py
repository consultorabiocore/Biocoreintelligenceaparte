"""BioCore Intelligence - cliente Supabase para scheduler canónico v2."""
import logging
import os
from datetime import datetime
from supabase import create_client

logger = logging.getLogger(__name__)


class SupabaseClient:
    def __init__(self):
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL y SUPABASE_KEY son requeridos")
        self.client = create_client(url, key)

    def obtener_todos_usuarios(self):
        try:
            return self.client.table("usuarios").select("*").execute().data or []
        except Exception as exc:
            logger.error("Error obteniendo usuarios: %s", exc)
            return []

    def obtener_ultimo_reporte_canonico(self, proyecto):
        try:
            response = (
                self.client.table("historial_reportes")
                .select("proyecto,analysis_id,schema_version,method_version,payload_json,source_data_date,created_at")
                .eq("proyecto", proyecto)
                .eq("schema_version", "biocore-report-2.0")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as exc:
            logger.error("Error obteniendo reporte canónico de %s: %s", proyecto, exc)
            return None

    def actualizar_ultimo_envio(self, usuario_id, fecha_envio):
        if not usuario_id:
            return False
        try:
            value = fecha_envio.isoformat() if isinstance(fecha_envio, datetime) else str(fecha_envio)
            self.client.table("usuarios").update({"ultimo_envio": value}).eq("id", usuario_id).execute()
            return True
        except Exception as exc:
            logger.error("Error actualizando ultimo_envio: %s", exc)
            return False
