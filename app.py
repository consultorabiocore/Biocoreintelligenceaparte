# ============================================================================
# BIOCORE INTELLIGENCE - SISTEMA COMPLETO (VERSIÓN FINAL INTEGRADA)
# ============================================================================

# Compatibility entrypoint for the existing Streamlit Community Cloud app.
# The legacy implementation remains below for rollback/reference, but the
# deployed `app.py` now executes the professional public/private platform.
# `run_path` is intentional: Streamlit must rebuild the authenticated context
# on every rerun instead of reusing Python's cached import.
from pathlib import Path
import runpy

import streamlit as st

runpy.run_path(
    str(Path(__file__).with_name("biocore_app.py")),
    run_name="__main__",
)

st.stop()

import streamlit as st
import ee
import folium
from streamlit_folium import folium_static
import json
import pandas as pd
import requests
from datetime import datetime, time, timedelta
import plotly.graph_objects as go
from supabase import create_client, Client
import matplotlib.pyplot as plt
import matplotlib
import matplotlib.ticker as ticker
from fpdf import FPDF
import os
import tempfile
import hashlib
import hmac
from io import BytesIO
import numpy as np

import streamlit as st
import os

from telegram_reporter import (
    mostrar_formulario_reportes,
    mostrar_resumen_reportes,
    guardar_reporte,
    obtener_reporte_existente
)

matplotlib.use('Agg')

# --- CONFIGURACIÓN ---
st.set_page_config(page_title="Biocore Intelligence", layout="wide")

st.markdown("""
<style>
    [data-testid="stSidebar"] {
        background-color: #0e1117;
    }
    h1 {
        font-size: 2rem !important;
    }
    h2 {
        font-size: 1.5rem !important;
    }
</style>
""", unsafe_allow_html=True)

# === INICIALIZAR BASES DE DATOS ===
@st.cache_resource
def init_db():
    return create_client(
        st.secrets["connections"]["supabase"]["url"],
        st.secrets["connections"]["supabase"]["key"]
    )

supabase = init_db()

def iniciar_gee():
    try:
        if not ee.data.is_initialized():
            creds = json.loads(st.secrets["gee"]["json"])
            ee_creds = ee.ServiceAccountCredentials(
                creds['client_email'], 
                key_data=creds['private_key']
            )
            ee.Initialize(ee_creds, project=creds.get('project_id'))
            return True
    except Exception as e:
        st.error(f"Error crítico en GEE: {e}")
        return False

gee_status = iniciar_gee()

def clean(text):
    """Limpia caracteres especiales para FPDF"""
    return text.encode('latin-1', errors='replace').decode('latin-1')

# === AUTENTICACIÓN ===
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def es_admin(contraseña_admin):
    """Temporary legacy access; disabled unless a secure hash is configured."""
    config = st.secrets.get("legacy_admin", {})
    if not config.get("enabled", False):
        return False
    salt = str(config.get("password_salt") or "")
    expected = str(config.get("password_hash") or "")
    iterations = int(config.get("iterations") or 600_000)
    if not salt or not expected:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        contraseña_admin.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(derived, expected)

def verificar_credenciales_usuario(email, password):
    try:
        email_normalizado = email.strip().lower()
        res = supabase.table("usuarios").select("*").eq("email_cliente", email_normalizado).execute()
        if res.data:
            cliente = res.data[0]
            password_guardada = cliente.get('password_cliente', '')
            if password_guardada and hash_password(password) == password_guardada:
                return True, cliente
            return False, None
        # Si no encontró por email, intentar búsqueda insensible a mayúsculas/minúsculas
        # (por si el email fue guardado con formato diferente)
        res_all = supabase.table("usuarios").select("email_cliente, password_cliente, Proyecto, Tipo, Coordenadas, ano_linea_base, id_telegram, hora_reporte, frecuencia_reporte").execute()
        if res_all.data:
            for cliente in res_all.data:
                email_guardado = (cliente.get('email_cliente') or '').strip().lower()
                if email_guardado == email_normalizado:
                    password_guardada = cliente.get('password_cliente', '')
                    if password_guardada and hash_password(password) == password_guardada:
                        return True, cliente
        return False, None
    except Exception as e:
        return False, None

# === FUNCIÓN DE MAPA ===
def dibujar_mapa_biocore(coordenadas):
    try:
        if isinstance(coordenadas, str):
            coordenadas = json.loads(coordenadas)

        lons = [c[0] for c in coordenadas]
        lats = [c[1] for c in coordenadas]
        centro = [sum(lats)/len(lats), sum(lons)/len(lons)]

        m = folium.Map(
            location=centro,
            zoom_start=13,
            tiles='https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
            attr='Google Satellite'
        )

        folium.Polygon(
            locations=[[c[1], c[0]] for c in coordenadas],
            color="cyan",
            weight=2,
            fill=True,
            fill_opacity=0.2
        ).add_to(m)

        return m
    except Exception as e:
        return folium.Map(location=[-33.45, -70.66], zoom_start=4)

# === OBTENER COORDENADAS CORRECTAMENTE ===
def limpiar_coordenadas(coords):
    """
    Valida y limpia coordenadas.
    Asegura que estén en formato [[lon, lat], ...] y en rango válido.
    """
    if not isinstance(coords, list):
        raise ValueError(f"Coordenadas debe ser una lista, no {type(coords)}")

    if len(coords) < 3:
        raise ValueError("Mínimo 3 puntos requeridos para polígono")

    coords_limpios = []
    for i, coord in enumerate(coords):
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            try:
                lon = float(coord[0])
                lat = float(coord[1])
                if not (-180 <= lon <= 180):
                    raise ValueError(f"Longitud fuera de rango: {lon}")
                if not (-90 <= lat <= 90):
                    raise ValueError(f"Latitud fuera de rango: {lat}")
                coords_limpios.append([lon, lat])
            except (ValueError, TypeError) as e:
                raise ValueError(f"Coordenada {i} inválida: {coord} - {str(e)}")
        else:
            raise ValueError(f"Coordenada {i} no es [lon, lat]: {coord}")

    # Asegurar que el polígono cierre
    if coords_limpios[0] != coords_limpios[-1]:
        coords_limpios.append(coords_limpios[0])

    return coords_limpios


def obtener_coordenadas_correctamente(p):
    """
    Obtiene las coordenadas del proyecto desde Supabase.
    Versión mejorada con corrección automática de formato y validación de rangos.
    """
    raw_coords = p.get('Coordenadas')

    if raw_coords is None or raw_coords == '' or raw_coords == 'null':
        raise ValueError('Coordenadas vacías')

    if isinstance(raw_coords, list):
        return limpiar_coordenadas(raw_coords)

    if isinstance(raw_coords, str):
        try:
            coords = json.loads(raw_coords)
            return limpiar_coordenadas(coords)
        except json.JSONDecodeError:
            try:
                coords = eval(raw_coords)
                return limpiar_coordenadas(coords)
            except:
                raise ValueError(f'No se pudo parsear: {raw_coords}')

    if isinstance(raw_coords, dict):
        if 'coordinates' in raw_coords:
            return limpiar_coordenadas(raw_coords['coordinates'])

    raise ValueError(f'Formato no reconocido: {type(raw_coords)}')

# ============================================================================
# MÓDULO 0: PORTADA MEJORADA
# ============================================================================
def crear_portada_biocore():
    """Portada mejorada con diseño profesional BioCore Intelligence"""

    st.markdown("""
    <style>
    .bc-logo-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 0 18px 0;
        border-bottom: 0.5px solid #1e2535;
        margin-bottom: 18px;
    }
    .bc-logo-bar img {
        height: 52px;
        object-fit: contain;
    }
    .bc-live-badge {
        background: #0f2a1a;
        border: 0.5px solid #1a5c35;
        color: #4ade80;
        font-size: 0.75em;
        padding: 4px 12px;
        border-radius: 20px;
    }
    .bc-tagline {
        text-align: center;
        margin-bottom: 6px;
    }
    .bc-tagline .bc-sup {
        font-size: 0.72em;
        color: #64748b;
        letter-spacing: 2px;
        text-transform: uppercase;
        display: block;
        margin-bottom: 6px;
    }
    .bc-tagline h1 {
        font-size: 1.6rem !important;
        font-weight: 500;
        color: #e2e8f0;
        margin-bottom: 6px;
    }
    .bc-tagline p {
        color: #64748b;
        font-size: 0.92em;
        max-width: 600px;
        margin: 0 auto;
        line-height: 1.6;
    }
    .bc-sensors {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 7px;
        margin: 14px 0 18px 0;
    }
    .bc-sensor-pill {
        background: #0f1a2e;
        border: 0.5px solid #1e3a5f;
        color: #7ec8f5;
        font-size: 0.72em;
        padding: 4px 11px;
        border-radius: 20px;
    }
    .bc-features {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 12px;
        margin: 18px 0;
    }
    @media (max-width: 700px) {
        .bc-features { grid-template-columns: repeat(2, 1fr); }
    }
    .bc-feat-card {
        background: #0d1120;
        border: 0.5px solid #1e2535;
        border-radius: 10px;
        padding: 14px 14px 12px;
    }
    .bc-feat-card .bc-icon { font-size: 1.4em; margin-bottom: 6px; }
    .bc-feat-card .bc-ftitle {
        font-size: 0.82em;
        font-weight: 500;
        color: #cbd5e1;
        margin-bottom: 4px;
    }
    .bc-feat-card .bc-fdesc {
        font-size: 0.72em;
        color: #475569;
        line-height: 1.45;
    }
    .bc-access-box {
        background: #0d1120;
        border: 0.5px solid #1e2535;
        border-radius: 10px;
        padding: 18px 22px;
        text-align: center;
        margin-top: 4px;
    }
    .bc-access-box h3 {
        color: #94a3b8;
        font-size: 0.95em;
        font-weight: 500;
        margin-bottom: 6px;
    }
    .bc-access-box p {
        color: #475569;
        font-size: 0.82em;
        margin-bottom: 4px;
    }
    .bc-access-box a { color: #7ec8f5; }
    </style>

    <div class="bc-logo-bar">
        <img src="https://raw.githubusercontent.com/consultorabiocore/Biocoreintelligence/refs/heads/main/logo_biocore.png"
             alt="BioCore Intelligence"
             onerror="this.style.display='none'">
        <span class="bc-live-badge">⬀ Sistema activo</span>
    </div>

    <div class="bc-tagline">
        <span class="bc-sup">BioCore Intelligence</span>
        <h1>Auditoría de Vigilancia Ambiental y Resiliencia Climática</h1>
        <p>Evidencia técnica satelital antes de que llegue la fiscalización.</p>
    </div>

    <div class="bc-sensors">
        <span class="bc-sensor-pill">Sentinel-2</span>
        <span class="bc-sensor-pill">Sentinel-1 SAR</span>
        <span class="bc-sensor-pill">Landsat 8/9</span>
        <span class="bc-sensor-pill">MODIS NASA</span>
        <span class="bc-sensor-pill">NASA FIRMS</span>
        <span class="bc-sensor-pill">Hansen GFC</span>
        <span class="bc-sensor-pill">ERA5-Land ECMWF</span>
        <span class="bc-sensor-pill">CONAF Bosques</span>
        <span class="bc-sensor-pill">Copernicus LC</span>
    </div>
    """, unsafe_allow_html=True)

    # TAB para elegir entre mapa 2D o 3D
    tab_2d, tab_3d = st.tabs(["🗺️ Mapa 2D (Folium)", "🎯 Mapa 3D (Three.js)"])
    
    with tab_2d:
        demo_map = folium.Map(
            location=[-37.0, -71.5],
            zoom_start=5,
            tiles='https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
            attr='Google Satellite'
        )
        folium_static(demo_map, width=1200, height=380)

    with tab_3d:
        st.markdown("""
<iframe 
    src="http://127.0.0.1:5173" 
    width="100%" 
    height="900" 
    frameborder="0"
    style="border: none; border-radius: 10px;">
</iframe>
""", unsafe_allow_html=True)

    st.markdown("""
    <div class="bc-features">
        <div class="bc-feat-card">
            <div class="bc-icon">🛰️</div>
            <div class="bc-ftitle">Índices espectrales</div>
            <div class="bc-fdesc">SAVI, NDWI, NDSI, NDVI en resolución 10-30m con Sentinel-2 y Landsat</div>
        </div>
        <div class="bc-feat-card">
            <div class="bc-icon">📄</div>
            <div class="bc-ftitle">Reportes PDF</div>
            <div class="bc-fdesc">Descargables con firma técnica y análisis histórico de 20 años</div>
        </div>
        <div class="bc-feat-card">
            <div class="bc-icon">📡</div>
            <div class="bc-ftitle">Alertas Telegram</div>
            <div class="bc-fdesc">Notificaciones automáticas según frecuencia diaria o semanal</div>
        </div>
        <div class="bc-feat-card">
            <div class="bc-icon">🔭</div>
            <div class="bc-ftitle">Radar Sentinel-1</div>
            <div class="bc-fdesc">Detección de focos activos y análisis de superficie vía SAR</div>
        </div>
    </div>

    <div class="bc-access-box">
        <h3>Acceso restringido</h3>
        <p>Inicia sesión desde el panel izquierdo</p>
        <p><a href="mailto:consultorabiocore@gmail.com">consultorabiocore@gmail.com</a></p>
    </div>
    """, unsafe_allow_html=True)

# [RESTO DEL CÓDIGO - continúa igual desde aquí...]
# Incluye todos los módulos de generar_mensaje_telegram_dinamico, generar_graficos_profesionales, etc.
# hasta el final del archivo
# ============================================================================
# MÓDULO 1: GENERADOR DE REPORTE TELEGRAM DINÁMICO (CORREGIDO)
# ============================================================================
def generar_mensaje_telegram_dinamico(reporte_data, proyecto_data):
    """
    Generador dinámico de reportes BioCore Intelligence.
    Integra análisis específico por tipo de proyecto con año de línea base.
    Versión mejorada con secciones contextualizadas.
    """
    try:
        # Extracción segura de datos
        tipo = str((proyecto_data or {}).get('Tipo', 'GENERAL')).upper()
        proyecto = str((proyecto_data or {}).get('Proyecto', 'N/A'))
        fecha = str((reporte_data or {}).get('fecha', 'N/A'))
        anio_base = int((proyecto_data or {}).get('ano_linea_base', 2017))

        # Índices espectrales
        savi = float((reporte_data or {}).get('savi_actual', 0) or 0)
        ndwi = float((reporte_data or {}).get('ndwi', 0) or 0)
        swir = float((reporte_data or {}).get('swir', 0) or 0)
        ndsi = float((reporte_data or {}).get('ndsi', 0) or 0)
        ndvi = float((reporte_data or {}).get('ndvi', 0) or 0)
        clay = float((reporte_data or {}).get('clay', 0) or 0)
        sar_vv = float((reporte_data or {}).get('sar_vv', 0) or 0)
        temp = float((reporte_data or {}).get('temp', 0) or 0)
        incendios = int((reporte_data or {}).get('incendios_activos', 0) or 0)
        
        # Variaciones
        v_savi = float((reporte_data or {}).get('variacion', 0) or 0)
        v_ndwi = float((reporte_data or {}).get('variacion_ndwi', 0) or 0)
        v_ndsi = float((reporte_data or {}).get('variacion_ndsi', 0) or 0)
        v_ndvi = float((reporte_data or {}).get('variacion_ndvi', 0) or 0)
        
        # Líneas base
        savi_base = float((reporte_data or {}).get('savi_base', savi) or savi)
        ndwi_base = float((reporte_data or {}).get('ndwi_base', ndwi) or ndwi)
        ndsi_base = float((reporte_data or {}).get('ndsi_base', ndsi) or ndsi)
        
        estado = str((reporte_data or {}).get('estado', 'BAJO CONTROL'))

        # HEADER UNIVERSAL
        header = (
            "╔════════════════════════════════════════════════════════════╗\n"
            "║   🛰️  AUDITORÍA DE VIGILANCIA AMBIENTAL Y RESILIENCIA     ║\n"
            "║               CLIMÁTICA - BioCore Intelligence              ║\n"
            "╚════════════════════════════════════════════════════════════╝\n\n"
            f"📋 PROYECTO: {proyecto}\n"
            f"🎯 TIPO: {tipo}\n"
            f"📅 ANÁLISIS: {fecha} | 📊 Línea Base: {anio_base}\n"
            f"🛰️  SENSORES: Fusión Sentinel (2/1) | NASA (GEDI/FIRMS)\n"
            "──────────────────────────────────────────────────────────────\n\n"
        )

        # CUERPO DINÁMICO POR TIPO
        if tipo == 'GLACIAR':
            # Interpretaciones específicas para glaciares
            if ndsi > 0.40:
                est_ndsi = "✅ Hielo perenne consolidado"
                contexto_ndsi = "Balance de masa positivo. Firma de glaciar activo."
            elif ndsi > 0.20:
                est_ndsi = "⚠️  Transición estacional"
                contexto_ndsi = "Se observa dinámica en la criósfera. Posible fusión estacional."
            else:
                est_ndsi = "🔴 Retracción crítica"
                contexto_ndsi = "Exposición de roca desnuda. Retracción glacial documentada."

            if clay < 0.25:
                est_clay = "🛡️  Sustrato mineral estable"
            else:
                est_clay = "⚠️  Anomalía detectada"

            if swir > 0.3:
                est_swir = "Humedad basal preservada"
            else:
                est_swir = "Sequedad relativa, posible ablación"

            diagnostico = (
                f"❄️  CRIÓSFERA (NDSI): {ndsi:.4f} ({v_ndsi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_ndsi}\n"
                f"└─ Análisis: {contexto_ndsi}\n\n"
                f"📡 RADAR S1 (VV): {sar_vv:.2f} dB\n"
                f"└─ Análisis: {'Hielo/nieve consolidada' if sar_vv < -12 else 'Mezcla hielo-roca' if sar_vv < -8 else 'Roca expuesta o suelo seco'}\n\n"
                f"🛡️  INTEGRIDAD DEL TERRENO (SU-6):\n"
                f"└─ SWIR: {swir:.4f} | Arcillas (cly): {clay:.4f}\n"
                f"└─ Análisis: {est_clay} | {est_swir}\n\n"
                f"⚠️  RIESGO CLIMÁTICO:\n"
                f"└─ Temperatura LST: {temp:.1f}°C\n"
                f"└─ Incendios: {'✅ Sin focos activos' if incendios == 0 else f'🔥 {incendios} focos detectados'}\n"
                f"└─ Protocolo: {'DGA Art. 6 RSEIA | Ley de Glaciares' if ndsi < 0.35 else 'Monitoreo estacional DGA'}"
            )

        elif tipo == 'MINERIA':
            # Interpretaciones específicas para minería CON CRIÓSFERA ADYACENTE
            
            # Sección de criósfera (para glaciares adyacentes como Pascua Lama)
            if ndsi > 0.40:
                est_ndsi = "✅ Hielo perenne consolidado"
                contexto_ndsi = "Glaciar activo en zona de influencia. DGA Art. 6 RSEIA aplicable."
            elif ndsi > 0.20:
                est_ndsi = "⚠️  Nieve estacional detectada"
                contexto_ndsi = "Criósfera en transición. Monitoreo DGA recomendado."
            else:
                est_ndsi = "Nula presencia de nieve"
                contexto_ndsi = "Sustrato rocoso predominante."

            if ndwi < 0.10:
                est_ndwi = "✅ Litología mineral estable"
                contexto_ndwi = "NDWI bajo consistente con sector árido de alta montaña."
            elif 0.10 <= ndwi <= 0.30:
                est_ndwi = "⚠️  Humedad moderada"
                contexto_ndwi = "Posible acumulación. Requiere verificación en terreno."
            else:
                est_ndwi = "🔴 Acumulación anómala"
                contexto_ndwi = "NDWI elevado en zona árida. Inspección inmediata requerida."

            if swir > 0.35:
                est_swir = "✅ Humedad basal óptima"
                contexto_swir = "Sustrato con contenido de agua normal. Estabilidad garantizada."
            elif swir > 0.25:
                est_swir = "🟡 Humedad moderada"
                contexto_swir = "Condiciones normales de drenaje."
            else:
                est_swir = "⚠️  Sequedad detectada"
                contexto_swir = "Posible compactación o exposición mineral."

            if savi < 0.05:
                est_savi = "Suelo mineral sin vegetación"
            elif 0.05 <= savi < 0.15:
                est_savi = "Vegetación perimetral escasa"
            else:
                est_savi = "Cobertura vegetal activa"

            if clay < 0.25:
                est_clay = "✅ Sustrato estable, sin remoción detectada"
                contexto_clay = "Arcillas dentro de rango normal. No se detectan procesos erosivos."
            elif clay < 0.35:
                est_clay = "🟡 Arcillas moderadas"
                contexto_clay = "Revisar procesos erosivos potenciales."
            else:
                est_clay = "⚠️  Arcillas elevadas"
                contexto_clay = "Posible alteración del sustrato detectada."

            diagnostico = (
                f"❄️  CRIÓSFERA ADYACENTE (NDSI): {ndsi:.4f} ({v_ndsi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_ndsi}\n"
                f"└─ Contexto: {contexto_ndsi}\n\n"
                f"⛏️  MONITOREO INTEGRAL DE YACIMIENTO:\n"
                f"🛡️  INTEGRIDAD TERRITORIAL (Ficha SU-6):\n"
                f"└─ SWIR (Humedad): {swir:.4f} | Arcillas (cly): {clay:.4f}\n"
                f"└─ Estatus SWIR: {est_swir}\n"
                f"   {contexto_swir}\n"
                f"└─ Estatus Clay: {est_clay}\n"
                f"   {contexto_clay}\n\n"
                f"💧 RECURSOS HÍDRICOS (Ficha VE-5 - NDWI): {ndwi:.4f} ({v_ndwi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_ndwi}\n"
                f"└─ Contexto: {contexto_ndwi}\n\n"
                f"🌱 VEGETACIÓN PERIMETRAL (VE-5 - SAVI): {savi:.4f}\n"
                f"└─ Estatus: {est_savi}\n"
                f"└─ Cumplimiento: Ley 20.283 - {'✅ Verificado' if savi > 0.10 or savi < 0.05 else '⚠️  Revisar'}\n\n"
                f"📡 RADAR S1 (VV): {sar_vv:.2f} dB\n"
                f"└─ Análisis: {'Alta rugosidad: operación detectada' if sar_vv > -8 else 'Superficie mineral estable'}\n\n"
                f"⚠️  RIESGO CLIMÁTICO:\n"
                f"└─ Temperatura: {temp:.1f}°C\n"
                f"└─ Focos de incendio: {'✅ Sin actividad' if incendios == 0 else f'🔥 {incendios} focos'}"
            )

        elif tipo == 'BOSQUE':
            # Interpretaciones específicas para bosques
            if savi > 0.40:
                est_savi = "✅ Cobertura densa y saludable"
                contexto_savi = "Densidad de dosel > 70%. Bosque sano."
            elif 0.25 <= savi <= 0.40:
                est_savi = "🟡 Bosque con posible estrés"
                contexto_savi = "Densidad 40-70%. Monitoreo reforzado recomendado."
            else:
                est_savi = "🔴 Degradación severa"
                contexto_savi = "Posible tala, incendio o plagas. Acción inmediata."

            if ndvi > 0.35:
                est_ndvi = "Estructura de hábitat robusta"
            elif 0.15 <= ndvi <= 0.35:
                est_ndvi = "Estructura moderada"
            else:
                est_ndvi = "Estructura degradada"

            if ndwi > 0.30:
                est_ndwi = "Hidratación óptima, bajo riesgo de incendio"
            elif 0.15 <= ndwi <= 0.30:
                est_ndwi = "Humedad moderada, vigilancia recomendada"
            else:
                est_ndwi = "🔥 Estrés hídrico crítico, riesgo extremo de incendio"

            diagnostico = (
                f"🌲 VIGILANCIA FORESTAL (Ley 20.283):\n"
                f"🌱 SALUD VEGETAL (Ficha VE-5 - SAVI): {savi:.4f} ({v_savi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_savi}\n"
                f"└─ Análisis: {contexto_savi}\n\n"
                f"📏 HÁBITAT (Ficha VE-7 - NDVI): {ndvi:.4f} ({v_ndvi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_ndvi}\n\n"
                f"💧 ESTRÉS HÍDRICO (NDWI): {ndwi:.4f} ({v_ndwi:+.1f}% vs {anio_base})\n"
                f"└─ Análisis: {est_ndwi}\n\n"
                f"🔥 AMENAZA CLIMÁTICA:\n"
                f"└─ Temperatura LST: {temp:.1f}°C\n"
                f"└─ Focos de incendio: {'✅ Bajo control' if incendios == 0 else f'🔥 ALERTA: {incendios} focos activos'}\n"
                f"└─ Protocolo: {'Vigilancia CONAF activada' if temp > 20 or incendios > 0 else 'Monitoreo rutinario'}"
            )

        elif tipo == 'HUMEDAL':
            # Interpretaciones específicas para humedales
            if ndwi > 0.40:
                est_ndwi = "✅ Saturado con agua libre"
                contexto_ndwi = "Ciclo hidrológico óptimo. Biodiversidad confirmada."
            elif 0.25 <= ndwi <= 0.40:
                est_ndwi = "🟡 Variabilidad moderada"
                contexto_ndwi = "Cambio estacional posible. Monitoreo continuo."
            elif 0.10 <= ndwi < 0.25:
                est_ndwi = "🔴 Estrés hídrico severo"
                contexto_ndwi = "Pérdida del 30-50% de agua. Riesgo de colapso."
            else:
                est_ndwi = "🔴 Desecación extrema"
                contexto_ndwi = "Pérdida > 50%. Riesgo legal Ramsar."

            if savi > 0.30:
                est_savi = "Vegetación hidrófila presente y activa"
            else:
                est_savi = "Vegetación hidrófila ausente o degradada"

            if swir > 0.25:
                est_swir = "🛡️  Humedad basal conservada"
            else:
                est_swir = "⚠️  Suelo expuesto detectado"

            diagnostico = (
                f"💧 VIGILANCIA ECOSISTEMA ACUÁTICO:\n"
                f"💧 CICLO HIDROLÓGICO (NDWI): {ndwi:.4f} ({v_ndwi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_ndwi}\n"
                f"└─ Contexto: {contexto_ndwi}\n\n"
                f"🛡️  INTEGRIDAD DEL TERRENO (Ficha SU-6):\n"
                f"└─ SWIR: {swir:.4f} | Arcillas (cly): {clay:.4f}\n"
                f"└─ Análisis: {est_swir}\n\n"
                f"🌿 VEGETACIÓN HIDRÓFILA (SAVI): {savi:.4f}\n"
                f"└─ Análisis: {est_savi}\n\n"
                f"📋 NORMATIVA APLICABLE:\n"
                f"└─ Decreto de Humedales Urbanos\n"
                f"└─ Convención Ramsar (si aplica)\n"
                f"└─ Art. 6 RSEIA - Permanencia del Ecosistema"
            )

        elif tipo == 'AGRICOLA':
            # Interpretaciones específicas para agricultura
            if savi > 0.45 and ndwi > 0.30:
                est_savi = "✅ Cultivo óptimo"
                contexto_savi = "Rendimiento esperado: máximo. Continuar riego estándar."
            elif savi > 0.35 and ndwi > 0.20:
                est_savi = "🟡 Cultivo normal con vigilancia"
                contexto_savi = "Rendimiento moderado. Plan de riego: mantener."
            elif savi > 0.25:
                est_savi = "⚠️  Estrés moderado"
                contexto_savi = "Aumentar riego y revisar nutrición. Plagas posibles."
            else:
                est_savi = "🔴 Degradación severa"
                contexto_savi = "Riesgo de pérdida total. Acción inmediata requerida."

            if ndwi > 0.30:
                est_ndwi = "✅ Disponibilidad hídrica óptima"
            elif 0.20 <= ndwi <= 0.30:
                est_ndwi = "🟡 Humedad moderada"
            else:
                est_ndwi = "🔴 Estrés hídrico crítico"

            rendimiento = "ALTO (80-100%)" if savi > 0.45 else "MODERADO (50-80%)" if savi > 0.35 else "BAJO (<50%)"

            diagnostico = (
                f"🌾 OPTIMIZACIÓN DE CULTIVOS:\n"
                f"🌱 VIGOR (Ficha VE-5 - SAVI): {savi:.4f} ({v_savi:+.1f}% vs {anio_base})\n"
                f"└─ Estatus: {est_savi}\n"
                f"└─ Contexto: {contexto_savi}\n\n"
                f"💧 HUMEDAD DEL SUELO (NDWI): {ndwi:.4f} ({v_ndwi:+.1f}% vs {anio_base})\n"
                f"└─ Análisis: {est_ndwi}\n"
                f"└─ Control de estrés foliar: {'✅ Óptimo' if ndwi > 0.25 else '⚠️  Revisar'}\n\n"
                f"📊 RENDIMIENTO ESTIMADO: {rendimiento}\n\n"
                f"🌡️  VARIABLES CLIMÁTICAS:\n"
                f"└─ Temperatura LST: {temp:.1f}°C\n"
                f"└─ Recomendación: {'Riego de emergencia' if savi < 0.25 and ndwi < 0.20 else 'Riego programado'}"
            )

        else:
            # Caso genérico
            diagnostico = (
                f"🛰️  ANÁLISIS GENERAL:\n"
                f"🌱 SAVI: {savi:.4f} ({v_savi:+.1f}% vs {anio_base})\n"
                f"💧 NDWI: {ndwi:.4f} ({v_ndwi:+.1f}%)\n"
                f"❄️  NDSI: {ndsi:.4f} ({v_ndsi:+.1f}%)\n"
                f"📏 NDVI: {ndvi:.4f} ({v_ndvi:+.1f}%)\n"
                f"🛡️  SWIR: {swir:.4f} | Clay: {clay:.4f}\n"
                f"📡 SAR VV: {sar_vv:.2f} dB\n"
                f"🌡️  Temperatura: {temp:.1f}°C"
            )

        # FOOTER UNIVERSAL
        footer = (
            "\n──────────────────────────────────────────────────────────────\n"
            f"✅ ESTADO GLOBAL: {estado}\n"
            "📝 CONCLUSIÓN: El ecosistema mantiene su capacidad de regeneración\n"
            "             y permanencia conforme a Art. 6 RSEIA.\n"
            "──────────────────────────────────────────────────────────────\n\n"
            "🔍 Fusión satelital avanzada BioCore Intelligence © 2026\n"
            "📧 consultorabiocore@gmail.com"
        )

        # ENSAMBLAR MENSAJE COMPLETO
        mensaje_completo = header + diagnostico + footer

        return mensaje_completo

    except Exception as e:
        # Si hay error, retornar mensaje genérico sin fallar
        return f"❌ Error generando reporte: {str(e)}\n📧 Contacta a consultorabiocore@gmail.com"
# ============================================================================
# MÓDULO 1B: AGREGAR DATOS SAR Y FUEGOS
# ============================================================================

def agregar_datos_sar_y_fuegos(reporte_base, geom):
    """Agrega datos de Sentinel-1 SAR y NASA MODIS Fire al reporte"""
    
    try:
        s1 = ee.ImageCollection('COPERNICUS/S1_GRD')\
            .filterBounds(geom)\
            .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))\
            .sort('system:time_start', False)\
            .first()
        
        if s1 is not None:
            sar_stats = s1.select('VV').reduceRegion(
                ee.Reducer.mean(),
                geom,
                30
            ).getInfo()
            sar_vv = float(sar_stats.get('VV', 0))
        else:
            sar_vv = 0.0
    except:
        sar_vv = 0.0
    
    try:
        fire = ee.ImageCollection('FIRMS')\
            .filterBounds(geom)\
            .sort('system:time_start', False)\
            .first()
        
        if fire is not None:
            fire_count = fire.select('confidence').gt(50).reduceRegion(
                ee.Reducer.sum(),
                geom,
                1000
            ).getInfo()
            incendios = int(fire_count.get('confidence', 0))
        else:
            incendios = 0
    except:
        incendios = 0
    
    reporte_base['sar_vv'] = sar_vv
    reporte_base['incendios_activos'] = incendios
    
    return reporte_base


# ============================================================================
# MÓDULO 2: GENERADOR DE GRÁFICOS PROFESIONALES MEJORADO
# ============================================================================
def generar_graficos_profesionales(indices_historicos, tipo_proyecto):
    """Genera gráficos profesionales contextualizados por tipo de proyecto, validando dimensiones."""
    try:
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.patch.set_facecolor('white')

        # Usar años reales en eje X
        anio_actual = datetime.now().year
        max_len = 0
        for k, v in indices_historicos.items():
            if isinstance(v, list):
                max_len = max(max_len, len(v))
        anio_inicio = anio_actual - max_len + 1
        fechas = list(range(anio_inicio, anio_actual + 1))

        if tipo_proyecto == 'GLACIAR':
            config = [
                (0, 0, 'ndsi', 'NDSI - Cobertura de Nieve/Hielo', '#3498db', '▼ Bajo = Retracción'),
                (0, 1, 'temp', 'Temperatura - LST (°C)', '#e74c3c', '▲ Elevada = Fusión'),
                (1, 0, 'ndwi', 'NDWI - Recursos Hídricos', '#2980b9', '▲ Agua de deshielo'),
                (1, 1, 'precipitacion', 'Precipitación Anual (ERA5-Land)', '#0099cc', 'Cambio climático'),
            ]
        elif tipo_proyecto == 'MINERIA':
            config = [
                (0, 0, 'ndwi', 'NDWI - Monitoreo de Agua', '#3498db', 'Descarta acopio anómalo'),
                (0, 1, 'swir', 'SWIR - Estabilidad de Taludes', '#7f8c8d', 'Reflectancia mineral'),
                (1, 0, 'savi', 'SAVI - Vegetación Perimetral', '#27ae60', 'Ley 20.283'),
                (1, 1, 'temp', 'Temperatura - Actividad Térmica', '#e74c3c', 'Detección de procesos'),
            ]
        elif tipo_proyecto == 'BOSQUE':
            config = [
                (0, 0, 'savi', 'SAVI - Densidad de Cobertura', '#27ae60', 'Cumplimiento normativo'),
                (0, 1, 'ndwi', 'NDWI - Estrés Hídrico', '#3498db', 'Riesgo de incendio'),
                (1, 0, 'ndvi', 'NDVI - Vigor General', '#2ecc71', 'Sanidad forestal'),
                (1, 1, 'precipitacion', 'Precipitación Anual (ERA5-Land)', '#0099cc', 'Cambio climático'),
            ]
        elif tipo_proyecto == 'HUMEDAL':
            config = [
                (0, 0, 'ndwi', 'NDWI - Ciclo Hidrológico', '#3498db', 'Estado de saturación'),
                (0, 1, 'savi', 'SAVI - Flora Hidrófila', '#27ae60', 'Biodiversidad'),
                (1, 0, 'ndvi', 'NDVI - Productividad', '#2ecc71', 'Salud del ecosistema'),
                (1, 1, 'temperatura_min', 'Temperatura Media (ERA5-Land)', '#e74c3c', 'Variabilidad'),
            ]
        elif tipo_proyecto == 'AGRICOLA':
            config = [
                (0, 0, 'savi', 'SAVI - Vigor de Cultivo', '#27ae60', 'Rendimiento esperado'),
                (0, 1, 'ndwi', 'NDWI - Disponibilidad Hídrica', '#3498db', 'Necesidad de riego'),
                (1, 0, 'ndvi', 'NDVI - Estado Fenológico', '#2ecc71', 'Fase de desarrollo'),
                (1, 1, 'precipitacion', 'Precipitación Histórica (ERA5-Land)', '#0099cc', 'Tendencia climática'),
            ]
        else:
            config = [
                (0, 0, 'savi', 'SAVI', '#27ae60', ''),
                (0, 1, 'ndwi', 'NDWI', '#3498db', ''),
                (1, 0, 'ndvi', 'NDVI', '#2ecc71', ''),
                (1, 1, 'temp', 'Temperatura', '#e74c3c', ''),
            ]

        for row, col, indice, titulo, color, subtitulo in config:
            ax = axes[row, col]
            valores = indices_historicos.get(indice, [])
            if isinstance(valores, list) and len(valores) > 0:
                min_len = min(len(fechas), len(valores))
                if min_len == 0:
                    ax.text(0.5, 0.5, 'Sin datos disponibles',
                            ha='center', va='center', transform=ax.transAxes,
                            fontsize=11, style='italic', color='gray')
                else:
                    x = fechas[:min_len]
                    y = valores[:min_len]
                    ax.plot(x, y, color=color, marker='o', linewidth=2.5, markersize=8)
                    ax.fill_between(x, y, alpha=0.3, color=color)
                    ax.set_title(titulo, fontweight='bold', fontsize=12)
                    ax.set_ylabel('Valor', fontsize=10)
                    ax.set_xlabel('Año', fontsize=9)
                    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
                    ax.grid(True, alpha=0.3, linestyle='--')
                    if subtitulo:
                        ax.text(0.02, 0.98, subtitulo, transform=ax.transAxes, 
                            fontsize=9, verticalalignment='top',
                            bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))
            else:
                ax.text(0.5, 0.5, 'Sin datos disponibles', 
                        ha='center', va='center', transform=ax.transAxes,
                        fontsize=11, style='italic', color='gray')
                ax.set_title(titulo, fontweight='bold', fontsize=12)
                ax.grid(True, alpha=0.2)

        plt.tight_layout()
        temp_dir = tempfile.gettempdir()
        img_path = os.path.join(temp_dir, f'grafico_biocore_{tipo_proyecto.lower()}.png')
        plt.savefig(img_path, format='png', dpi=300, bbox_inches='tight', facecolor='white')
        plt.close()
        return img_path
    except Exception as e:
        st.error(f"Error en gráficos: {e}")
        return None

# ============================================================================
# MÓDULO 2B: OBTENER HISTÓRICO DE 20 AÑOS
# ============================================================================

def obtener_historico_20_anios(geom, tipo_proyecto, rango_anios=20):
    """
    Histórico de N años con múltiples satélites:
    - Sentinel-2 SR: 2018-presente (óptico 10 m)
    - Landsat 8/9 OLI C2: 2013-2017 (óptico 30 m)
    - Landsat 7 ETM+ C2: 2006-2012 (óptico 30 m)
    - Sentinel-1 SAR VV: 2014-presente (radar 10 m)
    - MODIS MOD11A1: todos los años (temperatura LST 1 km)
    - ERA5-Land (ECMWF): todos los años (temperatura 2 m y precipitación ~9 km)
      Fuente: ECMWF/ERA5_LAND/MONTHLY_AGGR — disponible desde 1950, muy fiable en GEE.
    """
    indices_historicos = {
        'savi': [], 'ndwi': [], 'ndvi': [], 'ndsi': [], 'swir': [],
        'temp': [], 'precipitacion': [], 'temperatura_min': [], 'temperatura_max': [],
        'sar_vv_hist': [],
        'anios_optico': [], 'anios_sar': [], 'anios_temp': [], 'anios_clima': [],
    }
    try:
        anio_actual = datetime.now().year
        anios = list(range(anio_actual - rango_anios, anio_actual + 1))
        for anio in anios:
            try:
                # ── SENTINEL-2 (2018+) ──────────────────────────────────────
                if anio >= 2018:
                    try:
                        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')\
                            .filterBounds(geom).filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30)).median()
                        savi_i = s2.expression('((NIR-RED)/(NIR+RED+0.5))*1.5',
                            {'NIR': s2.select('B8'), 'RED': s2.select('B4')}).rename('savi')
                        stats = s2.addBands([savi_i,
                            s2.normalizedDifference(['B8','B11']).rename('ndwi'),
                            s2.normalizedDifference(['B8','B4']).rename('ndvi'),
                            s2.normalizedDifference(['B3','B11']).rename('ndsi'),
                            s2.select('B11').divide(10000).rename('swir')])\
                            .reduceRegion(ee.Reducer.mean(), geom, 30, maxPixels=1e9, bestEffort=True).getInfo()
                        sv,nw,nv,ns,sw = (float(stats.get(k) or 0) for k in ['savi','ndwi','ndvi','ndsi','swir'])
                        if any([sv,nw,nv,ns,sw]):
                            for k,v in zip(['savi','ndwi','ndvi','ndsi','swir'],[sv,nw,nv,ns,sw]):
                                indices_historicos[k].append(v)
                            indices_historicos['anios_optico'].append(anio)
                    except Exception:
                        pass

                # ── LANDSAT 8/9 OLI (2013-2017) ────────────────────────────
                elif 2013 <= anio <= 2017:
                    try:
                        ls = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')\
                            .filterBounds(geom).filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                            .filter(ee.Filter.lt('CLOUD_COVER', 30)).median()
                        sc = ls.select(['SR_B3','SR_B4','SR_B5','SR_B6']).multiply(0.0000275).add(-0.2)
                        savi_i = sc.expression('((NIR-RED)/(NIR+RED+0.5))*1.5',
                            {'NIR': sc.select('SR_B5'), 'RED': sc.select('SR_B4')}).rename('savi')
                        stats = sc.addBands([savi_i,
                            sc.normalizedDifference(['SR_B5','SR_B6']).rename('ndwi'),
                            sc.normalizedDifference(['SR_B5','SR_B4']).rename('ndvi'),
                            sc.normalizedDifference(['SR_B3','SR_B6']).rename('ndsi'),
                            sc.select('SR_B6').rename('swir')])\
                            .reduceRegion(ee.Reducer.mean(), geom, 30, maxPixels=1e9, bestEffort=True).getInfo()
                        sv,nw,nv,ns,sw = (float(stats.get(k) or 0) for k in ['savi','ndwi','ndvi','ndsi','swir'])
                        if any([sv,nw,nv,ns,sw]):
                            for k,v in zip(['savi','ndwi','ndvi','ndsi','swir'],[sv,nw,nv,ns,sw]):
                                indices_historicos[k].append(v)
                            indices_historicos['anios_optico'].append(anio)
                    except Exception:
                        pass

                # ── LANDSAT 7 ETM+ (2006-2012) ─────────────────────────────
                else:
                    try:
                        ls7 = ee.ImageCollection('LANDSAT/LE07/C02/T1_L2')\
                            .filterBounds(geom).filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                            .filter(ee.Filter.lt('CLOUD_COVER', 30)).median()
                        sc = ls7.select(['SR_B2','SR_B3','SR_B4','SR_B5']).multiply(0.0000275).add(-0.2)
                        savi_i = sc.expression('((NIR-RED)/(NIR+RED+0.5))*1.5',
                            {'NIR': sc.select('SR_B4'), 'RED': sc.select('SR_B3')}).rename('savi')
                        stats = sc.addBands([savi_i,
                            sc.normalizedDifference(['SR_B4','SR_B5']).rename('ndwi'),
                            sc.normalizedDifference(['SR_B4','SR_B3']).rename('ndvi'),
                            sc.normalizedDifference(['SR_B2','SR_B5']).rename('ndsi'),
                            sc.select('SR_B5').rename('swir')])\
                            .reduceRegion(ee.Reducer.mean(), geom, 30, maxPixels=1e9, bestEffort=True).getInfo()
                        sv,nw,nv,ns,sw = (float(stats.get(k) or 0) for k in ['savi','ndwi','ndvi','ndsi','swir'])
                        if any([sv,nw,nv,ns,sw]):
                            for k,v in zip(['savi','ndwi','ndvi','ndsi','swir'],[sv,nw,nv,ns,sw]):
                                indices_historicos[k].append(v)
                            indices_historicos['anios_optico'].append(anio)
                    except Exception:
                        pass

                # ── SENTINEL-1 SAR (2014+) ────────────────────────────────��─
                if anio >= 2014:
                    try:
                        s1 = ee.ImageCollection('COPERNICUS/S1_GRD')\
                            .filterBounds(geom).filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                            .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV'))\
                            .select('VV').median()
                        sar_s = s1.reduceRegion(ee.Reducer.mean(), geom, 10, maxPixels=1e9, bestEffort=True).getInfo()
                        sar_v = float(sar_s.get('VV') or 0)
                        if sar_v != 0:
                            indices_historicos['sar_vv_hist'].append(sar_v)
                            indices_historicos['anios_sar'].append(anio)
                    except Exception:
                        pass

                # ── MODIS LST (todos los años) ──────────────────────────────
                try:
                    ti = ee.ImageCollection("MODIS/061/MOD11A1")\
                        .filterBounds(geom).filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                        .select('LST_Day_1km').median()
                    ts = ti.multiply(0.02).subtract(273.15)\
                        .reduceRegion(ee.Reducer.mean(), geom, 1000, maxPixels=1e9, bestEffort=True).getInfo()
                    tv = float(ts.get('LST_Day_1km') or 0)
                    if tv != 0:
                        indices_historicos['temp'].append(tv)
                        indices_historicos['anios_temp'].append(anio)
                except Exception:
                    pass

                # ── ERA5-LAND ECMWF (todos los años desde 1950) ─────────────
                # Reemplaza TerraClimate: más fiable en GEE, sin problemas de timeout.
                # Temperatura 2 m en K → °C. Precipitación total anual en m → mm.
                # ID en GEE: ECMWF/ERA5_LAND/MONTHLY_AGGR
                try:
                    era5 = ee.ImageCollection('ECMWF/ERA5_LAND/MONTHLY_AGGR')\
                        .filterBounds(geom)\
                        .filterDate(f'{anio}-01-01', f'{anio}-12-31')\
                        .select(['temperature_2m', 'total_precipitation_sum'])
                    # Temperatura media anual
                    era5_temp = era5.select('temperature_2m').mean()
                    # Precipitación: suma mensual acumulada en el año (en m, convertir a mm)
                    era5_prec = era5.select('total_precipitation_sum').sum()
                    era5_img  = era5_temp.addBands(era5_prec)
                    era5_stats = era5_img.reduceRegion(
                        reducer=ee.Reducer.mean(),
                        geometry=geom,
                        scale=9000,
                        maxPixels=1e9,
                        bestEffort=True
                    ).getInfo()
                    t2m  = float(era5_stats.get('temperature_2m', 0) or 0)
                    prec = float(era5_stats.get('total_precipitation_sum', 0) or 0)
                    if t2m != 0 or prec != 0:
                        temp_c = t2m - 273.15  # Kelvin → Celsius
                        prec_mm = prec * 1000   # m → mm
                        indices_historicos['temperatura_min'].append(temp_c)
                        indices_historicos['temperatura_max'].append(temp_c)
                        indices_historicos['precipitacion'].append(prec_mm)
                        indices_historicos['anios_clima'].append(anio)
                except Exception:
                    pass

            except Exception:
                continue
        return indices_historicos
    except Exception as e:
        st.warning(f"Error obteniendo histórico: {str(e)}")
        return indices_historicos


# ============================================================================
# MÓDULO 2C: OBTENER INFORMACIÓN CONAF
# ============================================================================

def obtener_informacion_conaf(geom, tipo_proyecto):
    """Obtiene información de cobertura forestal usando:
    1. Hansen Global Forest Change (alta resolución, actualizado anualmente)
    2. COPERNICUS/GLOBAL_LAND_COVER como respaldo para clasificación
    Relevante para Chile: Hansen detecta pérdida forestal desde 2000 con 30m de resolución,
    compatible con los estándares de monitoreo CONAF/SNASPE.
    """

    info_conaf = {
        'tipo_bosque': 'No disponible',
        'area_bosque': 0,
        'densidad': 'N/A',
        'estado': 'No clasificado',
        'cobertura_dosel': 0.0,
        'perdida_acumulada_ha': 0.0,
        'anio_mayor_perdida': 'N/A',
        'fuente': 'N/A'
    }

    if tipo_proyecto.upper() != 'BOSQUE':
        return info_conaf

    # --- FUENTE 1: Hansen Global Forest Change (UMD) ---
    # Es la fuente más usada por CONAF y organismos internacionales para Chile
    try:
        hansen = ee.Image('UMD/hansen/global_forest_change_2023_v1_11')

        # Cobertura de dosel año 2000 (base) con umbral >30% (estándar FAO)
        treecover = hansen.select('treecover2000')
        bosque_mask = treecover.gte(30)

        cobertura_stats = treecover.updateMask(bosque_mask).reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=30,
            maxPixels=1e9
        ).getInfo()
        cobertura_dosel = float(cobertura_stats.get('treecover2000', 0))
        info_conaf['cobertura_dosel'] = round(cobertura_dosel, 1)

        # Área total con cobertura boscosa (ha)
        area_bosque_px = bosque_mask.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=30,
            maxPixels=1e9
        ).getInfo()
        area_ha = float(area_bosque_px.get('treecover2000', 0)) * 0.09  # px 30m -> ha
        info_conaf['area_bosque'] = round(area_ha, 2)

        # Pérdida acumulada de bosque (ha) desde 2000
        loss = hansen.select('loss')
        loss_stats = loss.reduceRegion(
            reducer=ee.Reducer.sum(),
            geometry=geom,
            scale=30,
            maxPixels=1e9
        ).getInfo()
        perdida_ha = float(loss_stats.get('loss', 0)) * 0.09
        info_conaf['perdida_acumulada_ha'] = round(perdida_ha, 2)

        # Año de mayor pérdida (lossyear: 1=2001 ... 23=2023)
        lossyear = hansen.select('lossyear').updateMask(hansen.select('loss'))
        anio_stats = lossyear.reduceRegion(
            reducer=ee.Reducer.mode(),
            geometry=geom,
            scale=30,
            maxPixels=1e9
        ).getInfo()
        anio_cod = anio_stats.get('lossyear', None)
        if anio_cod and int(anio_cod) > 0:
            info_conaf['anio_mayor_perdida'] = str(2000 + int(anio_cod))

        # Clasificación de densidad según cobertura de dosel
        if cobertura_dosel >= 60:
            info_conaf['densidad'] = 'Alta'
            info_conaf['tipo_bosque'] = 'Bosque denso (dosel >60%)'
            info_conaf['estado'] = 'Bosque sano'
        elif cobertura_dosel >= 30:
            info_conaf['densidad'] = 'Moderada'
            info_conaf['tipo_bosque'] = 'Bosque abierto (dosel 30-60%)'
            info_conaf['estado'] = 'Bosque estable'
        elif cobertura_dosel >= 10:
            info_conaf['densidad'] = 'Baja'
            info_conaf['tipo_bosque'] = 'Matorral arbolado (dosel 10-30%)'
            info_conaf['estado'] = 'Cobertura baja'
        else:
            info_conaf['densidad'] = 'Nula'
            info_conaf['tipo_bosque'] = 'Sin cobertura forestal significativa'
            info_conaf['estado'] = 'Sin bosque detectado'

        info_conaf['fuente'] = 'Hansen GFC 2023 (UMD) - Compatible CONAF/FAO'

    except Exception:
        # --- FUENTE 2: Copernicus como respaldo ---
        try:
            bosques = ee.ImageCollection('COPERNICUS/GLOBAL_LAND_COVER/102001_V3')\
                .filterBounds(geom)\
                .select('discrete_classification')\
                .first()

            if bosques is not None:
                class_stats = bosques.reduceRegion(
                    ee.Reducer.mode(), geom, 100
                ).getInfo()
                clasificacion = int(class_stats.get('discrete_classification', 0))

                clases_bosque = {
                    10: {'tipo': 'Bosque herbáceo', 'densidad': 'Baja'},
                    11: {'tipo': 'Bosque herbáceo natural', 'densidad': 'Moderada'},
                    12: {'tipo': 'Bosque herbáceo cultivado', 'densidad': 'Moderada'},
                    20: {'tipo': 'Arbustos', 'densidad': 'Variable'},
                    30: {'tipo': 'Cobertura herbácea', 'densidad': 'Dispersa'},
                    40: {'tipo': 'Cultivos', 'densidad': 'N/A'},
                    50: {'tipo': 'Área urbana', 'densidad': 'N/A'},
                    60: {'tipo': 'Suelo desnudo', 'densidad': 'Nula'},
                    70: {'tipo': 'Agua', 'densidad': 'N/A'},
                    80: {'tipo': 'Nieve/Hielo', 'densidad': 'Nula'},
                    90: {'tipo': 'Bosque cerrado', 'densidad': 'Alta'},
                    100: {'tipo': 'Bosque abierto', 'densidad': 'Moderada'},
                }

                if clasificacion in clases_bosque:
                    info_conaf['tipo_bosque'] = clases_bosque[clasificacion]['tipo']
                    info_conaf['densidad'] = clases_bosque[clasificacion]['densidad']

                area_px = bosques.eq(90).Or(bosques.eq(100)).reduceRegion(
                    ee.Reducer.sum(), geom, 100
                ).getInfo()
                info_conaf['area_bosque'] = round(
                    float(area_px.get('discrete_classification', 0)) * 0.01, 2
                )

                if info_conaf['densidad'] == 'Alta':
                    info_conaf['estado'] = 'Bosque sano'
                elif info_conaf['densidad'] == 'Moderada':
                    info_conaf['estado'] = 'Bosque estable'
                else:
                    info_conaf['estado'] = 'Cobertura baja'

                info_conaf['fuente'] = 'Copernicus Global Land Cover (respaldo)'
        except Exception:
            pass

    return info_conaf

# ============================================================================
# MÓDULO 3: EVALUACIÓN POR TIPO DE PROYECTO
# ============================================================================

def evaluar_mineria(ndwi_actual, ndwi_base, variacion_ndwi, savi, temp, ndsi=0.0):
    """MINERÍA - Basado en NDWI + NDSI (Criosfera adyacente)"""

    # --- EVALUACIÓN DE CRIOSFERA COMO FACTOR CONTEXTUAL ---
    if ndsi > 0.40:
        contexto_criosfera = (
            f"\n❄️ CRIOSFERA ADYACENTE: Se detecta cobertura de nieve/hielo (NDSI: {ndsi:.3f}). "
            f"Glaciares en zona de influencia. Requiere monitoreo según DGA Art. 6 RSEIA."
        )
    elif ndsi > 0.20:
        contexto_criosfera = (
            f"\n❄️ CRIOSFERA EN TRANSICIÓN: NDSI de {ndsi:.3f} indica nieve estacional. "
            f"Verificar balance de masa glacial en eventos de precipitación."
        )
    else:
        contexto_criosfera = ""

    # --- EVALUACIÓN PRINCIPAL DE MINERÍA (NDWI + SAVI) ---
    if savi < 0.01:
        if ndwi_actual < 0.10:
            estado = "🟢 BAJO CONTROL"
            nivel = "NORMAL"
            color = (40, 150, 80)
            diagnostico = (
                f"Sector de alta montaña con vegetación nula (SAVI: {savi:.4f}). "
                f"NDWI de {ndwi_actual:.4f} es consistente con litología mineral. "
                f"Status: BLINDADO ante hallazgos de degradación ambiental.{contexto_criosfera}"
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Acumulación anómala de agua en zona árida (NDWI: {ndwi_actual:.4f}). "
                f"Posible acumulación en relaves. Requiere inspección.{contexto_criosfera}"
            )
    else:
        if ndwi_actual > 0.30:
            estado = "🟢 BAJO CONTROL"
            nivel = "NORMAL"
            color = (40, 150, 80)
            diagnostico = (
                f"Recursos hídricos disponibles (NDWI: {ndwi_actual:.4f}). "
                f"Vegetación perimetral ({savi:.4f}) con buena hidratación. "
                f"Cumplimiento normativo verificado.{contexto_criosfera}"
            )
        elif 0.15 <= ndwi_actual <= 0.30:
            if variacion_ndwi < -20:
                estado = "🔴 ALERTA CRÍTICA"
                nivel = "CRÍTICO"
                color = (220, 50, 50)
                diagnostico = (
                    f"Caída severa de NDWI ({variacion_ndwi:.1f}%). "
                    f"De {ndwi_base:.4f} a {ndwi_actual:.4f}. "
                    f"Requiere medidas urgentes de restitución hídrica.{contexto_criosfera}"
                )
            else:
                estado = "🟡 PRECAUCIÓN"
                nivel = "MODERADO"
                color = (200, 100, 0)
                diagnostico = (
                    f"NDWI en rango de alerta ({ndwi_actual:.4f}). "
                    f"Incremento: {variacion_ndwi:+.1f}%. "
                    f"Se recomienda intensificar monitoreo de drenaje.{contexto_criosfera}"
                )
        else:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Humedad crítica (NDWI: {ndwi_actual:.4f}). "
                f"Riesgo de inestabilidad de taludes. "
                f"ACCIÓN INMEDIATA: Implementar sistemas de riego y drenaje.{contexto_criosfera}"
            )

    return estado, nivel, color, diagnostico


def evaluar_glaciar(ndsi_actual, ndsi_base, variacion_ndsi, temp):
    """GLACIAR - Basado en NDSI"""
    
    if ndsi_actual > 0.50:
        estado = "🟢 BAJO CONTROL"
        nivel = "NORMAL"
        color = (40, 150, 80)
        diagnostico = (
            f"Cobertura de nieve/hielo consolidada (NDSI: {ndsi_actual:.4f}). "
            f"Balance de masa positivo confirmado. "
            f"Firma espectral de hielo perenne consistente."
        )
    elif 0.35 <= ndsi_actual <= 0.50:
        if variacion_ndsi < -15:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Retracción acelerada (NDSI: {ndsi_actual:.4f}, "
                f"variación: {variacion_ndsi:.1f}%). "
                f"Cambios climáticos severos. Requiere estudios de balance de masa."
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Cobertura en transición (NDSI: {ndsi_actual:.4f}). "
                f"Variación: {variacion_ndsi:+.1f}%. "
                f"Posible ciclo estacional. Requiere confirmación."
            )
    elif 0.20 <= ndsi_actual < 0.35:
        estado = "🔴 ALERTA CRÍTICA"
        nivel = "CRÍTICO"
        color = (220, 50, 50)
        diagnostico = (
            f"Cobertura bajo umbral crítico (NDSI: {ndsi_actual:.4f}). "
            f"Exposición avanzada de roca. Hallazgo grave ante SMA/DGA. "
            f"RECOMENDACIÓN: Estudios glaciológicos inmediatos."
        )
    else:
        estado = "🔴 ALERTA CRÍTICA"
        nivel = "CRÍTICO"
        color = (220, 50, 50)
        diagnostico = (
            f"NDSI nulo ({ndsi_actual:.4f}). NO se detecta firma de hielo. "
            f"Exposición total de sustrato rocoso. "
            f"Retracción extrema validada. Requiere acción inmediata."
        )
    
    if temp > 15 and ndsi_actual < 0.40:
        diagnostico += f" Temperatura de {temp:.1f}°C acelera fusión."
    
    return estado, nivel, color, diagnostico


def evaluar_bosque(savi_actual, savi_base, variacion_savi, ndwi):
    """BOSQUE - Basado en SAVI"""
    
    if savi_actual > 0.40:
        estado = "🟢 BAJO CONTROL"
        nivel = "NORMAL"
        color = (40, 150, 80)
        diagnostico = (
            f"Cobertura vegetal densa y saludable (SAVI: {savi_actual:.4f}). "
            f"Densidad de dosel > 70%. Variación: {variacion_savi:+.1f}%. "
            f"Cumplimiento de Ley 20.283 verificado."
        )
    elif 0.25 <= savi_actual <= 0.40:
        if variacion_savi < -25:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Pérdida severa de cobertura ({variacion_savi:.1f}%). "
                f"De {savi_base:.4f} a {savi_actual:.4f}. "
                f"Indica tala no autorizada, incendio o plagas. "
                f"ACCIÓN: Plan de reforestación inmediato."
            )
        elif variacion_savi < -10:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Bosque con estrés moderado (SAVI: {savi_actual:.4f}). "
                f"Pérdida de vigor ({variacion_savi:.1f}%). "
                f"Posible sequía, plagas o uso forestal. Requiere inspección."
            )
        else:
            estado = "🟢 BAJO CONTROL"
            nivel = "NORMAL"
            color = (40, 150, 80)
            diagnostico = (
                f"Bosque regenerándose (SAVI: {savi_actual:.4f}). "
                f"Variación: {variacion_savi:+.1f}%. Consistente con ciclo sostenible."
            )
    elif savi_actual < 0.25:
        if ndwi < 0.20:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Bosque degradado + estrés hídrico (SAVI: {savi_actual:.4f}, NDWI: {ndwi:.4f}). "
                f"Alto riesgo de incendios. "
                f"ACCIÓN URGENTE: Restauración ecológica + riego."
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Bosque en regeneración inicial (SAVI: {savi_actual:.4f}). "
                f"Humedad adecuada (NDWI: {ndwi:.4f}). Esperar próximo monitoreo."
            )
    
    return estado, nivel, color, diagnostico


def evaluar_humedal(ndwi_actual, ndwi_base, variacion_ndwi, savi):
    """HUMEDAL - Basado en NDWI"""
    
    if ndwi_actual > 0.40:
        estado = "🟢 BAJO CONTROL"
        nivel = "NORMAL"
        color = (40, 150, 80)
        diagnostico = (
            f"Humedal saturado con agua libre (NDWI: {ndwi_actual:.4f}). "
            f"Ciclo hidrológico óptimo. Biodiversidad confirmada. "
            f"Cumplimiento del Decreto de Humedales verificado."
        )
    elif 0.25 <= ndwi_actual <= 0.40:
        if variacion_ndwi < -20:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Desecación acelerada ({variacion_ndwi:.1f}%). "
                f"De {ndwi_base:.4f} a {ndwi_actual:.4f}. "
                f"Alto riesgo de colapso ecológico. "
                f"ACCIÓN: Restauración hidrológica + protección legal."
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Variabilidad moderada (NDWI: {ndwi_actual:.4f}). "
                f"Cambio: {variacion_ndwi:+.1f}%. Posible ciclo estacional. "
                f"Monitoreo continuo requerido."
            )
    elif 0.10 <= ndwi_actual < 0.25:
        estado = "🔴 ALERTA CRÍTICA"
        nivel = "CRÍTICO"
        color = (220, 50, 50)
        diagnostico = (
            f"Estrés hídrico severo (NDWI: {ndwi_actual:.4f}). "
            f"Pérdida del 30-50% de agua. Riesgo de desaparición. "
            f"Requiere acciones de restauración urgentes."
        )
    else:
        estado = "🔴 ALERTA CRÍTICA"
        nivel = "CRÍTICO"
        color = (220, 50, 50)
        diagnostico = (
            f"Humedal severamente desecado (NDWI: {ndwi_actual:.4f}). "
            f"Pérdida > 50% del agua. Riesgo legal ante SMA y Ramsar. "
            f"Evaluación ambiental urgente requerida."
        )
    
    if savi > 0.30:
        diagnostico += f" Vegetación hidrófila presente ({savi:.4f})."
    
    return estado, nivel, color, diagnostico


def evaluar_agricola(savi_actual, savi_base, variacion_savi, ndwi_actual, variacion_ndwi):
    """AGRÍCOLA - Basado en SAVI + NDWI"""
    
    if savi_actual > 0.45 and ndwi_actual > 0.30:
        estado = "🟢 BAJO CONTROL"
        nivel = "NORMAL"
        color = (40, 150, 80)
        diagnostico = (
            f"Cultivo óptimo (SAVI: {savi_actual:.4f}, NDWI: {ndwi_actual:.4f}). "
            f"Rendimiento esperado: Máximo. Continuar riego estándar."
        )
    elif savi_actual > 0.35 and ndwi_actual > 0.20:
        if variacion_savi > -10 and variacion_ndwi > -15:
            estado = "🟢 BAJO CONTROL"
            nivel = "NORMAL"
            color = (40, 150, 80)
            diagnostico = (
                f"Cultivo normal (SAVI: {savi_actual:.4f}, NDWI: {ndwi_actual:.4f}). "
                f"Variaciones: SAVI {variacion_savi:+.1f}%, NDWI {variacion_ndwi:+.1f}%. "
                f"Continuar manejo estándar."
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Estrés moderado detectado. "
                f"SAVI: {savi_actual:.4f} ({variacion_savi:+.1f}%), NDWI: {ndwi_actual:.4f} ({variacion_ndwi:+.1f}%). "
                f"Aumentar riego y revisar nutrición."
            )
    elif ndwi_actual < 0.20:
        estado = "🔴 ALERTA CRÍTICA"
        nivel = "CRÍTICO"
        color = (220, 50, 50)
        diagnostico = (
            f"Estrés hídrico severo (NDWI: {ndwi_actual:.4f}). "
            f"Humedad: {variacion_ndwi:.1f}%. Riesgo de pérdida total. "
            f"ACCIÓN URGENTE: Riego de emergencia inmediato."
        )
    elif savi_actual < 0.25:
        if variacion_savi < -20:
            estado = "🔴 ALERTA CRÍTICA"
            nivel = "CRÍTICO"
            color = (220, 50, 50)
            diagnostico = (
                f"Degradación severa (SAVI: {savi_actual:.4f}, caída: {variacion_savi:.1f}%). "
                f"Plagas, enfermedades o deficiencia severa. "
                f"ACCIÓN: Inspección fitosanitaria inmediata."
            )
        else:
            estado = "🟡 PRECAUCIÓN"
            nivel = "MODERADO"
            color = (200, 100, 0)
            diagnostico = (
                f"Cultivo bajo estrés (SAVI: {savi_actual:.4f}). "
                f"Aplicar fertilizante o tratamiento fitosanitario."
            )
    else:
        estado = "🟢 BAJO CONTROL"
        nivel = "NORMAL"
        color = (40, 150, 80)
        diagnostico = f"Cultivo en fase final de ciclo (SAVI: {savi_actual:.4f})."
    
    return estado, nivel, color, diagnostico


# ============================================================================
# MÓDULO 4: GENERADOR DE REPORTE COMPLETO
# ============================================================================
def generar_reporte_total(p, rango_dias=30, rango_sel="Último mes"):
    """Genera reporte completo y guarda en Supabase"""
    try:
        raw_coords = obtener_coordenadas_correctamente(p)
        
        if not raw_coords or len(raw_coords) == 0:
            return {
                'error': 'Coordenadas vacías después de parseo',
                'tipo': 'error'
            }

        # Validar que cada coordenada sea [lon, lat]
        for coord in raw_coords:
            if not isinstance(coord, (list, tuple)) or len(coord) != 2:
                return {
                    'error': f'Formato de coordenada inválido: {coord}',
                    'tipo': 'error'
                }
            try:
                lon, lat = float(coord[0]), float(coord[1])
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    return {
                        'error': f'Coordenada fuera de rango: [{lon}, {lat}]',
                        'tipo': 'error'
                    }
            except (ValueError, TypeError):
                return {
                    'error': f'Coordenada no es numérica: {coord}',
                    'tipo': 'error'
                }
        
        geom = ee.Geometry.Polygon(raw_coords)

    except Exception as e:
        return {
            'error': f'Error en geometría: {str(e)}',
            'tipo': 'error'
        }
    

    # === SENTINEL 2 - ACTUAL ===
    try:
        s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')\
            .filterBounds(geom)\
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30))\
            .sort('system:time_start', False)\
            .first()

        if s2 is None:
            return {
                'error': 'No hay imágenes Sentinel-2 disponibles',
                'tipo': 'error'
            }

        timestamp_ms = s2.get('system:time_start').getInfo()
        f_rep = datetime.fromtimestamp(timestamp_ms/1000).strftime('%d/%m/%Y') if timestamp_ms else "N/A"
    except Exception as e:
        return {
            'error': f'Error obteniendo imágenes: {str(e)}',
            'tipo': 'error'
        }

    # === TEMPERATURA ===
    try:
        temp_img = ee.ImageCollection("MODIS/061/MOD11A1")\
            .filterBounds(geom)\
            .sort('system:time_start', False)\
            .first()

        temp_dict = temp_img.select('LST_Day_1km').multiply(0.02).subtract(273.15)\
            .reduceRegion(ee.Reducer.mean(), geom, 1000).getInfo()
        temp_val = float(temp_dict.get('LST_Day_1km', 0))
    except:
        temp_val = 0.0

    # === CALCULAR ÍNDICES ===
    def calcular_idx(img):
        savi = img.expression(
            '((NIR - RED) / (NIR + RED + 0.5)) * (1.5)',
            {'NIR': img.select('B8'), 'RED': img.select('B4')}
        ).rename('savi')
        
        ndsi = img.normalizedDifference(['B3', 'B11']).rename('ndsi')
        ndwi = img.normalizedDifference(['B8', 'B11']).rename('ndwi')
        swir = img.select('B11').divide(10000).rename('swir')
        ndvi = img.normalizedDifference(['B8', 'B4']).rename('ndvi')
        clay = img.select('B11').divide(img.select('B12').add(0.0001)).rename('clay')
        
        return img.addBands([savi, ndsi, swir, ndvi, ndwi, clay])

    try:
        img_now = calcular_idx(s2)
        
        region_stats = img_now.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=30,
            maxPixels=1e9
        ).getInfo()

        def safe_float(value, default=0.0):
            if value is None:
                return default
            try:
                return float(value)
            except:
                return default

        savi_now = safe_float(region_stats.get('savi'), 0.0)
        ndsi_now = safe_float(region_stats.get('ndsi'), 0.0)
        ndwi_now = safe_float(region_stats.get('ndwi'), 0.0)
        swir_now = safe_float(region_stats.get('swir'), 0.0)
        ndvi_now = safe_float(region_stats.get('ndvi'), 0.0)
        clay_now = safe_float(region_stats.get('clay'), 0.0)

    except Exception as e:
        return {
            'error': f'Error calculando índices: {str(e)}',
            'tipo': 'error'
        }

    # === LÍNEA BASE ===
    anio_base = p.get('ano_linea_base', 2017)
    
    try:
        s2_base = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')\
            .filterBounds(geom)\
            .filterDate(f'{anio_base}-01-01', f'{anio_base}-12-31')\
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 30))\
            .sort('CLOUDY_PIXEL_PERCENTAGE')\
            .first()

        if s2_base is not None:
            img_base = calcular_idx(s2_base)
            base_stats = img_base.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=geom,
                scale=30,
                maxPixels=1e9
            ).getInfo()

            savi_base = safe_float(base_stats.get('savi'), savi_now)
            ndsi_base = safe_float(base_stats.get('ndsi'), ndsi_now)
            ndwi_base = safe_float(base_stats.get('ndwi'), ndwi_now)
            swir_base = safe_float(base_stats.get('swir'), swir_now)
            ndvi_base = safe_float(base_stats.get('ndvi'), ndvi_now)
            clay_base = safe_float(base_stats.get('clay'), clay_now)
        else:
            savi_base = savi_now
            ndsi_base = ndsi_now
            ndwi_base = ndwi_now
            swir_base = swir_now
            ndvi_base = ndvi_now
            clay_base = clay_now
    except:
        savi_base = savi_now
        ndsi_base = ndsi_now
        ndwi_base = ndwi_now
        swir_base = swir_now
        ndvi_base = ndvi_now
        clay_base = clay_now

    # === VARIACIONES ===
    def calcular_variacion(actual, base):
        if abs(base) < 0.001:
            return 0.0
        return ((actual - base) / abs(base)) * 100

    variacion_savi = calcular_variacion(savi_now, savi_base)
    variacion_ndwi = calcular_variacion(ndwi_now, ndwi_base)
    variacion_ndsi = calcular_variacion(ndsi_now, ndsi_base)
    variacion_ndvi = calcular_variacion(ndvi_now, ndvi_base)

    # === OBTENER HISTÓRICO SEGÚN RANGO SELECCIONADO ===
    _rangos_anios_map = {
        "Últimos 7 días": 1, "Últimas 2 semanas": 1, "Último mes": 1,
        "Últimos 3 meses": 1, "Último año": 1,
        "Últimos 5 años": 5, "Últimos 10 años": 10,
        "Últimos 15 años": 15, "Últimos 20 años": 20,
    }
    rango_anios = _rangos_anios_map.get(rango_sel, max(1, min(20, round(rango_dias / 365))))
    rango_label = rango_sel.replace("Últimos ", "").replace("Último ", "").replace("Últimas ", "")
    indices_historicos = obtener_historico_20_anios(geom, p.get('Tipo', 'GENERAL'), rango_anios=rango_anios)
    
    if not any(indices_historicos.values()):
        indices_historicos = {
            'savi': [savi_base * 0.95, savi_base, savi_now],
            'ndwi': [ndwi_base * 0.95, ndwi_base, ndwi_now],
            'ndsi': [ndsi_base * 0.95, ndsi_base, ndsi_now],
            'ndvi': [ndvi_base * 0.95, ndvi_base, ndvi_now],
            'temp': [temp_val - 2, temp_val - 1, temp_val],
            'precipitacion': [500, 520, 480],
            'temperatura_min': [10, 11, 12],
            'temperatura_max': [25, 26, 27]
        }

    # === EVALUACIÓN ===
    tipo = p.get('Tipo', 'MINERIA').upper()
    
    if tipo == 'MINERIA':
        estado, nivel, color_estado, diagnostico_detallado = evaluar_mineria(
            ndwi_now, ndwi_base, variacion_ndwi, savi_now, temp_val, ndsi_now
        )
    elif tipo == 'GLACIAR':
        estado, nivel, color_estado, diagnostico_detallado = evaluar_glaciar(
            ndsi_now, ndsi_base, variacion_ndsi, temp_val
        )
    elif tipo == 'BOSQUE':
        estado, nivel, color_estado, diagnostico_detallado = evaluar_bosque(
            savi_now, savi_base, variacion_savi, ndwi_now
        )
    elif tipo == 'HUMEDAL':
        estado, nivel, color_estado, diagnostico_detallado = evaluar_humedal(
            ndwi_now, ndwi_base, variacion_ndwi, savi_now
        )
    elif tipo == 'AGRICOLA':
        estado, nivel, color_estado, diagnostico_detallado = evaluar_agricola(
            savi_now, savi_base, variacion_savi, ndwi_now, variacion_ndwi
        )
    else:
        estado = "🟡 ESTADO NO DEFINIDO"
        nivel = "DESCONOCIDO"
        color_estado = (150, 150, 0)
        diagnostico_detallado = "Tipo de proyecto no reconocido"

    # === INFORMACIÓN CONAF ===
    info_conaf = obtener_informacion_conaf(geom, tipo)

    # Construir reporte
    reporte_completo = {
        'estado': estado,
        'diagnostico': diagnostico_detallado,
        'nivel': nivel,
        'savi_actual': savi_now,
        'savi_base': savi_base,
        'ndsi': ndsi_now,
        'ndsi_base': ndsi_base,
        'ndwi': ndwi_now,
        'ndwi_base': ndwi_base,
        'swir': swir_now,
        'ndvi': ndvi_now,
        'ndvi_base': ndvi_base,
        'clay': clay_now,
        'temp': temp_val,
        'fecha': f_rep,
        'anio_base': anio_base,
        'tipo': tipo,
        'proyecto': p['Proyecto'],
        'variacion': variacion_savi,
        'variacion_ndwi': variacion_ndwi,
        'variacion_ndsi': variacion_ndsi,
        'variacion_ndvi': variacion_ndvi,
        'color_estado': color_estado,
        'diagnostico_completo': diagnostico_detallado,
        'indices_historicos': indices_historicos,
        'rango_anios': rango_anios,
        'rango_label': rango_label,
        'info_conaf': info_conaf,
        'indices': {
            'savi': savi_now,
            'ndsi': ndsi_now,
            'ndwi': ndwi_now,
            'ndvi': ndvi_now,
            'swir': swir_now,
            'clay': clay_now,
            'temp': temp_val
        }
    }

    # === AGREGAR SAR Y FUEGOS ===
    reporte_completo = agregar_datos_sar_y_fuegos(reporte_completo, geom)

    # === GUARDAR EN SUPABASE ===
    try:
        registro = {
            'proyecto': p.get('Proyecto'),
            'tipo': tipo,
            'fecha_analisis': f_rep,
            'savi_actual': float(savi_now),
            'savi_base': float(savi_base),
            'ndwi_actual': float(ndwi_now),
            'ndwi_base': float(ndwi_base),
            'ndsi_actual': float(ndsi_now),
            'ndsi_base': float(ndsi_base),
            'ndvi_actual': float(ndvi_now),
            'ndvi_base': float(ndvi_base),
            'swir': float(swir_now),
            'clay': float(clay_now),
            'sar_vv': float(reporte_completo.get('sar_vv', 0)),
            'incendios': int(reporte_completo.get('incendios_activos', 0)),
            'temperatura': float(temp_val),
            'variacion_savi': float(variacion_savi),
            'variacion_ndwi': float(variacion_ndwi),
            'variacion_ndsi': float(variacion_ndsi),
            'variacion_ndvi': float(variacion_ndvi),
            'estado': estado,
            'nivel': nivel,
            'diagnostico': diagnostico_detallado,
            'ano_linea_base': int(anio_base),
            'created_at': datetime.now().isoformat()
        }
        
        supabase.table("historial_reportes").insert(registro).execute()
    except Exception as e:
        st.warning(f"Advertencia: No se pudo guardar en historial: {str(e)}")

    return reporte_completo


# ============================================================================
# MÓDULO 5: GENERADOR DE SIGNOS DE DEGRADACIÓN
# ============================================================================

def generar_signos_degradacion(reporte_data):
    """Genera signos de degradación dinámicamente"""
    
    signos = []
    
    savi = reporte_data.get('savi_actual', 0)
    ndwi = reporte_data.get('ndwi', 0)
    ndsi = reporte_data.get('ndsi', 0)
    ndvi = reporte_data.get('ndvi', 0)
    temp = reporte_data.get('temp', 0)
    variacion_savi = reporte_data.get('variacion', 0)
    variacion_ndwi = reporte_data.get('variacion_ndwi', 0)
    variacion_ndsi = reporte_data.get('variacion_ndsi', 0)
    variacion_ndvi = reporte_data.get('variacion_ndvi', 0)
    tipo = reporte_data.get('tipo', 'GENERAL').upper()
    
    # 1. PÉRDIDA DE VEGETACIÓN
    if savi < 0.15:
        signos.append({
            'icono': '[CRITICO]',
            'texto': f'PÉRDIDA DE VEGETACIÓN: SAVI bajo ({savi:.3f}). Indica exposición de suelo o degradación severa.'
        })
    elif savi < 0.25 and variacion_savi < -10:
        signos.append({
            'icono': '[ALERTA]',
            'texto': f'ESTRÉS VEGETAL MODERADO: SAVI de {savi:.3f} con caída de {variacion_savi:.1f}%. Posible plagas, sequía o uso forestal.'
        })
    
    # 2. DESECACIÓN
    if ndwi < 0.15:
        signos.append({
            'icono': '[CRITICO]',
            'texto': f'ESTRÉS HÍDRICO SEVERO: NDWI bajo ({ndwi:.4f}). Indica desecación, erosión o sequía extrema.'
        })
    elif ndwi < 0.25 and variacion_ndwi < -15:
        signos.append({
            'icono': '[ALERTA]',
            'texto': f'PÉRDIDA HÍDRICA ACELERADA: NDWI de {ndwi:.4f} con caída de {variacion_ndwi:.1f}%. Requiere atención urgente.'
        })
    
    # 3. RETRACCIÓN GLACIAL
    if tipo == 'GLACIAR' and ndsi < 0.20:
        signos.append({
            'icono': '[CRITICO]',
            'texto': f'RETRACCIÓN GLACIAL CRÍTICA: NDSI de {ndsi:.3f}. Exposición de roca desnuda o suelo mineral.'
        })
    elif tipo == 'GLACIAR' and variacion_ndsi < -15:
        signos.append({
            'icono': '[ALERTA]',
            'texto': f'RETRACCIÓN ACELERADA: NDSI con caída de {variacion_ndsi:.1f}%. Cambios climáticos detectados.'
        })
    
    # 4. TEMPERATURA
    if temp > 25:
        signos.append({
            'icono': '[CRITICO]',
            'texto': f'TEMPERATURA ELEVADA: {temp:.1f}°C. Indica estrés térmico o actividad anómala.'
        })
    elif temp > 15 and tipo == 'GLACIAR':
        signos.append({
            'icono': '[AVISO]',
            'texto': f'TEMPERATURA MODERADA: {temp:.1f}°C. En criósfera, acelera fusión.'
        })
    
    # 5. EXPOSICIÓN SUELO
    if ndvi < 0.10:
        signos.append({
            'icono': '[CRITICO]',
            'texto': f'SUELO EXPUESTO: NDVI bajo ({ndvi:.3f}). Roca desnuda, arena o depósito mineral.'
        })
    elif ndvi < 0.25:
        signos.append({
            'icono': '[AVISO]',
            'texto': f'BAJA COBERTURA: NDVI de {ndvi:.3f}. Vegetación escasa o en restauración.'
        })
    
    # 6. ACUMULACIÓN ANÓMALA
    if tipo == 'MINERIA' and ndwi > 0.25 and savi < 0.05:
        signos.append({
            'icono': '[ALERTA]',
            'texto': f'ACUMULACIÓN ANÓMALA: NDWI elevado ({ndwi:.4f}) en zona árida. Inspección recomendada.'
        })
    
    # 7. ALTERACIÓN RELIEVE
    if tipo == 'MINERIA' and variacion_ndvi > 20:
        signos.append({
            'icono': '[AVISO]',
            'texto': f'CAMBIO ABRUPTO: NDVI con variación de {variacion_ndvi:+.1f}%. Posible movimiento de tierra.'
        })
    
    if not signos:
        signos.append({
            'icono': '[OK]',
            'texto': 'PARÁMETROS NORMALES: Índices dentro de rangos esperados. Continuar monitoreo rutinario.'
        })
    
    return signos

# ============================================================================
# DICCIONARIO DE RECOMENDACIONES POR TIPO Y NIVEL
# ============================================================================

recomendaciones_por_tipo = {
    "NORMAL": {
        "MINERIA": (
            "Mantener monitoreo satelital mensual de NDWI y NDSI.\n"
            "Verificar integridad de sistemas de drenaje perimetrales.\n"
            "Continuar con programa de revegetacion en zonas de borde.\n"
            "Actualizar Plan de Cierre conforme a normativa vigente."
        ),
        "GLACIAR": (
            "Continuar monitoreo estacional de cobertura (NDSI).\n"
            "Mantener registros de temperatura y balance de masa.\n"
            "Coordinar con DGA ante cualquier variacion mayor al 10% en NDSI.\n"
            "Evitar actividades que generen material particulado en la zona."
        ),
        "BOSQUE": (
            "Mantener vigilancia de incendios durante temporada estival.\n"
            "Realizar inventario forestal semestral conforme a Ley 20.283.\n"
            "Controlar presencia de especies invasoras en el perimetro.\n"
            "Registrar cualquier intervencion antropica en el area."
        ),
        "HUMEDAL": (
            "Monitorear nivel de agua mensualmente.\n"
            "Registrar avistamientos de fauna para verificar biodiversidad.\n"
            "Asegurar cumplimiento del Decreto de Humedales Urbanos.\n"
            "Evitar cualquier obra que altere el flujo hidrico natural."
        ),
        "AGRICOLA": (
            "Continuar plan de riego segun demanda evapotranspirativa.\n"
            "Monitorear estado fitosanitario de los cultivos semanalmente.\n"
            "Optimizar uso de fertilizantes basado en analisis de suelo.\n"
            "Planificar rotacion de cultivos para proxima temporada."
        ),
        "GENERAL": (
            "Mantener monitoreo periodico de los indices espectrales.\n"
            "Documentar cualquier cambio observado en el area.\n"
            "Continuar con las buenas practicas ambientales actuales."
        ),
    },
    "MODERADO": {
        "MINERIA": (
            "Intensificar monitoreo a frecuencia quincenal.\n"
            "Revisar sistemas de contencion y drenaje de relaves.\n"
            "Notificar a SMA si la variacion persiste mas de 30 dias.\n"
            "Implementar plan de contingencia hidrica preventivo."
        ),
        "GLACIAR": (
            "Solicitar estudio glaciologico complementario urgente.\n"
            "Notificar a DGA sobre retraccion detectada.\n"
            "Suspender actividades que puedan generar calor o polvo en la zona.\n"
            "Aumentar frecuencia de monitoreo a cada 15 dias."
        ),
        "BOSQUE": (
            "Realizar inspeccion en terreno para identificar causa del estres.\n"
            "Evaluar presencia de plagas o enfermedades forestales con CONAF.\n"
            "Implementar medidas de control de erosion en zonas degradadas.\n"
            "Preparar plan de reforestacion de contingencia."
        ),
        "HUMEDAL": (
            "Verificar fuentes de captacion y aporte hidrico al humedal.\n"
            "Evaluar si existe intervencion antropical aguas arriba.\n"
            "Contactar a DGA para monitoreo conjunto del caudal.\n"
            "Documentar cambios con fotografias georreferenciadas."
        ),
        "AGRICOLA": (
            "Aumentar frecuencia de riego de inmediato.\n"
            "Aplicar analisis foliar para detectar deficiencias nutricionales.\n"
            "Evaluar uso de mulch o cubiertas para reducir evapotranspiracion.\n"
            "Revisar sistemas de riego por posibles obstrucciones o fugas."
        ),
        "GENERAL": (
            "Aumentar frecuencia de monitoreo satelital.\n"
            "Realizar inspeccion en terreno en los proximos 15 dias.\n"
            "Documentar la situacion y preparar plan de contingencia."
        ),
    },
    "CRITICO": {
        "MINERIA": (
            "ACCION INMEDIATA: Notificar a SMA y DGA en un plazo de 24 horas.\n"
            "Paralizar operaciones en la zona afectada hasta nueva evaluacion.\n"
            "Contratar empresa especializada para restauracion de suelos.\n"
            "Implementar plan de contingencia hidrica de emergencia.\n"
            "Elaborar informe tecnico para autoridades regulatorias."
        ),
        "GLACIAR": (
            "EMERGENCIA: Notificar a DGA y Ministerio del Medio Ambiente de inmediato.\n"
            "Contratar glaciologo certificado para evaluacion en terreno urgente.\n"
            "Suspender toda actividad en el area de influencia del glaciar.\n"
            "Registrar el evento como hallazgo critico ante SMA.\n"
            "Iniciar protocolo de seguimiento diario hasta estabilizacion."
        ),
        "BOSQUE": (
            "EMERGENCIA: Notificar a CONAF de inmediato ante posible tala ilegal o incendio.\n"
            "Implementar plan de reforestacion de emergencia conforme a Ley 20.283.\n"
            "Solicitar declaracion de zona de proteccion a autoridades.\n"
            "Controlar acceso al area hasta determinar la causa.\n"
            "Elaborar informe tecnico de dano para presentar a SMA."
        ),
        "HUMEDAL": (
            "EMERGENCIA: Notificar a DGA y SMA de desecacion critica.\n"
            "Iniciar restauracion hidrologica de emergencia.\n"
            "Documentar el estado actual como linea base de dano.\n"
            "Evaluar si se aplica Decreto de Humedales Urbanos o Ramsar.\n"
            "Contratar especialista en restauracion de ecosistemas acuaticos."
        ),
        "AGRICOLA": (
            "ACCION URGENTE: Implementar riego de emergencia de inmediato.\n"
            "Evaluar perdida de cosecha y notificar a aseguradora si corresponde.\n"
            "Aplicar tratamiento fitosanitario de emergencia ante plagas.\n"
            "Revisar infraestructura de riego en su totalidad.\n"
            "Consultar con agronomo para plan de recuperacion del cultivo."
        ),
        "GENERAL": (
            "ACCION URGENTE requerida en las proximas 48 horas.\n"
            "Notificar a las autoridades ambientales competentes.\n"
            "Realizar inspeccion en terreno de manera inmediata.\n"
            "Elaborar informe tecnico de la situacion detectada."
        ),
    },
}

# ============================================================================
# MÓDULO 6: GENERADOR DE PDF
# ============================================================================

class AuditoriaPDF(FPDF):
    """Clase personalizada para PDF"""
    
    def header(self):
        """Encabezado"""
        self.set_fill_color(20, 50, 80)
        self.rect(0, 0, 210, 30, 'F')
        
        self.set_text_color(255, 255, 255)
        self.set_font("helvetica", "B", 14)
        self.set_xy(10, 5)
        self.cell(0, 10, "AUDITORIA DE VIGILANCIA AMBIENTAL Y RESILIENCIA CLIMATICA", ln=1)
        
        self.set_font("helvetica", "I", 10)
        self.set_xy(10, 18)
        self.cell(0, 5, "BioCore Intelligence | Art. 6 RSEIA - Capacidad de Regeneracion y Permanencia")

        self.ln(15)
    
    def footer(self):
        """Pie"""
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Página {self.page_no()}", align="C")


def generar_pdf_auditoria_dinamico(proyecto_data, reporte_data, img_path=None):
    """Genera PDF dinámico"""
    
    pdf = AuditoriaPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    # SECCIÓN 1: INFORMACIÓN
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "1. INFORMACIÓN DEL PROYECTO", ln=1)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(0, 0, 0)
    
    info_table = [
        ["Proyecto:", proyecto_data.get('Proyecto', 'N/A')],
        ["Tipo:", proyecto_data.get('Tipo', 'N/A')],
        ["Responsable:", "Loreto Campos Carrasco"],
        ["Fecha:", reporte_data.get('fecha', 'N/A')],
        ["Año Base:", str(proyecto_data.get('ano_linea_base', 2017))],
    ]
    
    for row in info_table:
        pdf.set_font("helvetica", "B", 9)
        pdf.cell(50, 7, clean(row[0]))
        pdf.set_font("helvetica", "", 9)
        pdf.cell(0, 7, clean(row[1]), ln=1)
    
    pdf.ln(5)
    
    # SECCIÓN 2: CONAF
    if reporte_data.get('tipo') == 'BOSQUE':
        pdf.set_font("helvetica", "B", 14)
        pdf.set_text_color(20, 50, 80)
        pdf.cell(0, 10, "2. CLASIFICACIÓN FORESTAL (COMPATIBLE CONAF)", ln=1)

        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)

        info_conaf = reporte_data.get('info_conaf', {})

        conaf_table = [
            ["Tipo de Bosque:",         info_conaf.get('tipo_bosque', 'N/A')],
            ["Densidad:",               info_conaf.get('densidad', 'N/A')],
            ["Estado:",                 info_conaf.get('estado', 'No clasificado')],
            ["Área con cobertura (ha):", f"{info_conaf.get('area_bosque', 0):.2f}"],
            ["Cobertura de dosel (%):", f"{info_conaf.get('cobertura_dosel', 0):.1f}%"],
            ["Pérdida acumulada (ha):", f"{info_conaf.get('perdida_acumulada_ha', 0):.2f}"],
            ["Año mayor pérdida:",      info_conaf.get('anio_mayor_perdida', 'N/A')],
            ["Fuente de datos:",        info_conaf.get('fuente', 'N/A')],
        ]

        for row in conaf_table:
            pdf.set_font("helvetica", "B", 9)
            pdf.cell(65, 7, clean(row[0]))
            pdf.set_font("helvetica", "", 9)
            pdf.cell(0, 7, clean(str(row[1])), ln=1)

        # Alerta si hay pérdida significativa
        perdida = info_conaf.get('perdida_acumulada_ha', 0)
        area = info_conaf.get('area_bosque', 1)
        if area > 0 and perdida > 0:
            pct_perdida = (perdida / (area + perdida)) * 100
            pdf.ln(2)
            if pct_perdida > 20:
                pdf.set_font("helvetica", "B", 9)
                pdf.set_text_color(220, 50, 50)
                pdf.set_x(10)
                pdf.multi_cell(190, 5, clean(
                    f"ALERTA: Se ha perdido el {pct_perdida:.1f}% de la cobertura original "
                    f"desde el año 2000. Requiere evaluación conforme a Ley 20.283."
                ))
            elif pct_perdida > 5:
                pdf.set_font("helvetica", "B", 9)
                pdf.set_text_color(200, 100, 0)
                pdf.set_x(10)
                pdf.multi_cell(190, 5, clean(
                    f"PRECAUCION: Pérdida del {pct_perdida:.1f}% de cobertura detectada "
                    f"desde el año 2000. Monitoreo reforzado recomendado."
                ))
            pdf.set_text_color(0, 0, 0)

        pdf.ln(5)
    
    # SECCIÓN 3: ESTADO
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "3. ESTADO Y EVALUACIÓN", ln=1)
    
    color_r, color_g, color_b = reporte_data.get('color_estado', (100, 100, 100))
    pdf.set_fill_color(color_r, color_g, color_b)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 12)
    
    estado_texto = reporte_data.get('estado', 'ESTADO DESCONOCIDO').replace('🟢', '').replace('🟡', '').replace('🔴', '').strip()
    pdf.cell(0, 10, clean(f"ESTADO: {estado_texto}"), ln=1, fill=True)
    
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 3, "", ln=1)
    
    nivel_color = {
        'NORMAL': (40, 150, 80),
        'MODERADO': (200, 100, 0),
        'CRÍTICO': (220, 50, 50)
    }
    
    nivel = reporte_data.get('nivel', 'DESCONOCIDO')
    r, g, b = nivel_color.get(nivel, (100, 100, 100))
    
    pdf.set_fill_color(r, g, b)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 10)
    pdf.cell(0, 8, clean(f"Nivel de Riesgo: {nivel}"), ln=1, fill=True)
    
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    
    # SECCIÓN 4: SIGNOS
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "4. INDICADORES DE DEGRADACIÓN", ln=1)
    
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    
    signos = generar_signos_degradacion(reporte_data)
    colores_signo = {
        '[CRITICO]': (220, 50, 50),
        '[ALERTA]':  (200, 100, 0),
        '[AVISO]':   (100, 100, 0),
        '[OK]':      (40, 150, 80),
    }
    etiquetas_signo = {
        '[CRITICO]': '>> CRITICO: ',
        '[ALERTA]':  '>> ALERTA:  ',
        '[AVISO]':   '>> AVISO:   ',
        '[OK]':      '>> OK:      ',
    }
    for signo in signos:
        icono = signo['icono']
        r, g, b = colores_signo.get(icono, (80, 80, 80))
        etiqueta = etiquetas_signo.get(icono, '>> ')
        pdf.set_font("helvetica", "B", 9)
        pdf.set_text_color(r, g, b)
        pdf.cell(32, 5, clean(etiqueta))
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(158, 5, clean(signo['texto']))
        pdf.ln(1)
    
    pdf.ln(3)
    
    # SECCIÓN 5: ÍNDICES
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "5. ÍNDICES ESPECTRALES", ln=1)
    
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(40, 80, 120)
    pdf.set_text_color(255, 255, 255)
    
    col_widths = [35, 30, 30, 30, 45]
    headers = ["Indice", "Actual", "Linea Base", "Variacion", "Interpretacion"]

    for header, width in zip(headers, col_widths):
        pdf.cell(width, 8, header, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(0, 0, 0)

    indices_data = [
        ("SAVI (Ficha VE-5)", reporte_data.get('savi_actual', 0), reporte_data.get('savi_base', 0),
         reporte_data.get('variacion', 0), "Salud Vegetal"),
        ("NDWI", reporte_data.get('ndwi', 0), reporte_data.get('ndwi_base', 0),
         reporte_data.get('variacion_ndwi', 0), "Contenido agua"),
        ("NDSI", reporte_data.get('ndsi', 0), reporte_data.get('ndsi_base', 0),
         reporte_data.get('variacion_ndsi', 0), "Nieve/Hielo"),
        ("NDVI (Ficha VE-7)", reporte_data.get('ndvi', 0), reporte_data.get('ndvi_base', 0),
         reporte_data.get('variacion_ndvi', 0), "Estructura Habitat"),
        ("SWIR/Clay (SU-6)", reporte_data.get('swir', 0), reporte_data.get('clay', 0),
         0.0, "Integridad de Suelo"),
    ]

    for nombre, actual, base, variacion, interp in indices_data:
        pdf.cell(col_widths[0], 10
        , nombre, border=1)
        pdf.cell(col_widths[1], 10, f"{float(actual):.4f}", border=1, align="C")
        pdf.cell(col_widths[2], 10, f"{float(base):.4f}", border=1, align="C")
        pdf.cell(col_widths[3], 10, f"{float(variacion):+.1f}%", border=1, align="C")
        pdf.cell(col_widths[4], 10, interp, border=1, ln=1)

    pdf.cell(col_widths[0], 10, "TEMP", border=1)
    pdf.cell(col_widths[1], 10, f"{float(reporte_data.get('temp', 0)):.1f}C", border=1, align="C")
    pdf.cell(col_widths[2], 10, "-", border=1, align="C")
    pdf.cell(col_widths[3], 10, "-", border=1, align="C")
    pdf.cell(col_widths[4], 10, "Temperatura LST", border=1, ln=1)
    pdf.ln(10)
    
    # SECCIÓN 6: DIAGNÓSTICO
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "6. DIAGNÓSTICO TÉCNICO", ln=1)
    
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    diagnostico = reporte_data.get('diagnostico_completo', 'Sin diagnóstico')
    pdf.set_x(10)
    pdf.multi_cell(190, 6, clean(diagnostico.strip()))

    # SECCIÓN 7: SENTINEL-1 SAR — siempre en página nueva
    pdf.add_page()
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "7. MONITOREO RADAR - SENTINEL-1 SAR", ln=1)

    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    pdf.set_x(10)
    pdf.multi_cell(190, 5, clean(
        "Sentinel-1 opera en banda C (5.4 GHz) con polarizacion VV. "
        "A diferencia de sensores opticos, atraviesa nubes y lluvia, "
        "siendo el unico dato disponible en dias de alta nubosidad."
    ))
    pdf.ln(3)

    sar_vv = float(reporte_data.get('sar_vv', 0))

    # Tabla SAR
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(40, 80, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(65, 8, "Parametro",      border=1, align="C", fill=True)
    pdf.cell(35, 8, "Valor",          border=1, align="C", fill=True)
    pdf.cell(90, 8, "Interpretacion", border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(65, 8, "Retrodispersion VV (dB)", border=1)
    pdf.cell(35, 8, f"{sar_vv:.2f} dB", border=1, align="C")

    tipo_sar = reporte_data.get('tipo', 'GENERAL').upper()
    if tipo_sar == 'GLACIAR':
        if sar_vv < -15:
            interp_sar = "Hielo/nieve consolidada"
        elif sar_vv < -8:
            interp_sar = "Superficie mixta hielo-roca"
        else:
            interp_sar = "Roca expuesta o suelo seco"
    elif tipo_sar == 'HUMEDAL':
        if sar_vv < -15:
            interp_sar = "Agua libre presente"
        elif sar_vv < -8:
            interp_sar = "Humedad superficial moderada"
        else:
            interp_sar = "Superficie seca o sedimentos"
    elif tipo_sar == 'BOSQUE':
        if sar_vv > -5:
            interp_sar = "Dosel denso o biomasa alta"
        elif sar_vv > -10:
            interp_sar = "Bosque moderado"
        else:
            interp_sar = "Vegetacion escasa o suelo expuesto"
    elif tipo_sar == 'MINERIA':
        if sar_vv > -5:
            interp_sar = "Alta retrodispersion: superficie rugosa o material acumulado"
        elif sar_vv > -8:
            interp_sar = "Retrodispersion moderada: sustrato mineral expuesto"
        elif sar_vv > -12:
            interp_sar = "Superficie mineral sin alteraciones recientes"
        else:
            interp_sar = "Baja retrodispersion: posible superficie lisa o humeda"
    elif tipo_sar == 'AGRICOLA':
        if sar_vv > -8:
            interp_sar = "Cultivo desarrollado o suelo humedo"
        else:
            interp_sar = "Suelo desnudo o cultivo raso"
    else:
        interp_sar = "Sin interpretacion especifica"

    pdf.cell(90, 8, clean(interp_sar), border=1)
    pdf.ln(10)

    pdf.set_font("helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.set_x(10)
    pdf.multi_cell(190, 4, clean(
        "Nota: Valores tipicos de referencia VV: agua libre < -15 dB | "
        "vegetacion densa -10 a -5 dB | suelo seco/estructuras > -8 dB. "
        "Util como dato independiente en condiciones de nubosidad total."
    ))

    # SECCIÓN 8: GRÁFICOS
    rango_anios_pdf = reporte_data.get('rango_anios', 20)
    rango_label_pdf = reporte_data.get('rango_label', f"{rango_anios_pdf} años")
    if img_path and os.path.exists(img_path):
        pdf.add_page()
        
        pdf.set_font("helvetica", "B", 14)
        pdf.set_text_color(20, 50, 80)
        pdf.cell(0, 10, f"8. ANÁLISIS ESPECTRAL - {rango_label_pdf.upper()}", ln=1)
        pdf.ln(5)
        
        try:
            pdf.image(img_path, x=10, y=40, w=190)
        except:
            pass
    
    # SECCIÓN 9: ANÁLISIS DE VULNERABILIDAD Y RESILIENCIA CLIMÁTICA
    pdf.add_page()

    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "9. ANÁLISIS DE VULNERABILIDAD Y RESILIENCIA CLIMÁTICA", ln=1)
    pdf.ln(2)

    # --- 9.1 Vulnerabilidad Comunal (Arclim) ---
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 8, "9.1 Vulnerabilidad Comunal (Arclim - MMA)", ln=1)
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)

    tipo_vuln = reporte_data.get('tipo', 'GENERAL').upper()
    savi_v    = reporte_data.get('savi_actual', 0)
    ndwi_v    = reporte_data.get('ndwi', 0)
    temp_v    = reporte_data.get('temp', 0)
    nivel_v   = reporte_data.get('nivel', 'NORMAL')

    if tipo_vuln == 'GLACIAR':
        amenaza_arclim = "estres termico critico y perdida de criosfera"
        vuln_nivel = "Muy Alta"
    elif tipo_vuln == 'HUMEDAL':
        amenaza_arclim = "estres hidrico y desecacion de ecosistemas acuaticos"
        vuln_nivel = "Alta" if ndwi_v < 0.30 else "Moderada"
    elif tipo_vuln == 'BOSQUE':
        amenaza_arclim = "incendios forestales y estres hidrico de dosel"
        vuln_nivel = "Alta" if temp_v > 20 or ndwi_v < 0.20 else "Moderada"
    elif tipo_vuln == 'MINERIA':
        amenaza_arclim = "variabilidad hidrica y degradacion de sustrato mineral"
        vuln_nivel = "Alta" if nivel_v == 'CRITICO' else "Moderada"
    elif tipo_vuln == 'AGRICOLA':
        amenaza_arclim = "estres hidrico de cultivos y perdida de rendimiento"
        vuln_nivel = "Alta" if savi_v < 0.30 else "Moderada"
    else:
        amenaza_arclim = "variabilidad climatica general"
        vuln_nivel = "Moderada"

    pdf.set_x(10)
    pdf.multi_cell(190, 5, clean(
        f"Para el poligono en analisis, Arclim (Ministerio del Medio Ambiente) proyecta "
        f"una vulnerabilidad '{vuln_nivel}' por {amenaza_arclim}, justificando la "
        f"vigilancia activa y el monitoreo satelital continuo como instrumento de "
        f"cumplimiento preventivo ante el SEA y la SMA."
    ))
    pdf.ln(4)

    # --- 9.2 Certificación de Sumidero de Carbono ---
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 8, "9.2 Certificacion de Sumidero de Carbono (Ley 21.455)", ln=1)
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)

    if savi_v > 0.30:
        cert_texto = (
            f"Se certifica que la biomasa presente (SAVI: {savi_v:.3f}) actua como "
            f"reservorio de carbono nativo, contribuyendo a la meta de carbono "
            f"neutralidad al 2050 establecida en la Ley Marco de Cambio Climatico "
            f"(Ley 21.455). El nivel de vegetacion detectado es consistente con "
            f"la funcion de sumidero activo segun Art. 6 RSEIA."
        )
    elif savi_v > 0.15:
        cert_texto = (
            f"La biomasa presente (SAVI: {savi_v:.3f}) mantiene una funcion parcial "
            f"de sumidero de carbono. Se recomienda implementar medidas de "
            f"restauracion para fortalecer la capacidad de secuestro de CO2 "
            f"conforme a Ley 21.455 y el Plan de Accion Climatica Nacional."
        )
    else:
        cert_texto = (
            f"El nivel de biomasa actual (SAVI: {savi_v:.3f}) indica capacidad de "
            f"sumidero reducida. Se requiere plan de restauracion ecologica para "
            f"cumplir metas de carbono neutralidad (Ley 21.455). Estado reportable "
            f"ante la SMA como hallazgo de degradacion de servicios ecosistemicos."
        )

    pdf.set_x(10)
    pdf.multi_cell(190, 5, clean(cert_texto))
    pdf.ln(4)

    # --- 9.3 Estabilidad Basal ---
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 8, "9.3 Estabilidad Basal del Sustrato (Clay Index - Ficha SU-6)", ln=1)
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)

    clay_v = reporte_data.get('clay', 0)
    swir_v = reporte_data.get('swir', 0)

    if clay_v < 0.25:
        clay_texto = (
            f"El indice de arcillas (cly: {clay_v:.3f}) demuestra que el sustrato "
            f"mineral permanece estable y sin remocion no autorizada. Esto evidencia "
            f"que no existe erosion por actividades antropical, cumpliendo "
            f"plenamente con la Ficha SU-6 del SEA (Procesos Erosivos). "
            f"SWIR complementario: {swir_v:.3f}."
        )
        clay_estado = "ESTABLE"
        clay_color = (40, 150, 80)
    else:
        clay_texto = (
            f"El indice de arcillas (cly: {clay_v:.3f}) presenta valores elevados, "
            f"indicando posible alteracion del sustrato mineral. Se recomienda "
            f"inspeccion en terreno para verificar ausencia de procesos erosivos "
            f"no autorizados conforme a Ficha SU-6 del SEA. "
            f"SWIR: {swir_v:.3f}."
        )
        clay_estado = "REVISAR"
        clay_color = (200, 100, 0)

    pdf.set_x(10)
    pdf.multi_cell(190, 5, clean(clay_texto))
    pdf.ln(3)

    pdf.set_fill_color(*clay_color)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 9)
    pdf.cell(60, 7, clean(f"Clay Index (cly): {clay_v:.3f}"), border=1, fill=True)
    pdf.cell(60, 7, clean(f"SWIR (B11): {swir_v:.3f}"), border=1, fill=True)
    pdf.cell(60, 7, clean(f"Estado SU-6: {clay_estado}"), border=1, fill=True, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # --- 9.4 Análisis de Correlación Hídrica ---
    swir_base_pdf = reporte_data.get('swir_base', swir_v)
    ndwi_base_pdf = reporte_data.get('ndwi_base', ndwi_v)

    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 8, "9.4 Diagnostico de Respuesta Hidrica (Art. 6 RSEIA)", ln=1)
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    pdf.set_x(10)

    if swir_v < swir_base_pdf * 0.85 and ndwi_v > ndwi_base_pdf * 1.10:
        pdf.multi_cell(190, 5, clean(
            "Se certifica recarga hidrica natural por eventos de precipitacion. "
            "La correlacion entre la reduccion de SWIR y el incremento de NDWI "
            "es consistente con una respuesta ecosistemica saludable, validando "
            "la capacidad de regeneracion del area conforme al Art. 6 RSEIA."
        ))
    elif swir_v > swir_base_pdf * 1.15 and ndwi_v < ndwi_base_pdf * 0.90:
        pdf.multi_cell(190, 5, clean(
            "Se detecta patron de sequia o perdida hidrica: SWIR elevado e NDWI "
            "reducido respecto a la linea base. Este patron puede indicar estres "
            "hidrico ecosistemico. Se recomienda verificacion en terreno y "
            "notificacion preventiva a la DGA conforme a Art. 6 RSEIA."
        ))
    else:
        pdf.multi_cell(190, 5, clean(
            "Los indices hidricos (SWIR y NDWI) muestran variaciones dentro del "
            "rango esperado para el ecosistema monitoreado. La respuesta hidrica "
            "es consistente con condiciones normales de operacion del ciclo "
            "hidrologico segun Art. 6 RSEIA."
        ))

    pdf.ln(4)

    # --- 9.5 Plan de Acción Adaptativo ---
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 8, "9.5 Plan de Accion Adaptativo", ln=1)

    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(40, 80, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(25, 8, "Nivel", border=1, fill=True, align="C")
    pdf.cell(80, 8, "Accion Requerida", border=1, fill=True, align="C")
    pdf.cell(85, 8, "Responsable / Marco Legal", border=1, fill=True, align="C", ln=1)

    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(0, 0, 0)

    acciones = [
        ("Nivel 1", "Cruce con meteorologia local e inspeccion tecnica en terreno", "Tecnico de Terreno"),
        ("Nivel 2", "Ajuste de Plan Hidrico e informe preventivo a la SMA", "Especialista en Monitoreo Ambiental | Art. 6 RSEIA"),
    ]

    if nivel_v in ('MODERADO', 'CRITICO'):
        acciones.append(("Nivel 3", "Notificacion formal a DGA/SMA y auditoria en terreno", "Director Tecnico | Ley 19.300 / Ley 21.455"))
    if nivel_v == 'CRITICO':
        acciones.append(("Nivel 4", "Paralizar actividades y activar protocolo de emergencia ambiental", "SMA / DGA | RCA y RSEIA"))

    fill = False
    for nivel_acc, accion, responsable in acciones:
        pdf.set_fill_color(240, 245, 255) if not fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(25, 8, clean(nivel_acc), border=1, fill=True)
        pdf.cell(80, 8, clean(accion), border=1, fill=True)
        pdf.cell(85, 8, clean(responsable), border=1, fill=True, ln=1)
        fill = not fill

    pdf.ln(5)
    
    # SECCIÓN 10: RECOMENDACIONES
    pdf.add_page()
    
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 10, "10. RECOMENDACIONES Y PLAN DE ACCION", ln=1)
    
    riesgo_val = reporte_data.get('nivel', 'NORMAL')
    tipo_val = reporte_data.get('tipo', 'GENERAL')
    
    texto_final_recom = recomendaciones_por_tipo.get(riesgo_val, {}).get(tipo_val, "Sin recomendaciones específicas.")
    
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(0, 0, 0)
    
    lineas_encontradas = texto_final_recom.split('\n')
    for contador, contenido_linea in enumerate(lineas_encontradas, 1):
        item_para_pdf = contenido_linea.strip()
        if item_para_pdf:
            pdf.set_x(10)
            pdf.multi_cell(190, 6, f"{contador}. {clean(item_para_pdf)}")
    
    pdf.ln(10)
    
    # --- NOTA DE TERRENO ---
    pdf.set_font("helvetica", "B", 10)
    pdf.set_text_color(139, 0, 0)
    pdf.cell(0, 8, "NOTA: VERIFICACIÓN EN TERRENO RECOMENDADA", ln=1)
    
    pdf.set_font("helvetica", "", 7)
    pdf.set_text_color(80, 80, 80)
    mensaje_nota = (
        "Los índices espectrales satelitales (SAVI, NDWI, NDSI, NDVI) proporcionan estimaciones con resolución de 10-30 metros. "
        "Se recomienda realizar inspecciones en terreno periódicamente para validar observaciones satelitales."
    )
    pdf.set_x(10)
    pdf.multi_cell(190, 4, clean(mensaje_nota))
    
    pdf.ln(20)
    
    # --- FIRMA FINAL ---
    pdf.set_font("helvetica", "B", 11)
    pdf.set_text_color(20, 50, 80)
    pdf.cell(0, 5, "Loreto Campos Carrasco", align="C")
    pdf.ln(6)
    
    pdf.set_font("helvetica", "I", 9)
    pdf.cell(0, 4, "Directora Técnica - BioCore Intelligence", align="C")
    pdf.ln(5)
    
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    fecha_reporte = datetime.now().strftime("%d/%m/%Y")
    pdf.cell(0, 4, f"Fecha de emisión: {fecha_reporte}", align="C")

    return pdf


# === INICIALIZAR SESSION STATE ===
if 'authenticated' not in st.session_state:
    st.session_state['authenticated'] = False
    st.session_state['admin_mode'] = False
    st.session_state['proyecto_cliente'] = None
    st.session_state['reporte_actual'] = None
    st.session_state['reporte_preview'] = None
    st.session_state['mostrar_preview'] = False

# === SIDEBAR ===
with st.sidebar:
    st.markdown("### 🔐 Autenticación")
    
    with st.expander("🔑 Iniciar Sesión", expanded=True):
        admin_mode = st.checkbox("🔑 Modo Admin")
        
        if admin_mode:
            password_admin = st.text_input("Contraseña Admin", type="password", key="admin_pwd")
            if st.button("✅ Entrar como Admin", key="btn_admin"):
                if es_admin(password_admin):
                    st.session_state['admin_mode'] = True
                    st.session_state['authenticated'] = True
                    st.success("✅ Modo Admin activado")
                    st.rerun()
                else:
                    st.error("❌ Contraseña incorrecta")
        else:
            email_login = st.text_input("Correo electrónico", key="login_email",
                                        placeholder="usuario@ejemplo.com")
            password_login = st.text_input("Contraseña", type="password", key="login_pwd")
            if st.button("✅ Entrar como Cliente", key="btn_cliente"):
                is_valid, cliente = verificar_credenciales_usuario(email_login, password_login)
                if is_valid:
                    proyecto_nombre = cliente.get('Proyecto', email_login)
                    st.session_state['authenticated'] = True
                    st.session_state['proyecto_cliente'] = cliente.get('Proyecto')
                    st.session_state['cliente_data'] = cliente
                    st.success(f"✅ Bienvenido, {proyecto_nombre}")
                    st.rerun()
                else:
                    st.error("❌ Correo o contraseña incorrectos")
    
    st.markdown("---")
    
    if st.session_state.get('authenticated'):
        if st.session_state.get('admin_mode'):
            st.info("🔑 **Sesión Admin Activa**")
        else:
            proyecto = st.session_state.get('proyecto_cliente', 'N/A')
            st.info(f"👤 **Usuario**: {proyecto}")
        
        if st.button("🚪 Cerrar Sesión"):
            st.session_state['authenticated'] = False
            st.session_state['admin_mode'] = False
            st.session_state['proyecto_cliente'] = None
            st.session_state['reporte_actual'] = None
            st.rerun()

# === PANTALLA DE BIENVENIDA ===
if not st.session_state.get('authenticated'):
    crear_portada_biocore()
    st.stop()


def mostrar_guia():
    """Contenido de la pestaña Guía — accesible para admin y clientes"""
    st.title("📖 Guía de Uso BioCore Intelligence")

    with st.expander("📚 1. ¿Qué es BioCore Intelligence?", expanded=True):
        st.markdown("""
BioCore Intelligence es un sistema de **vigilancia ambiental satelital** que monitorea proyectos en tiempo real usando datos de NASA y ESA.

Utiliza el **Protocolo de Validación de Línea Base Espectral**, que distingue entre:
- ✅ **Cambios reales** (degradación, contaminación, pérdida forestal, desecación)
- 🔄 **Ruido de sensor** (variaciones naturales en zonas áridas o minerales)

Esto garantiza que las alertas generadas sean técnicamente sólidas y defendibles ante autoridades regulatorias (SMA, DGA, CONAF).
        """)

    with st.expander("🛰️ 2. Sensores y Fuentes de Datos"):
        st.markdown("""
| Sensor | Uso principal | Resolución |
|---|---|---|
| **Sentinel-2** (ESA) | Índices espectrales SAVI, NDWI, NDSI, NDVI | 10–30 m |
| **Sentinel-1 SAR** (ESA) | Radar de apertura sintética, atraviesa nubes | 10 m |
| **MODIS** (NASA) | Temperatura de superficie (LST) | 1 km |
| **NASA FIRMS** | Detección de focos de incendio activos | 375 m |
| **ERA5-Land** (ECMWF) | Precipitación y temperatura histórica (N años) | ~9 km |
| **Hansen GFC** | Pérdida forestal acumulada desde el año 2000 | 30 m |
| **Copernicus LC** | Clasificación de cobertura y uso de suelo | 100 m |
        """)

    with st.expander("🎯 3. Tipos de Proyectos Monitoreados"):
        df_tipos = pd.DataFrame({
            "Tipo": ["⛏️ MINERÍA", "❄️ GLACIAR", "🌲 BOSQUE", "💧 HUMEDAL", "🌾 AGRÍCOLA"],
            "Índice Principal": ["NDWI", "NDSI", "SAVI", "NDWI", "SAVI + NDWI"],
            "Normativa Asociada": [
                "SMA / DGA",
                "DGA / Min. Medio Ambiente",
                "Ley 20.283 / CONAF",
                "Decreto Humedales / Ramsar",
                "SAG / normativa agrícola"
            ],
            "Alerta Crítica si...": [
                "NDWI < 0.10 o caída > 20%",
                "NDSI < 0.20 o retracción > 15%",
                "SAVI < 0.25 o pérdida > 25%",
                "NDWI < 0.25 o caída > 20%",
                "SAVI < 0.25 y NDWI < 0.20"
            ]
        })
        st.dataframe(df_tipos, use_container_width=True)

    with st.expander("🔢 4. Índices Espectrales Explicados"):
        st.markdown("""
#### SAVI — Índice de Vegetación Ajustado al Suelo
- **Rango:** -1 a 1 (en la práctica 0 a 0.7 para vegetación)
- **> 0.40** → Vegetación densa y sana
- **0.25 – 0.40** → Vegetación moderada o bajo estrés
- **< 0.25** → Vegetación degradada o suelo expuesto
- *Más preciso que NDVI en zonas áridas o con suelo desnudo*

#### NDWI — Índice de Agua en Vegetación/Cuerpos de Agua
- **Rango:** -1 a 1
- **> 0.40** → Agua libre o vegetación muy húmeda
- **0.20 – 0.40** → Humedad moderada
- **< 0.10** → Estrés hídrico severo o ausencia de agua

#### NDSI — Índice de Nieve y Hielo
- **Rango:** -1 a 1
- **> 0.50** → Hielo perenne consolidado
- **0.35 – 0.50** → Cobertura en transición estacional
- **< 0.20** → Sin firma de hielo, sustrato expuesto

#### NDVI — Índice de Vegetación de Diferencia Normalizada
- **Rango:** -1 a 1
- Indicador de verdor general, complementa al SAVI
- Más sensible al ruido de suelo en zonas áridas

#### Temperatura LST (MODIS)
- Temperatura de superficie terrestre en °C
- Umbral de alerta: **> 15°C en zonas glaciares**
- Indicador de riesgo de incendio en bosques y humedales

#### SAR VV (Sentinel-1)
- Retrodispersión radar en banda C, polarización VV
- Valores típicos: **agua libre < -15 dB** | **vegetación densa -10 a -5 dB** | **suelo/estructuras > -8 dB**
- Único sensor que funciona con cobertura nubosa total
        """)

    with st.expander("🔍 5. Clasificación Automática del Terreno"):
        df_terrenos = pd.DataFrame({
            "Clasificación": ["MINERAL_ARIDO", "VEGETADO", "CRIOSFERA", "HIDRICO"],
            "Criterio de detección": ["SAVI < 0.10", "SAVI >= 0.30", "NDSI >= 0.35", "NDWI >= 0.20"],
            "Características": [
                "Sin vegetación, mayor tolerancia a variaciones",
                "Bosques, cultivos — alta sensibilidad",
                "Glaciares y nieve permanente",
                "Lagos, humedales, ríos"
            ],
            "Comportamiento del sistema": [
                "Permite mayor variación sin alerta",
                "Alerta ante pérdidas > 10–25%",
                "Alerta ante retracción > 15%",
                "Alerta ante desecación > 20%"
            ]
        })
        st.dataframe(df_terrenos, use_container_width=True)

    with st.expander("⚖️ 6. Reglas de Validación y Niveles de Riesgo"):
        st.markdown("""
El sistema calcula automáticamente el **nivel de riesgo** en tres categorías:

#### 🟢 NORMAL — Bajo Control
- Los índices están dentro de los rangos históricos esperados
- Variación respecto a línea base dentro de umbrales normales
- No se requiere acción inmediata

#### 🟡 MODERADO — Precaución
- Se detectan variaciones significativas pero no críticas
- Se recomienda intensificar el monitoreo
- Posible ciclo estacional o estrés temporal

#### 🔴 CRÍTICO — Alerta
- Degradación confirmada o pérdida acelerada
- Se requiere acción inmediata y notificación a autoridades
- El reporte PDF documenta el hallazgo para presentación regulatoria

---
**Reglas de validación por variación:**

| Variación vs. Línea Base | Clasificación |
|---|---|
| < 10% | Normal |
| 10% – 25% | Moderado |
| > 25% (o índice bajo umbral crítico) | Crítico |
        """)

    with st.expander("📋 7. Cómo Generar un Reporte"):
        st.markdown("""
1. Inicia sesión con tus credenciales de proyecto
2. Ve a la pestaña **🛰️ Vigilancia**
3. Selecciona tu proyecto en el menú desplegable
4. Haz clic en **"🚀 Ejecutar Análisis Satelital"**
5. Espera el procesamiento (puede tomar 30–90 segundos según el área)
6. Revisa el diagnóstico, índices y gráficos históricos
7. Descarga el **PDF** o envía por **Telegram** usando los botones correspondientes

> 💡 El análisis usa siempre la imagen más reciente disponible con menos del 30% de nubosidad.
        """)

    with st.expander("📊 8. Interpretación del Reporte PDF"):
        st.markdown("""
El reporte PDF generado contiene:

- **Sección 1:** Información del proyecto y responsable
- **Sección 2:** Clasificación CONAF (solo proyectos BOSQUE)
- **Sección 3:** Estado global y nivel de riesgo con color
- **Sección 4:** Indicadores de degradación detectados
- **Sección 5:** Tabla de índices espectrales actuales vs. línea base
- **Sección 6:** Diagnóstico técnico detallado
- **Sección 7:** Monitoreo radar Sentinel-1 SAR
- **Sección 8:** Gráficos históricos de 20 años
- **Sección 9:** Análisis climático ERA5-Land (temperatura y precipitación del período)
- **Sección 10:** Recomendaciones según nivel de riesgo y tipo de proyecto

> El PDF es un documento técnico válido para presentación ante **SMA, DGA y CONAF**.
        """)

    with st.expander("📞 9. Contacto y Soporte"):
        st.markdown("""
**Responsable Técnica:** Loreto Campos Carrasco
📧 consultorabiocore@gmail.com
⏰ Lunes a Viernes, 8:00 – 18:00 hrs

Para ajustes de parámetros, nuevas coordenadas o problemas técnicos, contacta directamente al equipo BioCore Intelligence.
        """)

# === TABS PRINCIPALES ===
if st.session_state.get('admin_mode'):
    tab1, tab_informe, tab_excel, tab_clientes, tab_guia = st.tabs([
        "🛰️ Vigilancia", 
        "📋 Auditorías", 
        "📊 Base Datos",
        "👥 Gestión de Clientes",
        "📖 Guía"
    ])
else:
    tab1, tab_informe, tab_excel, tab_historial, tab_config, tab_guia = st.tabs([
        "🛰️ Vigilancia", 
        "📋 Auditorías", 
        "📊 Base Datos",
        "📨 Mi Historial",
        "⚙️ Configuración",
        "📖 Guía"
    ])

# === PESTAÑA 1: VIGILANCIA ===
with tab1:
    try:
        proyectos = supabase.table("usuarios").select("*").execute().data
    except:
        proyectos = []

    if st.session_state.get('admin_mode'):
        proyectos_mostrar = proyectos
    else:
        proyecto_cliente = st.session_state.get('proyecto_cliente')
        proyectos_mostrar = [p for p in proyectos if p.get('Proyecto') == proyecto_cliente]

    if proyectos_mostrar:
        for p_idx, p in enumerate(proyectos_mostrar):
            st.markdown(f"### 📍 {p['Proyecto']}")

            col_mapa, col_reporte = st.columns([2.5, 1])

            with col_mapa:
                try:
                    coords = obtener_coordenadas_correctamente(p)
                    m_obj = dibujar_mapa_biocore(coords)
                    folium_static(m_obj, width=850, height=500)
                except Exception as e:
                    st.error(f"Error al dibujar mapa: {str(e)}")

            with col_reporte:
                if st.button("🚀 Ejecutar Reporte", key=f"vigilancia_btn_{p_idx}"):
                    with st.spinner("Analizando..."):
                        reporte = generar_reporte_total(p)
                        
                        if reporte.get('tipo') != 'error':
                            st.session_state['reporte_actual'] = reporte
                            
                            estado_color = '#10b981' if 'CONTROL' in reporte['estado'] else '#f97316' if 'PRECAUCIÓN' in reporte['estado'] else '#ef4444'
                            st.markdown(f"""
                            <div style="background-color:#1e293b; padding:20px; border-radius:10px; border-left:6px solid {estado_color};">
                            <h2 style="color: white; margin: 0;">{reporte['estado']}</h2>
                            <p style="color: #cbd5e1; margin: 10px 0 0 0;"><b>Riesgo:</b> {reporte['nivel']}</p>
                            </div>
                            """, unsafe_allow_html=True)
                            
                            fig_gauge = go.Figure(go.Indicator(
                                mode="gauge+number+delta",
                                value=reporte['savi_actual'],
                                number={'suffix': '', 'valueformat': '.4f'},
                                title={'text': "SAVI — Vigor de Vegetacion"},
                                delta={
                                    'reference': reporte['savi_base'],
                                    'suffix': '% vs base',
                                    'relative': True,
                                    'valueformat': '.1f'
                                },
                                gauge={
                                    'axis': {
                                        'range': [0, 0.8],
                                        'tickfont': {'size': 12}
                                    },
                                    'bar': {'color': "#1e40af"},
                                    'borderwidth': 2,
                                    'bordercolor': "#333",
                                    'steps': [
                                        {'range': [0, 0.15], 'color': "#fee2e2"},
                                        {'range': [0.15, 0.35], 'color': "#fef3c7"},
                                        {'range': [0.35, 0.8], 'color': "#dcfce7"}
                                    ],
                                    'threshold': {
                                        'line': {'color': "black", 'width': 4},
                                        'thickness': 0.75,
                                        'value': reporte['savi_actual']
                                    }
                                }
                            ))
                            fig_gauge.update_layout(
                                height=420,
                                font={'size': 14, 'family': 'Arial'},
                                margin=dict(l=20, r=20, t=100, b=20),
                                paper_bgcolor='rgba(0,0,0,0)',
                                plot_bgcolor='rgba(0,0,0,0)'
                            )
                            st.plotly_chart(fig_gauge, use_container_width=True)
                            
                            col1, col2 = st.columns(2)
                            with col1:
                                st.metric("SAVI (VE-5)", f"{reporte['savi_actual']:.4f}", f"{reporte['variacion']:+.1f}%")
                                st.metric("NDSI", f"{reporte['ndsi']:.4f}", f"{reporte['variacion_ndsi']:+.1f}%")
                                st.metric("SWIR (SU-6)", f"{reporte['swir']:.4f}")
                                st.metric("SAR VV", f"{reporte.get('sar_vv', 0):.2f} dB")
                            with col2:
                                st.metric("NDWI", f"{reporte['ndwi']:.4f}", f"{reporte['variacion_ndwi']:+.1f}%")
                                st.metric("NDVI (VE-7)", f"{reporte['ndvi']:.4f}", f"{reporte['variacion_ndvi']:+.1f}%")
                                st.metric("Clay Index", f"{reporte['clay']:.4f}")
                                st.metric("Temp LST", f"{reporte['temp']:.1f} C")
                            
                            st.success(reporte['diagnostico_completo'])
                        else:
                            st.error(reporte.get('error', 'Error desconocido'))
    else:
        st.warning("No hay proyectos disponibles")

# === PESTAÑA 2: AUDITORÍAS ===
with tab_informe:
    st.subheader("📋 Generador de Auditorías Profesionales")
    
    try:
        proyectos_list = supabase.table("usuarios").select("Proyecto,Tipo").execute().data
        proyectos_dict = {p['Proyecto']: p.get('Tipo', 'MINERIA') for p in proyectos_list}
        proyectos_nombres = list(proyectos_dict.keys())
    except:
        proyectos_nombres = []
        proyectos_dict = {}
    
    if proyectos_nombres:
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            proyecto_sel = st.selectbox("📍 Seleccionar Proyecto", proyectos_nombres, key="audit_proj")
        
        with col2:
            rango_sel = st.selectbox("📊 Rango de Análisis", 
                ["Últimos 7 días", "Últimas 2 semanas", "Último mes", "Últimos 3 meses",
                 "Último año", "Últimos 5 años", "Últimos 10 años", "Últimos 15 años", "Últimos 20 años"],
                key="audit_rango")
        
        rango_dias_map = {
            "Últimos 7 días": 7,
            "Últimas 2 semanas": 14,
            "Último mes": 30,
            "Últimos 3 meses": 90,
            "Último año": 365,
            "Últimos 5 años": 365 * 5,
            "Últimos 10 años": 365 * 10,
            "Últimos 15 años": 365 * 15,
            "Últimos 20 años": 365 * 20,
        }
        rango_dias = rango_dias_map.get(rango_sel, 30)

        meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

        with col3:
            if rango_sel == "Último mes":
                anio_sel = st.number_input("📆 Año", value=datetime.now().year, min_value=2010, max_value=datetime.now().year, key="audit_anio")
            else:
                anio_sel = datetime.now().year
                st.empty()

        with col4:
            rangos_multianio = ["Últimos 5 años", "Últimos 10 años", "Últimos 15 años", "Últimos 20 años"]
            if rango_sel == "Último mes":
                mes_sel = st.selectbox("📅 Mes", meses, index=datetime.now().month - 1, key="audit_mes")
            elif rango_sel == "Último año":
                mes_sel = "Año completo"
                st.info("📅 Año completo")
            elif rango_sel in rangos_multianio:
                anio_ini = datetime.now().year - rango_dias_map[rango_sel] // 365
                mes_sel = f"{anio_ini} — {datetime.now().year}"
                st.info(f"📅 {anio_ini} — {datetime.now().year}")
            else:
                fecha_ini = (datetime.now() - timedelta(days=rango_dias)).strftime("%d/%m/%Y")
                fecha_fin = datetime.now().strftime("%d/%m/%Y")
                mes_sel = f"{fecha_ini} al {fecha_fin}"
                st.info(f"📅 {fecha_ini} al {fecha_fin}")

        if st.button("🚀 Generar Auditoría Completa", key="btn_gen_audit"):
            with st.spinner("⏳ Procesando auditoría..."):
                try:
                    proyecto_data = supabase.table("usuarios")\
                        .select("*")\
                        .eq("Proyecto", proyecto_sel)\
                        .execute().data[0]
                    
                    reporte_data = generar_reporte_total(proyecto_data, rango_dias, rango_sel=rango_sel)
                    
                    if reporte_data.get('tipo') == 'error':
                        st.error(f"❌ {reporte_data.get('error', 'Error desconocido')}")
                    else:
                        st.session_state['reporte_actual'] = reporte_data
                        st.session_state['proyecto_audit'] = proyecto_sel
                        st.session_state['mes_audit'] = mes_sel
                        st.session_state['anio_audit'] = anio_sel
                        st.session_state['proyecto_data'] = proyecto_data
                        st.session_state['mostrar_preview'] = True
                        
                        st.success("✅ Auditoría generada exitosamente")
                        st.rerun()
                
                except Exception as e:
                    st.error(f"❌ Error al generar auditoría: {str(e)}")
        
        st.markdown("---")
        
        if st.session_state.get('mostrar_preview') and 'reporte_actual' in st.session_state and st.session_state['reporte_actual']:
            reporte = st.session_state['reporte_actual']
            proyecto = st.session_state.get('proyecto_audit', 'N/A')
            mes = st.session_state.get('mes_audit', 'N/A')
            anio = st.session_state.get('anio_audit', datetime.now().year)
            proyecto_data = st.session_state.get('proyecto_data', {})
            
            st.info("VISTA PREVIA DEL REPORTE")
            
            estado_color = '#10b981' if 'CONTROL' in reporte['estado'] else '#f97316' if 'PRECAUCIÓN' in reporte['estado'] else '#ef4444'
            
            st.markdown(f"""
            <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); 
                        padding:25px; border-radius:15px; border-left:6px solid {estado_color}; margin-bottom: 20px;">
            <h2 style="color: white; margin: 0;">{reporte['estado']}</h2>
            <p style="color: #cbd5e1; margin: 10px 0 0 0;"><b>Riesgo:</b> {reporte['nivel']}</p>
            <p style="color: #cbd5e1; margin: 5px 0;"><b>Período:</b> {mes} {anio}</p>
            </div>
            """, unsafe_allow_html=True)
            
            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            with col_m1:
                st.metric("SAVI (VE-5)", f"{reporte['savi_actual']:.4f}", f"{reporte['variacion']:+.1f}%")
            with col_m2:
                st.metric("NDWI", f"{reporte['ndwi']:.4f}", f"{reporte['variacion_ndwi']:+.1f}%")
            with col_m3:
                st.metric("NDSI", f"{reporte['ndsi']:.4f}", f"{reporte['variacion_ndsi']:+.1f}%")
            with col_m4:
                st.metric("NDVI (VE-7)", f"{reporte['ndvi']:.4f}", f"{reporte['variacion_ndvi']:+.1f}%")

            col_m5, col_m6, col_m7, col_m8 = st.columns(4)
            with col_m5:
                st.metric("SWIR (SU-6)", f"{reporte['swir']:.4f}")
            with col_m6:
                st.metric("Clay Index", f"{reporte['clay']:.4f}")
            with col_m7:
                st.metric("SAR VV", f"{reporte.get('sar_vv', 0):.2f} dB")
            with col_m8:
                st.metric("Temp LST", f"{reporte['temp']:.1f} C")
            
            st.markdown("")
            
            st.markdown("### Diagnostico Tecnico")
            st.info(f"{reporte['diagnostico_completo']}")
            
            with st.expander("Ver Mensaje Telegram"):
                mensaje_telegram = generar_mensaje_telegram_dinamico(reporte, proyecto_data)
                st.code(mensaje_telegram, language="text")
            
            st.markdown("### Acciones")
            col_btn1, col_btn2, col_btn3, col_btn4 = st.columns(4)
            
            with col_btn1:
                if st.button("Descargar PDF", key="btn_download_pdf"):
                    with st.spinner("Generando PDF..."):
                        try:
                            img_path = generar_graficos_profesionales(
                                reporte.get('indices_historicos', {}),
                                reporte.get('tipo', 'GENERAL')
                            )
                            pdf = generar_pdf_auditoria_dinamico(proyecto_data, reporte, img_path)

                            result = pdf.output(dest='S')
                            if isinstance(result, str):
                                pdf_bytes = result.encode('latin-1')
                            elif isinstance(result, (bytes, bytearray)):
                                pdf_bytes = bytes(result)
                            else:
                                raise Exception("Tipo inesperado en la exportación PDF: " + str(type(result)))

                            st.download_button(
                                label="Descargar Auditoria PDF",
                                data=pdf_bytes,
                                file_name=f"Auditoria_{proyecto}_{mes}_{anio}.pdf",
                                mime="application/pdf",
                                key="download_btn"
                            )

                            if img_path and os.path.exists(img_path):
                                os.remove(img_path)
                            st.success("PDF listo para descargar")
                        except Exception as e:
                            st.error(f"Error al generar PDF: {str(e)}")
            
            with col_btn2:
                if st.button("Regenerar", key="btn_new_audit"):
                    st.session_state['reporte_actual'] = None
                    st.session_state['mostrar_preview'] = False
                    st.rerun()
            
            with col_btn3:
                if st.button("Enviar Telegram", key="btn_send_telegram"):
                    with st.spinner("Enviando..."):
                        try:
                            if proyecto_data.get('id_telegram'):
                                token_telegram = st.secrets.get('telegram', {}).get('token', '')
                                if token_telegram:
                                    mensaje_telegram = generar_mensaje_telegram_dinamico(reporte, proyecto_data)
                                    response = requests.post(
                                        f"https://api.telegram.org/bot{token_telegram}/sendMessage",
                                        data={
                                            "chat_id": proyecto_data.get('id_telegram'),
                                            "text": mensaje_telegram,
                                        },
                                        timeout=10
                                    )
                                    if response.status_code == 200:
                                        st.success("Reporte enviado por Telegram")
                                    else:
                                        st.error(f"Error Telegram: {response.status_code}")
                                else:
                                    st.warning("Token de Telegram no configurado")
                            else:
                                st.warning("No hay ID de Telegram registrado")
                        except Exception as e:
                            st.error(f"Error: {str(e)}")
            
            with col_btn4:
                if st.button("Cancelar", key="btn_cancel"):
                    st.session_state['reporte_actual'] = None
                    st.session_state['mostrar_preview'] = False
                    st.rerun()
    else:
        st.warning("📌 No hay proyectos registrados")

# === PESTAÑA 3: BASE DE DATOS ===
with tab_excel:
    st.subheader("📊 Base de Datos")
    
    if st.session_state.get('admin_mode'):
        try:
            res = supabase.table("usuarios").select("*").execute()
            if res.data:
                df = pd.DataFrame(res.data)
                st.dataframe(df, use_container_width=True)
                
                csv = df.to_csv(index=False)
                st.download_button(
                    label="📥 Descargar CSV",
                    data=csv,
                    file_name="usuarios.csv",
                    mime="text/csv"
                )
            else:
                st.info("No hay datos")
        except Exception as e:
            st.error(f"Error: {e}")
    else:
        st.info("Acceso restringido a administradores")

# === PESTAÑA 4: GESTIÓN DE CLIENTES ===
if st.session_state.get('admin_mode'):
    with tab_clientes:
        st.subheader("👥 Gestión de Clientes")
        
        tab_nuevo, tab_editar, tab_eliminar = st.tabs(["➕ Nuevo Cliente", "✏️ Editar", "🗑️ Eliminar"])
        
        with tab_nuevo:
            st.markdown("### Registrar nuevo cliente")
            
            with st.form("form_nuevo_cliente"):
                proyecto = st.text_input("Nombre del Proyecto *")
                tipo = st.selectbox("Tipo de Proyecto *", ["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"])
                coordenadas = st.text_area("Coordenadas (JSON format) *", 
                                           placeholder='[[lon1, lat1], [lon2, lat2], [lon3, lat3], [lon1, lat1]]',
                                           height=100)
                anio_base = st.number_input("Año de Línea Base", value=2015, min_value=2000)
                email = st.text_input("Email del cliente")
                telegram = st.text_input("ID Telegram (opcional)")
                
                st.markdown("---")
                
                col_h, col_f = st.columns(2)
                with col_h:
                    hora_reporte = st.time_input("Hora de envío", value=time(8, 0))
                with col_f:
                    frecuencia_reporte = st.selectbox("Frecuencia", ["Diario", "Semanal"])
                
                password_cliente = st.text_input("Contraseña cliente *", type="password")
                
                if st.form_submit_button("✅ Registrar Cliente"):
                    if not proyecto or not tipo or not coordenadas or not password_cliente:
                        st.error("❌ Completa todos los campos requeridos (*)")
                    else:
                        try:
                            coords_parsed = json.loads(coordenadas)
                            
                            if not isinstance(coords_parsed, list) or len(coords_parsed) < 3:
                                raise ValueError("Mínimo 3 coordenadas requeridas")
                            
                            pwd_hash = hash_password(password_cliente)
                            
                            nuevo_registro = {
                                "Proyecto": proyecto,
                                "Tipo": tipo,
                                "Coordenadas": json.dumps(coords_parsed),
                                "email_cliente": email.strip().lower() if email else None,
                                "password_cliente": pwd_hash,
                                "id_telegram": telegram if telegram else None,
                                "ano_linea_base": int(anio_base),
                                "hora_reporte": hora_reporte.strftime("%H:%M"),
                                "frecuencia_reporte": frecuencia_reporte
                            }
                            
                            supabase.table("usuarios").insert(nuevo_registro).execute()
                            st.success(f"✅ Cliente {proyecto} registrado exitosamente")
                            st.balloons()
                        except json.JSONDecodeError:
                            st.error("❌ Coordenadas con formato JSON inválido")
                        except ValueError as ve:
                            st.error(f"❌ Error: {str(ve)}")
                        except Exception as e:
                            st.error(f"❌ Error al registrar: {str(e)}")

        # === EDITAR CLIENTE ===
        with tab_editar:
            st.markdown("### Editar cliente existente")

            try:
                res_clientes = supabase.table("usuarios").select("Proyecto").execute()
                proyectos_lista = [r["Proyecto"] for r in res_clientes.data] if res_clientes.data else []
            except Exception as e:
                st.error(f"Error al cargar clientes: {e}")
                proyectos_lista = []

            if proyectos_lista:
                proyecto_sel = st.selectbox("Selecciona el cliente a editar", proyectos_lista, key="sel_editar")

                if proyecto_sel:
                    try:
                        res_edit = supabase.table("usuarios").select("*").eq("Proyecto", proyecto_sel).execute()
                        cliente_edit = res_edit.data[0] if res_edit.data else {}
                    except Exception as e:
                        st.error(f"Error al cargar datos: {e}")
                        cliente_edit = {}

                    if cliente_edit:
                        with st.form("form_editar_cliente"):
                            tipo_edit = st.selectbox(
                                "Tipo de Proyecto",
                                ["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"],
                                index=["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"].index(
                                    cliente_edit.get("Tipo", "MINERIA")
                                ) if cliente_edit.get("Tipo") in ["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"] else 0
                            )

                            coords_raw = cliente_edit.get("Coordenadas", "")
                            if isinstance(coords_raw, list):
                                coords_raw = json.dumps(coords_raw)

                            coordenadas_edit = st.text_area(
                                "Coordenadas (JSON)",
                                value=coords_raw or "",
                                height=100
                            )

                            anio_base_edit = st.number_input(
                                "Año de Línea Base",
                                value=int(cliente_edit.get("ano_linea_base", 2015)),
                                min_value=2000
                            )

                            email_edit = st.text_input(
                                "Email del cliente",
                                value=cliente_edit.get("email_cliente") or ""
                            )

                            telegram_edit = st.text_input(
                                "ID Telegram",
                                value=cliente_edit.get("id_telegram") or ""
                            )

                            st.markdown("---")
                            st.markdown("**⏰ Configuración de Reportes Automáticos**")
                            col_h2, col_f2 = st.columns(2)

                            hora_str = cliente_edit.get("hora_reporte", "08:00") or "08:00"
                            try:
                                h2, m2 = map(int, hora_str.split(":"))
                                hora_default_edit = time(h2, m2)
                            except:
                                hora_default_edit = time(8, 0)

                            freq_actual_edit = cliente_edit.get("frecuencia_reporte", "Diario") or "Diario"

                            with col_h2:
                                hora_edit = st.time_input("Hora de envío", value=hora_default_edit, key="hora_edit")
                            with col_f2:
                                frecuencia_edit = st.selectbox(
                                    "Frecuencia",
                                    ["Diario", "Semanal"],
                                    index=0 if freq_actual_edit == "Diario" else 1
                                )

                            st.markdown("---")
                            nueva_password = st.text_input(
                                "Nueva contraseña (dejar en blanco para no cambiar)",
                                type="password",
                                key="nueva_pwd_edit"
                            )

                            if st.form_submit_button("💾 Guardar cambios"):
                                try:
                                    update_data = {
                                        "Tipo": tipo_edit,
                                        "email_cliente": email_edit.strip().lower() if email_edit else None,
                                        "id_telegram": telegram_edit if telegram_edit else None,
                                        "ano_linea_base": int(anio_base_edit),
                                        "hora_reporte": hora_edit.strftime("%H:%M"),
                                        "frecuencia_reporte": frecuencia_edit,
                                    }

                                    if coordenadas_edit.strip():
                                        coords_parsed_edit = json.loads(coordenadas_edit)
                                        update_data["Coordenadas"] = json.dumps(coords_parsed_edit)

                                    if nueva_password.strip():
                                        update_data["password_cliente"] = hash_password(nueva_password)

                                    supabase.table("usuarios").update(update_data).eq("Proyecto", proyecto_sel).execute()
                                    st.success(f"✅ Cliente '{proyecto_sel}' actualizado correctamente")
                                    st.rerun()
                                except json.JSONDecodeError:
                                    st.error("❌ Coordenadas con formato JSON inválido")
                                except Exception as e:
                                    st.error(f"❌ Error al actualizar: {str(e)}")
            else:
                st.info("No hay clientes registrados")

        # === ELIMINAR CLIENTE ===
        with tab_eliminar:
            st.markdown("### Eliminar cliente")

            try:
                res_del = supabase.table("usuarios").select("Proyecto").execute()
                proyectos_del = [r["Proyecto"] for r in res_del.data] if res_del.data else []
            except Exception as e:
                st.error(f"Error al cargar clientes: {e}")
                proyectos_del = []

            if proyectos_del:
                proyecto_del = st.selectbox("Selecciona el cliente a eliminar", proyectos_del, key="sel_eliminar")

                st.warning(f"⚠️ Esta acción eliminará permanentemente al cliente **{proyecto_del}** y todos sus datos.")

                confirmar = st.checkbox("Confirmo que deseo eliminar este cliente", key="check_eliminar")

                if st.button("🗑️ Eliminar Cliente", disabled=not confirmar, key="btn_eliminar"):
                    try:
                        supabase.table("usuarios").delete().eq("Proyecto", proyecto_del).execute()
                        st.success(f"✅ Cliente '{proyecto_del}' eliminado correctamente")
                        st.rerun()
                    except Exception as e:
                        st.error(f"❌ Error al eliminar: {str(e)}")
            else:
                st.info("No hay clientes registrados")

# === PESTAÑA GUIA (ADMIN) ===
if st.session_state.get('admin_mode'):
    with tab_guia:
        mostrar_guia()

# === PESTAÑA HISTORIAL ===
if not st.session_state.get('admin_mode'):
    with tab_historial:
        st.title("📨 Mi Historial")
        proyecto_cliente = st.session_state.get('proyecto_cliente')
        
        try:
            res = supabase.table("historial_reportes").select("*").eq("proyecto", proyecto_cliente).execute()
            if res.data:
                df_hist = pd.DataFrame(res.data)
                st.dataframe(df_hist, use_container_width=True)
                
                csv = df_hist.to_csv(index=False)
                st.download_button(
                    label="📥 Descargar Historial (CSV)",
                    data=csv,
                    file_name=f"Historial_{proyecto_cliente}.csv",
                    mime="text/csv"
                )
            else:
                st.info("No hay reportes generados aún")
        except Exception as e:
            st.error(f"Error al cargar historial: {e}")
    
    
    with tab_config:
        st.title("⚙️ Configurar Reportes Automáticos por Telegram")
        proyecto_cliente = st.session_state.get('proyecto_cliente')
        
        # Obtener nombre de empresa (es el nombre del proyecto)
        try:
            res = supabase.table("usuarios").select("Proyecto").eq("Proyecto", proyecto_cliente).execute()
            nombre_empresa = res.data[0]['Proyecto'] if res.data else proyecto_cliente
        except:
            nombre_empresa = proyecto_cliente
        
        # ===== LLAMAR AL MÓDULO TELEGRAM_REPORTER =====
        mostrar_formulario_reportes()
        st.markdown("---")
        mostrar_resumen_reportes()
    
    with tab_guia:
        mostrar_guia()

st.markdown("---")
st.markdown("""
<div style="text-align: center; color: #888; font-size: 0.9em; padding: 20px;">
<b>BioCore Intelligence</b> © 2026 | Todos los derechos reservados
</div>
""", unsafe_allow_html=True)
