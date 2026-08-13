"""BioCore Intelligence - generador automático canónico v2."""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt(v, decimals=4, suffix=""):
    n = _num(v)
    return "N/D" if n is None else f"{n:.{decimals}f}{suffix}"


def _short_id(report):
    return str(report.get("analysis_id") or "")[:12]


def validar_reporte_canonico(report: Dict[str, Any]) -> bool:
    return (
        isinstance(report, dict)
        and report.get("schema_version") == "biocore-report-2.0"
        and len(str(report.get("analysis_id") or "")) == 64
        and isinstance(report.get("metrics"), list)
        and isinstance(report.get("project"), dict)
    )


def formatear_reporte_canonico(report: Dict[str, Any]) -> str:
    project = report.get("project") or {}
    diagnosis = report.get("diagnosis") or {}
    trace = report.get("data_traceability") or {}
    quality = report.get("quality") or {}

    lines = [
        "📊 BioCore Intelligence - Vigilancia Ambiental",
        f"🌍 Proyecto: {project.get('name') or 'N/D'}",
        f"🏷️ Tipo: {project.get('type') or 'N/D'}",
        f"🔎 ID análisis: {_short_id(report)}",
        "",
        f"📌 Estado técnico: {diagnosis.get('level') or 'N/D'}",
        f"🧭 {diagnosis.get('title') or 'Sin síntesis disponible'}",
        "",
        "🛰️ Trazabilidad",
        f"• Sensor comparable: {trace.get('comparison_sensor') or 'N/D'}",
        f"• Periodo actual: {trace.get('current_window_start') or 'N/D'} a {trace.get('current_window_end') or 'N/D'}",
        f"• Referencia: {trace.get('baseline_window_start') or 'N/D'} a {trace.get('baseline_window_end') or 'N/D'}",
        f"• Regla: {trace.get('comparison_rule') or 'N/D'}",
        "",
        "📈 Indicadores del mismo análisis",
    ]

    for m in report.get("metrics") or []:
        current = _fmt(m.get("current"))
        baseline = _fmt(m.get("baseline"))
        delta = "N/D"
        if m.get("delta") is not None:
            delta = f"{float(m['delta']):+.4f}"
            if m.get("pct") is not None:
                delta += f" ({float(m['pct']):+.1f}%)"
        lines.append(f"• {m.get('code')}: {current} | ref {baseline} | Δ {delta}")

    context = report.get("scene_context") or {}
    if context.get("lst_mean_c") is not None:
        lines += ["", f"🌡️ LST MODIS: {float(context['lst_mean_c']):.1f} °C"]

    regulatory = report.get("regulatory") or {}
    if regulatory:
        lines += ["", "⚖️ Seguimiento regulatorio"]
        lines.append(f"• Estado: {regulatory.get('status') or 'N/D'}")
        if regulatory.get("summary"):
            lines.append(f"• {regulatory.get('summary')}")

    warnings = quality.get("warnings") or []
    if warnings:
        lines += ["", "⚠️ Calidad / limitaciones"]
        lines.extend(f"• {w}" for w in warnings[:4])

    for rec in (diagnosis.get("recommendations") or [])[:4]:
        if "💡 Acciones recomendadas" not in lines:
            lines += ["", "💡 Acciones recomendadas"]
        lines.append(f"• {rec}")

    lines += [
        "",
        "ℹ️ Evidencia de vigilancia satelital; no determina por sí sola impacto significativo, causalidad, incumplimiento ni obligación de notificación.",
        f"🔐 PDF y Telegram: ID {_short_id(report)}",
    ]
    return "\n".join(lines)


class GeneradorReportes:
    def __init__(self, supabase_client=None):
        self.supabase = supabase_client

    def generar_reporte(self, cliente: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        proyecto = str(cliente.get("Proyecto") or "").strip()
        if not proyecto or self.supabase is None:
            return None

        try:
            row = self.supabase.obtener_ultimo_reporte_canonico(proyecto)
            if not row:
                logger.warning("%s: sin reporte canónico v2; no se envía nada", proyecto)
                return None

            payload = row.get("payload_json")
            if not validar_reporte_canonico(payload):
                logger.error("%s: payload canónico inválido", proyecto)
                return None

            return {
                "canonical": True,
                "analysis_id": payload["analysis_id"],
                "proyecto": proyecto,
                "mensaje_directo": formatear_reporte_canonico(payload),
            }
        except Exception as exc:
            logger.error("%s: error generando reporte canónico: %s", proyecto, exc, exc_info=True)
            return None
