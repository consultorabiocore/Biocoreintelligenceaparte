"""
BioCore Intelligence - Interfaz profesional v2 (Correccion 8)

Pantalla de resultados basada exclusivamente en el reporte canonico.

Principios UX:
- No usa gauges con umbrales universales.
- No llama "riesgo" a una clasificacion tecnica si no existe un criterio regulatorio.
- Distingue evidencia, interpretacion y estado regulatorio.
- Muestra N/D en vez de 0 para datos faltantes.
- PDF y Telegram salen del mismo analysis_id.
- Diseno responsive: las columnas de Streamlit se apilan en pantallas pequenas.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from biocore_reporte_canonico_v2 import (
    format_telegram,
    render_pdf,
    verify_analysis_id,
)


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------

def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value, decimals=4, suffix=""):
    value = _num(value)
    return "N/D" if value is None else f"{value:.{decimals}f}{suffix}"


def _fmt_delta(metric: Dict[str, Any]) -> str:
    delta = _num(metric.get("delta"))
    pct = _num(metric.get("pct"))

    if delta is None:
        return "N/D"

    text = f"{delta:+.4f}"
    if pct is not None:
        text += f" ({pct:+.1f}%)"
    return text


def _metric_lookup(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(m.get("code")): m
        for m in (report.get("metrics") or [])
        if m.get("code")
    }


def _status_icon(status: str) -> str:
    s = str(status or "").upper()
    if "INSUFICIENTE" in s or "NO EVALUABLE" in s:
        return "⚪"
    if "PRIORITARIA" in s or "LIMITE" in s:
        return "🔴"
    if "REVISAR" in s or "INTERMEDIO" in s:
        return "🟠"
    if "SIN SEÑAL" in s or "DENTRO" in s:
        return "🟢"
    if "SIN CRITERIO" in s:
        return "🔵"
    return "⚪"


def _pct(value) -> str:
    value = _num(value)
    return "N/D" if value is None else f"{value:.1%}"


# ---------------------------------------------------------------------------
# CSS y encabezado
# ---------------------------------------------------------------------------

def inject_biocore_ui_css():
    st.markdown(
        """
        <style>
        .bc-v2-hero {
            border: 1px solid rgba(148,163,184,.25);
            border-radius: 16px;
            padding: 18px 20px;
            margin-bottom: 16px;
            background: rgba(15,23,42,.42);
        }
        .bc-v2-eyebrow {
            font-size: .72rem;
            letter-spacing: .12em;
            text-transform: uppercase;
            opacity: .7;
            margin-bottom: 6px;
        }
        .bc-v2-title {
            font-size: 1.45rem;
            font-weight: 650;
            line-height: 1.2;
            margin: 0;
        }
        .bc-v2-sub {
            font-size: .88rem;
            opacity: .75;
            margin-top: 8px;
            line-height: 1.45;
        }
        .bc-v2-id {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: .75rem;
            opacity: .72;
            margin-top: 8px;
            word-break: break-all;
        }
        .bc-v2-note {
            border-left: 4px solid rgba(148,163,184,.65);
            padding: 10px 12px;
            margin: 8px 0;
            background: rgba(148,163,184,.08);
            border-radius: 0 8px 8px 0;
            font-size: .88rem;
        }
        .bc-v2-small {
            font-size: .78rem;
            opacity: .72;
        }
        @media (max-width: 700px) {
            .bc-v2-hero { padding: 15px 15px; }
            .bc-v2-title { font-size: 1.2rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_analysis_header(report: Dict[str, Any]):
    project = report.get("project") or {}
    aid = str(report.get("analysis_id") or "")

    st.markdown(
        f"""
        <div class="bc-v2-hero">
            <div class="bc-v2-eyebrow">BioCore Intelligence · análisis canónico</div>
            <div class="bc-v2-title">{project.get('name') or 'Proyecto sin nombre'}</div>
            <div class="bc-v2-sub">
                {project.get('type') or 'N/D'} ·
                referencia {project.get('baseline_year') or 'N/D'} ·
                generado {report.get('generated_at') or 'N/D'}
            </div>
            <div class="bc-v2-id">analysis_id: {aid}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Resumen ejecutivo
# ---------------------------------------------------------------------------

def render_status_row(report: Dict[str, Any]):
    quality = report.get("quality") or {}
    diagnosis = report.get("diagnosis") or {}
    regulatory = report.get("regulatory") or {}

    quality_status = (
        "EVIDENCIA UTILIZABLE"
        if quality.get("usable") is True
        else "EVIDENCIA INSUFICIENTE"
    )
    technical_status = diagnosis.get("level") or "NO EVALUADO"
    regulatory_status = regulatory.get("status") or "SIN CRITERIO REGULATORIO CARGADO"

    c1, c2, c3 = st.columns(3)

    with c1:
        st.metric(
            "Calidad de evidencia",
            f"{_status_icon(quality_status)} {quality_status}",
            help=(
                "Indica si la calidad y disponibilidad de los datos permiten "
                "una interpretación técnica. No es un estado ambiental."
            ),
        )

    with c2:
        st.metric(
            "Estado técnico",
            f"{_status_icon(technical_status)} {technical_status}",
            help=(
                "Síntesis del análisis de vigilancia. No equivale por sí sola "
                "a impacto significativo ni incumplimiento."
            ),
        )

    with c3:
        st.metric(
            "Estado regulatorio",
            f"{_status_icon(regulatory_status)} {regulatory_status}",
            help=(
                "Sólo puede activar umbrales cuando existe un instrumento "
                "trazable cargado en BioCore."
            ),
        )


def render_executive_summary(report: Dict[str, Any]):
    diagnosis = report.get("diagnosis") or {}
    regulatory = report.get("regulatory") or {}

    st.subheader("Lectura ejecutiva")
    st.markdown(
        f"""
        <div class="bc-v2-note">
        <strong>Interpretación técnica:</strong>
        {diagnosis.get('title') or 'No hay interpretación técnica disponible.'}
        </div>
        """,
        unsafe_allow_html=True,
    )

    if regulatory.get("summary"):
        st.markdown(
            f"""
            <div class="bc-v2-note">
            <strong>Lectura regulatoria:</strong>
            {regulatory.get('summary')}
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.caption(
        "BioCore separa observación, interpretación y obligación regulatoria. "
        "Una señal satelital no demuestra por sí sola causalidad, incumplimiento "
        "ni significancia ambiental."
    )


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------

PRIMARY_METRICS = ["NDVI", "SAVI", "NDWI", "NDMI", "NDSI"]


def render_primary_metrics(report: Dict[str, Any]):
    metrics = _metric_lookup(report)

    st.subheader("Indicadores comparables")

    # Primera fila: 3. Segunda: 2. En móvil Streamlit los apila.
    first = st.columns(3)
    second = st.columns(2)

    positions = list(zip(first + second, PRIMARY_METRICS))

    for col, code in positions:
        metric = metrics.get(code)
        with col:
            if not metric:
                st.metric(code, "N/D")
                continue

            st.metric(
                code,
                _fmt(metric.get("current")),
                _fmt_delta(metric),
                help=metric.get("interpretation_scope"),
            )
            st.caption(
                f"Referencia: {_fmt(metric.get('baseline'))} · "
                f"{metric.get('unit') or 'adimensional'}"
            )

    with st.expander("Ver todos los indicadores y alcance interpretativo"):
        rows = []
        for m in report.get("metrics") or []:
            rows.append(
                {
                    "Indicador": m.get("code"),
                    "Actual": _fmt(m.get("current")),
                    "Referencia": _fmt(m.get("baseline")),
                    "Cambio": _fmt_delta(m),
                    "Unidad": m.get("unit"),
                    "Alcance": m.get("interpretation_scope"),
                }
            )

        if rows:
            st.dataframe(
                pd.DataFrame(rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No hay indicadores disponibles.")


# ---------------------------------------------------------------------------
# Calidad y contexto
# ---------------------------------------------------------------------------

def render_quality_context(report: Dict[str, Any]):
    quality = report.get("quality") or {}
    context = report.get("scene_context") or {}

    st.subheader("Calidad y contexto de escena")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Píxeles válidos", _pct(quality.get("valid_fraction")))
    c2.metric("Vegetación SCL", _pct(context.get("scl_vegetation_fraction")))
    c3.metric("Agua SCL", _pct(context.get("scl_water_fraction")))
    c4.metric("Nieve/hielo SCL", _pct(context.get("scl_snow_ice_fraction")))

    c5, c6 = st.columns(2)
    with c5:
        st.metric(
            "Suelo desnudo SCL",
            _pct(context.get("scl_bare_fraction")),
        )
    with c6:
        st.metric(
            "LST MODIS",
            _fmt(context.get("lst_mean_c"), 1, " °C"),
            help="Temperatura de superficie, no temperatura del aire.",
        )

    warnings = quality.get("warnings") or []
    if warnings:
        st.warning(
            "Limitaciones de calidad:\n\n" +
            "\n".join(f"- {w}" for w in warnings)
        )
    else:
        st.success(
            "No se registraron advertencias adicionales de calidad en este análisis."
        )


# ---------------------------------------------------------------------------
# Hallazgos
# ---------------------------------------------------------------------------

def render_findings(report: Dict[str, Any]):
    diagnosis = report.get("diagnosis") or {}
    findings = diagnosis.get("findings") or []

    st.subheader("Evidencia e interpretación")

    if not findings:
        st.info("No hay hallazgos interpretables con los datos disponibles.")
        return

    for finding in findings:
        title = str(finding.get("object") or "Hallazgo").capitalize()

        with st.expander(title, expanded=True):
            observations = finding.get("observations") or []
            confounders = finding.get("confounders") or []

            if observations:
                st.markdown("**Observaciones medidas**")
                for obs in observations:
                    st.write(f"• {obs}")

            if confounders:
                st.markdown("**Confusores / limitaciones**")
                for conf in confounders:
                    st.warning(conf)

            if finding.get("interpretation"):
                st.markdown("**Alcance de interpretación**")
                st.write(finding["interpretation"])


# ---------------------------------------------------------------------------
# SAR y anomalías térmicas
# ---------------------------------------------------------------------------

def render_ancillary(report: Dict[str, Any]):
    ancillary = report.get("ancillary") or {}
    s1 = ancillary.get("sentinel1") or {}
    fire = ancillary.get("active_fire") or {}

    st.subheader("Evidencia complementaria")

    t_sar, t_fire = st.tabs(["Sentinel-1 SAR", "VIIRS Active Fire"])

    with t_sar:
        if not s1.get("available"):
            st.info("No hay datos Sentinel-1 comparables en esta ejecución.")
        else:
            st.caption(
                s1.get("interpretation_scope") or
                "Evidencia radar complementaria."
            )

            rows = []
            for key in ("ascending", "descending"):
                block = (s1.get("passes") or {}).get(key)
                if not block:
                    continue

                cur = block.get("current") or {}
                base = block.get("baseline") or {}
                change = block.get("change") or {}

                rows.append(
                    {
                        "Pasada": block.get("orbit_pass"),
                        "Órbita relativa": block.get("relative_orbit"),
                        "VV actual (dB)": _fmt(cur.get("vv_db"), 2),
                        "VV referencia (dB)": _fmt(base.get("vv_db"), 2),
                        "Δ VV (dB)": _fmt(change.get("vv_db"), 2),
                        "VH actual (dB)": _fmt(cur.get("vh_db"), 2),
                        "Ángulo incidencia": _fmt(
                            cur.get("incidence_angle_deg"), 1, "°"
                        ),
                        "Comparabilidad": block.get("comparability"),
                    }
                )

            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )

            for warning in s1.get("warnings") or []:
                st.caption(f"• {warning}")

    with t_fire:
        if not fire.get("available"):
            st.info("No hay información VIIRS disponible en esta ejecución.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric(
                "Detección térmica",
                "Sí" if fire.get("detection") else "No",
            )
            c2.metric(
                "Píxel-día nominal/alto",
                str(fire.get("combined_nominal_high_pixel_days", 0)),
            )
            c3.metric(
                "Píxel-día alta confianza",
                str(fire.get("combined_high_confidence_pixel_days", 0)),
            )
            st.caption(
                f"Ventana: {fire.get('window_start') or 'N/D'} a "
                f"{fire.get('window_end') or 'N/D'}."
            )
            st.info(
                fire.get("interpretation") or
                "Las detecciones térmicas no equivalen a incendios únicos."
            )
            st.caption(fire.get("quality_note") or "")


# ---------------------------------------------------------------------------
# Series temporales
# ---------------------------------------------------------------------------

def render_temporal(
    report: Dict[str, Any],
    temporal_bundle: Optional[Dict[str, Any]] = None,
    chart_paths: Optional[List[str]] = None,
):
    temporal = report.get("temporal") or {}
    bundle = temporal_bundle or {}
    charts = [p for p in (chart_paths or []) if p and Path(p).exists()]

    st.subheader("Serie temporal")

    c1, c2, c3 = st.columns(3)
    c1.metric("Rango solicitado", temporal.get("range_label") or "N/D")
    c2.metric(
        "Observaciones espectrales",
        str(temporal.get("n_spectral_records") or 0),
    )
    c3.metric(
        "Observaciones climáticas",
        str(temporal.get("n_climate_records") or 0),
    )

    if charts:
        for path in charts:
            st.image(path, use_container_width=True)
    else:
        st.info(
            "No hay gráficos válidos para este período. "
            "BioCore no completa la serie con puntos ficticios."
        )

    with st.expander("Ver datos de la serie"):
        spectral_records = bundle.get("spectral_records") or []
        climate_records = bundle.get("climate_records") or []

        if spectral_records:
            st.markdown("**Registros espectrales**")
            st.dataframe(
                pd.DataFrame(spectral_records),
                use_container_width=True,
                hide_index=True,
            )

        if climate_records:
            st.markdown("**Contexto climático**")
            st.dataframe(
                pd.DataFrame(climate_records),
                use_container_width=True,
                hide_index=True,
            )

        if not spectral_records and not climate_records:
            st.info("No hay registros temporales disponibles.")


# ---------------------------------------------------------------------------
# Regulación
# ---------------------------------------------------------------------------

def render_regulatory(report: Dict[str, Any]):
    regulatory = report.get("regulatory") or {}

    st.subheader("Estado regulatorio")

    if not regulatory:
        st.info(
            "No se cargó un instrumento regulatorio específico. "
            "BioCore no infiere incumplimiento ni obligación de reporte."
        )
        return

    st.metric(
        "Resultado",
        f"{_status_icon(regulatory.get('status'))} "
        f"{regulatory.get('status') or 'N/D'}",
    )

    if regulatory.get("summary"):
        st.write(regulatory["summary"])

    if regulatory.get("plan_name"):
        st.caption(
            f"Plan: {regulatory.get('plan_name')} · "
            f"Componente: {regulatory.get('component') or 'N/D'} · "
            f"Variable: {regulatory.get('environmental_variable') or 'N/D'}"
        )

    checks = regulatory.get("checks") or []
    if checks:
        rows = []
        for c in checks:
            source = c.get("source") or {}
            rows.append(
                {
                    "Regla": c.get("rule_id"),
                    "Parámetro": c.get("parameter"),
                    "Estado": c.get("status"),
                    "Valor": c.get("observed_value"),
                    "Δ": c.get("delta_value"),
                    "Fuente": source.get("tipo"),
                    "Identificador": source.get("identificador"),
                    "Sección/página": (
                        source.get("seccion")
                        or source.get("pagina")
                        or source.get("clausula")
                    ),
                }
            )

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

    obligations = regulatory.get("reporting_obligations") or []
    if obligations:
        st.warning("El instrumento cargado contiene obligación(es) de reporte:")
        for ob in obligations:
            st.write(
                f"• {ob.get('report_to') or 'N/D'} · "
                f"plazo {ob.get('deadline') or 'N/D'} · "
                f"regla {ob.get('rule_id') or 'N/D'}"
            )
    else:
        st.caption(
            "No se infiere una obligación de reporte fuera de las reglas "
            "expresamente cargadas."
        )


# ---------------------------------------------------------------------------
# Trazabilidad
# ---------------------------------------------------------------------------

def render_traceability(report: Dict[str, Any]):
    trace = report.get("data_traceability") or {}

    st.subheader("Trazabilidad metodológica")

    rows = [
        ("Sensor comparable", trace.get("comparison_sensor")),
        ("Colección comparable", trace.get("comparison_collection")),
        ("Resolución comparable", (
            f"{trace.get('comparison_scale_m')} m"
            if trace.get("comparison_scale_m") is not None else "N/D"
        )),
        (
            "Período actual",
            f"{trace.get('current_window_start') or 'N/D'} a "
            f"{trace.get('current_window_end') or 'N/D'}",
        ),
        (
            "Período referencia",
            f"{trace.get('baseline_window_start') or 'N/D'} a "
            f"{trace.get('baseline_window_end') or 'N/D'}",
        ),
        ("Escenas actuales", trace.get("current_n_scenes")),
        ("Escenas referencia", trace.get("baseline_n_scenes")),
        ("Regla de comparación", trace.get("comparison_rule")),
        ("Sentinel-2 más reciente", trace.get("latest_sentinel2_date")),
        ("Máscara Sentinel-2", trace.get("sentinel2_mask")),
        ("Colección LST", trace.get("lst_collection")),
        ("QA LST", trace.get("lst_qa")),
    ]

    st.dataframe(
        pd.DataFrame(rows, columns=["Elemento", "Valor"]),
        use_container_width=True,
        hide_index=True,
    )

    references = report.get("references") or []
    if references:
        st.markdown("**Referencias metodológicas del análisis**")
        for ref in references:
            st.markdown(
                f"**[{ref.get('id')}]** {ref.get('citation')}  \n"
                f"{ref.get('role')}"
            )

    integrity = verify_analysis_id(report)
    if integrity:
        st.success(
            f"Integridad verificada: analysis_id {report.get('analysis_id')}"
        )
    else:
        st.error(
            "El analysis_id no coincide con el contenido del reporte. "
            "No descargues ni envíes este análisis."
        )


# ---------------------------------------------------------------------------
# Acciones: PDF y Telegram
# ---------------------------------------------------------------------------

def _build_pdf_if_needed(
    report: Dict[str, Any],
    chart_paths: Optional[List[str]],
) -> Optional[str]:
    try:
        filename = (
            f"BioCore_{report.get('project', {}).get('name', 'Proyecto')}_"
            f"{str(report.get('analysis_id') or '')[:12]}.pdf"
        )
        safe_name = "".join(
            c if c.isalnum() or c in "._-" else "_"
            for c in filename
        )
        path = os.path.join(tempfile.gettempdir(), safe_name)

        render_pdf(
            report,
            output_path=path,
            chart_paths=chart_paths or [],
        )
        return path
    except Exception as exc:
        st.error(f"No fue posible generar el PDF: {exc}")
        return None


def render_report_actions(
    report: Dict[str, Any],
    chart_paths: Optional[List[str]] = None,
):
    st.subheader("Reporte y mensajería")

    if not verify_analysis_id(report):
        st.error(
            "Integridad del análisis no válida. "
            "Se bloquean PDF y Telegram."
        )
        return

    pdf_path = _build_pdf_if_needed(report, chart_paths)

    c1, c2 = st.columns(2)

    with c1:
        if pdf_path and Path(pdf_path).exists():
            pdf_bytes = Path(pdf_path).read_bytes()
            st.download_button(
                "📄 Descargar PDF técnico",
                data=pdf_bytes,
                file_name=Path(pdf_path).name,
                mime="application/pdf",
                use_container_width=True,
            )
            st.caption(
                f"PDF vinculado al analysis_id "
                f"{str(report.get('analysis_id'))[:12]}."
            )

    with c2:
        telegram_text = format_telegram(report)
        st.download_button(
            "📱 Descargar texto Telegram",
            data=telegram_text.encode("utf-8"),
            file_name=(
                f"Telegram_"
                f"{str(report.get('analysis_id') or '')[:12]}.txt"
            ),
            mime="text/plain",
            use_container_width=True,
        )
        st.caption(
            "El scheduler automático debe leer este mismo reporte canónico."
        )

    with st.expander("Vista previa exacta de Telegram"):
        st.code(format_telegram(report), language="text")


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------

def render_analysis_dashboard(
    report: Dict[str, Any],
    temporal_bundle: Optional[Dict[str, Any]] = None,
    chart_paths: Optional[List[str]] = None,
):
    """
    Punto de entrada de la Corrección 8.
    """
    inject_biocore_ui_css()
    render_analysis_header(report)
    render_status_row(report)
    render_executive_summary(report)

    tabs = st.tabs(
        [
            "Resumen",
            "Evidencia",
            "Series",
            "Regulación",
            "Trazabilidad",
            "Reporte",
        ]
    )

    with tabs[0]:
        render_primary_metrics(report)
        render_quality_context(report)

    with tabs[1]:
        render_findings(report)
        render_ancillary(report)

    with tabs[2]:
        render_temporal(
            report,
            temporal_bundle=temporal_bundle,
            chart_paths=chart_paths,
        )

    with tabs[3]:
        render_regulatory(report)

    with tabs[4]:
        render_traceability(report)

    with tabs[5]:
        render_report_actions(report, chart_paths=chart_paths)
