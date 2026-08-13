# ============================================================================
# BIOCORE INTELLIGENCE - APP FINAL INTEGRADA v3.0
# Vigilancia ambiental + geoespacial 3D con trazabilidad PDF / Telegram / Supabase
# ============================================================================

import ast
import hashlib
import hmac
import json
import math
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import ee
import folium
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go
from fpdf import FPDF
from streamlit_folium import folium_static
from supabase import create_client

from telegram_reporter import (
    mostrar_formulario_reportes,
    mostrar_resumen_reportes,
)

st.set_page_config(
    page_title="BioCore Intelligence",
    page_icon="🛰️",
    layout="wide",
)

st.markdown("""
<style>
[data-testid="stSidebar"] { background-color: #0e1117; }
h1 { font-size: 2rem !important; }
h2 { font-size: 1.5rem !important; }
.block-container { padding-top: 1.4rem; }

/* Tabs más usables en móvil */
.stTabs [data-baseweb="tab-list"] {
  gap: 0.2rem;
  overflow-x: auto;
  scrollbar-width: none;
  flex-wrap: nowrap;
}
.stTabs [data-baseweb="tab"] {
  white-space: nowrap;
  padding-left: 0.55rem;
  padding-right: 0.55rem;
}

/* Métricas: evitar títulos gigantes y truncados */
[data-testid="stMetric"] {
  padding: 0.2rem 0.1rem;
}
[data-testid="stMetricLabel"] {
  font-size: 0.95rem !important;
}
[data-testid="stMetricValue"] {
  font-size: 2rem !important;
  line-height: 1.08 !important;
  white-space: normal !important;
  overflow-wrap: anywhere !important;
}
[data-testid="stMetricDelta"] {
  white-space: normal !important;
}

@media (max-width: 700px) {
  .block-container { padding-left: 0.8rem; padding-right: 0.8rem; }
  [data-testid="stMetricValue"] { font-size: 1.3rem !important; }
  [data-testid="stMetricLabel"] { font-size: 0.85rem !important; }
  .stTabs [data-baseweb="tab"] {
    font-size: 0.92rem;
    padding-left: 0.45rem;
    padding-right: 0.45rem;
  }
}
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Infraestructura
# ---------------------------------------------------------------------------

@st.cache_resource
def init_db():
    return create_client(
        st.secrets["connections"]["supabase"]["url"],
        st.secrets["connections"]["supabase"]["key"],
    )

supabase = init_db()

@st.cache_resource
def iniciar_gee():
    try:
        if not ee.data.is_initialized():
            creds = json.loads(st.secrets["gee"]["json"])
            ee_creds = ee.ServiceAccountCredentials(
                creds["client_email"],
                key_data=creds["private_key"],
            )
            ee.Initialize(ee_creds, project=creds.get("project_id"))
        return True
    except Exception as exc:
        st.error(f"No fue posible inicializar Google Earth Engine: {exc}")
        return False

GEE_OK = iniciar_gee()


def hash_password(password: str) -> str:
    # Compatibilidad con las cuentas existentes. Se migrará a hashes salteados.
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def es_admin(password: str) -> bool:
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
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()
    return hmac.compare_digest(derived, expected)


def verificar_credenciales_usuario(email: str, password: str):
    try:
        email_normalizado = (email or "").strip().lower()
        res = supabase.table("usuarios").select("*").execute()
        for cliente in (res.data or []):
            guardado = (cliente.get("email_cliente") or "").strip().lower()
            if guardado != email_normalizado:
                continue
            password_guardada = cliente.get("password_cliente") or ""
            if password_guardada and hmac.compare_digest(
                hash_password(password), str(password_guardada)
            ):
                return True, cliente
        return False, None
    except Exception:
        return False, None


def limpiar_coordenadas(coords):
    if not isinstance(coords, list):
        raise ValueError("Las coordenadas deben ser una lista.")
    if len(coords) < 3:
        raise ValueError("Se requieren al menos 3 puntos para formar el polígono.")

    cleaned = []
    for i, coord in enumerate(coords):
        if not isinstance(coord, (list, tuple)) or len(coord) != 2:
            raise ValueError(f"Coordenada {i} inválida: se esperaba [lon, lat].")
        lon = float(coord[0])
        lat = float(coord[1])
        if not -180 <= lon <= 180:
            raise ValueError(f"Longitud fuera de rango: {lon}")
        if not -90 <= lat <= 90:
            raise ValueError(f"Latitud fuera de rango: {lat}")
        cleaned.append([lon, lat])

    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return cleaned


def obtener_coordenadas_correctamente(project_data: Dict[str, Any]):
    raw = project_data.get("Coordenadas")
    if raw in (None, "", "null"):
        raise ValueError("El proyecto no tiene coordenadas.")

    if isinstance(raw, list):
        return limpiar_coordenadas(raw)

    if isinstance(raw, str):
        try:
            return limpiar_coordenadas(json.loads(raw))
        except json.JSONDecodeError:
            try:
                return limpiar_coordenadas(ast.literal_eval(raw))
            except Exception as exc:
                raise ValueError("No fue posible interpretar las coordenadas.") from exc

    if isinstance(raw, dict) and "coordinates" in raw:
        coords = raw["coordinates"]
        # GeoJSON Polygon: [[[lon,lat],...]]
        if coords and isinstance(coords[0], list) and coords[0] and isinstance(coords[0][0], list):
            coords = coords[0]
        return limpiar_coordenadas(coords)

    raise ValueError(f"Formato de coordenadas no reconocido: {type(raw).__name__}")


def dibujar_mapa_biocore(coordenadas):
    """Mapa técnico 2D. Se mantiene separado de la visualización 3D."""
    coords = limpiar_coordenadas(coordenadas)
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    centro = [sum(lats) / len(lats), sum(lons) / len(lons)]

    m = folium.Map(
        location=centro,
        zoom_start=13,
        tiles="https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        attr="Google Satellite",
    )
    folium.Polygon(
        locations=[[c[1], c[0]] for c in coords],
        color="cyan",
        weight=2,
        fill=True,
        fill_opacity=0.18,
        tooltip="Área de análisis BioCore",
    ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    return m


def _bbox_info(coords):
    coords = limpiar_coordenadas(coords)
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    center_lon = (west + east) / 2
    center_lat = (south + north) / 2
    width_m = max(1.0, (east - west) * 111_320 * math.cos(math.radians(center_lat)))
    height_m = max(1.0, (north - south) * 110_540)
    return {
        "west": west, "east": east, "south": south, "north": north,
        "center_lon": center_lon, "center_lat": center_lat,
        "width_m": width_m, "height_m": height_m,
        "area_km2": (width_m * height_m) / 1_000_000,
    }


def _parse_osm_height(tags):
    raw = str((tags or {}).get("height") or "").lower().replace("meters", "").replace("meter", "").replace("m", "").strip()
    try:
        value = float(raw)
        if 1 <= value <= 300:
            return value
    except Exception:
        pass
    try:
        levels = float((tags or {}).get("building:levels") or 0)
        if levels > 0:
            return min(300.0, max(3.0, levels * 3.0))
    except Exception:
        pass
    return 7.0


@st.cache_data(ttl=86400, show_spinner=False)
def _fetch_osm_context_cached(west, south, east, north):
    """
    Contexto OSM opcional para la vista 3D.
    Sólo se usa como visualización contextual, nunca como levantamiento topográfico.
    """
    center_lat = (south + north) / 2
    width_m = max(1.0, (east - west) * 111_320 * math.cos(math.radians(center_lat)))
    height_m = max(1.0, (north - south) * 110_540)
    area_km2 = width_m * height_m / 1_000_000
    if area_km2 > 80:
        return {
            "buildings": {"type": "FeatureCollection", "features": []},
            "roads": {"type": "FeatureCollection", "features": []},
            "warning": "Área demasiado extensa para consultar edificios/caminos OSM de forma responsable.",
        }

    query = f"""[out:json][timeout:20];
    (
      way["building"]({south},{west},{north},{east});
      way["highway"]({south},{west},{north},{east});
    );
    out tags geom;"""

    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    data = None
    last_error = None
    for endpoint in endpoints:
        try:
            response = requests.post(endpoint, data=query.encode("utf-8"), timeout=22)
            response.raise_for_status()
            data = response.json()
            break
        except Exception as exc:
            last_error = str(exc)

    if not data:
        return {
            "buildings": {"type": "FeatureCollection", "features": []},
            "roads": {"type": "FeatureCollection", "features": []},
            "warning": f"OSM 3D no disponible en esta carga: {last_error or 'sin respuesta'}",
        }

    buildings = []
    roads = []
    for el in (data.get("elements") or [])[:5000]:
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        coords_ll = [[float(p["lon"]), float(p["lat"])] for p in geom if "lon" in p and "lat" in p]
        tags = el.get("tags") or {}

        if tags.get("building") and len(coords_ll) >= 3:
            if coords_ll[0] != coords_ll[-1]:
                coords_ll.append(coords_ll[0])
            buildings.append({
                "type": "Feature",
                "properties": {
                    "height": _parse_osm_height(tags),
                    "name": tags.get("name") or "Edificio OSM",
                },
                "geometry": {"type": "Polygon", "coordinates": [coords_ll]},
            })
        elif tags.get("highway") and len(coords_ll) >= 2:
            roads.append({
                "type": "Feature",
                "properties": {
                    "class": tags.get("highway"),
                    "name": tags.get("name") or "Vía OSM",
                },
                "geometry": {"type": "LineString", "coordinates": coords_ll},
            })

    return {
        "buildings": {"type": "FeatureCollection", "features": buildings},
        "roads": {"type": "FeatureCollection", "features": roads},
        "warning": None,
    }


def fetch_osm_3d_context(coords):
    b = _bbox_info(coords)
    return _fetch_osm_context_cached(b["west"], b["south"], b["east"], b["north"])


def render_mapa_3d_biocore(coordenadas, osm_context=None, height=560):
    """
    Visor 3D robusto de BioCore basado en Plotly + Copernicus DEM GLO-30.

    No depende de un servidor localhost, MapLibre, Three.js ni de CDN externos.
    Esto lo hace mucho más estable dentro de Streamlit Community Cloud y en
    navegadores móviles.

    OpenStreetMap se usa sólo como contexto opcional de caminos/edificios.
    El relieve cuantitativo proviene de Copernicus DEM GLO-30.
    """
    coords = limpiar_coordenadas(coordenadas)
    b = _bbox_info(coords)

    dem_info = obtener_dem_grid_cached(
        json.dumps(coords, ensure_ascii=False),
        max_cells=58,
    )
    dem = np.asarray(dem_info["dem"], dtype=float)
    rows, cols = dem.shape

    width_m = max(float(b["width_m"]), 1.0)
    height_m = max(float(b["height_m"]), 1.0)

    xs = np.linspace(-width_m / 2.0, width_m / 2.0, cols)
    ys = np.linspace(-height_m / 2.0, height_m / 2.0, rows)

    fig = go.Figure()

    fig.add_trace(go.Surface(
        x=np.tile(xs, (rows, 1)),
        y=np.tile(ys[:, None], (1, cols)),
        z=dem,
        colorscale="Earth",
        showscale=True,
        colorbar=dict(
            title="Elevación (m)",
            thickness=12,
            len=0.68,
        ),
        hovertemplate=(
            "X %{x:.0f} m<br>"
            "Y %{y:.0f} m<br>"
            "Elevación %{z:.1f} m<extra>DEM Copernicus</extra>"
        ),
        name="Copernicus DEM",
    ))

    def _xy_from_lonlat(lon, lat):
        east_west = max(float(b["east"]) - float(b["west"]), 1e-12)
        north_south = max(float(b["north"]) - float(b["south"]), 1e-12)
        x = ((float(lon) - float(b["west"])) / east_west) * width_m - width_m / 2.0
        y = ((float(lat) - float(b["south"])) / north_south) * height_m - height_m / 2.0
        return x, y

    def _z_from_xy(x, y, offset=5.0):
        col = ((x + width_m / 2.0) / width_m) * max(cols - 1, 1)
        row = ((y + height_m / 2.0) / height_m) * max(rows - 1, 1)
        return float(_bilinear_grid(dem, row, col)) + float(offset)

    # AOI
    aoi_x, aoi_y, aoi_z = [], [], []
    for lon, lat in coords:
        x, y = _xy_from_lonlat(lon, lat)
        aoi_x.append(x)
        aoi_y.append(y)
        aoi_z.append(_z_from_xy(x, y, offset=8.0))

    fig.add_trace(go.Scatter3d(
        x=aoi_x,
        y=aoi_y,
        z=aoi_z,
        mode="lines",
        line=dict(width=7),
        name="Área BioCore",
        hovertemplate="Límite del área de estudio<extra></extra>",
    ))

    # Contexto OSM opcional. Se dibuja como huellas/líneas sobre el DEM;
    # no se interpreta como levantamiento topográfico.
    osm_context = osm_context or {}
    roads = ((osm_context.get("roads") or {}).get("features") or [])[:80]
    buildings = ((osm_context.get("buildings") or {}).get("features") or [])[:120]

    first_road = True
    for feature in roads:
        geometry = feature.get("geometry") or {}
        road_coords = geometry.get("coordinates") or []
        if geometry.get("type") != "LineString" or len(road_coords) < 2:
            continue
        rx, ry, rz = [], [], []
        for lon, lat in road_coords:
            x, y = _xy_from_lonlat(lon, lat)
            rx.append(x)
            ry.append(y)
            rz.append(_z_from_xy(x, y, offset=4.0))
        fig.add_trace(go.Scatter3d(
            x=rx, y=ry, z=rz,
            mode="lines",
            line=dict(width=3),
            name="Caminos OSM",
            legendgroup="roads",
            showlegend=first_road,
            hoverinfo="skip",
        ))
        first_road = False

    first_building = True
    for feature in buildings:
        geometry = feature.get("geometry") or {}
        rings = geometry.get("coordinates") or []
        if geometry.get("type") != "Polygon" or not rings:
            continue
        ring = rings[0]
        if len(ring) < 3:
            continue
        bx, by, bz = [], [], []
        for lon, lat in ring:
            x, y = _xy_from_lonlat(lon, lat)
            bx.append(x)
            by.append(y)
            bz.append(_z_from_xy(x, y, offset=5.0))
        fig.add_trace(go.Scatter3d(
            x=bx, y=by, z=bz,
            mode="lines",
            line=dict(width=2),
            name="Edificios OSM",
            legendgroup="buildings",
            showlegend=first_building,
            hoverinfo="skip",
        ))
        first_building = False

    st.markdown("### 🗻 BioCore 3D")
    st.caption("Copernicus DEM GLO-30 · límite del área de estudio")

    fig.update_layout(
        height=int(height),
        margin=dict(l=0, r=0, t=8, b=0),
        showlegend=False,
        scene=dict(
            xaxis_title="Este relativo (m)",
            yaxis_title="Norte relativo (m)",
            zaxis_title="Elevación (m)",
            aspectmode="data",
            camera=dict(
                eye=dict(x=1.45, y=-1.55, z=1.15)
            ),
        ),
    )

    st.plotly_chart(
        fig,
        use_container_width=True,
        config={
            "displaylogo": False,
            "scrollZoom": True,
            "responsive": True,
        },
        key=f"biocore_3d_{abs(hash(json.dumps(coords))) % 1000000}",
    )

    st.caption(
        "Relieve: Copernicus DEM GLO-30. Caminos/edificios: OpenStreetMap cuando existen. "
        "Vista de contexto; no sustituye un levantamiento topográfico de ingeniería."
    )

def _utm_epsg(lon, lat):
    zone = int((float(lon) + 180) // 6) + 1
    return f"EPSG:{32600 + zone if float(lat) >= 0 else 32700 + zone}"


@st.cache_data(ttl=3600, show_spinner=False)
def obtener_dem_grid_cached(coords_json: str, max_cells: int = 48):
    """Obtiene una malla DEM moderada para visualización/LOS; evita cargas masivas."""
    coords = limpiar_coordenadas(json.loads(coords_json))
    b = _bbox_info(coords)
    scale_m = max(30.0, max(b["width_m"], b["height_m"]) / max(20, int(max_cells)))
    scale_m = float(math.ceil(scale_m / 10.0) * 10.0)
    rect = ee.Geometry.Rectangle([b["west"], b["south"], b["east"], b["north"]], geodesic=False)
    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").select("DEM").mosaic()
    proj = ee.Projection(_utm_epsg(b["center_lon"], b["center_lat"])).atScale(scale_m)
    sample = dem.reproject(proj).sampleRectangle(region=rect, defaultValue=-9999).getInfo()
    raw = ((sample or {}).get("properties") or {}).get("DEM")
    if raw is None:
        raise RuntimeError("Earth Engine no devolvió la malla DEM.")
    arr = np.asarray(raw, dtype=float)
    if arr.ndim != 2 or min(arr.shape) < 3:
        raise RuntimeError(f"Malla DEM inválida: {arr.shape}")
    arr[arr <= -9000] = np.nan
    if not np.isfinite(arr).any():
        raise RuntimeError("La malla DEM no contiene elevaciones válidas.")
    fill = float(np.nanmedian(arr))
    arr = np.where(np.isfinite(arr), arr, fill)

    # Seguridad adicional: reducir matrices inesperadamente grandes.
    while max(arr.shape) > 70:
        arr = arr[::2, ::2]
        scale_m *= 2

    rows, cols = arr.shape
    dx = b["width_m"] / max(1, cols - 1)
    dy = b["height_m"] / max(1, rows - 1)
    return {
        "dem": arr.tolist(),
        "rows": rows,
        "cols": cols,
        "dx_m": float(dx),
        "dy_m": float(dy),
        "nominal_scale_m": float(scale_m),
        "bbox": b,
        "source": "COPERNICUS/DEM/GLO30",
    }


def _azimuth_in_sector(azimuth, center, width):
    if width >= 359.9:
        return True
    diff = ((azimuth - center + 180) % 360) - 180
    return abs(diff) <= width / 2


def _bilinear_grid(grid, row, col):
    rows, cols = grid.shape
    r0 = int(max(0, min(rows - 1, math.floor(row))))
    c0 = int(max(0, min(cols - 1, math.floor(col))))
    r1 = min(rows - 1, r0 + 1)
    c1 = min(cols - 1, c0 + 1)
    tr = max(0.0, min(1.0, row - r0))
    tc = max(0.0, min(1.0, col - c0))
    return (
        (1-tr)*(1-tc)*grid[r0,c0] +
        (1-tr)*tc*grid[r0,c1] +
        tr*(1-tc)*grid[r1,c0] +
        tr*tc*grid[r1,c1]
    )


def _count_components(mask):
    rows, cols = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    count = 0
    sizes = []
    for r in range(rows):
        for c in range(cols):
            if not mask[r, c] or seen[r, c]:
                continue
            count += 1
            size = 0
            stack = [(r, c)]
            seen[r, c] = True
            while stack:
                rr, cc = stack.pop()
                size += 1
                for dr, dc in ((-1,0),(1,0),(0,-1),(0,1)):
                    nr, nc = rr+dr, cc+dc
                    if 0 <= nr < rows and 0 <= nc < cols and mask[nr,nc] and not seen[nr,nc]:
                        seen[nr,nc] = True
                        stack.append((nr,nc))
            sizes.append(size)
    return count, sorted(sizes, reverse=True)


def simular_cobertura_radar_geotecnico(
    dem,
    dx_m,
    dy_m,
    radar_row,
    radar_col,
    radar_height_m,
    max_range_m,
    az_center_deg,
    az_width_deg,
    el_min_deg=-60,
    el_max_deg=60,
):
    """
    Simulación geométrica propia de BioCore: línea de vista + sensibilidad geométrica.
    No modela SNR real ni reemplaza software geotécnico validado.
    """
    grid = np.asarray(dem, dtype=float)
    rows, cols = grid.shape
    rr = int(max(0, min(rows-1, radar_row)))
    rc = int(max(0, min(cols-1, radar_col)))
    rx, ry = rc * dx_m, rr * dy_m
    rz = float(grid[rr, rc]) + float(radar_height_m)

    analyzable = np.zeros((rows, cols), dtype=bool)
    visible = np.zeros((rows, cols), dtype=bool)
    quality = np.full((rows, cols), np.nan, dtype=float)

    grad_y, grad_x = np.gradient(grid, dy_m, dx_m)

    for r in range(rows):
        for c in range(cols):
            tx, ty, tz = c * dx_m, r * dy_m, float(grid[r, c])
            vx, vy = tx - rx, ty - ry
            dist2 = math.hypot(vx, vy)
            if dist2 < max(dx_m, dy_m) * 0.5 or dist2 > max_range_m:
                continue
            elevation = math.degrees(math.atan2(tz - rz, dist2))
            if not (el_min_deg <= elevation <= el_max_deg):
                continue
            azimuth = math.degrees(math.atan2(vx, vy))
            if not _azimuth_in_sector(azimuth, az_center_deg, az_width_deg):
                continue
            analyzable[r, c] = True

            steps = max(2, int(max(abs(r-rr), abs(c-rc))))
            shadowed = False
            for k in range(1, steps):
                t = k / steps
                sr = rr + t * (r - rr)
                sc = rc + t * (c - rc)
                ray_z = rz + t * (tz - rz)
                terrain_z = _bilinear_grid(grid, sr, sc)
                # pequeña tolerancia para evitar autoclusiones numéricas
                if terrain_z > ray_z + 0.75:
                    shadowed = True
                    break
            if shadowed:
                continue

            visible[r, c] = True
            nx, ny, nz = -grad_x[r, c], -grad_y[r, c], 1.0
            nn = math.sqrt(nx*nx + ny*ny + nz*nz) or 1.0
            nx, ny, nz = nx/nn, ny/nn, nz/nn
            # vector desde la superficie hacia el radar
            sx, sy, sz = rx-tx, ry-ty, rz-tz
            sn = math.sqrt(sx*sx + sy*sy + sz*sz) or 1.0
            sx, sy, sz = sx/sn, sy/sn, sz/sn
            incidence_factor = max(0.0, min(1.0, nx*sx + ny*sy + nz*sz))
            distance_factor = math.sqrt(max(0.0, 1.0 - dist2/max_range_m))
            quality[r, c] = incidence_factor * distance_factor

    n_an = int(analyzable.sum())
    n_vis = int(visible.sum())
    coverage_pct = (100.0 * n_vis / n_an) if n_an else 0.0
    shadow_mask = analyzable & (~visible)
    zone_count, zone_sizes = _count_components(shadow_mask)
    mean_quality = float(np.nanmean(quality)) if np.isfinite(quality).any() else None

    return {
        "coverage_pct": float(coverage_pct),
        "visible_area_m2": float(n_vis * dx_m * dy_m),
        "analyzable_area_m2": float(n_an * dx_m * dy_m),
        "shadow_zone_count": int(zone_count),
        "largest_shadow_zone_cells": int(zone_sizes[0]) if zone_sizes else 0,
        "geometric_quality_mean": mean_quality,
        "visible": visible,
        "analyzable": analyzable,
        "quality": quality,
        "radar_row": rr,
        "radar_col": rc,
        "radar_z_m": rz,
    }


def _geotech_plot(dem_info, result):
    grid = np.asarray(dem_info["dem"], dtype=float)
    rows, cols = grid.shape
    x = np.arange(cols) * dem_info["dx_m"]
    y = np.arange(rows) * dem_info["dy_m"]
    q = np.asarray(result["quality"], dtype=float)
    surface_color = np.where(np.isfinite(q), q, -0.08)

    fig = go.Figure()
    fig.add_trace(go.Surface(
        x=x, y=y, z=grid,
        surfacecolor=surface_color,
        cmin=-0.1, cmax=1.0,
        colorscale=[
            [0.0, "#273244"],
            [0.08, "#6b7280"],
            [0.1, "#ef4444"],
            [0.45, "#f59e0b"],
            [0.72, "#84cc16"],
            [1.0, "#10b981"],
        ],
        colorbar=dict(title="Sensibilidad geométrica"),
        showscale=True,
        hovertemplate="X %{x:.0f} m<br>Y %{y:.0f} m<br>Z %{z:.0f} m<extra></extra>",
    ))
    rr, rc = result["radar_row"], result["radar_col"]
    fig.add_trace(go.Scatter3d(
        x=[rc * dem_info["dx_m"]],
        y=[rr * dem_info["dy_m"]],
        z=[result["radar_z_m"]],
        mode="markers+text",
        text=["Radar"],
        textposition="top center",
        marker=dict(size=6, color="#00d4ff"),
        name="Radar",
    ))
    fig.update_layout(
        margin=dict(l=0, r=0, b=0, t=35),
        title="Cobertura geométrica sobre DEM",
        scene=dict(
            xaxis_title="Este relativo (m)",
            yaxis_title="Norte relativo (m)",
            zaxis_title="Elevación (m)",
            aspectmode="data",
        ),
        height=560,
    )
    return fig


def _insar_session_key(project_name):
    return f"insar_v3::{project_name}"


def _geotech_session_key(project_name):
    return f"geotech_v3::{project_name}"


def build_insar_request(project_data, coords, start_date, end_date, orbit_pass="AUTO"):
    return {
        "schema_version": "biocore-insar-job-1.0",
        "project": project_data.get("Proyecto") or "N/D",
        "project_type": project_data.get("Tipo") or "N/D",
        "sensor": "Sentinel-1 SLC",
        "analysis": "DInSAR_LOS_DISPLACEMENT",
        "date_range": {"start": str(start_date), "end": str(end_date)},
        "preferred_orbit_pass": orbit_pass,
        "aoi": {"type": "Polygon", "coordinates": [limpiar_coordenadas(coords)]},
        "required_outputs": [
            "coherence", "wrapped_interferogram", "unwrapped_phase",
            "los_displacement_mm", "geocoded_displacement", "qa_metadata"
        ],
        "quality_requirements": {
            "preserve_slc_phase": True,
            "report_orbit_and_relative_orbit": True,
            "report_temporal_baseline": True,
            "report_coherence": True,
            "terrain_correction": True,
        },
    }


def _validate_imported_insar(data):
    if not isinstance(data, dict):
        raise ValueError("El resultado InSAR debe ser un objeto JSON.")
    # No exigimos desplazamiento si el procesador declara que falló, pero sí trazabilidad.
    required = ["period_start", "period_end", "processor"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(f"Faltan campos de trazabilidad InSAR: {', '.join(missing)}")
    out = {
        "available": True,
        "status": str(data.get("status") or "RESULTADO_IMPORTADO"),
        "product": str(data.get("product") or "Sentinel-1 SLC InSAR"),
        "processor": str(data.get("processor")),
        "period_start": str(data.get("period_start")),
        "period_end": str(data.get("period_end")),
        "orbit_pass": data.get("orbit_pass"),
        "relative_orbit": data.get("relative_orbit"),
        "coherence_mean": _safe_num(data.get("coherence_mean")),
        "los_displacement_mm_mean": _safe_num(data.get("los_displacement_mm_mean")),
        "los_displacement_mm_p95": _safe_num(data.get("los_displacement_mm_p95")),
        "valid_fraction": _safe_num(data.get("valid_fraction")),
        "source_uri": data.get("source_uri"),
        "dem_product": data.get("dem_product"),
        "dem_resolution_m": _safe_num(data.get("dem_resolution_m")),
        "dem_date": data.get("dem_date"),
        "interpretation_scope": (
            "Desplazamiento en línea de vista derivado de fase SLC. No equivale por sí solo "
            "a inestabilidad geotécnica, velocidad 3D ni causalidad."
        ),
        "limitations": list(data.get("limitations") or []),
    }
    return out


def _insar_default_status():
    return {
        "available": False,
        "status": "NO_EJECUTADO",
        "product": "Sentinel-1 SLC InSAR",
        "interpretation_scope": (
            "El InSAR real requiere datos SLC con fase, co-registro, interferograma, coherencia, "
            "desenvolvimiento de fase y geocodificación. BioCore no sustituye ese flujo con GRD."
        ),
    }


def _geotech_default_status():
    return {
        "available": False,
        "status": "NO_EJECUTADO",
        "interpretation_scope": (
            "La cobertura geotécnica es una simulación geométrica de línea de vista; "
            "no representa SNR real ni certifica estabilidad."
        ),
    }


def _advanced_context_for_project(project_data):
    name = str(project_data.get("Proyecto") or "N/D")
    return {
        "insar": deepcopy(st.session_state.get(_insar_session_key(name)) or _insar_default_status()),
        "geotechnical": deepcopy(st.session_state.get(_geotech_session_key(name)) or _geotech_default_status()),
    }


def render_insar_workflow(project_data, coords, key_prefix="insar"):
    name = str(project_data.get("Proyecto") or "N/D")
    st.markdown("#### 🛰️ InSAR: deformación con Sentinel-1 SLC")
    st.caption(
        "Este módulo no convierte Sentinel-1 GRD en InSAR. Prepara/importa un flujo SLC trazable "
        "para desplazamiento en línea de vista y coherencia."
    )

    today = datetime.now().date()
    c1, c2, c3 = st.columns(3)
    with c1:
        start_date = st.date_input("Inicio", value=today - timedelta(days=24), key=f"{key_prefix}_start")
    with c2:
        end_date = st.date_input("Fin", value=today, key=f"{key_prefix}_end")
    with c3:
        orbit = st.selectbox("Pasada", ["AUTO", "ASCENDING", "DESCENDING"], key=f"{key_prefix}_orbit")

    payload = build_insar_request(project_data, coords, start_date, end_date, orbit)
    st.download_button(
        "⬇️ Descargar solicitud InSAR JSON",
        data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name=f"BioCore_InSAR_{name.replace(' ','_')}.json",
        mime="application/json",
        key=f"{key_prefix}_download",
        use_container_width=True,
    )

    backend = st.secrets.get("insar_backend", {})
    backend_url = str(backend.get("url") or "").strip() if backend else ""
    backend_enabled = bool(backend.get("enabled", False)) if backend else False
    if backend_enabled and backend_url:
        if st.button("🚀 Enviar trabajo al backend InSAR", key=f"{key_prefix}_submit", use_container_width=True):
            headers = {"Content-Type": "application/json"}
            token = str(backend.get("token") or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = requests.post(backend_url.rstrip("/") + "/jobs", json=payload, headers=headers, timeout=30)
                response.raise_for_status()
                data = response.json()
                st.session_state[_insar_session_key(name)] = {
                    "available": False,
                    "status": str(data.get("status") or "SUBMITTED"),
                    "job_id": data.get("job_id"),
                    "product": "Sentinel-1 SLC InSAR",
                    "period_start": str(start_date),
                    "period_end": str(end_date),
                    "orbit_pass": orbit,
                    "interpretation_scope": _insar_default_status()["interpretation_scope"],
                }
                st.success(f"Trabajo InSAR enviado. Job: {data.get('job_id') or 'N/D'}")
            except Exception as exc:
                st.error(f"No fue posible enviar el trabajo InSAR: {exc}")
    else:
        st.info(
            "Backend InSAR SLC aún no configurado. La app ya deja preparado el contrato de trabajo "
            "y permite importar un resultado procesado externamente sin inventar deformación."
        )

    uploaded = st.file_uploader(
        "Importar resultado InSAR JSON",
        type=["json"],
        key=f"{key_prefix}_upload",
        help="Use un resultado proveniente de un procesador SLC real y trazable.",
    )
    if uploaded is not None:
        try:
            imported = json.loads(uploaded.getvalue().decode("utf-8"))
            validated = _validate_imported_insar(imported)
            st.session_state[_insar_session_key(name)] = validated
            st.success("Resultado InSAR importado y asociado al proyecto.")
        except Exception as exc:
            st.error(f"Resultado InSAR inválido: {exc}")

    status = st.session_state.get(_insar_session_key(name)) or _insar_default_status()
    st.caption(f"Estado InSAR del proyecto: {status.get('status')}")
    if status.get("available"):
        m1, m2, m3 = st.columns(3)
        m1.metric("Coherencia media", _fmt(status.get("coherence_mean"), 3))
        m2.metric("Desplazamiento LOS medio", _fmt(status.get("los_displacement_mm_mean"), 2, " mm"))
        m3.metric("P95 |LOS|", _fmt(status.get("los_displacement_mm_p95"), 2, " mm"))


def render_geotechnical_workflow(project_data, coords, key_prefix="geo"):
    name = str(project_data.get("Proyecto") or "N/D")
    st.markdown("#### 📡 Planificación de radar geotécnico")
    st.caption(
        "Simulación BioCore desde cero: DEM + línea de vista + sensibilidad geométrica. "
        "No se copia código de GeotRadarSim y no se presenta como SNR real."
    )
    if not GEE_OK:
        st.warning("Earth Engine no está disponible para cargar el DEM.")
        return

    try:
        with st.spinner("Cargando DEM Copernicus GLO-30 para la simulación..."):
            dem_info = obtener_dem_grid_cached(json.dumps(limpiar_coordenadas(coords)), max_cells=48)
    except Exception as exc:
        st.error(f"No fue posible preparar el DEM geotécnico: {exc}")
        return

    grid = np.asarray(dem_info["dem"], dtype=float)
    rows, cols = grid.shape
    c1, c2, c3 = st.columns(3)
    with c1:
        x_pct = st.slider("Posición radar Este (%)", 0, 100, 15, key=f"{key_prefix}_x")
        y_pct = st.slider("Posición radar Norte (%)", 0, 100, 50, key=f"{key_prefix}_y")
    with c2:
        radar_height = st.number_input("Altura radar sobre terreno (m)", 1.0, 20.0, 3.0, 0.5, key=f"{key_prefix}_h")
        max_range = st.number_input("Alcance geométrico máximo (m)", 100.0, 10000.0, 2500.0, 100.0, key=f"{key_prefix}_range")
    with c3:
        az_center = st.slider("Azimut central (°)", -180, 180, 0, key=f"{key_prefix}_az")
        az_width = st.slider("Apertura horizontal (°)", 10, 360, 120, key=f"{key_prefix}_width")

    rr = int(round((100 - y_pct) / 100 * (rows - 1)))
    rc = int(round(x_pct / 100 * (cols - 1)))

    if st.button("📡 Calcular cobertura geométrica", key=f"{key_prefix}_run", use_container_width=True):
        result = simular_cobertura_radar_geotecnico(
            grid,
            dem_info["dx_m"],
            dem_info["dy_m"],
            rr, rc,
            radar_height,
            max_range,
            az_center,
            az_width,
        )
        b = dem_info["bbox"]
        lon = b["west"] + (rc / max(1, cols-1)) * (b["east"] - b["west"])
        lat = b["north"] - (rr / max(1, rows-1)) * (b["north"] - b["south"])
        summary = {
            "available": True,
            "status": "SIMULACION_GEOMETRICA_COMPLETADA",
            "dem_source": dem_info["source"],
            "grid_resolution_m": round(float(max(dem_info["dx_m"], dem_info["dy_m"])), 1),
            "coverage_pct": round(result["coverage_pct"], 2),
            "visible_area_m2": round(result["visible_area_m2"], 1),
            "analyzable_area_m2": round(result["analyzable_area_m2"], 1),
            "shadow_zone_count": result["shadow_zone_count"],
            "largest_shadow_zone_cells": result["largest_shadow_zone_cells"],
            "geometric_quality_mean": None if result["geometric_quality_mean"] is None else round(result["geometric_quality_mean"], 4),
            "radar_position": {"lon": round(lon, 7), "lat": round(lat, 7), "height_agl_m": float(radar_height)},
            "configuration": {"max_range_m": float(max_range), "azimuth_center_deg": int(az_center), "azimuth_width_deg": int(az_width)},
            "interpretation_scope": (
                "Planificación geométrica de visibilidad sobre DEM. No simula SNR real, deformación, "
                "probabilidad de falla ni estabilidad geotécnica."
            ),
        }
        st.session_state[_geotech_session_key(name)] = summary
        st.session_state[f"{_geotech_session_key(name)}::plot"] = result
        st.success("Cobertura geométrica calculada y asociada al proyecto.")

    summary = st.session_state.get(_geotech_session_key(name))
    plot_result = st.session_state.get(f"{_geotech_session_key(name)}::plot")
    if summary and summary.get("available"):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Cobertura visible", f"{summary.get('coverage_pct',0):.1f}%")
        m2.metric("Área visible", f"{summary.get('visible_area_m2',0)/1_000_000:.2f} km²")
        m3.metric("Zonas de sombra", str(summary.get("shadow_zone_count", 0)))
        m4.metric("Sensibilidad geom. media", _fmt(summary.get("geometric_quality_mean"), 3))
        if plot_result:
            st.plotly_chart(_geotech_plot(dem_info, plot_result), use_container_width=True, config={"displaylogo": False})
        st.caption(summary.get("interpretation_scope"))


def render_advanced_geospatial_tools(project_data, coords, key_prefix="advanced"):
    ptype = str(project_data.get("Tipo") or "GENERAL").upper()
    with st.expander("🗻 Topografía, InSAR y monitoreo 3D", expanded=False):
        render_insar_workflow(project_data, coords, key_prefix=f"{key_prefix}_insar")
        if ptype == "MINERIA":
            st.divider()
            render_geotechnical_workflow(project_data, coords, key_prefix=f"{key_prefix}_geotech")
        else:
            st.caption("La simulación de radar geotécnico se habilita automáticamente para proyectos MINERIA.")

def crear_portada_biocore():
    # -----------------------------------------------------------------------
    # PORTADA PÚBLICA — mensaje comercial breve + mapa inmediatamente visible
    # Inspiración de estructura: problema -> solución -> producto -> CTA.
    # -----------------------------------------------------------------------

    # Logo BioCore: usa el archivo real del repositorio si está disponible.
    logo_path = Path("logo_biocore.png")
    if logo_path.exists():
        c_logo, _ = st.columns([1, 4])
        with c_logo:
            st.image(str(logo_path), width=82)

    st.html("""
    <style>
    .bc-home{
        max-width:1050px;
        margin:0 auto;
        padding:0 0 28px 0;
    }
    .bc-hero{
        padding:18px 2px 12px 2px;
    }
    .bc-brand{
        color:#7dd3fc;
        font-size:.73rem;
        font-weight:800;
        letter-spacing:.14em;
        text-transform:uppercase;
        margin-bottom:11px;
    }
    .bc-hero h1{
        margin:0;
        max-width:900px;
        color:#f8fafc;
        font-size:clamp(2.15rem,7vw,4.25rem);
        line-height:1.01;
        letter-spacing:-.048em;
    }
    .bc-lead{
        max-width:780px;
        margin:17px 0 0 0;
        color:#cbd5e1;
        font-size:clamp(1rem,2vw,1.12rem);
        line-height:1.58;
    }
    .bc-lead strong{color:#fff;}
    .bc-solution{
        max-width:780px;
        color:#94a3b8;
        font-size:.91rem;
        line-height:1.58;
        margin:10px 0 0 0;
    }
    .bc-actions{
        display:flex;
        flex-wrap:wrap;
        gap:10px;
        margin-top:17px;
    }
    .bc-btn{
        display:inline-block;
        padding:10px 16px;
        border-radius:10px;
        text-decoration:none !important;
        font-size:.84rem;
        font-weight:750;
    }
    .bc-btn-primary{
        background:#38bdf8;
        color:#07111d !important;
    }
    .bc-btn-secondary{
        border:1px solid rgba(148,163,184,.28);
        color:#e2e8f0 !important;
        background:rgba(15,23,42,.42);
    }
    .bc-map-title{
        margin:20px 0 7px 0;
        display:flex;
        align-items:end;
        justify-content:space-between;
        gap:12px;
    }
    .bc-map-title b{
        color:#f8fafc;
        font-size:1.05rem;
    }
    .bc-map-title span{
        color:#64748b;
        font-size:.72rem;
    }
    .bc-mini-grid{
        display:grid;
        grid-template-columns:repeat(3,minmax(0,1fr));
        gap:10px;
        margin-top:18px;
    }
    .bc-mini{
        border-top:1px solid rgba(148,163,184,.18);
        padding:14px 3px 3px 3px;
    }
    .bc-mini b{
        display:block;
        color:#f1f5f9;
        margin-bottom:5px;
        font-size:.91rem;
    }
    .bc-mini span{
        color:#94a3b8;
        font-size:.79rem;
        line-height:1.47;
    }
    .bc-line{
        margin:22px 0 0 0;
        padding:15px 17px;
        border-radius:13px;
        background:rgba(14,165,233,.055);
        border:1px solid rgba(56,189,248,.14);
        color:#cbd5e1;
        font-size:.83rem;
        line-height:1.55;
    }
    .bc-line b{color:#f8fafc;}
    .bc-for{
        margin-top:21px;
        color:#94a3b8;
        font-size:.78rem;
    }
    .bc-tags{
        display:flex;
        flex-wrap:wrap;
        gap:7px;
        margin-top:8px;
    }
    .bc-tag{
        border:1px solid rgba(148,163,184,.14);
        border-radius:999px;
        padding:6px 9px;
        color:#cbd5e1;
        font-size:.72rem;
    }
    @media(max-width:700px){
        .bc-hero{padding-top:8px;}
        .bc-mini-grid{grid-template-columns:1fr;}
        .bc-map-title{align-items:start;flex-direction:column;}
        .bc-hero h1{font-size:2.25rem;}
    }
    </style>

    <div class="bc-home">
      <section class="bc-hero">
        <div class="bc-brand">BioCore Intelligence</div>

        <h1>Detecta cambios ambientales antes de que sea demasiado tarde.</h1>

        <p class="bc-lead">
          <strong>Una desviación descubierta tarde deja menos tiempo para actuar.</strong>
          BioCore Intelligence vigila tu territorio para que puedas verificar, corregir y documentar
          antes de que el problema escale.
        </p>

        <p class="bc-solution">
          Vigilancia satelital, series temporales, radar, terreno 3D y reportes
          trazables en una sola plataforma.
        </p>

        <div class="bc-actions">
          <a class="bc-btn bc-btn-primary"
             href="mailto:consultorabiocore@gmail.com?subject=Solicitar%20demo%20BioCore%20Intelligence">
             Solicitar una demo
          </a>
          <a class="bc-btn bc-btn-secondary" href="#biocore-map">
             Ver la plataforma
          </a>
        </div>
      </section>

      <div id="biocore-map" class="bc-map-title">
        <b>Observa el territorio</b>
        <span>El visor de clientes incorpora además DEM 3D, capas e InSAR.</span>
      </div>
    </div>
    """)

    # -----------------------------------------------------------------------
    # MAPA PÚBLICO REAL — arriba, responsive y sin datos inventados.
    # Muestra contexto territorial de Chile; el AOI real aparece al iniciar sesión.
    # -----------------------------------------------------------------------
    mapa_publico = folium.Map(
        location=[-33.2, -71.0],
        zoom_start=4,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
    )

    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap contributors",
        name="Mapa",
        control=True,
    ).add_to(mapa_publico)

    folium.TileLayer(
        tiles=(
            "https://server.arcgisonline.com/ArcGIS/rest/services/"
            "World_Imagery/MapServer/tile/{z}/{y}/{x}"
        ),
        attr="Esri, Maxar, Earthstar Geographics",
        name="Satélite",
        control=True,
    ).add_to(mapa_publico)

    folium.LayerControl(position="topright", collapsed=True).add_to(mapa_publico)

    # Render responsive dentro de Streamlit. No se agregan marcadores ficticios.
    map_html = mapa_publico.get_root().render()
    st.components.v1.html(
        map_html,
        height=390,
        scrolling=False,
    )

    st.html("""
    <div class="bc-home">
      <div class="bc-mini-grid">
        <div class="bc-mini">
          <b>Anticipa</b>
          <span>Detecta señales y cambios antes de depender únicamente de una inspección tardía.</span>
        </div>
        <div class="bc-mini">
          <b>Comprende</b>
          <span>Ve qué cambió, qué evidencia lo respalda y qué limitaciones tiene el análisis.</span>
        </div>
        <div class="bc-mini">
          <b>Documenta</b>
          <span>Genera historial, alertas e informes vinculados a un análisis verificable.</span>
        </div>
      </div>

      <div class="bc-line">
        <b>El objetivo no es prometer “cero multas”.</b>
        Es darte más tiempo y mejor evidencia para reducir incertidumbre y exposición
        a observaciones, incumplimientos, costos de corrección y sanciones evitables.
      </div>

      <div class="bc-for">Diseñado para</div>
      <div class="bc-tags">
        <span class="bc-tag">⛏️ Minería</span>
        <span class="bc-tag">🌿 Gestión ambiental</span>
        <span class="bc-tag">🏗️ Infraestructura</span>
        <span class="bc-tag">💧 Recursos hídricos</span>
        <span class="bc-tag">🌲 Ecosistemas</span>
        <span class="bc-tag">🗺️ Territorio</span>
      </div>
    </div>
    """)


# ============================================================================
# INTEGRADO DESDE biocore_metodologia_espectral_v2.py
# ============================================================================
S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"

L8_COLLECTION = "LANDSAT/LC08/C02/T1_L2"

MODIS_LST_COLLECTION = "MODIS/061/MOD11A1"

def _safe_get_number(stats: Dict[str, Any], key: str) -> Optional[float]:
    value = stats.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _same_month_day(date_ref: datetime, year: int) -> datetime:
    """Cambia el año manteniendo mes/día; corrige 29-feb si corresponde."""
    try:
        return date_ref.replace(year=int(year))
    except ValueError:
        # 29 de febrero en un año no bisiesto
        return date_ref.replace(year=int(year), day=28)

def _change(actual: Optional[float], base: Optional[float]) -> Dict[str, Optional[float]]:
    """
    Entrega diferencia absoluta y, sólo cuando es numéricamente razonable,
    cambio relativo porcentual.

    Para índices normalizados cercanos a 0, el % puede explotar y ser engañoso.
    Por eso se conserva delta como métrica principal.
    """
    if actual is None or base is None:
        return {"delta": None, "pct": None}

    delta = actual - base

    # Evitar porcentajes absurdos cuando la línea base está cerca de cero.
    if abs(base) < 0.10:
        pct = None
    else:
        pct = (delta / abs(base)) * 100.0

    return {"delta": delta, "pct": pct}

def _collection_size(collection: ee.ImageCollection) -> int:
    try:
        return int(collection.size().getInfo() or 0)
    except Exception:
        return 0

def mask_scale_sentinel2(image: ee.Image) -> ee.Image:
    """
    Sentinel-2 L2A/SR:
    - conserva nieve/hielo y agua porque son objetos de interés;
    - elimina píxeles saturados, sombra de nube y clases de nube/cirrus;
    - convierte SR escalada por 10000 a reflectancia 0–1.
    """
    scl = image.select("SCL")

    invalid = (
        scl.eq(1)   # saturado/defectuoso
        .Or(scl.eq(3))   # sombra de nube
        .Or(scl.eq(7))   # no clasificado / nube baja prob.
        .Or(scl.eq(8))   # nube prob. media
        .Or(scl.eq(9))   # nube prob. alta
        .Or(scl.eq(10))  # cirrus
    )
    clear_mask = invalid.Not()

    sr = (
        image.select(["B2", "B3", "B4", "B8", "B11", "B12"])
        .multiply(0.0001)
        .updateMask(clear_mask)
    )

    return sr.copyProperties(
        image,
        ["system:time_start", "CLOUDY_PIXEL_PERCENTAGE", "PRODUCT_ID"],
    )

def mask_scale_landsat8(image: ee.Image) -> ee.Image:
    """
    Landsat 8 Collection 2 Level 2 Surface Reflectance.
    Se enmascaran nubes/sombras, pero se conserva nieve para poder analizarla.
    """
    qa = image.select("QA_PIXEL")

    # QA_PIXEL C2:
    # bit 1: dilated cloud
    # bit 2: cirrus
    # bit 3: cloud
    # bit 4: cloud shadow
    mask = (
        qa.bitwiseAnd(1 << 1).eq(0)
        .And(qa.bitwiseAnd(1 << 2).eq(0))
        .And(qa.bitwiseAnd(1 << 3).eq(0))
        .And(qa.bitwiseAnd(1 << 4).eq(0))
    )

    sr = (
        image.select(
            ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"],
            ["B2", "B3", "B4", "B8", "B11", "B12"],
        )
        .multiply(0.0000275)
        .add(-0.2)
        .updateMask(mask)
    )

    return sr.copyProperties(
        image,
        ["system:time_start", "CLOUD_COVER", "LANDSAT_PRODUCT_ID"],
    )

def _normalized_difference(a: ee.Image, b: ee.Image, name: str) -> ee.Image:
    denom = a.add(b)
    valid = denom.abs().gt(1e-6)
    return a.subtract(b).divide(denom).updateMask(valid).rename(name)

def add_spectral_indices(reflectance: ee.Image) -> ee.Image:
    """
    Fórmulas sobre reflectancia superficial escalada correctamente.

    NDVI  = (NIR - RED) / (NIR + RED)
    SAVI  = 1.5 * (NIR - RED) / (NIR + RED + 0.5)
    NDWI  = (GREEN - NIR) / (GREEN + NIR)        [agua superficial, McFeeters]
    NDMI  = (NIR - SWIR1) / (NIR + SWIR1)        [humedad]
    NDSI  = (GREEN - SWIR1) / (GREEN + SWIR1)    [nieve/hielo, contextual]
    SWIR1 = reflectancia de SWIR1
    SWIR_RATIO = SWIR1 / SWIR2                    [razón espectral; NO prueba arcillas]
    """
    green = reflectance.select("B3")
    red = reflectance.select("B4")
    nir = reflectance.select("B8")
    swir1 = reflectance.select("B11")
    swir2 = reflectance.select("B12")

    ndvi = _normalized_difference(nir, red, "ndvi")

    savi = (
        nir.subtract(red)
        .multiply(1.5)
        .divide(nir.add(red).add(0.5))
        .rename("savi")
    )

    ndwi = _normalized_difference(green, nir, "ndwi")
    ndmi = _normalized_difference(nir, swir1, "ndmi")
    ndsi = _normalized_difference(green, swir1, "ndsi")

    swir_ratio = (
        swir1.divide(swir2)
        .updateMask(swir2.abs().gt(1e-6))
        .rename("swir_ratio")
    )

    return reflectance.addBands(
        [
            savi,
            ndvi,
            ndwi,
            ndmi,
            ndsi,
            swir1.rename("swir1"),
            swir_ratio,
        ]
    )

INDEX_NAMES = ["savi", "ndvi", "ndwi", "ndmi", "ndsi", "swir1", "swir_ratio"]

def reduce_indices(
    indexed_image: ee.Image,
    geom: ee.Geometry,
    scale: int,
) -> Dict[str, Any]:
    reducer = ee.Reducer.mean().combine(
        reducer2=ee.Reducer.stdDev(),
        sharedInputs=True,
    )

    stats = (
        indexed_image.select(INDEX_NAMES)
        .reduceRegion(
            reducer=reducer,
            geometry=geom,
            scale=scale,
            maxPixels=1e9,
            bestEffort=True,
        )
        .getInfo()
    )

    result: Dict[str, Any] = {}
    for name in INDEX_NAMES:
        result[name] = _safe_get_number(stats, f"{name}_mean")
        result[f"{name}_sd"] = _safe_get_number(stats, f"{name}_stdDev")

    try:
        valid_fraction = (
            indexed_image.select("ndvi")
            .mask()
            .reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=geom,
                scale=scale,
                maxPixels=1e9,
                bestEffort=True,
            )
            .get("ndvi")
            .getInfo()
        )
        result["valid_fraction"] = (
            float(valid_fraction) if valid_fraction is not None else None
        )
    except Exception:
        result["valid_fraction"] = None

    return result

def sentinel2_scene_context(
    raw_image: ee.Image,
    geom: ee.Geometry,
) -> Dict[str, Optional[float]]:
    """
    SCL se usa sólo como contexto de clasificación de escena,
    no como cartografía temática definitiva.
    """
    scl = raw_image.select("SCL")

    classes = {
        "scl_vegetation_fraction": 4,
        "scl_bare_fraction": 5,
        "scl_water_fraction": 6,
        "scl_snow_ice_fraction": 11,
    }

    out: Dict[str, Optional[float]] = {}
    for key, cls in classes.items():
        try:
            value = (
                scl.eq(cls)
                .reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=geom,
                    scale=20,
                    maxPixels=1e9,
                    bestEffort=True,
                )
                .get("SCL")
                .getInfo()
            )
            out[key] = float(value) if value is not None else None
        except Exception:
            out[key] = None

    return out

def _s2_collection(
    geom: ee.Geometry,
    start: datetime,
    end: datetime,
) -> ee.ImageCollection:
    return (
        ee.ImageCollection(S2_COLLECTION)
        .filterBounds(geom)
        .filterDate(start.strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
    )

def _l8_collection(
    geom: ee.Geometry,
    start: datetime,
    end: datetime,
) -> ee.ImageCollection:
    return (
        ee.ImageCollection(L8_COLLECTION)
        .filterBounds(geom)
        .filterDate(start.strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d"))
        .filter(ee.Filter.lt("CLOUD_COVER", 70))
    )

def _find_current_window(
    geom: ee.Geometry,
    requested_days: int,
    sensor: str,
) -> Tuple[Optional[ee.ImageCollection], Optional[datetime], Optional[datetime], int]:
    now = datetime.now(timezone.utc)
    attempts = []
    for d in [max(1, int(requested_days)), 30, 60, 90]:
        if d not in attempts:
            attempts.append(d)

    for days in attempts:
        start = now - timedelta(days=days)
        end = now
        col = _s2_collection(geom, start, end) if sensor == "S2" else _l8_collection(geom, start, end)
        if _collection_size(col) > 0:
            return col, start, end, days

    return None, None, None, 0

def _find_baseline_window(
    geom: ee.Geometry,
    baseline_year: int,
    current_end: datetime,
    initial_days: int,
    sensor: str,
) -> Tuple[Optional[ee.ImageCollection], Optional[datetime], Optional[datetime], int]:
    base_end = _same_month_day(current_end, int(baseline_year))

    attempts = []
    for d in [max(30, int(initial_days)), 60, 90, 120]:
        if d not in attempts:
            attempts.append(d)

    for days in attempts:
        base_start = base_end - timedelta(days=days)
        col = _s2_collection(geom, base_start, base_end) if sensor == "S2" else _l8_collection(geom, base_start, base_end)
        if _collection_size(col) > 0:
            return col, base_start, base_end, days

    return None, None, None, 0

def _composite_stats(
    collection: ee.ImageCollection,
    geom: ee.Geometry,
    sensor: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if sensor == "S2":
        prepared = collection.map(mask_scale_sentinel2)
        scale = 20
    else:
        prepared = collection.map(mask_scale_landsat8)
        scale = 30

    indexed = prepared.map(add_spectral_indices)
    composite = indexed.median()
    stats = reduce_indices(composite, geom, scale=scale)

    latest = collection.sort("system:time_start", False).first()
    ts = latest.get("system:time_start").getInfo()
    latest_date = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        if ts
        else None
    )

    meta = {
        "sensor": "Sentinel-2 MSI L2A" if sensor == "S2" else "Landsat 8 OLI C2 L2",
        "collection": S2_COLLECTION if sensor == "S2" else L8_COLLECTION,
        "scale_m": scale,
        "n_scenes": _collection_size(collection),
        "latest_scene_date": latest_date,
    }

    return stats, meta

def get_latest_s2_snapshot(
    geom: ee.Geometry,
    requested_days: int = 30,
) -> Optional[Dict[str, Any]]:
    col, start, end, used_days = _find_current_window(
        geom, requested_days=requested_days, sensor="S2"
    )
    if col is None:
        return None

    stats, meta = _composite_stats(col, geom, "S2")

    # Contexto SCL de la escena más reciente; no sustituye validación de terreno.
    latest_raw = col.sort("system:time_start", False).first()
    context = sentinel2_scene_context(latest_raw, geom)

    return {
        "stats": stats,
        "meta": {
            **meta,
            "window_start": start.strftime("%Y-%m-%d"),
            "window_end": end.strftime("%Y-%m-%d"),
            "window_days": used_days,
            "mask": "SCL: excluye saturados, sombra de nube, clases 7-10; conserva agua y nieve/hielo",
        },
        "context": context,
    }

def get_temporal_comparison(
    geom: ee.Geometry,
    baseline_year: int,
    requested_days: int = 30,
) -> Dict[str, Any]:
    """
    Si la línea base es anterior a la disponibilidad robusta de S2 SR,
    usa Landsat 8 TANTO para la línea base COMO para el período actual.

    Así se evita comparar directamente un índice Sentinel-2 con otro Landsat
    y atribuir al ambiente diferencias que pueden ser instrumentales.
    """
    baseline_year = int(baseline_year)

    preferred_sensor = "S2" if baseline_year >= 2017 else "L8"

    current_col, current_start, current_end, current_days = _find_current_window(
        geom, requested_days=requested_days, sensor=preferred_sensor
    )

    if current_col is None:
        return {
            "available": False,
            "reason": f"Sin escenas válidas actuales para {preferred_sensor}.",
        }

    base_col, base_start, base_end, base_days = _find_baseline_window(
        geom=geom,
        baseline_year=baseline_year,
        current_end=current_end,
        initial_days=current_days,
        sensor=preferred_sensor,
    )

    # Para líneas base S2 sin cobertura disponible, retroceder a L8 para AMBOS periodos.
    if base_col is None and preferred_sensor == "S2":
        preferred_sensor = "L8"
        current_col, current_start, current_end, current_days = _find_current_window(
            geom, requested_days=requested_days, sensor="L8"
        )
        if current_col is not None:
            base_col, base_start, base_end, base_days = _find_baseline_window(
                geom=geom,
                baseline_year=baseline_year,
                current_end=current_end,
                initial_days=current_days,
                sensor="L8",
            )

    if current_col is None or base_col is None:
        return {
            "available": False,
            "reason": (
                "No fue posible construir una comparación temporal con el mismo sensor "
                "para el período actual y la línea base."
            ),
        }

    current_stats, current_meta = _composite_stats(
        current_col, geom, preferred_sensor
    )
    base_stats, base_meta = _composite_stats(
        base_col, geom, preferred_sensor
    )

    changes: Dict[str, Any] = {}
    for name in INDEX_NAMES:
        changes[name] = _change(current_stats.get(name), base_stats.get(name))

    return {
        "available": True,
        "sensor_code": preferred_sensor,
        "current": current_stats,
        "baseline": base_stats,
        "changes": changes,
        "meta": {
            "sensor": current_meta["sensor"],
            "collection": current_meta["collection"],
            "scale_m": current_meta["scale_m"],
            "current_n_scenes": current_meta["n_scenes"],
            "baseline_n_scenes": base_meta["n_scenes"],
            "current_latest_scene_date": current_meta["latest_scene_date"],
            "baseline_latest_scene_date": base_meta["latest_scene_date"],
            "current_window_start": current_start.strftime("%Y-%m-%d"),
            "current_window_end": current_end.strftime("%Y-%m-%d"),
            "baseline_window_start": base_start.strftime("%Y-%m-%d"),
            "baseline_window_end": base_end.strftime("%Y-%m-%d"),
            "current_window_days": current_days,
            "baseline_window_days": base_days,
            "comparison_rule": "mismo sensor + misma familia de procesamiento + ventana estacional equivalente",
        },
    }

def get_modis_lst(
    geom: ee.Geometry,
    requested_days: int = 30,
) -> Optional[Dict[str, Any]]:
    now = datetime.now(timezone.utc)

    for days in [max(1, int(requested_days)), 30, 60]:
        start = now - timedelta(days=days)

        col = (
            ee.ImageCollection(MODIS_LST_COLLECTION)
            .filterBounds(geom)
            .filterDate(
                start.strftime("%Y-%m-%d"),
                (now + timedelta(days=1)).strftime("%Y-%m-%d"),
            )
        )

        if _collection_size(col) == 0:
            continue

        def mask_lst(image: ee.Image) -> ee.Image:
            qc = image.select("QC_Day")
            mandatory_qa = qc.bitwiseAnd(3)
            lst_error = qc.rightShift(6).bitwiseAnd(3)

            # QA obligatorio bueno y error estimado <= 2 K.
            mask = mandatory_qa.eq(0).And(lst_error.lte(1))

            return (
                image.select("LST_Day_1km")
                .updateMask(mask)
                .multiply(0.02)
                .subtract(273.15)
                .rename("lst_c")
                .copyProperties(image, ["system:time_start"])
            )

        lst_col = col.map(mask_lst)
        composite = lst_col.median()

        stats = (
            composite.reduceRegion(
                reducer=ee.Reducer.mean().combine(
                    reducer2=ee.Reducer.stdDev(),
                    sharedInputs=True,
                ),
                geometry=geom,
                scale=1000,
                maxPixels=1e9,
                bestEffort=True,
            )
            .getInfo()
        )

        mean = _safe_get_number(stats, "lst_c_mean")
        sd = _safe_get_number(stats, "lst_c_stdDev")

        if mean is not None:
            return {
                "mean_c": mean,
                "sd_c": sd,
                "window_start": start.strftime("%Y-%m-%d"),
                "window_end": now.strftime("%Y-%m-%d"),
                "window_days": days,
                "n_scenes": _collection_size(col),
                "collection": MODIS_LST_COLLECTION,
                "scale_m": 1000,
                "qa": "QC_Day obligatorio=bueno; error LST estimado <=2 K",
            }

    return None

def build_optical_analysis(
    geom: ee.Geometry,
    baseline_year: int,
    requested_days: int = 30,
) -> Dict[str, Any]:
    """
    Fuente única para PDF, interfaz y Telegram.

    No evalúa significancia ambiental ni cumplimiento legal.
    Sólo produce observaciones, comparación temporal y trazabilidad.
    """
    snapshot = get_latest_s2_snapshot(geom, requested_days=requested_days)

    comparison = get_temporal_comparison(
        geom,
        baseline_year=baseline_year,
        requested_days=requested_days,
    )

    lst = get_modis_lst(geom, requested_days=requested_days)

    warnings = []

    if snapshot is None:
        warnings.append("No fue posible obtener observación Sentinel-2 reciente.")

    if not comparison.get("available"):
        warnings.append(comparison.get("reason", "Comparación temporal no disponible."))

    if lst is None:
        warnings.append("No fue posible obtener LST MODIS con QA aceptable.")

    if comparison.get("available"):
        vf = comparison["current"].get("valid_fraction")
        if vf is not None and vf < 0.50:
            warnings.append(
                f"Cobertura válida baja en el compuesto comparable ({vf:.0%}); "
                "interpretar con cautela."
            )

    return {
        "method_version": "BioCore spectral-v2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_year": int(baseline_year),
        "requested_days": int(requested_days),
        "snapshot_s2": snapshot,
        "comparison": comparison,
        "lst": lst,
        "warnings": warnings,
        "interpretation_limits": (
            "Los índices espectrales constituyen evidencia de teledetección. "
            "No prueban por sí solos causalidad, degradación, cumplimiento normativo, "
            "presencia de relaves, estabilidad geotécnica, balance de masa glaciar ni "
            "condición de sumidero de carbono. Esas conclusiones requieren antecedentes "
            "adicionales y, cuando corresponda, verificación en terreno."
        ),
    }


# ============================================================================
# INTEGRADO DESDE biocore_series_temporales_v2.py
# ============================================================================
S2 = "COPERNICUS/S2_SR_HARMONIZED"

L8 = "LANDSAT/LC08/C02/T1_L2"

L7 = "LANDSAT/LE07/C02/T1_L2"

ERA5 = "ECMWF/ERA5_LAND/MONTHLY_AGGR"

@dataclass(frozen=True)
class RangeSpec:
    label: str
    mode: str
    value: int
    unit: str

RANGE_SPECS = {
    "Últimos 7 días": RangeSpec("Últimos 7 días", "scene", 7, "days"),
    "Últimas 2 semanas": RangeSpec("Últimas 2 semanas", "scene", 14, "days"),
    "Último mes": RangeSpec("Último mes", "scene", 30, "days"),
    "Últimos 3 meses": RangeSpec("Últimos 3 meses", "scene", 90, "days"),
    "Último año": RangeSpec("Último año", "monthly", 12, "months"),
    "Últimos 5 años": RangeSpec("Últimos 5 años", "annual", 5, "years"),
    "Últimos 10 años": RangeSpec("Últimos 10 años", "annual", 10, "years"),
    "Últimos 15 años": RangeSpec("Últimos 15 años", "annual", 15, "years"),
    "Últimos 20 años": RangeSpec("Últimos 20 años", "annual", 20, "years"),
}

def get_range_spec(label: str) -> RangeSpec:
    if label not in RANGE_SPECS:
        raise ValueError(f"Rango temporal no reconocido: {label}")
    return RANGE_SPECS[label]

def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _collection_size(collection: ee.ImageCollection) -> int:
    try:
        return int(collection.size().getInfo() or 0)
    except Exception:
        return 0

def _reduce_indexed_image(
    indexed: ee.Image,
    geom: ee.Geometry,
    scale: int,
) -> Dict[str, Optional[float]]:
    names = ["savi", "ndvi", "ndwi", "ndmi", "ndsi", "swir1", "swir_ratio"]
    raw = (
        indexed.select(names)
        .reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=geom,
            scale=scale,
            maxPixels=1e9,
            bestEffort=True,
        )
        .getInfo()
    )
    return {name: _safe_float(raw.get(name)) for name in names}

def get_s2_scene_series(
    geom: ee.Geometry,
    days: int,
) -> List[Dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days))

    raw_col = (
        ee.ImageCollection(S2)
        .filterBounds(geom)
        .filterDate(
            start.strftime("%Y-%m-%d"),
            (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .sort("system:time_start")
    )

    count = _collection_size(raw_col)
    if count == 0:
        return []

    images = raw_col.toList(count)
    records: List[Dict[str, Any]] = []

    for i in range(count):
        try:
            raw = ee.Image(images.get(i))
            prepared = mask_scale_sentinel2(raw)
            indexed = add_spectral_indices(prepared)
            stats = _reduce_indexed_image(indexed, geom, scale=20)

            ts = raw.get("system:time_start").getInfo()
            date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

            cloud_meta = _safe_float(raw.get("CLOUDY_PIXEL_PERCENTAGE").getInfo())

            # No registrar escenas que quedaron sin ningún índice válido.
            if not any(v is not None for v in stats.values()):
                continue

            records.append(
                {
                    "date": date.date().isoformat(),
                    "datetime_utc": date.isoformat(),
                    "sensor": "Sentinel-2 MSI L2A",
                    "collection": S2,
                    "scene_cloud_pct_metadata": cloud_meta,
                    **stats,
                }
            )
        except Exception:
            continue

    return records

def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=timezone.utc)

def _next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(dt.year, dt.month + 1, 1, tzinfo=timezone.utc)

def get_s2_monthly_series(
    geom: ee.Geometry,
    months: int = 12,
) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    starts: List[datetime] = []

    cursor = _month_start(now.year, now.month)
    for _ in range(int(months)):
        starts.append(cursor)
        prev_month_end = cursor - timedelta(days=1)
        cursor = _month_start(prev_month_end.year, prev_month_end.month)

    starts.reverse()
    records: List[Dict[str, Any]] = []

    for start in starts:
        end = _next_month(start)
        raw_col = (
            ee.ImageCollection(S2)
            .filterBounds(geom)
            .filterDate(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        )

        n = _collection_size(raw_col)
        if n == 0:
            continue

        try:
            indexed_col = raw_col.map(mask_scale_sentinel2).map(add_spectral_indices)
            composite = indexed_col.median()
            stats = _reduce_indexed_image(composite, geom, scale=20)

            if not any(v is not None for v in stats.values()):
                continue

            records.append(
                {
                    "date": start.date().isoformat(),
                    "period": start.strftime("%Y-%m"),
                    "sensor": "Sentinel-2 MSI L2A",
                    "collection": S2,
                    "n_scenes": n,
                    **stats,
                }
            )
        except Exception:
            continue

    return records

def _mask_scale_landsat7(image: ee.Image) -> ee.Image:
    qa = image.select("QA_PIXEL")
    mask = (
        qa.bitwiseAnd(1 << 1).eq(0)
        .And(qa.bitwiseAnd(1 << 3).eq(0))
        .And(qa.bitwiseAnd(1 << 4).eq(0))
    )

    return (
        image.select(
            ["SR_B1", "SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B7"],
            ["B2", "B3", "B4", "B8", "B11", "B12"],
        )
        .multiply(0.0000275)
        .add(-0.2)
        .updateMask(mask)
        .copyProperties(
            image,
            ["system:time_start", "CLOUD_COVER", "LANDSAT_PRODUCT_ID"],
        )
    )

def _landsat_year_collection(
    geom: ee.Geometry,
    year: int,
):
    start = f"{year}-01-01"
    end = f"{year + 1}-01-01"

    if year >= 2013:
        collection_id = L8
        sensor_name = "Landsat 8 OLI C2 L2"
        raw = (
            ee.ImageCollection(L8)
            .filterBounds(geom)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUD_COVER", 70))
        )
        prepared = raw.map(mask_scale_landsat8)
    else:
        collection_id = L7
        sensor_name = "Landsat 7 ETM+ C2 L2"
        raw = (
            ee.ImageCollection(L7)
            .filterBounds(geom)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUD_COVER", 70))
        )
        prepared = raw.map(_mask_scale_landsat7)

    return raw, prepared, sensor_name, collection_id

def get_landsat_annual_series(
    geom: ee.Geometry,
    years: int,
) -> List[Dict[str, Any]]:
    now_year = datetime.now(timezone.utc).year
    first_year = now_year - int(years) + 1
    records: List[Dict[str, Any]] = []

    for year in range(first_year, now_year + 1):
        try:
            raw, prepared, sensor_name, collection_id = _landsat_year_collection(
                geom, year
            )
            n = _collection_size(raw)
            if n == 0:
                continue

            indexed = prepared.map(add_spectral_indices)
            composite = indexed.median()
            stats = _reduce_indexed_image(composite, geom, scale=30)

            if not any(v is not None for v in stats.values()):
                continue

            records.append(
                {
                    "date": f"{year}-07-01",
                    "year": year,
                    "sensor": sensor_name,
                    "collection": collection_id,
                    "n_scenes": n,
                    **stats,
                }
            )
        except Exception:
            continue

    return records

def get_era5_annual_climate(
    geom: ee.Geometry,
    years: int,
) -> List[Dict[str, Any]]:
    """
    Entrega:
    - temperatura media anual a 2 m (°C)
    - precipitación total anual (mm)

    No inventa temperatura mínima/máxima a partir de la media.
    """
    now_year = datetime.now(timezone.utc).year
    first_year = now_year - int(years) + 1
    records: List[Dict[str, Any]] = []

    for year in range(first_year, now_year + 1):
        try:
            col = (
                ee.ImageCollection(ERA5)
                .filterBounds(geom)
                .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                .select(["temperature_2m", "total_precipitation_sum"])
            )

            n = _collection_size(col)
            if n == 0:
                continue

            temp = col.select("temperature_2m").mean()
            precip = col.select("total_precipitation_sum").sum()

            img = (
                temp.subtract(273.15)
                .rename("temperature_2m_c")
                .addBands(
                    precip.multiply(1000).rename("precipitation_mm")
                )
            )

            raw = (
                img.reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=geom,
                    scale=11132,
                    maxPixels=1e9,
                    bestEffort=True,
                )
                .getInfo()
            )

            temp_c = _safe_float(raw.get("temperature_2m_c"))
            precip_mm = _safe_float(raw.get("precipitation_mm"))

            if temp_c is None and precip_mm is None:
                continue

            records.append(
                {
                    "year": year,
                    "date": f"{year}-07-01",
                    "temperature_mean_c": temp_c,
                    "precipitation_total_mm": precip_mm,
                    "source": "ERA5-Land monthly aggregated",
                    "collection": ERA5,
                    "n_months": n,
                }
            )
        except Exception:
            continue

    return records

def build_temporal_bundle(
    geom: ee.Geometry,
    range_label: str,
) -> Dict[str, Any]:
    spec = get_range_spec(range_label)
    warnings: List[str] = []

    if spec.mode == "scene":
        spectral = get_s2_scene_series(geom, spec.value)
        climate = []
        expected_min = 2

        if len(spectral) < expected_min:
            warnings.append(
                "El período contiene menos de dos adquisiciones ópticas válidas; "
                "no corresponde inferir una tendencia temporal."
            )

    elif spec.mode == "monthly":
        spectral = get_s2_monthly_series(geom, spec.value)
        climate = []
        if len(spectral) < 6:
            warnings.append(
                "Hay menos de seis meses con datos ópticos válidos en el período."
            )

    else:
        spectral = get_landsat_annual_series(geom, spec.value)
        climate = get_era5_annual_climate(geom, spec.value)

        sensors = sorted({r.get("sensor") for r in spectral if r.get("sensor")})
        if len(sensors) > 1:
            warnings.append(
                "La serie histórica cruza generaciones Landsat. Se muestran segmentos "
                "por sensor y no debe usarse esta gráfica para atribuir automáticamente "
                "un salto entre sensores a un cambio ambiental."
            )

    return {
        "range_label": spec.label,
        "mode": spec.mode,
        "spectral_records": spectral,
        "climate_records": climate,
        "warnings": warnings,
        "rules": {
            "short_range": "cada punto = adquisición Sentinel-2 válida",
            "one_year": "cada punto = mediana mensual Sentinel-2",
            "long_range": (
                "cada punto = mediana anual Landsat; identidad del sensor preservada"
            ),
            "no_fill": "los datos faltantes no se sustituyen por ceros ni valores sintéticos",
        },
    }

METRIC_LABELS = {
    "savi": "SAVI",
    "ndvi": "NDVI",
    "ndwi": "NDWI (Green–NIR, agua superficial)",
    "ndmi": "NDMI (NIR–SWIR1, humedad)",
    "ndsi": "NDSI (nieve/hielo)",
    "swir1": "Reflectancia SWIR1",
    "swir_ratio": "Razón SWIR1/SWIR2",
}

def _spectral_dataframe(bundle: Dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(bundle.get("spectral_records", []))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).sort_values("date")

def plot_metric(
    bundle: Dict[str, Any],
    metric: str,
    output_dir: str,
) -> Optional[str]:
    """
    Genera UN gráfico por métrica.
    Para series multisensor no une automáticamente sensores distintos.
    """
    if metric not in METRIC_LABELS:
        raise ValueError(f"Métrica no soportada: {metric}")

    df = _spectral_dataframe(bundle)
    if df.empty or metric not in df.columns:
        return None

    df = df[df[metric].notna()].copy()
    if df.empty:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.8))

    if "sensor" in df.columns and df["sensor"].nunique() > 1:
        for sensor, group in df.groupby("sensor"):
            group = group.sort_values("date")
            ax.plot(group["date"], group[metric], marker="o", label=sensor)
        ax.legend()
    else:
        ax.plot(df["date"], df[metric], marker="o")

    ax.set_title(f"{METRIC_LABELS[metric]} — {bundle['range_label']}")
    ax.set_xlabel("Fecha")
    ax.set_ylabel(METRIC_LABELS[metric])
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"biocore_{metric}_{bundle['mode']}.png"
    fig.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return str(path)

def plot_climate_metric(
    bundle: Dict[str, Any],
    metric: str,
    output_dir: str,
) -> Optional[str]:
    labels = {
        "temperature_mean_c": "Temperatura media anual ERA5-Land (°C)",
        "precipitation_total_mm": "Precipitación total anual ERA5-Land (mm)",
    }
    if metric not in labels:
        raise ValueError(f"Métrica climática no soportada: {metric}")

    df = pd.DataFrame(bundle.get("climate_records", []))
    if df.empty or metric not in df.columns:
        return None

    df = df[df[metric].notna()].copy()
    if df.empty:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(df["year"], df[metric], marker="o")
    ax.set_title(f"{labels[metric]} — {bundle['range_label']}")
    ax.set_xlabel("Año")
    ax.set_ylabel(labels[metric])
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"biocore_{metric}_{bundle['mode']}.png"
    fig.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return str(path)

def generate_report_charts(
    bundle: Dict[str, Any],
    project_type: str,
    output_dir: str,
) -> List[str]:
    """
    Selección conservadora de métricas por tipo de proyecto.
    Los títulos describen señales, no "cumplimiento", "estabilidad garantizada"
    ni causalidades no demostradas.
    """
    ptype = str(project_type or "GENERAL").upper()

    if ptype == "MINERIA":
        metrics = ["ndwi", "ndmi", "ndsi", "ndvi"]
    elif ptype == "GLACIAR":
        metrics = ["ndsi", "ndwi", "ndmi", "ndvi"]
    elif ptype == "BOSQUE":
        metrics = ["savi", "ndvi", "ndmi", "ndwi"]
    elif ptype == "HUMEDAL":
        metrics = ["ndwi", "ndmi", "savi", "ndvi"]
    elif ptype == "AGRICOLA":
        metrics = ["savi", "ndvi", "ndmi", "ndwi"]
    else:
        metrics = ["savi", "ndvi", "ndwi", "ndmi"]

    paths: List[str] = []
    for metric in metrics:
        path = plot_metric(bundle, metric, output_dir)
        if path:
            paths.append(path)

    # El contexto climático anual sólo corresponde a rangos multianuales.
    if bundle.get("mode") == "annual":
        for metric in ["temperature_mean_c", "precipitation_total_mm"]:
            path = plot_climate_metric(bundle, metric, output_dir)
            if path:
                paths.append(path)

    return paths


# ============================================================================
# INTEGRADO DESDE biocore_diagnostico_v2.py
# ============================================================================
LEVEL_INSUFFICIENT = "EVIDENCIA INSUFICIENTE"

LEVEL_NO_FLAG = "SIN SEÑAL PRIORITARIA"

LEVEL_REVIEW = "SEÑAL A REVISAR"

LEVEL_PRIORITY = "VERIFICACIÓN PRIORITARIA"

def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _fmt(value, decimals=4) -> str:
    value = _num(value)
    return "N/D" if value is None else f"{value:.{decimals}f}"

def _delta_text(change: Dict[str, Any]) -> str:
    delta = _num((change or {}).get("delta"))
    pct = _num((change or {}).get("pct"))

    if delta is None:
        return "sin comparación válida"

    text = f"Δ={delta:+.4f}"
    if pct is not None:
        text += f" ({pct:+.1f}%)"
    return text

def _get_thresholds(project_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Se espera, opcionalmente:
    project_data["umbrales_monitoreo"] = {
        "ndvi": {"delta_min": -0.10},
        "ndwi": {"delta_max": 0.15},
        ...
    }

    Sólo deben cargarse umbrales que tengan fuente trazable:
    RCA, PAS, plan de seguimiento aprobado, norma, protocolo validado, etc.
    """
    raw = (project_data or {}).get("umbrales_monitoreo")
    return raw if isinstance(raw, dict) else {}

def _threshold_result(
    metric: str,
    current: Optional[float],
    base: Optional[float],
    change: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    cfg = thresholds.get(metric)
    if not isinstance(cfg, dict):
        return None

    delta = _num(change.get("delta"))
    current = _num(current)

    triggered = False
    reasons: List[str] = []

    if current is not None and cfg.get("current_min") is not None:
        limit = float(cfg["current_min"])
        if current < limit:
            triggered = True
            reasons.append(f"valor {current:.4f} < umbral {limit:.4f}")

    if current is not None and cfg.get("current_max") is not None:
        limit = float(cfg["current_max"])
        if current > limit:
            triggered = True
            reasons.append(f"valor {current:.4f} > umbral {limit:.4f}")

    if delta is not None and cfg.get("delta_min") is not None:
        limit = float(cfg["delta_min"])
        if delta < limit:
            triggered = True
            reasons.append(f"Δ {delta:+.4f} < umbral {limit:+.4f}")

    if delta is not None and cfg.get("delta_max") is not None:
        limit = float(cfg["delta_max"])
        if delta > limit:
            triggered = True
            reasons.append(f"Δ {delta:+.4f} > umbral {limit:+.4f}")

    return {
        "metric": metric,
        "triggered": triggered,
        "reasons": reasons,
        "source": cfg.get("source"),
        "name": cfg.get("name", "umbral específico del proyecto"),
    }

def _quality_gate(
    spectral_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    warnings: List[str] = list(spectral_analysis.get("warnings") or [])
    comparison = spectral_analysis.get("comparison") or {}

    if not comparison.get("available"):
        return {
            "usable": False,
            "level": LEVEL_INSUFFICIENT,
            "warnings": warnings + [
                "No existe comparación temporal homogénea válida."
            ],
        }

    current = comparison.get("current") or {}
    base = comparison.get("baseline") or {}

    if not current or not base:
        return {
            "usable": False,
            "level": LEVEL_INSUFFICIENT,
            "warnings": warnings + [
                "Faltan estadísticos actuales o de referencia."
            ],
        }

    valid_fraction = _num(current.get("valid_fraction"))
    if valid_fraction is not None and valid_fraction < 0.30:
        return {
            "usable": False,
            "level": LEVEL_INSUFFICIENT,
            "warnings": warnings + [
                f"Fracción válida del compuesto demasiado baja ({valid_fraction:.0%})."
            ],
        }

    if valid_fraction is not None and valid_fraction < 0.50:
        warnings.append(
            f"Fracción válida limitada ({valid_fraction:.0%}); interpretación cautelosa."
        )

    return {
        "usable": True,
        "level": None,
        "warnings": warnings,
    }

def _context_flags(
    spectral_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Usa SCL sólo como contexto de escena Sentinel-2.
    No lo interpreta como cartografía definitiva.
    """
    snapshot = spectral_analysis.get("snapshot_s2") or {}
    context = snapshot.get("context") or {}

    snow = _num(context.get("scl_snow_ice_fraction"))
    water = _num(context.get("scl_water_fraction"))
    vegetation = _num(context.get("scl_vegetation_fraction"))
    bare = _num(context.get("scl_bare_fraction"))

    return {
        "snow_ice_fraction": snow,
        "water_fraction": water,
        "vegetation_fraction": vegetation,
        "bare_fraction": bare,
    }

def _screen_vegetation(
    comparison: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    cur = comparison.get("current") or {}
    base = comparison.get("baseline") or {}
    changes = comparison.get("changes") or {}

    ndvi = _num(cur.get("ndvi"))
    savi = _num(cur.get("savi"))
    vegetation_fraction = _num(context.get("vegetation_fraction"))

    confounders = []
    if vegetation_fraction is not None and vegetation_fraction < 0.10:
        confounders.append(
            "La escena reciente tiene baja fracción clasificada como vegetación; "
            "un NDVI/SAVI bajo no equivale por sí solo a pérdida de vegetación."
        )

    return {
        "object": "vegetación",
        "observations": [
            f"NDVI actual {_fmt(ndvi)}; {_delta_text(changes.get('ndvi') or {})}.",
            f"SAVI actual {_fmt(savi)}; {_delta_text(changes.get('savi') or {})}.",
        ],
        "confounders": confounders,
        "interpretation": (
            "Señal de verdor/cobertura fotosintética. La atribución a pérdida, estrés, "
            "tala, degradación u otra causa requiere que el área evaluada corresponda "
            "realmente a vegetación y debe contrastarse con antecedentes espaciales, "
            "temporales y/o de terreno."
        ),
    }

def _screen_water_moisture(
    comparison: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    cur = comparison.get("current") or {}
    changes = comparison.get("changes") or {}

    ndwi = _num(cur.get("ndwi"))
    ndmi = _num(cur.get("ndmi"))
    water_fraction = _num(context.get("water_fraction"))
    snow_fraction = _num(context.get("snow_ice_fraction"))

    confounders = []
    if snow_fraction is not None and snow_fraction > 0.05:
        confounders.append(
            "Existe nieve/hielo clasificado en la escena reciente; debe descartarse "
            "su influencia antes de atribuir una señal hídrica a agua líquida."
        )

    if water_fraction is not None and water_fraction == 0:
        confounders.append(
            "SCL no clasifica agua en la escena reciente; una anomalía de NDMI no debe "
            "describirse como acumulación superficial de agua."
        )

    return {
        "object": "agua/humedad",
        "observations": [
            f"NDWI (Green–NIR) actual {_fmt(ndwi)}; {_delta_text(changes.get('ndwi') or {})}.",
            f"NDMI (NIR–SWIR1) actual {_fmt(ndmi)}; {_delta_text(changes.get('ndmi') or {})}.",
        ],
        "confounders": confounders,
        "interpretation": (
            "NDWI se usa como señal de agua superficial y NDMI como señal de humedad. "
            "Ninguno identifica por sí solo el origen del agua, una fuga, un relave, "
            "un drenaje deficiente o incumplimiento."
        ),
    }

def _screen_cryosphere(
    comparison: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    cur = comparison.get("current") or {}
    changes = comparison.get("changes") or {}

    ndsi = _num(cur.get("ndsi"))
    snow_fraction = _num(context.get("snow_ice_fraction"))

    observations = [
        f"NDSI actual {_fmt(ndsi)}; {_delta_text(changes.get('ndsi') or {})}."
    ]

    if snow_fraction is not None:
        observations.append(
            f"Fracción SCL clasificada como nieve/hielo en escena reciente: "
            f"{snow_fraction:.1%}."
        )

    return {
        "object": "criósfera",
        "observations": observations,
        "confounders": [],
        "interpretation": (
            "La combinación es evidencia espectral compatible con nieve/hielo. "
            "No demuestra por sí sola presencia de un glaciar inventariado, balance "
            "de masa, retracción glaciar, hielo perenne ni relación causal con el proyecto."
        ),
    }

def _screen_swir(
    comparison: Dict[str, Any],
) -> Dict[str, Any]:
    cur = comparison.get("current") or {}
    changes = comparison.get("changes") or {}

    return {
        "object": "SWIR/sustrato",
        "observations": [
            f"Reflectancia SWIR1 actual {_fmt(cur.get('swir1'))}; "
            f"{_delta_text(changes.get('swir1') or {})}.",
            f"Razón SWIR1/SWIR2 actual {_fmt(cur.get('swir_ratio'))}; "
            f"{_delta_text(changes.get('swir_ratio') or {})}.",
        ],
        "confounders": [],
        "interpretation": (
            "Estas variables describen respuesta espectral SWIR. Sin calibración "
            "mineralógica/geológica específica no deben denominarse contenido de arcilla "
            "ni usarse para confirmar erosión, remoción o estabilidad de taludes."
        ),
    }

def _sea_alignment() -> Dict[str, str]:
    """
    Campos de evaluación que una vigilancia satelital puede apoyar, pero no resolver sola.
    """
    return {
        "calidad_cantidad": (
            "Puede aportar evidencia cuantitativa de cambios espectrales/coberturas; "
            "debe integrarse con la caracterización del objeto de protección."
        ),
        "magnitud": (
            "Requiere delimitar espacialmente el cambio y relacionarlo con el objeto "
            "de protección y el impacto evaluado."
        ),
        "duracion_frecuencia": (
            "Requiere serie temporal suficiente; una sola escena no determina duración."
        ),
        "extension": (
            "Requiere estimar superficie afectada con una clasificación espacial validada."
        ),
        "resiliencia_regeneracion_permanencia": (
            "No puede inferirse de un único índice o fecha; requiere trayectoria temporal "
            "y conocimiento ecológico del componente."
        ),
        "biodiversidad": (
            "Los índices pueden aportar contexto de hábitat, pero no sustituyen inventarios "
            "de flora/fauna ni evaluación de poblaciones."
        ),
        "cambio_climatico": (
            "Debe incorporar series climáticas y considerar exposición/vulnerabilidad "
            "del componente, sin atribución automática."
        ),
        "cumplimiento": (
            "Debe contrastarse con RCA, permisos, compromisos, planes de seguimiento, "
            "normas y umbrales aplicables al proyecto."
        ),
    }

def build_surveillance_diagnosis(
    spectral_analysis: Dict[str, Any],
    project_data: Dict[str, Any],
) -> Dict[str, Any]:
    quality = _quality_gate(spectral_analysis)

    if not quality["usable"]:
        return {
            "level": LEVEL_INSUFFICIENT,
            "title": "No es posible emitir una interpretación técnica robusta",
            "quality": quality,
            "findings": [],
            "threshold_checks": [],
            "recommendations": [
                "Obtener una ventana con cobertura válida suficiente.",
                "Revisar máscara de nubes/nieve y disponibilidad del sensor.",
                "No emitir conclusiones ambientales o regulatorias con estos datos.",
            ],
            "sea_alignment": _sea_alignment(),
            "regulatory_statement": (
                "BioCore Intelligence no determina cumplimiento ni obligaciones de reporte "
                "cuando la evidencia es insuficiente."
            ),
        }

    comparison = spectral_analysis["comparison"]
    context = _context_flags(spectral_analysis)
    thresholds = _get_thresholds(project_data)

    findings = [
        _screen_vegetation(comparison, context),
        _screen_water_moisture(comparison, context),
        _screen_cryosphere(comparison, context),
        _screen_swir(comparison),
    ]

    threshold_checks: List[Dict[str, Any]] = []
    current = comparison.get("current") or {}
    baseline = comparison.get("baseline") or {}
    changes = comparison.get("changes") or {}

    for metric in ["savi", "ndvi", "ndwi", "ndmi", "ndsi", "swir1", "swir_ratio"]:
        result = _threshold_result(
            metric=metric,
            current=current.get(metric),
            base=baseline.get(metric),
            change=changes.get(metric) or {},
            thresholds=thresholds,
        )
        if result is not None:
            threshold_checks.append(result)

    triggered = [x for x in threshold_checks if x.get("triggered")]

    if triggered:
        level = LEVEL_PRIORITY
        title = "Se alcanzó al menos un umbral específico y trazable del proyecto"
        recommendations = [
            "Verificar el hallazgo con los medios definidos en el instrumento que origina el umbral.",
            "Revisar la persistencia temporal y la extensión espacial de la señal.",
            "Aplicar el plan de acción asociado al umbral sólo según la RCA, permiso, "
            "plan de seguimiento o protocolo que lo establezca.",
        ]
    else:
        # Sin umbral específico no se fuerza una clasificación crítica.
        level = LEVEL_REVIEW if quality["warnings"] else LEVEL_NO_FLAG
        title = (
            "Observaciones de vigilancia para revisión técnica"
            if level == LEVEL_REVIEW
            else "Sin umbral específico excedido en la configuración disponible"
        )
        recommendations = [
            "Interpretar los cambios junto con el tipo de cobertura y contexto del proyecto.",
            "Revisar persistencia temporal antes de atribuir causalidad.",
            "Usar verificación espacial/terreno cuando el objeto de protección lo requiera.",
            "Contrastar cualquier decisión regulatoria con la RCA, permisos y plan de seguimiento aplicables.",
        ]

    return {
        "level": level,
        "title": title,
        "quality": quality,
        "context": context,
        "findings": findings,
        "threshold_checks": threshold_checks,
        "recommendations": recommendations,
        "sea_alignment": _sea_alignment(),
        "regulatory_statement": (
            "El resultado es una herramienta de vigilancia y alerta temprana. "
            "No constituye por sí solo una determinación de impacto significativo, "
            "incumplimiento, causalidad ni obligación de notificación."
        ),
        "carbon_statement": (
            "No se evalúa ni certifica capacidad de sumidero de carbono a partir de SAVI/NDVI."
        ),
        "cryosphere_statement": (
            "La detección espectral de nieve/hielo no sustituye inventarios oficiales "
            "ni estudios glaciológicos."
        ),
    }


# ============================================================================
# INTEGRADO DESDE biocore_regulatorio_v2.py
# ============================================================================
STATUS_NO_RULE = "SIN CRITERIO REGULATORIO CARGADO"

STATUS_NOT_EVALUABLE = "NO EVALUABLE"

STATUS_WITHIN = "DENTRO DEL CRITERIO CONFIGURADO"

STATUS_INTERMEDIATE = "UMBRAL INTERMEDIO ALCANZADO"

STATUS_LIMIT = "UMBRAL LIMITE ALCANZADO"

ALLOWED_OPERATORS = {"<", "<=", ">", ">=", "between", "delta<", "delta<=", "delta>", "delta>="}

@dataclass
class RuleCheck:
    rule_id: str
    parameter: str
    status: str
    triggered: bool
    observed_value: Optional[float]
    delta_value: Optional[float]
    message: str
    source: Dict[str, Any]
    action: Optional[Dict[str, Any]]

def _num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _metric_map(canonical_report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        str(m.get("code")): m
        for m in (canonical_report.get("metrics") or [])
        if m.get("code")
    }

def _validate_source(source: Dict[str, Any]) -> List[str]:
    errors = []
    if not isinstance(source, dict):
        return ["La fuente regulatoria debe ser un objeto/diccionario."]

    for field in ["tipo", "titulo", "identificador"]:
        if not str(source.get(field) or "").strip():
            errors.append(f"Falta source.{field}")

    # Para una regla accionable exigimos ubicación concreta dentro del instrumento.
    if not (
        str(source.get("seccion") or "").strip()
        or str(source.get("pagina") or "").strip()
        or str(source.get("clausula") or "").strip()
    ):
        errors.append(
            "La fuente debe indicar al menos sección, página o cláusula."
        )

    return errors

def validate_monitoring_plan(plan: Dict[str, Any]) -> List[str]:
    """
    Valida un plan configurado en BioCore.

    Estructura esperada:
    {
      "plan_id": "...",
      "nombre": "...",
      "componente_ambiental": "...",
      "variable_ambiental": "...",
      "impacto_asociado": "...",
      "metodo": {...},
      "frecuencia": {...},
      "rules": [...]
    }
    """
    errors: List[str] = []

    if not isinstance(plan, dict):
        return ["El plan de seguimiento debe ser un diccionario."]

    for field in [
        "plan_id",
        "nombre",
        "componente_ambiental",
        "variable_ambiental",
    ]:
        if not str(plan.get(field) or "").strip():
            errors.append(f"Falta {field}")

    method = plan.get("metodo")
    if not isinstance(method, dict):
        errors.append("Falta bloque metodo")
    else:
        if not str(method.get("descripcion") or "").strip():
            errors.append("Falta metodo.descripcion")
        if not str(method.get("unidad") or "").strip():
            errors.append("Falta metodo.unidad")

    frequency = plan.get("frecuencia")
    if not isinstance(frequency, dict):
        errors.append("Falta bloque frecuencia")
    else:
        if not str(frequency.get("descripcion") or "").strip():
            errors.append("Falta frecuencia.descripcion")

    rules = plan.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("El plan no contiene reglas/umbrales configurados.")
        return errors

    seen = set()
    for i, rule in enumerate(rules):
        prefix = f"rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{prefix} no es un diccionario")
            continue

        rid = str(rule.get("rule_id") or "").strip()
        if not rid:
            errors.append(f"{prefix}.rule_id faltante")
        elif rid in seen:
            errors.append(f"{prefix}.rule_id duplicado: {rid}")
        else:
            seen.add(rid)

        parameter = str(rule.get("parameter") or "").strip()
        if not parameter:
            errors.append(f"{prefix}.parameter faltante")

        operator = str(rule.get("operator") or "").strip()
        if operator not in ALLOWED_OPERATORS:
            errors.append(f"{prefix}.operator inválido: {operator}")

        level = str(rule.get("level") or "").lower()
        if level not in {"intermediate", "limit"}:
            errors.append(
                f"{prefix}.level debe ser 'intermediate' o 'limit'"
            )

        if operator == "between":
            if _num(rule.get("min")) is None or _num(rule.get("max")) is None:
                errors.append(f"{prefix}: between requiere min y max numéricos")
        else:
            if _num(rule.get("threshold")) is None:
                errors.append(f"{prefix}.threshold faltante/no numérico")

        source_errors = _validate_source(rule.get("source") or {})
        errors.extend([f"{prefix}: {e}" for e in source_errors])

        action = rule.get("action")
        if action is not None:
            if not isinstance(action, dict):
                errors.append(f"{prefix}.action debe ser diccionario")
            else:
                if not str(action.get("descripcion") or "").strip():
                    errors.append(f"{prefix}.action.descripcion faltante")
                # Si la acción implica reporte, exigir destino/plazo configurado.
                if action.get("report_required") is True:
                    if not str(action.get("report_to") or "").strip():
                        errors.append(f"{prefix}.action.report_to faltante")
                    if not str(action.get("deadline") or "").strip():
                        errors.append(f"{prefix}.action.deadline faltante")

    return errors

def _compare(
    operator: str,
    current: Optional[float],
    delta: Optional[float],
    rule: Dict[str, Any],
) -> Optional[bool]:
    if operator.startswith("delta"):
        value = delta
        op = operator.replace("delta", "")
    else:
        value = current
        op = operator

    if value is None:
        return None

    if op == "between":
        lo = _num(rule.get("min"))
        hi = _num(rule.get("max"))
        if lo is None or hi is None:
            return None
        return lo <= value <= hi

    threshold = _num(rule.get("threshold"))
    if threshold is None:
        return None

    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold

    return None

def evaluate_rule(
    rule: Dict[str, Any],
    metric: Dict[str, Any],
) -> RuleCheck:
    current = _num(metric.get("current"))
    delta = _num(metric.get("delta"))

    result = _compare(
        str(rule.get("operator")),
        current=current,
        delta=delta,
        rule=rule,
    )

    if result is None:
        return RuleCheck(
            rule_id=str(rule.get("rule_id")),
            parameter=str(rule.get("parameter")),
            status=STATUS_NOT_EVALUABLE,
            triggered=False,
            observed_value=current,
            delta_value=delta,
            message=(
                "No es posible evaluar esta regla con los datos disponibles "
                "del análisis actual."
            ),
            source=deepcopy(rule.get("source") or {}),
            action=None,
        )

    if result:
        level = str(rule.get("level") or "").lower()
        status = STATUS_LIMIT if level == "limit" else STATUS_INTERMEDIATE
        action = deepcopy(rule.get("action")) if rule.get("action") else None

        return RuleCheck(
            rule_id=str(rule.get("rule_id")),
            parameter=str(rule.get("parameter")),
            status=status,
            triggered=True,
            observed_value=current,
            delta_value=delta,
            message=str(rule.get("message") or status),
            source=deepcopy(rule.get("source") or {}),
            action=action,
        )

    return RuleCheck(
        rule_id=str(rule.get("rule_id")),
        parameter=str(rule.get("parameter")),
        status=STATUS_WITHIN,
        triggered=False,
        observed_value=current,
        delta_value=delta,
        message="El parámetro no alcanza el umbral configurado.",
        source=deepcopy(rule.get("source") or {}),
        action=None,
    )

def evaluate_monitoring_plan(
    canonical_report: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Evalúa un plan específico contra UN reporte canónico.

    Nunca genera una obligación regulatoria si no hay plan válido.
    """
    if not plan:
        return {
            "status": STATUS_NO_RULE,
            "valid_plan": False,
            "errors": [],
            "checks": [],
            "actions": [],
            "reporting_obligations": [],
            "summary": (
                "No se cargó una RCA, PAS, CAV, plan de seguimiento u otro "
                "instrumento con umbrales trazables. BioCore Intelligence no determina "
                "incumplimiento ni obligación de reporte."
            ),
        }

    errors = validate_monitoring_plan(plan)
    if errors:
        return {
            "status": STATUS_NOT_EVALUABLE,
            "valid_plan": False,
            "errors": errors,
            "checks": [],
            "actions": [],
            "reporting_obligations": [],
            "summary": (
                "El plan regulatorio cargado está incompleto o no es trazable. "
                "No se ejecutan acciones automáticas."
            ),
        }

    metrics = _metric_map(canonical_report)
    checks: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    obligations: List[Dict[str, Any]] = []

    for rule in plan.get("rules") or []:
        parameter = str(rule.get("parameter"))
        metric = metrics.get(parameter)

        if metric is None:
            check = RuleCheck(
                rule_id=str(rule.get("rule_id")),
                parameter=parameter,
                status=STATUS_NOT_EVALUABLE,
                triggered=False,
                observed_value=None,
                delta_value=None,
                message=(
                    f"El reporte canónico no contiene el parámetro {parameter}."
                ),
                source=deepcopy(rule.get("source") or {}),
                action=None,
            )
        else:
            check = evaluate_rule(rule, metric)

        check_dict = {
            "rule_id": check.rule_id,
            "parameter": check.parameter,
            "status": check.status,
            "triggered": check.triggered,
            "observed_value": check.observed_value,
            "delta_value": check.delta_value,
            "message": check.message,
            "source": check.source,
            "action": check.action,
        }
        checks.append(check_dict)

        if check.triggered and check.action:
            action_entry = {
                "rule_id": check.rule_id,
                "status": check.status,
                **deepcopy(check.action),
                "source": deepcopy(check.source),
            }
            actions.append(action_entry)

            if check.action.get("report_required") is True:
                obligations.append(
                    {
                        "rule_id": check.rule_id,
                        "report_to": check.action.get("report_to"),
                        "deadline": check.action.get("deadline"),
                        "means_of_verification": check.action.get(
                            "means_of_verification"
                        ),
                        "source": deepcopy(check.source),
                    }
                )

    if any(c["status"] == STATUS_LIMIT for c in checks):
        status = STATUS_LIMIT
    elif any(c["status"] == STATUS_INTERMEDIATE for c in checks):
        status = STATUS_INTERMEDIATE
    elif all(c["status"] == STATUS_NOT_EVALUABLE for c in checks):
        status = STATUS_NOT_EVALUABLE
    else:
        status = STATUS_WITHIN

    return {
        "status": status,
        "valid_plan": True,
        "plan_id": plan.get("plan_id"),
        "plan_name": plan.get("nombre"),
        "component": plan.get("componente_ambiental"),
        "environmental_variable": plan.get("variable_ambiental"),
        "associated_impact": plan.get("impacto_asociado"),
        "method": deepcopy(plan.get("metodo") or {}),
        "frequency": deepcopy(plan.get("frecuencia") or {}),
        "checks": checks,
        "actions": actions,
        "reporting_obligations": obligations,
        "summary": _build_summary(status, checks, obligations),
    }

def _build_summary(
    status: str,
    checks: List[Dict[str, Any]],
    obligations: List[Dict[str, Any]],
) -> str:
    triggered = [c for c in checks if c.get("triggered")]

    if status == STATUS_LIMIT:
        text = (
            f"Se alcanzó al menos un umbral límite configurado y trazable "
            f"({len(triggered)} regla(s) activada(s))."
        )
    elif status == STATUS_INTERMEDIATE:
        text = (
            f"Se alcanzó al menos un umbral intermedio configurado "
            f"({len(triggered)} regla(s) activada(s))."
        )
    elif status == STATUS_WITHIN:
        text = "No se alcanzaron los umbrales configurados en el plan."
    else:
        text = "No fue posible evaluar de forma completa las reglas configuradas."

    if obligations:
        text += (
            f" Existen {len(obligations)} obligación(es) de reporte configurada(s); "
            "BioCore las muestra exactamente como fueron cargadas desde el instrumento."
        )
    else:
        text += (
            " No se infiere obligación de reporte fuera de las reglas expresamente cargadas."
        )

    return text

def sea_followup_structure(plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Estructura de control de completitud inspirada en los contenidos mínimos
    de seguimiento ambiental descritos por SEA.
    """
    if not plan:
        return {
            "complete": False,
            "missing": [
                "nombre asociado",
                "componente ambiental",
                "variable ambiental",
                "impacto asociado",
                "parámetros/unidades",
                "método",
                "frecuencia/temporalidad",
                "umbrales",
                "acciones",
                "entrega de informes cuando corresponda",
            ],
        }

    missing = []

    checks = {
        "nombre asociado": plan.get("nombre"),
        "componente ambiental": plan.get("componente_ambiental"),
        "variable ambiental": plan.get("variable_ambiental"),
        "impacto asociado": plan.get("impacto_asociado"),
        "parámetros/unidades": (plan.get("metodo") or {}).get("unidad"),
        "método": (plan.get("metodo") or {}).get("descripcion"),
        "frecuencia/temporalidad": (plan.get("frecuencia") or {}).get("descripcion"),
        "umbrales": plan.get("rules"),
    }

    for label, value in checks.items():
        if not value:
            missing.append(label)

    has_actions = any(
        isinstance(r, dict) and r.get("action")
        for r in (plan.get("rules") or [])
    )
    if not has_actions:
        missing.append("acciones")

    return {
        "complete": not missing,
        "missing": missing,
    }

EMPTY_PLAN_TEMPLATE = {
    "plan_id": "",
    "nombre": "",
    "componente_ambiental": "",
    "variable_ambiental": "",
    "impacto_asociado": "",
    "metodo": {
        "descripcion": "",
        "unidad": "",
        "escala_espacial": "",
        "control_calidad": "",
    },
    "frecuencia": {
        "descripcion": "",
        "ventana_temporal": "",
    },
    "rules": [
        {
            "rule_id": "",
            "parameter": "NDVI",
            "operator": "delta<=",
            "threshold": None,
            "level": "intermediate",
            "message": "",
            "source": {
                "tipo": "RCA/PAS/CAV/Plan/Norma",
                "titulo": "",
                "identificador": "",
                "seccion": "",
                "pagina": "",
                "clausula": "",
                "url": "",
            },
            "action": {
                "descripcion": "",
                "report_required": False,
                "report_to": "",
                "deadline": "",
                "means_of_verification": "",
            },
        }
    ],
}


# ============================================================================
# INTEGRADO DESDE biocore_sar_incendios_v2.py
# ============================================================================
S1 = "COPERNICUS/S1_GRD"

VIIRS_SNPP = "NASA/LANCE/SNPP_VIIRS/C2"

VIIRS_NOAA20 = "NASA/LANCE/NOAA20_VIIRS/C2"

def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _size(col: ee.ImageCollection) -> int:
    try:
        return int(col.size().getInfo() or 0)
    except Exception:
        return 0

def _same_month_day(date_ref: datetime, year: int) -> datetime:
    try:
        return date_ref.replace(year=int(year))
    except ValueError:
        return date_ref.replace(year=int(year), day=28)

def _s1_base_collection(
    geom: ee.Geometry,
    start: datetime,
    end: datetime,
    orbit_pass: str,
) -> ee.ImageCollection:
    return (
        ee.ImageCollection(S1)
        .filterBounds(geom)
        .filterDate(
            start.strftime("%Y-%m-%d"),
            (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
    )

def _orbit_histogram(col: ee.ImageCollection) -> Dict[str, int]:
    try:
        raw = col.aggregate_histogram("relativeOrbitNumber_start").getInfo() or {}
        return {str(k): int(v) for k, v in raw.items()}
    except Exception:
        return {}

def _select_comparable_orbit(
    current_col: ee.ImageCollection,
    baseline_col: ee.ImageCollection,
) -> Optional[int]:
    """
    Selecciona una orbita relativa presente en ambos periodos.
    Prioriza la que maximiza la cantidad minima de escenas entre ambos periodos.
    """
    cur = _orbit_histogram(current_col)
    base = _orbit_histogram(baseline_col)

    common = set(cur) & set(base)
    if not common:
        return None

    best = max(common, key=lambda k: (min(cur[k], base[k]), cur[k] + base[k]))
    return int(best)

def _mask_s1_edges(image: ee.Image) -> ee.Image:
    """
    Enmascara bordes muy oscuros siguiendo la practica de ejemplo del
    catalogo Earth Engine. Conserva VV, VH y angle.
    """
    vv = image.select("VV")
    mask = vv.gt(-30.0)
    return image.updateMask(mask)

def _s1_stats(
    col: ee.ImageCollection,
    geom: ee.Geometry,
) -> Dict[str, Any]:
    if _size(col) == 0:
        return {}

    prepared = col.map(_mask_s1_edges)
    median = prepared.select(["VV", "VH", "angle"]).median()

    vv_vh = median.select("VV").subtract(median.select("VH")).rename("VV_minus_VH")
    img = median.addBands(vv_vh)

    reducer = ee.Reducer.mean().combine(
        reducer2=ee.Reducer.stdDev(),
        sharedInputs=True,
    )

    raw = (
        img.reduceRegion(
            reducer=reducer,
            geometry=geom,
            scale=20,
            maxPixels=1e9,
            bestEffort=True,
        )
        .getInfo()
    )

    result = {
        "vv_db": _safe_float(raw.get("VV_mean")),
        "vv_sd_db": _safe_float(raw.get("VV_stdDev")),
        "vh_db": _safe_float(raw.get("VH_mean")),
        "vh_sd_db": _safe_float(raw.get("VH_stdDev")),
        "vv_minus_vh_db": _safe_float(raw.get("VV_minus_VH_mean")),
        "incidence_angle_deg": _safe_float(raw.get("angle_mean")),
        "n_scenes": _size(col),
    }

    try:
        latest = col.sort("system:time_start", False).first()
        ts = latest.get("system:time_start").getInfo()
        result["latest_scene_date"] = (
            datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if ts else None
        )
    except Exception:
        result["latest_scene_date"] = None

    return result

def _delta(a, b) -> Optional[float]:
    a = _safe_float(a)
    b = _safe_float(b)
    if a is None or b is None:
        return None
    return a - b

def build_sentinel1_analysis(
    geom: ee.Geometry,
    baseline_year: int,
    requested_days: int = 30,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=max(7, int(requested_days)))
    base_end = _same_month_day(now, int(baseline_year))
    base_start = base_end - timedelta(days=max(7, int(requested_days)))

    passes = {}

    for orbit_pass in ["ASCENDING", "DESCENDING"]:
        current_all = _s1_base_collection(
            geom, current_start, now, orbit_pass
        )
        baseline_all = _s1_base_collection(
            geom, base_start, base_end, orbit_pass
        )

        if _size(current_all) == 0:
            continue

        relative_orbit = _select_comparable_orbit(current_all, baseline_all)

        if relative_orbit is not None:
            current = current_all.filter(
                ee.Filter.eq("relativeOrbitNumber_start", relative_orbit)
            )
            baseline = baseline_all.filter(
                ee.Filter.eq("relativeOrbitNumber_start", relative_orbit)
            )
            comparability = "misma pasada y misma orbita relativa"
        else:
            current = current_all
            baseline = baseline_all
            comparability = (
                "misma pasada; no se encontro una orbita relativa comun "
                "en ambos periodos"
            )

        cur = _s1_stats(current, geom)
        base = _s1_stats(baseline, geom) if _size(baseline) > 0 else {}

        passes[orbit_pass.lower()] = {
            "orbit_pass": orbit_pass,
            "relative_orbit": relative_orbit,
            "comparability": comparability,
            "current": cur,
            "baseline": base or None,
            "change": {
                "vv_db": _delta(cur.get("vv_db"), (base or {}).get("vv_db")),
                "vh_db": _delta(cur.get("vh_db"), (base or {}).get("vh_db")),
                "vv_minus_vh_db": _delta(
                    cur.get("vv_minus_vh_db"),
                    (base or {}).get("vv_minus_vh_db"),
                ),
            },
        }

    available = bool(passes)

    warnings: List[str] = []
    if not available:
        warnings.append("No hay escenas Sentinel-1 IW VV+VH válidas en la ventana actual.")
    if int(baseline_year) < 2014:
        warnings.append(
            "La línea base antecede Sentinel-1; no existe comparación SAR histórica equivalente."
        )
    warnings.append(
        "La retrodispersión SAR depende de rugosidad, humedad, vegetación, geometría "
        "e incidencia. No debe traducirse automáticamente a litología, agua, "
        "estabilidad de talud u operación."
    )
    warnings.append(
        "En relieve montañoso pueden persistir efectos topográficos; este módulo no "
        "implementa por sí solo una corrección radiométrica de terreno/terrain flattening."
    )

    return {
        "available": available,
        "collection": S1,
        "frequency_ghz": 5.405,
        "mode": "IW",
        "polarizations": ["VV", "VH"],
        "current_window_start": current_start.strftime("%Y-%m-%d"),
        "current_window_end": now.strftime("%Y-%m-%d"),
        "baseline_window_start": base_start.strftime("%Y-%m-%d"),
        "baseline_window_end": base_end.strftime("%Y-%m-%d"),
        "passes": passes,
        "warnings": warnings,
        "interpretation_scope": (
            "Evidencia radar complementaria. Priorizar cambios temporales con geometría "
            "comparable; evitar interpretaciones absolutas no calibradas."
        ),
    }

def _fire_stats_for_collection(
    collection_id: str,
    geom: ee.Geometry,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    col = (
        ee.ImageCollection(collection_id)
        .filterBounds(geom)
        .filterDate(
            start.strftime("%Y-%m-%d"),
            (end + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        .sort("system:time_start")
    )

    n_images = _size(col)
    if n_images == 0:
        return {
            "collection": collection_id,
            "available": False,
            "n_daily_images": 0,
        }

    # Nominal/alta: confidence >= 1. Alta: confidence == 2.
    nominal_high_sum = col.map(
        lambda img: img.select("confidence").gte(1).rename("det").unmask(0)
    ).sum()

    high_sum = col.map(
        lambda img: img.select("confidence").eq(2).rename("det").unmask(0)
    ).sum()

    max_conf = col.select("confidence").max()

    try:
        nominal_pixel_days = (
            nominal_high_sum.reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=geom,
                scale=375,
                maxPixels=1e9,
                bestEffort=True,
            )
            .get("det")
            .getInfo()
        )
        nominal_pixel_days = int(round(float(nominal_pixel_days or 0)))
    except Exception:
        nominal_pixel_days = 0

    try:
        high_pixel_days = (
            high_sum.reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=geom,
                scale=375,
                maxPixels=1e9,
                bestEffort=True,
            )
            .get("det")
            .getInfo()
        )
        high_pixel_days = int(round(float(high_pixel_days or 0)))
    except Exception:
        high_pixel_days = 0

    try:
        max_confidence = (
            max_conf.reduceRegion(
                reducer=ee.Reducer.max(),
                geometry=geom,
                scale=375,
                maxPixels=1e9,
                bestEffort=True,
            )
            .get("confidence")
            .getInfo()
        )
        max_confidence = int(max_confidence) if max_confidence is not None else None
    except Exception:
        max_confidence = None

    days_with_detection = 0
    latest_detection_date = None

    # Ventanas de alerta son cortas; iterar imágenes permite conservar la fecha.
    images = col.toList(n_images)
    for i in range(n_images):
        try:
            img = ee.Image(images.get(i))
            any_det = (
                img.select("confidence")
                .gte(1)
                .reduceRegion(
                    reducer=ee.Reducer.max(),
                    geometry=geom,
                    scale=375,
                    maxPixels=1e9,
                    bestEffort=True,
                )
                .get("confidence")
                .getInfo()
            )

            if any_det:
                days_with_detection += 1
                ts = img.get("system:time_start").getInfo()
                if ts:
                    d = datetime.fromtimestamp(
                        ts / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%d")
                    latest_detection_date = d
        except Exception:
            continue

    return {
        "collection": collection_id,
        "available": True,
        "pixel_size_m": 375,
        "n_daily_images": n_images,
        "nominal_high_pixel_days": nominal_pixel_days,
        "high_confidence_pixel_days": high_pixel_days,
        "days_with_detection": days_with_detection,
        "max_confidence_code": max_confidence,
        "latest_detection_date": latest_detection_date,
    }

def build_active_fire_analysis(
    geom: ee.Geometry,
    days: int = 7,
) -> Dict[str, Any]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))

    sources = [
        _fire_stats_for_collection(VIIRS_SNPP, geom, start, end),
        _fire_stats_for_collection(VIIRS_NOAA20, geom, start, end),
    ]

    total_nominal_high = sum(
        int(s.get("nominal_high_pixel_days") or 0) for s in sources
    )
    total_high = sum(
        int(s.get("high_confidence_pixel_days") or 0) for s in sources
    )

    detection = total_nominal_high > 0

    return {
        "available": any(s.get("available") for s in sources),
        "detection": detection,
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": end.strftime("%Y-%m-%d"),
        "sources": sources,
        "combined_nominal_high_pixel_days": total_nominal_high,
        "combined_high_confidence_pixel_days": total_high,
        "interpretation": (
            "Las cifras son detecciones térmicas píxel-día y pueden incluir el mismo "
            "evento en más de una adquisición/satélite. No representan un número de "
            "incendios únicos."
        ),
        "quality_note": (
            "Producto VIIRS casi en tiempo real (NRT), apropiado para alerta temprana; "
            "no debe presentarse como producto retrospectivo de calidad científica final."
        ),
    }

def build_ancillary_analysis(
    geom: ee.Geometry,
    baseline_year: int,
    sar_days: int = 30,
    fire_days: int = 7,
) -> Dict[str, Any]:
    return {
        "sentinel1": build_sentinel1_analysis(
            geom=geom,
            baseline_year=baseline_year,
            requested_days=sar_days,
        ),
        "active_fire": build_active_fire_analysis(
            geom=geom,
            days=fire_days,
        ),
    }


# ============================================================================
# INTEGRADO DESDE biocore_reporte_canonico_v2.py
# ============================================================================
SCHEMA_VERSION = "biocore-report-2.0"

REPORT_METHOD_VERSION = "BioCore surveillance-report-v3.0"

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()

def _safe_num(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _fmt(value, decimals=4, suffix="") -> str:
    value = _safe_num(value)
    if value is None:
        return "N/D"
    return f"{value:.{decimals}f}{suffix}"

def _fmt_delta(change: Dict[str, Any]) -> str:
    change = change or {}
    delta = _safe_num(change.get("delta"))
    pct = _safe_num(change.get("pct"))
    if delta is None:
        return "N/D"
    text = f"{delta:+.4f}"
    if pct is not None:
        text += f" ({pct:+.1f}%)"
    return text

def _canonical_bytes(obj: Dict[str, Any]) -> bytes:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")

def compute_analysis_id(report_without_id: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(report_without_id)).hexdigest()

def _clean_pdf_text(text: Any) -> str:
    """
    Helvetica core de FPDF usa latin-1.
    Se reemplazan simbolos no compatibles y se conservan tildes latinas.
    """
    s = str(text if text is not None else "")
    replacements = {
        "–": "-",
        "—": "-",
        "−": "-",
        "→": "->",
        "≤": "<=",
        "≥": ">=",
        "Δ": "Delta",
        "•": "-",
        "°": "°",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "×": "x",
        "≈": "~",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    return s.encode("latin-1", errors="replace").decode("latin-1")

def _short_id(report: Dict[str, Any]) -> str:
    return str(report.get("analysis_id") or "")[:12]

BASE_REFERENCES = [
    {
        "id": "SEA-ET-2025",
        "citation": (
            "Servicio de Evaluacion Ambiental (2025). Guia para la prediccion "
            "y evaluacion de impactos sobre ecosistemas terrestres. Primera edicion."
        ),
        "role": (
            "Marco para criterios de calidad/cantidad, resiliencia, permanencia, "
            "regeneracion, condicion, magnitud, duracion, extension, biodiversidad, "
            "cambio climatico y seguimiento."
        ),
    },
    {
        "id": "S2-SR-HARMONIZED",
        "citation": "Copernicus Sentinel-2 MSI Level-2A Surface Reflectance Harmonized.",
        "role": "Reflectancia superficial e indices espectrales recientes.",
    },
    {
        "id": "LANDSAT-C2-L2",
        "citation": "USGS Landsat Collection 2 Level-2 Surface Reflectance.",
        "role": "Comparacion temporal homogenea cuando la linea base precede a Sentinel-2 SR.",
    },
    {
        "id": "MOD11A1-061",
        "citation": "NASA MODIS MOD11A1 v061 Land Surface Temperature.",
        "role": "Temperatura de superficie con control de calidad.",
    },
    {
        "id": "ERA5-LAND",
        "citation": "ECMWF ERA5-Land Monthly Aggregated.",
        "role": "Contexto climatico multianual cuando corresponde.",
    },
]

HUMEDAL_REFERENCE = {
    "id": "MMA-HU-2022",
    "citation": (
        "MMA - ONU Medio Ambiente (2022). Guia de Delimitacion y "
        "Caracterizacion de Humedales Urbanos de Chile."
    ),
    "role": (
        "Sensores remotos como apoyo de gabinete; limites y condiciones deben "
        "confirmarse/ajustarse con criterios de hidrologia, vegetacion y suelos "
        "cuando corresponda."
    ),
}

def build_canonical_report(
    project_data: Dict[str, Any],
    spectral_analysis: Dict[str, Any],
    diagnosis: Dict[str, Any],
    temporal_bundle: Optional[Dict[str, Any]] = None,
    ancillary: Optional[Dict[str, Any]] = None,
    regulatory: Optional[Dict[str, Any]] = None,
    author: str = "Loreto Campos Carrasco",
    role: str = "Directora Tecnica - BioCore Intelligence",
) -> Dict[str, Any]:
    """
    Construye un objeto inmutable en concepto: una vez calculado el analysis_id,
    PDF y Telegram deben leer exactamente este objeto.
    """
    project_data = deepcopy(project_data or {})
    spectral_analysis = deepcopy(spectral_analysis or {})
    diagnosis = deepcopy(diagnosis or {})
    temporal_bundle = deepcopy(temporal_bundle or {})
    ancillary = deepcopy(ancillary or {})
    regulatory = deepcopy(regulatory or {})

    project_type = str(project_data.get("Tipo") or "GENERAL").upper()
    project_name = str(project_data.get("Proyecto") or "N/D")
    baseline_year = project_data.get("ano_linea_base")

    comparison = spectral_analysis.get("comparison") or {}
    current = comparison.get("current") or {}
    baseline = comparison.get("baseline") or {}
    changes = comparison.get("changes") or {}
    comp_meta = comparison.get("meta") or {}

    snapshot = spectral_analysis.get("snapshot_s2") or {}
    snapshot_meta = snapshot.get("meta") or {}
    snapshot_context = snapshot.get("context") or {}
    lst = spectral_analysis.get("lst") or {}

    metrics = [
        {
            "code": "NDVI",
            "variable": "Indice de vegetacion de diferencia normalizada",
            "unit": "adimensional",
            "current": current.get("ndvi"),
            "baseline": baseline.get("ndvi"),
            "delta": (changes.get("ndvi") or {}).get("delta"),
            "pct": (changes.get("ndvi") or {}).get("pct"),
            "interpretation_scope": "Verdor/cobertura fotosintetica; no prueba degradacion por si solo.",
        },
        {
            "code": "SAVI",
            "variable": "Indice de vegetacion ajustado al suelo",
            "unit": "adimensional",
            "current": current.get("savi"),
            "baseline": baseline.get("savi"),
            "delta": (changes.get("savi") or {}).get("delta"),
            "pct": (changes.get("savi") or {}).get("pct"),
            "interpretation_scope": "Respuesta de vegetacion reduciendo influencia del brillo del suelo.",
        },
        {
            "code": "NDWI",
            "variable": "Indice Green-NIR para senal de agua superficial",
            "unit": "adimensional",
            "current": current.get("ndwi"),
            "baseline": baseline.get("ndwi"),
            "delta": (changes.get("ndwi") or {}).get("delta"),
            "pct": (changes.get("ndwi") or {}).get("pct"),
            "interpretation_scope": "Senal espectral de agua superficial; no identifica origen ni causalidad.",
        },
        {
            "code": "NDMI",
            "variable": "Indice NIR-SWIR1 para senal de humedad",
            "unit": "adimensional",
            "current": current.get("ndmi"),
            "baseline": baseline.get("ndmi"),
            "delta": (changes.get("ndmi") or {}).get("delta"),
            "pct": (changes.get("ndmi") or {}).get("pct"),
            "interpretation_scope": "Senal de humedad; no equivale a agua acumulada.",
        },
        {
            "code": "NDSI",
            "variable": "Indice Green-SWIR1 para senal de nieve/hielo",
            "unit": "adimensional",
            "current": current.get("ndsi"),
            "baseline": baseline.get("ndsi"),
            "delta": (changes.get("ndsi") or {}).get("delta"),
            "pct": (changes.get("ndsi") or {}).get("pct"),
            "interpretation_scope": "Compatible con nieve/hielo; no demuestra glaciar ni balance de masa.",
        },
        {
            "code": "SWIR1",
            "variable": "Reflectancia superficial SWIR1",
            "unit": "reflectancia",
            "current": current.get("swir1"),
            "baseline": baseline.get("swir1"),
            "delta": (changes.get("swir1") or {}).get("delta"),
            "pct": (changes.get("swir1") or {}).get("pct"),
            "interpretation_scope": "Respuesta espectral SWIR; no confirma propiedades geotecnicas.",
        },
        {
            "code": "SWIR1/SWIR2",
            "variable": "Razon espectral SWIR1/SWIR2",
            "unit": "adimensional",
            "current": current.get("swir_ratio"),
            "baseline": baseline.get("swir_ratio"),
            "delta": (changes.get("swir_ratio") or {}).get("delta"),
            "pct": (changes.get("swir_ratio") or {}).get("pct"),
            "interpretation_scope": "Razon espectral; no se denomina contenido de arcilla sin calibracion.",
        },
    ]

    references = list(BASE_REFERENCES)
    if project_type == "HUMEDAL":
        references.append(HUMEDAL_REFERENCE)

    quality = diagnosis.get("quality") or {}
    warnings = list(quality.get("warnings") or [])
    for w in spectral_analysis.get("warnings") or []:
        if w not in warnings:
            warnings.append(w)
    for w in temporal_bundle.get("warnings") or []:
        if w not in warnings:
            warnings.append(w)

    report = {
        "schema_version": SCHEMA_VERSION,
        "report_method_version": REPORT_METHOD_VERSION,
        "generated_at": _now_iso(),
        "project": {
            "name": project_name,
            "type": project_type,
            "baseline_year": baseline_year,
            "responsible": author,
            "role": role,
        },
        "scope": {
            "title": "Informe técnico de vigilancia ambiental satelital",
            "purpose": (
                "Apoyar la vigilancia y el seguimiento mediante evidencia de "
                "teledeteccion trazable. No sustituye evaluacion ambiental, "
                "inspeccion en terreno, plan de seguimiento aprobado ni "
                "pronunciamiento de la autoridad."
            ),
            "regulatory_scope": diagnosis.get("regulatory_statement"),
        },
        "data_traceability": {
            "comparison_sensor": comp_meta.get("sensor"),
            "comparison_collection": comp_meta.get("collection"),
            "comparison_scale_m": comp_meta.get("scale_m"),
            "current_window_start": comp_meta.get("current_window_start"),
            "current_window_end": comp_meta.get("current_window_end"),
            "baseline_window_start": comp_meta.get("baseline_window_start"),
            "baseline_window_end": comp_meta.get("baseline_window_end"),
            "current_n_scenes": comp_meta.get("current_n_scenes"),
            "baseline_n_scenes": comp_meta.get("baseline_n_scenes"),
            "comparison_rule": comp_meta.get("comparison_rule"),
            "latest_sentinel2_date": snapshot_meta.get("latest_scene_date"),
            "sentinel2_scale_m": snapshot_meta.get("scale_m"),
            "sentinel2_n_scenes": snapshot_meta.get("n_scenes"),
            "sentinel2_mask": snapshot_meta.get("mask"),
            "lst_collection": lst.get("collection"),
            "lst_window_start": lst.get("window_start"),
            "lst_window_end": lst.get("window_end"),
            "lst_scale_m": lst.get("scale_m"),
            "lst_qa": lst.get("qa"),
        },
        "quality": {
            "usable": quality.get("usable"),
            "valid_fraction": current.get("valid_fraction"),
            "warnings": warnings,
        },
        "metrics": metrics,
        "scene_context": {
            "scl_vegetation_fraction": snapshot_context.get("scl_vegetation_fraction"),
            "scl_bare_fraction": snapshot_context.get("scl_bare_fraction"),
            "scl_water_fraction": snapshot_context.get("scl_water_fraction"),
            "scl_snow_ice_fraction": snapshot_context.get("scl_snow_ice_fraction"),
            "lst_mean_c": lst.get("mean_c"),
            "lst_sd_c": lst.get("sd_c"),
        },
        "diagnosis": {
            "level": diagnosis.get("level"),
            "title": diagnosis.get("title"),
            "findings": diagnosis.get("findings") or [],
            "threshold_checks": diagnosis.get("threshold_checks") or [],
            "recommendations": diagnosis.get("recommendations") or [],
            "sea_alignment": diagnosis.get("sea_alignment") or {},
            "carbon_statement": diagnosis.get("carbon_statement"),
            "cryosphere_statement": diagnosis.get("cryosphere_statement"),
        },
        "temporal": {
            "range_label": temporal_bundle.get("range_label"),
            "mode": temporal_bundle.get("mode"),
            "n_spectral_records": len(temporal_bundle.get("spectral_records") or []),
            "n_climate_records": len(temporal_bundle.get("climate_records") or []),
            "rules": temporal_bundle.get("rules") or {},
        },
        "ancillary": ancillary,
        "regulatory": regulatory,
        "references": references,
    }

    # El ID se calcula sin incluirse a si mismo.
    report["analysis_id"] = compute_analysis_id(report)
    validate_canonical_report(report)
    return report

def validate_canonical_report(report: Dict[str, Any]) -> None:
    required = [
        "schema_version",
        "report_method_version",
        "generated_at",
        "project",
        "data_traceability",
        "quality",
        "metrics",
        "diagnosis",
        "analysis_id",
    ]
    missing = [k for k in required if k not in report]
    if missing:
        raise ValueError(f"Reporte canonico incompleto. Faltan: {missing}")

    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Version de esquema no soportada")

    if len(str(report["analysis_id"])) != 64:
        raise ValueError("analysis_id invalido")

    metric_codes = [m.get("code") for m in report.get("metrics", [])]
    if len(metric_codes) != len(set(metric_codes)):
        raise ValueError("Metricas duplicadas")

    # Regla anti-ficcion: un faltante debe ser None/N-D, nunca se exige 0.
    for metric in report.get("metrics", []):
        for field in ("current", "baseline", "delta", "pct"):
            value = metric.get(field)
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(
                    f"Valor no numerico en {metric.get('code')}::{field}: {value}"
                )

def verify_analysis_id(report: Dict[str, Any]) -> bool:
    copy = deepcopy(report)
    expected = copy.pop("analysis_id", None)
    return bool(expected) and expected == compute_analysis_id(copy)

def to_supabase_record(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Requiere la migracion SQL entregada junto con este archivo.
    payload_json es la fuente de verdad completa.
    """
    validate_canonical_report(report)

    trace = report.get("data_traceability") or {}
    project = report.get("project") or {}
    diagnosis = report.get("diagnosis") or {}

    return {
        "proyecto": project.get("name"),
        "tipo": project.get("type"),
        "ano_linea_base": project.get("baseline_year"),
        "schema_version": report.get("schema_version"),
        "method_version": report.get("report_method_version"),
        "analysis_id": report.get("analysis_id"),
        "source_data_date": trace.get("latest_sentinel2_date")
            or trace.get("current_window_end"),
        "estado": diagnosis.get("level"),
        "nivel": diagnosis.get("level"),
        "diagnostico": diagnosis.get("title"),
        "payload_json": report,
        "created_at": report.get("generated_at"),
    }

def format_telegram(report: Dict[str, Any]) -> str:
    """Mensaje en texto plano. Evita errores de parseo Markdown con nombres libres."""
    validate_canonical_report(report)

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
    if any(context.get(k) is not None for k in (
        "scl_vegetation_fraction", "scl_water_fraction", "scl_snow_ice_fraction", "lst_mean_c"
    )):
        lines += ["", "🧩 Contexto de escena Sentinel-2"]
        if context.get("scl_vegetation_fraction") is not None:
            lines.append(f"• Vegetación SCL: {float(context['scl_vegetation_fraction']):.1%}")
        if context.get("scl_water_fraction") is not None:
            lines.append(f"• Agua SCL: {float(context['scl_water_fraction']):.1%}")
        if context.get("scl_snow_ice_fraction") is not None:
            lines.append(f"• Nieve/hielo SCL: {float(context['scl_snow_ice_fraction']):.1%}")
        if context.get("lst_mean_c") is not None:
            lines.append(f"• LST MODIS: {float(context['lst_mean_c']):.1f} °C")

    ancillary = report.get("ancillary") or {}
    s1 = ancillary.get("sentinel1") or {}
    fire = ancillary.get("active_fire") or {}

    if s1.get("available"):
        lines += ["", "📡 Sentinel-1 SAR complementario"]
        for key in ("ascending", "descending"):
            block = (s1.get("passes") or {}).get(key)
            if not block:
                continue
            cur = block.get("current") or {}
            base = block.get("baseline") or {}
            ch = block.get("change") or {}
            orbit = block.get("relative_orbit")
            lines.append(
                f"• {block.get('orbit_pass')} / órbita {orbit if orbit is not None else 'N/D'}: "
                f"VV {_fmt(cur.get('vv_db'), 2, ' dB')} | ref {_fmt(base.get('vv_db'), 2, ' dB')} | "
                f"Δ {_fmt(ch.get('vv_db'), 2, ' dB')}"
            )
        lines.append("• Alcance: evidencia radar complementaria; no determina litología, agua, operación ni estabilidad por sí sola.")

    if fire.get("available"):
        lines += ["", "🔥 Anomalías térmicas VIIRS (alerta NRT)"]
        lines.append(f"• Ventana: {fire.get('window_start') or 'N/D'} a {fire.get('window_end') or 'N/D'}")
        lines.append(f"• Detecciones píxel-día nominales/altas: {fire.get('combined_nominal_high_pixel_days', 0)}")
        lines.append(f"• Detecciones píxel-día de alta confianza: {fire.get('combined_high_confidence_pixel_days', 0)}")
        lines.append("• No equivale a número de incendios únicos; producto casi en tiempo real.")

    insar = ancillary.get("insar") or {}
    if insar.get("available"):
        lines += ["", "🗻 InSAR Sentinel-1 SLC"]
        lines.append(f"• Periodo: {insar.get('period_start') or 'N/D'} a {insar.get('period_end') or 'N/D'}")
        lines.append(f"• Coherencia media: {_fmt(insar.get('coherence_mean'), 3)}")
        lines.append(f"• Desplazamiento LOS medio: {_fmt(insar.get('los_displacement_mm_mean'), 2, ' mm')}")
        lines.append("• El desplazamiento LOS no equivale por sí solo a inestabilidad geotécnica ni movimiento 3D.")

    geotech = ancillary.get("geotechnical") or {}
    if geotech.get("available"):
        lines += ["", "📡 Cobertura de radar geotécnico (simulación geométrica)"]
        lines.append(f"• Cobertura visible: {_fmt(geotech.get('coverage_pct'), 1, '%')}")
        lines.append(f"• Zonas de sombra: {geotech.get('shadow_zone_count', 'N/D')}")
        lines.append(f"• Sensibilidad geométrica media: {_fmt(geotech.get('geometric_quality_mean'), 3)}")
        lines.append("• No representa SNR real ni certifica estabilidad de taludes.")

    regulatory = report.get("regulatory") or {}
    if regulatory:
        lines += ["", "⚖️ Seguimiento regulatorio configurado"]
        lines.append(f"• Estado: {regulatory.get('status') or 'N/D'}")
        if regulatory.get("plan_name"):
            lines.append(f"• Plan: {regulatory.get('plan_name')}")
        if regulatory.get("summary"):
            lines.append(f"• {regulatory.get('summary')}")
        for ob in (regulatory.get("reporting_obligations") or [])[:3]:
            lines.append(
                f"• Reporte configurado a {ob.get('report_to') or 'N/D'} | "
                f"plazo: {ob.get('deadline') or 'N/D'} | regla: {ob.get('rule_id') or 'N/D'}"
            )

    warnings = quality.get("warnings") or []
    if warnings:
        lines += ["", "⚠️ Calidad / limitaciones"]
        for w in warnings[:4]:
            lines.append(f"• {w}")

    recs = diagnosis.get("recommendations") or []
    if recs:
        lines += ["", "💡 Acciones recomendadas"]
        for rec in recs[:4]:
            lines.append(f"• {rec}")

    lines += [
        "",
        "ℹ️ Este reporte es evidencia de vigilancia satelital; no determina por sí solo impacto significativo, causalidad, incumplimiento ni obligación de notificación.",
        f"🔐 Verificación: PDF y Telegram deben mostrar el mismo ID {_short_id(report)}.",
    ]
    return "\n".join(lines)

class BioCorePDF(FPDF):
    def __init__(self, report: Dict[str, Any]):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.report = report
        self.set_auto_page_break(auto=True, margin=17)
        self.alias_nb_pages()

    def header(self):
        self.set_fill_color(23, 55, 82)
        self.rect(0, 0, 210, 24, "F")
        self.set_xy(12, 7)
        self.set_text_color(255, 255, 255)
        self.set_font("helvetica", "B", 12)
        self.cell(0, 5, "BIOCORE INTELLIGENCE - VIGILANCIA AMBIENTAL")
        self.set_xy(12, 14)
        self.set_font("helvetica", "", 7.5)
        self.cell(
            0,
            4,
            _clean_pdf_text(
                f"Analysis ID: {_short_id(self.report)} | "
                f"Esquema: {self.report.get('schema_version')}"
            ),
        )
        self.ln(16)

    def footer(self):
        self.set_y(-11)
        self.set_text_color(110, 110, 110)
        self.set_font("helvetica", "I", 7)
        self.cell(
            0,
            4,
            _clean_pdf_text(
                f"BioCore Intelligence | {_short_id(self.report)} | "
                f"Página {self.page_no()}/{{nb}}"
            ),
            align="C",
        )

def _pdf_section(pdf: FPDF, title: str):
    pdf.ln(2)
    pdf.set_text_color(23, 55, 82)
    pdf.set_font("helvetica", "B", 11)
    pdf.multi_cell(
        0, 6, _clean_pdf_text(title),
        new_x="LMARGIN", new_y="NEXT"
    )
    pdf.set_draw_color(185, 195, 205)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)

def _pdf_body(pdf: FPDF, text: Any, size: float = 8.5, bold=False):
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("helvetica", "B" if bold else "", size)
    pdf.multi_cell(
        0, 4.6, _clean_pdf_text(text),
        new_x="LMARGIN", new_y="NEXT"
    )

def _pdf_kv(pdf: FPDF, key: str, value: Any):
    pdf.set_font("helvetica", "B", 8.3)
    pdf.set_text_color(35, 35, 35)
    pdf.cell(48, 5, _clean_pdf_text(key))
    pdf.set_font("helvetica", "", 8.3)
    pdf.multi_cell(
        0, 5,
        _clean_pdf_text(value if value not in (None, "") else "N/D"),
        new_x="LMARGIN", new_y="NEXT"
    )

def _pdf_metrics_table(pdf: FPDF, metrics: Iterable[Dict[str, Any]]):
    widths = [25, 29, 29, 31, 76]
    headers = ["Indicador", "Actual", "Referencia", "Cambio", "Alcance interpretativo"]

    pdf.set_fill_color(47, 89, 126)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 7)
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, _clean_pdf_text(h), border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_text_color(20, 20, 20)
    pdf.set_font("helvetica", "", 6.7)

    for m in metrics:
        code = m.get("code", "N/D")
        current = _fmt(m.get("current"))
        baseline = _fmt(m.get("baseline"))
        change = "N/D"
        if m.get("delta") is not None:
            change = f"{float(m['delta']):+.4f}"
            if m.get("pct") is not None:
                change += f" ({float(m['pct']):+.1f}%)"
        scope = _clean_pdf_text(m.get("interpretation_scope", ""))

        # Altura dinamica aproximada segun columna larga.
        chars_per_line = 48
        lines = max(1, (len(scope) // chars_per_line) + 1)
        h = max(7, lines * 4)

        x = pdf.get_x()
        y = pdf.get_y()
        vals = [code, current, baseline, change]

        for idx, (w, val) in enumerate(zip(widths[:4], vals)):
            pdf.rect(x + sum(widths[:idx]), y, w, h)
            pdf.set_xy(x + sum(widths[:idx]), y + 1)
            pdf.multi_cell(w, 3.7, _clean_pdf_text(val), align="C")

        x_scope = x + sum(widths[:4])
        pdf.rect(x_scope, y, widths[4], h)
        pdf.set_xy(x_scope + 1, y + 1)
        pdf.multi_cell(widths[4] - 2, 3.7, scope)

        pdf.set_xy(x, y + h)

def render_pdf(
    report: Dict[str, Any],
    output_path: str,
    chart_paths: Optional[List[str]] = None,
) -> str:
    validate_canonical_report(report)

    pdf = BioCorePDF(report)
    pdf.add_page()

    project = report["project"]
    diagnosis = report["diagnosis"]
    trace = report["data_traceability"]
    quality = report["quality"]
    context = report.get("scene_context") or {}

    pdf.set_text_color(23, 55, 82)
    pdf.set_font("helvetica", "B", 15)
    pdf.multi_cell(
        0, 8,
        _clean_pdf_text("INFORME TECNICO DE VIGILANCIA AMBIENTAL SATELITAL"),
        new_x="LMARGIN", new_y="NEXT"
    )
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(80, 80, 80)
    pdf.multi_cell(
        0,
        4.5,
        _clean_pdf_text(
            "Documento técnico de apoyo al seguimiento. No constituye por sí solo "
            "una determinación de impacto significativo, cumplimiento, causalidad "
            "ni obligación de notificación."
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )

    _pdf_section(pdf, "1. Identificación y trazabilidad")
    _pdf_kv(pdf, "Proyecto", project.get("name"))
    _pdf_kv(pdf, "Tipo", project.get("type"))
    _pdf_kv(pdf, "Responsable", project.get("responsible"))
    _pdf_kv(pdf, "Año de referencia", project.get("baseline_year"))
    _pdf_kv(pdf, "ID de análisis", report.get("analysis_id"))
    _pdf_kv(pdf, "Fecha de generación", report.get("generated_at"))

    _pdf_section(pdf, "2. Estado de vigilancia")
    _pdf_kv(pdf, "Nivel", diagnosis.get("level"))
    _pdf_kv(pdf, "Síntesis", diagnosis.get("title"))
    _pdf_body(pdf, report["scope"].get("purpose"))

    _pdf_section(pdf, "3. Datos, método y escalas")
    _pdf_kv(pdf, "Sensor comparable", trace.get("comparison_sensor"))
    _pdf_kv(pdf, "Colección", trace.get("comparison_collection"))
    _pdf_kv(pdf, "Resolución de análisis", (
        f"{trace.get('comparison_scale_m')} m"
        if trace.get("comparison_scale_m") is not None else "N/D"
    ))
    _pdf_kv(
        pdf,
        "Período actual",
        f"{trace.get('current_window_start') or 'N/D'} a "
        f"{trace.get('current_window_end') or 'N/D'}",
    )
    _pdf_kv(
        pdf,
        "Período referencia",
        f"{trace.get('baseline_window_start') or 'N/D'} a "
        f"{trace.get('baseline_window_end') or 'N/D'}",
    )
    _pdf_kv(pdf, "Regla de comparación", trace.get("comparison_rule"))
    _pdf_kv(pdf, "Escenas actual/referencia", (
        f"{trace.get('current_n_scenes') or 'N/D'} / "
        f"{trace.get('baseline_n_scenes') or 'N/D'}"
    ))

    _pdf_section(pdf, "4. Calidad de la evidencia")
    vf = quality.get("valid_fraction")
    _pdf_kv(pdf, "Fracción válida", f"{vf:.1%}" if vf is not None else "N/D")
    warnings = quality.get("warnings") or []
    if warnings:
        for w in warnings:
            _pdf_body(pdf, f"- {w}", size=8)
    else:
        _pdf_body(pdf, "No se registraron advertencias adicionales de calidad.")

    _pdf_section(pdf, "5. Indicadores espectrales comparables")
    _pdf_metrics_table(pdf, report.get("metrics") or [])

    _pdf_section(pdf, "6. Contexto de escena reciente")
    for label, key in [
        ("Fracción SCL vegetación", "scl_vegetation_fraction"),
        ("Fracción SCL suelo desnudo", "scl_bare_fraction"),
        ("Fracción SCL agua", "scl_water_fraction"),
        ("Fracción SCL nieve/hielo", "scl_snow_ice_fraction"),
    ]:
        value = context.get(key)
        _pdf_kv(pdf, label, f"{value:.1%}" if value is not None else "N/D")
    _pdf_kv(
        pdf,
        "LST MODIS",
        (
            f"{context.get('lst_mean_c'):.1f} °C"
            if context.get("lst_mean_c") is not None else "N/D"
        ),
    )

    _pdf_section(pdf, "7. Evidencia complementaria SAR y anomalías térmicas")
    ancillary = report.get("ancillary") or {}
    s1 = ancillary.get("sentinel1") or {}
    fire = ancillary.get("active_fire") or {}

    if s1.get("available"):
        _pdf_body(
            pdf,
            "Sentinel-1 GRD se utiliza como evidencia complementaria. "
            "La interpretación prioriza cambios temporales con geometría orbital comparable.",
            size=8,
        )
        for key in ("ascending", "descending"):
            block = (s1.get("passes") or {}).get(key)
            if not block:
                continue
            cur = block.get("current") or {}
            base = block.get("baseline") or {}
            ch = block.get("change") or {}
            _pdf_body(
                pdf,
                f"- {block.get('orbit_pass')} | órbita relativa "
                f"{block.get('relative_orbit') if block.get('relative_orbit') is not None else 'N/D'} | "
                f"VV actual {_fmt(cur.get('vv_db'), 2, ' dB')} | "
                f"referencia {_fmt(base.get('vv_db'), 2, ' dB')} | "
                f"Delta {_fmt(ch.get('vv_db'), 2, ' dB')}. "
                f"Comparabilidad: {block.get('comparability')}",
                size=7.7,
            )
        for w in s1.get("warnings") or []:
            _pdf_body(pdf, f"- Limitación SAR: {w}", size=7.4)
    else:
        _pdf_body(pdf, "Sentinel-1: sin datos complementarios válidos en esta ejecución.", size=8)

    if fire.get("available"):
        _pdf_body(pdf, "VIIRS Active Fire - alerta casi en tiempo real", bold=True)
        _pdf_body(
            pdf,
            f"- Ventana: {fire.get('window_start') or 'N/D'} a "
            f"{fire.get('window_end') or 'N/D'}.",
            size=7.7,
        )
        _pdf_body(
            pdf,
            f"- Detecciones píxel-día nominales/altas: "
            f"{fire.get('combined_nominal_high_pixel_days', 0)}.",
            size=7.7,
        )
        _pdf_body(
            pdf,
            f"- Detecciones píxel-día de alta confianza: "
            f"{fire.get('combined_high_confidence_pixel_days', 0)}.",
            size=7.7,
        )
        _pdf_body(pdf, f"- {fire.get('interpretation') or ''}", size=7.4)
        _pdf_body(pdf, f"- {fire.get('quality_note') or ''}", size=7.4)
    else:
        _pdf_body(pdf, "VIIRS Active Fire: sin datos disponibles en esta ejecución.", size=8)

    insar = ancillary.get("insar") or {}
    if insar.get("available"):
        _pdf_body(pdf, "InSAR Sentinel-1 SLC", bold=True)
        _pdf_body(pdf, f"- Periodo: {insar.get('period_start') or 'N/D'} a {insar.get('period_end') or 'N/D'}.", size=7.7)
        _pdf_body(pdf, f"- Procesador: {insar.get('processor') or 'N/D'}; pasada: {insar.get('orbit_pass') or 'N/D'}; órbita relativa: {insar.get('relative_orbit') or 'N/D'}.", size=7.7)
        _pdf_body(pdf, f"- Coherencia media: {_fmt(insar.get('coherence_mean'), 3)}.", size=7.7)
        _pdf_body(pdf, f"- Desplazamiento LOS medio: {_fmt(insar.get('los_displacement_mm_mean'), 2, ' mm')}; P95 |LOS|: {_fmt(insar.get('los_displacement_mm_p95'), 2, ' mm')}.", size=7.7)
        _pdf_body(pdf, f"- Alcance: {insar.get('interpretation_scope') or ''}", size=7.4)
    else:
        _pdf_body(pdf, "InSAR SLC: sin resultado procesado/importado asociado a este análisis.", size=7.7)

    geotech = ancillary.get("geotechnical") or {}
    if geotech.get("available"):
        _pdf_body(pdf, "Cobertura de radar geotécnico - simulación geométrica", bold=True)
        _pdf_body(pdf, f"- DEM: {geotech.get('dem_source') or 'N/D'}; resolución de malla ~{_fmt(geotech.get('grid_resolution_m'), 1, ' m')}.", size=7.7)
        _pdf_body(pdf, f"- Cobertura visible: {_fmt(geotech.get('coverage_pct'), 1, '%')}; área visible: {_fmt((geotech.get('visible_area_m2') or 0)/1_000_000, 3, ' km²')}.", size=7.7)
        _pdf_body(pdf, f"- Zonas de sombra: {geotech.get('shadow_zone_count', 'N/D')}; sensibilidad geométrica media: {_fmt(geotech.get('geometric_quality_mean'), 3)}.", size=7.7)
        _pdf_body(pdf, f"- Alcance: {geotech.get('interpretation_scope') or ''}", size=7.4)

    _pdf_section(pdf, "8. Hallazgos e interpretación técnica")
    findings = diagnosis.get("findings") or []
    if not findings:
        _pdf_body(pdf, "No hay hallazgos interpretables con la evidencia disponible.")
    for finding in findings:
        _pdf_body(pdf, finding.get("object", "Hallazgo").upper(), bold=True)
        for obs in finding.get("observations") or []:
            _pdf_body(pdf, f"- Observación: {obs}", size=8)
        for conf in finding.get("confounders") or []:
            _pdf_body(pdf, f"- Confusor/limitación: {conf}", size=8)
        _pdf_body(pdf, f"- Alcance: {finding.get('interpretation', '')}", size=8)
        pdf.ln(1)

    _pdf_section(pdf, "9. Umbrales específicos del proyecto")
    checks = diagnosis.get("threshold_checks") or []
    if not checks:
        _pdf_body(
            pdf,
            "No se cargaron umbrales específicos y trazables de RCA, permiso, "
            "plan de seguimiento o protocolo validado. Por esta razón BioCore "
            "no clasifica automáticamente un índice como incumplimiento.",
        )
    else:
        for c in checks:
            state = "ACTIVADO" if c.get("triggered") else "no activado"
            _pdf_body(
                pdf,
                f"- {c.get('metric')}: {state}. "
                f"{'; '.join(c.get('reasons') or [])}. "
                f"Fuente: {c.get('source') or 'N/D'}",
                size=8,
            )

    valid_charts = [p for p in (chart_paths or []) if p and Path(p).exists()]
    _pdf_section(pdf, "10. Series temporales")
    _pdf_body(
        pdf,
        (
            f"Rango: {report.get('temporal', {}).get('range_label') or 'N/D'}. "
            "Los gráficos sólo contienen observaciones/compuestos válidos; "
            "los faltantes no se rellenan con datos sintéticos."
        ),
        size=8,
    )
    if valid_charts:
        for chart in valid_charts:
            if pdf.get_y() > 215:
                pdf.add_page()
            pdf.image(chart, x=18, w=174)
            pdf.ln(4)
    else:
        _pdf_body(pdf, "No hay una serie temporal válida para graficar en esta ejecución.", size=8)

    _pdf_section(pdf, "11. Seguimiento regulatorio configurado")
    regulatory = report.get("regulatory") or {}
    if regulatory:
        _pdf_kv(pdf, "Estado", regulatory.get("status"))
        _pdf_kv(pdf, "Plan", regulatory.get("plan_name"))
        _pdf_kv(pdf, "Componente", regulatory.get("component"))
        _pdf_kv(pdf, "Variable ambiental", regulatory.get("environmental_variable"))
        _pdf_body(pdf, regulatory.get("summary") or "", size=8)

        checks = regulatory.get("checks") or []
        if checks:
            _pdf_body(pdf, "Reglas evaluadas:", bold=True)
            for c in checks:
                source = c.get("source") or {}
                source_label = " | ".join(
                    str(x) for x in [
                        source.get("tipo"),
                        source.get("identificador"),
                        source.get("seccion") or source.get("pagina") or source.get("clausula"),
                    ] if x
                )
                _pdf_body(
                    pdf,
                    f"- {c.get('rule_id')}: {c.get('status')} | "
                    f"Parámetro {c.get('parameter')} | "
                    f"{c.get('message') or ''} | Fuente: {source_label or 'N/D'}",
                    size=7.5,
                )

        obligations = regulatory.get("reporting_obligations") or []
        if obligations:
            _pdf_body(pdf, "Obligaciones de reporte cargadas:", bold=True)
            for ob in obligations:
                _pdf_body(
                    pdf,
                    f"- {ob.get('report_to') or 'N/D'} | "
                    f"Plazo: {ob.get('deadline') or 'N/D'} | "
                    f"Regla: {ob.get('rule_id') or 'N/D'}",
                    size=7.5,
                )
    else:
        _pdf_body(
            pdf,
            "No se cargó un instrumento regulatorio específico. "
            "BioCore Intelligence no infiere incumplimiento ni obligación de reporte.",
            size=8,
        )

    _pdf_section(pdf, "12. Recomendaciones y acción adaptativa")
    for rec in diagnosis.get("recommendations") or []:
        _pdf_body(pdf, f"- {rec}", size=8.3)

    _pdf_section(pdf, "13. Alcance regulatorio")
    _pdf_body(pdf, report["scope"].get("regulatory_scope") or "")
    if diagnosis.get("carbon_statement"):
        _pdf_body(pdf, diagnosis["carbon_statement"])
    if diagnosis.get("cryosphere_statement"):
        _pdf_body(pdf, diagnosis["cryosphere_statement"])

    _pdf_section(pdf, "14. Referencias metodológicas")
    for ref in report.get("references") or []:
        _pdf_body(
            pdf,
            f"[{ref.get('id')}] {ref.get('citation')} "
            f"Uso en este informe: {ref.get('role')}",
            size=7.5,
        )

    output = str(output_path)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    pdf.output(output)
    return output

def save_json(report: Dict[str, Any], output_path: str) -> str:
    validate_canonical_report(report)
    Path(output_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return str(output_path)


# ============================================================================
# INTEGRADO DESDE biocore_ui_v2.py
# ============================================================================
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
    generated_at = report.get("generated_at") or "N/D"
    if isinstance(generated_at, str) and "T" in generated_at:
        try:
            generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass

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
        "BioCore Intelligence separa observación, interpretación y obligación regulatoria. "
        "Una señal satelital no demuestra por sí sola causalidad, incumplimiento "
        "ni significancia ambiental."
    )

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

def render_ancillary(report: Dict[str, Any]):
    ancillary = report.get("ancillary") or {}
    s1 = ancillary.get("sentinel1") or {}
    fire = ancillary.get("active_fire") or {}
    insar = ancillary.get("insar") or {}
    geotech = ancillary.get("geotechnical") or {}

    st.subheader("Evidencia complementaria")

    t_sar, t_fire, t_insar, t_geo = st.tabs([
        "Sentinel-1 SAR", "VIIRS Active Fire", "InSAR SLC", "Radar geotécnico"
    ])

    with t_sar:
        if not s1.get("available"):
            st.info("No hay datos Sentinel-1 GRD comparables en esta ejecución.")
        else:
            st.caption(s1.get("interpretation_scope") or "Evidencia radar complementaria.")
            rows = []
            for key in ("ascending", "descending"):
                block = (s1.get("passes") or {}).get(key)
                if not block:
                    continue
                cur = block.get("current") or {}
                base = block.get("baseline") or {}
                change = block.get("change") or {}
                rows.append({
                    "Pasada": block.get("orbit_pass"),
                    "Órbita relativa": block.get("relative_orbit"),
                    "VV actual (dB)": _fmt(cur.get("vv_db"), 2),
                    "VV referencia (dB)": _fmt(base.get("vv_db"), 2),
                    "Δ VV (dB)": _fmt(change.get("vv_db"), 2),
                    "VH actual (dB)": _fmt(cur.get("vh_db"), 2),
                    "Ángulo incidencia": _fmt(cur.get("incidence_angle_deg"), 1, "°"),
                    "Comparabilidad": block.get("comparability"),
                })
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            for warning in s1.get("warnings") or []:
                st.caption(f"• {warning}")

    with t_fire:
        if not fire.get("available"):
            st.info("No hay información VIIRS disponible en esta ejecución.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Detección térmica", "Sí" if fire.get("detection") else "No")
            c2.metric("Píxel-día nominal/alto", str(fire.get("combined_nominal_high_pixel_days", 0)))
            c3.metric("Píxel-día alta confianza", str(fire.get("combined_high_confidence_pixel_days", 0)))
            st.caption(f"Ventana: {fire.get('window_start') or 'N/D'} a {fire.get('window_end') or 'N/D'}.")
            st.info(fire.get("interpretation") or "Las detecciones térmicas no equivalen a incendios únicos.")
            st.caption(fire.get("quality_note") or "")

    with t_insar:
        if not insar.get("available"):
            st.info(
                "No existe un resultado InSAR SLC asociado a este análisis. "
                "BioCore no sustituye fase SLC con Sentinel-1 GRD."
            )
            st.caption(insar.get("interpretation_scope") or "")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Coherencia media", _fmt(insar.get("coherence_mean"), 3))
            c2.metric("LOS medio", _fmt(insar.get("los_displacement_mm_mean"), 2, " mm"))
            c3.metric("P95 |LOS|", _fmt(insar.get("los_displacement_mm_p95"), 2, " mm"))
            st.caption(
                f"{insar.get('period_start') or 'N/D'} → {insar.get('period_end') or 'N/D'} · "
                f"{insar.get('processor') or 'procesador no informado'} · {insar.get('orbit_pass') or 'pasada N/D'}"
            )
            st.info(insar.get("interpretation_scope") or "")

    with t_geo:
        if not geotech.get("available"):
            st.info("No se ejecutó una simulación de cobertura de radar geotécnico antes de este análisis.")
            st.caption(geotech.get("interpretation_scope") or "")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Cobertura visible", _fmt(geotech.get("coverage_pct"), 1, "%"))
            c2.metric("Área visible", _fmt((geotech.get("visible_area_m2") or 0)/1_000_000, 3, " km²"))
            c3.metric("Zonas de sombra", str(geotech.get("shadow_zone_count", "N/D")))
            c4.metric("Sensibilidad geom.", _fmt(geotech.get("geometric_quality_mean"), 3))
            st.caption(
                f"DEM {geotech.get('dem_source') or 'N/D'} · malla ~{_fmt(geotech.get('grid_resolution_m'), 1, ' m')}"
            )
            st.info(geotech.get("interpretation_scope") or "")

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

def render_regulatory(report: Dict[str, Any]):
    regulatory = report.get("regulatory") or {}

    st.subheader("Estado regulatorio")

    if not regulatory:
        st.info(
            "No se cargó un instrumento regulatorio específico. "
            "BioCore Intelligence no infiere incumplimiento ni obligación de reporte."
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


# ============================================================================
# Orquestación integrada BioCore v2
# ============================================================================

RANGE_LABELS = [
    "Últimos 7 días",
    "Últimas 2 semanas",
    "Último mes",
    "Últimos 3 meses",
    "Último año",
    "Últimos 5 años",
    "Últimos 10 años",
    "Últimos 15 años",
    "Últimos 20 años",
]


def _formal_comparison_days(range_label: str) -> int:
    return {
        "Últimos 7 días": 7,
        "Últimas 2 semanas": 14,
        "Último mes": 30,
        "Últimos 3 meses": 90,
    }.get(range_label, 30)


def _parse_monitoring_plan(project_data: Dict[str, Any]):
    raw = project_data.get("monitoring_plan")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def _first_s1_vv(ancillary: Dict[str, Any]):
    s1 = (ancillary or {}).get("sentinel1") or {}
    for key in ("ascending", "descending"):
        block = (s1.get("passes") or {}).get(key) or {}
        value = ((block.get("current") or {}).get("vv_db"))
        if value is not None:
            return value
    return None


def guardar_reporte_canonico(report: Dict[str, Any]):
    """Guarda el JSON canónico y mantiene columnas históricas conocidas sin inventar datos."""
    validate_canonical_report(report)
    metric = {m.get("code"): m for m in report.get("metrics") or []}
    project = report.get("project") or {}
    diagnosis = report.get("diagnosis") or {}
    context = report.get("scene_context") or {}
    trace = report.get("data_traceability") or {}
    ancillary = report.get("ancillary") or {}
    fire = ancillary.get("active_fire") or {}

    def v(code, field="current"):
        return (metric.get(code) or {}).get(field)

    record = {
        "proyecto": project.get("name"),
        "tipo": project.get("type"),
        "fecha_analisis": str(report.get("generated_at") or "")[:10],
        "savi_actual": v("SAVI"),
        "ndwi_actual": v("NDWI"),
        "ndsi_actual": v("NDSI"),
        "ndvi_actual": v("NDVI"),
        "swir": v("SWIR1"),
        "clay": None,
        "sar_vv": _first_s1_vv(ancillary),
        "incendios": fire.get("combined_nominal_high_pixel_days"),
        "temperatura": context.get("lst_mean_c"),
        "variacion_savi": v("SAVI", "pct"),
        "variacion_ndwi": v("NDWI", "pct"),
        "variacion_ndsi": v("NDSI", "pct"),
        "variacion_ndvi": v("NDVI", "pct"),
        "estado": diagnosis.get("level"),
        "nivel": diagnosis.get("level"),
        "diagnostico": diagnosis.get("title"),
        "ano_linea_base": project.get("baseline_year"),
        "schema_version": report.get("schema_version"),
        "method_version": report.get("report_method_version"),
        "analysis_id": report.get("analysis_id"),
        "source_data_date": trace.get("latest_sentinel2_date") or trace.get("current_window_end"),
        "payload_json": report,
        "created_at": report.get("generated_at"),
    }

    try:
        supabase.table("historial_reportes").insert(record).execute()
        return True, None
    except Exception as exc:
        msg = str(exc)
        migration_markers = (
            "schema_version",
            "method_version",
            "analysis_id",
            "source_data_date",
            "payload_json",
        )
        if any(marker in msg for marker in migration_markers):
            return False, (
                "El análisis se generó correctamente, pero todavía no puede guardarse "
                "en Supabase porque falta aplicar la migración del reporte canónico. "
                "El PDF y la vista del análisis siguen disponibles; el historial y "
                "Telegram automático quedarán activos después de ejecutar el SQL."
            )
        raise


def ejecutar_analisis_canonico(
    project_data: Dict[str, Any],
    range_label: str,
    save: bool = True,
):
    if not GEE_OK:
        raise RuntimeError("Google Earth Engine no está inicializado.")

    coords = obtener_coordenadas_correctamente(project_data)
    geom = ee.Geometry.Polygon([coords])
    baseline_year = int(project_data.get("ano_linea_base") or 2015)
    comparison_days = _formal_comparison_days(range_label)

    spectral = build_optical_analysis(
        geom=geom,
        baseline_year=baseline_year,
        requested_days=comparison_days,
    )

    temporal = build_temporal_bundle(
        geom=geom,
        range_label=range_label,
    )

    diagnosis = build_surveillance_diagnosis(
        spectral_analysis=spectral,
        project_data=project_data,
    )

    ancillary_warnings = []
    try:
        ancillary = build_ancillary_analysis(
            geom=geom,
            baseline_year=baseline_year,
            sar_days=max(30, comparison_days),
            fire_days=7,
        )
    except Exception as exc:
        ancillary = {}
        ancillary_warnings.append(
            f"Evidencia complementaria SAR/VIIRS no disponible en esta ejecución: {exc}"
        )

    # Incorporar sólo resultados avanzados realmente ejecutados/importados.
    advanced = _advanced_context_for_project(project_data)
    ancillary.update(advanced)

    if ancillary_warnings:
        diagnosis.setdefault("quality", {}).setdefault("warnings", []).extend(ancillary_warnings)

    canonical_base = build_canonical_report(
        project_data=project_data,
        spectral_analysis=spectral,
        diagnosis=diagnosis,
        temporal_bundle=temporal,
        ancillary=ancillary,
        regulatory={},
    )

    regulatory = evaluate_monitoring_plan(
        canonical_report=canonical_base,
        plan=_parse_monitoring_plan(project_data),
    )

    canonical = build_canonical_report(
        project_data=project_data,
        spectral_analysis=spectral,
        diagnosis=diagnosis,
        temporal_bundle=temporal,
        ancillary=ancillary,
        regulatory=regulatory,
    )

    chart_dir = os.path.join(
        tempfile.gettempdir(),
        f"biocore_{canonical['analysis_id'][:12]}",
    )
    chart_paths = generate_report_charts(
        bundle=temporal,
        project_type=project_data.get("Tipo", "GENERAL"),
        output_dir=chart_dir,
    )

    st.session_state.pop("_biocore_save_warning", None)
    if save:
        saved, save_warning = guardar_reporte_canonico(canonical)
        if not saved and save_warning:
            st.session_state["_biocore_save_warning"] = save_warning

    return canonical, temporal, chart_paths


def enviar_telegram_canonico(report: Dict[str, Any], project_data: Dict[str, Any]):
    chat_id = project_data.get("id_telegram")
    if not chat_id:
        return False, "El proyecto no tiene ID de Telegram configurado."

    token = st.secrets.get("telegram", {}).get("token", "")
    if not token:
        return False, "El token de Telegram no está configurado."

    if not verify_analysis_id(report):
        return False, "El analysis_id no coincide con el contenido; envío bloqueado."

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": str(chat_id),
            "text": format_telegram(report),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if response.status_code == 200:
        return True, "Reporte canónico enviado por Telegram."
    return False, f"Telegram respondió HTTP {response.status_code}: {response.text[:180]}"


def _project_by_name(name: str) -> Optional[Dict[str, Any]]:
    try:
        result = supabase.table("usuarios").select("*").eq("Proyecto", name).execute()
        return result.data[0] if result.data else None
    except Exception:
        return None


def _accessible_projects() -> List[Dict[str, Any]]:
    try:
        all_projects = supabase.table("usuarios").select("*").execute().data or []
    except Exception:
        all_projects = []

    if st.session_state.get("admin_mode"):
        return all_projects
    target = st.session_state.get("proyecto_cliente")
    return [p for p in all_projects if p.get("Proyecto") == target]


def _store_analysis(prefix: str, report, temporal, charts, project_data):
    st.session_state[f"{prefix}_report"] = report
    st.session_state[f"{prefix}_temporal"] = temporal
    st.session_state[f"{prefix}_charts"] = charts
    st.session_state[f"{prefix}_project"] = project_data


def _render_stored_analysis(prefix: str):
    report = st.session_state.get(f"{prefix}_report")
    if not report:
        return
    temporal = st.session_state.get(f"{prefix}_temporal") or {}
    charts = st.session_state.get(f"{prefix}_charts") or []
    project_data = st.session_state.get(f"{prefix}_project") or {}

    render_analysis_dashboard(
        report=report,
        temporal_bundle=temporal,
        chart_paths=charts,
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("📡 Enviar este mismo análisis por Telegram", key=f"send_{prefix}", use_container_width=True):
            ok, message = enviar_telegram_canonico(report, project_data)
            if ok:
                st.success(message)
            else:
                st.error(message)
    with c2:
        if st.button("🧹 Cerrar resultado", key=f"clear_{prefix}", use_container_width=True):
            for suffix in ("report", "temporal", "charts", "project"):
                st.session_state.pop(f"{prefix}_{suffix}", None)
            st.rerun()


def mostrar_guia_v2():
    st.title("📖 Guía de uso y alcance")
    st.markdown(
        "BioCore Intelligence es una herramienta de **vigilancia ambiental satelital**. "
        "Apoya el seguimiento, pero no sustituye campañas de terreno, evaluación ambiental "
        "ni pronunciamientos de la autoridad."
    )

    with st.expander("1. Cómo se interpreta un análisis", expanded=True):
        st.markdown(
            "**Calidad de evidencia → observación → comparación → interpretación → "
            "estado regulatorio → acción.**\n\n"
            "Una señal espectral no se transforma automáticamente en degradación, "
            "incumplimiento o causalidad."
        )

    with st.expander("2. Índices y sensores"):
        st.markdown(
            "- **NDVI / SAVI:** señal de vegetación.\n"
            "- **NDWI Green-NIR:** señal de agua superficial.\n"
            "- **NDMI NIR-SWIR1:** señal de humedad.\n"
            "- **NDSI Green-SWIR1:** señal compatible con nieve/hielo.\n"
            "- **SWIR1 y SWIR1/SWIR2:** respuesta espectral, sin inferencia mineralógica automática.\n"
            "- **Sentinel-1 SAR:** evidencia radar complementaria, comparada con geometría orbital homogénea cuando es posible.\n"
            "- **MODIS LST:** temperatura de superficie con QA.\n"
            "- **VIIRS:** anomalías térmicas píxel-día para alerta NRT, no conteo de incendios únicos."
        )

    with st.expander("3. Series temporales"):
        st.markdown(
            "- 7, 14, 30 y 90 días: adquisiciones Sentinel-2 válidas.\n"
            "- 1 año: compuestos mensuales Sentinel-2.\n"
            "- 5-20 años: contexto anual Landsat con identidad de sensor preservada.\n\n"
            "Si faltan datos, BioCore muestra **N/D** o evidencia insuficiente; no inventa puntos."
        )

    with st.expander("4. Umbrales regulatorios"):
        st.markdown(
            "BioCore solo activa un umbral cuando existe una fuente trazable cargada: "
            "RCA, PAS, CAV, plan de seguimiento, norma u otro instrumento aplicable. "
            "Sin esa fuente, el estado regulatorio es **SIN CRITERIO REGULATORIO CARGADO**."
        )

    with st.expander("5. Integridad PDF / Telegram"):
        st.markdown(
            "Cada análisis tiene un **analysis_id** SHA-256. El PDF, Telegram y el registro "
            "de Supabase deben compartir ese mismo ID. Si la integridad falla, la interfaz "
            "bloquea la salida."
        )

    with st.expander("6. Mapa 3D, InSAR y radar geotécnico"):
        st.markdown(
            "- **Mapa técnico 2D:** selección y lectura cartográfica.\n"
            "- **Terreno 3D:** relieve visual con DEM y contexto OpenStreetMap; no reemplaza topografía de ingeniería.\n"
            "- **InSAR:** sólo prepara/importa resultados derivados de Sentinel-1 SLC con fase y QA trazable.\n"
            "- **Radar geotécnico:** simulación geométrica de línea de vista sobre DEM para planificación; no modela SNR real ni certifica estabilidad."
        )


# ---------------------------------------------------------------------------
# Sesión y autenticación
# ---------------------------------------------------------------------------

for key, default in {
    "authenticated": False,
    "admin_mode": False,
    "proyecto_cliente": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

with st.sidebar:
    st.markdown("### 🔐 Autenticación")

    if not st.session_state.get("authenticated"):
        admin_choice = st.checkbox("Modo administrador")
        if admin_choice:
            admin_password = st.text_input("Contraseña administrador", type="password")
            if st.button("Entrar como administrador", use_container_width=True):
                if es_admin(admin_password):
                    st.session_state["authenticated"] = True
                    st.session_state["admin_mode"] = True
                    st.rerun()
                st.error("Contraseña incorrecta.")
        else:
            email = st.text_input("Correo electrónico")
            password = st.text_input("Contraseña", type="password")
            if st.button("Entrar", use_container_width=True):
                valid, cliente = verificar_credenciales_usuario(email, password)
                if valid:
                    st.session_state["authenticated"] = True
                    st.session_state["admin_mode"] = False
                    st.session_state["proyecto_cliente"] = cliente.get("Proyecto")
                    st.session_state["cliente_data"] = cliente
                    st.rerun()
                st.error("Correo o contraseña incorrectos.")
    else:
        if st.session_state.get("admin_mode"):
            st.info("Sesión administradora activa")
        else:
            st.info(f"Proyecto: {st.session_state.get('proyecto_cliente') or 'N/D'}")
        if st.button("🚪 Cerrar sesión", use_container_width=True):
            for key in list(st.session_state.keys()):
                if key not in ():  # limpiar también análisis en memoria
                    del st.session_state[key]
            st.rerun()

if not st.session_state.get("authenticated"):
    crear_portada_biocore()
    st.stop()

projects = _accessible_projects()

# ---------------------------------------------------------------------------
# Aplicación
# ---------------------------------------------------------------------------

if st.session_state.get("admin_mode"):
    tab_vigilancia, tab_auditoria, tab_db, tab_clientes, tab_guia = st.tabs([
        "🛰️ Vigila",
        "📋 Auditoría",
        "🗃️ BD",
        "👥 Clientes",
        "📖 Guía",
    ])
else:
    tab_vigilancia, tab_auditoria, tab_historial, tab_config, tab_guia = st.tabs([
        "🛰️ Vigila",
        "📋 Auditoría",
        "📨 Historial",
        "⚙️ Telegram",
        "📖 Guía",
    ])

with tab_vigilancia:
    st.title("🛰️ Vigilancia ambiental")
    st.caption(
        "La ejecución genera un análisis canónico: los mismos datos alimentan interfaz, PDF, Supabase y Telegram."
    )

    if not projects:
        st.warning("No hay proyectos disponibles para esta cuenta.")
    else:
        names = [p.get("Proyecto") for p in projects]
        selected = st.selectbox("Proyecto", names, key="vig_project")
        project_data = next(p for p in projects if p.get("Proyecto") == selected)

        col_map, col_ctrl = st.columns([2.2, 1])
        coords = None
        with col_map:
            try:
                coords = obtener_coordenadas_correctamente(project_data)
                view_mode = st.radio(
                    "Vista cartográfica",
                    ["🛰️ Mapa técnico 2D", "🗻 Terreno 3D"],
                    horizontal=True,
                    label_visibility="collapsed",
                    key="vig_map_mode",
                )
                if view_mode == "🛰️ Mapa técnico 2D":
                    folium_static(dibujar_mapa_biocore(coords), width=850, height=500)
                else:
                    load_osm = st.checkbox(
                        "Añadir contexto OSM (edificios/caminos)",
                        value=False,
                        key="vig_osm3d",
                        help="Opcional. El relieve 3D funciona sin OSM.",
                    )
                    osm_context = fetch_osm_3d_context(coords) if load_osm else None
                    if osm_context and osm_context.get("warning"):
                        warning = str(osm_context.get("warning") or "")
                        if "429" in warning or "Too Many Requests" in warning:
                            st.caption(
                                "Contexto OSM temporalmente no disponible por límite de consultas. "
                                "El relieve Copernicus DEM funciona normalmente."
                            )
                        else:
                            st.caption("Contexto OSM no disponible en esta carga. El DEM funciona normalmente.")
                    render_mapa_3d_biocore(coords, osm_context=osm_context, height=520)
                    st.caption(
                        "Vista 3D: relieve visual + OpenStreetMap. Para análisis cuantitativos BioCore usa las fuentes y resoluciones indicadas en la trazabilidad."
                    )
            except Exception as exc:
                st.error(f"No se pudo representar el proyecto: {exc}")

        with col_ctrl:
            range_label = st.selectbox(
                "Ventana de vigilancia",
                ["Últimos 7 días", "Últimas 2 semanas", "Último mes", "Últimos 3 meses"],
                index=2,
                key="vig_range",
            )
            st.caption(
                "La comparación formal usa una ventana adaptativa y un sensor comparable con la línea base."
            )
            if st.button("🚀 Ejecutar análisis", key="vig_run", use_container_width=True):
                with st.spinner("Consultando sensores y construyendo evidencia trazable..."):
                    try:
                        report, temporal, charts = ejecutar_analisis_canonico(
                            project_data,
                            range_label,
                            save=True,
                        )
                        _store_analysis("vig", report, temporal, charts, project_data)
                        st.success(f"Análisis generado · ID {report['analysis_id'][:12]}")
                        save_warning = st.session_state.get("_biocore_save_warning")
                        if save_warning:
                            st.warning(save_warning)
                    except Exception as exc:
                        st.error(f"No fue posible completar el análisis: {exc}")

        if coords:
            render_advanced_geospatial_tools(project_data, coords, key_prefix="vig_advanced")

        _render_stored_analysis("vig")

with tab_auditoria:
    st.title("📋 Informe técnico de vigilancia")
    st.caption(
        "Seleccione el período que realmente desea visualizar. BioCore no convierte 7 días en una serie anual ni rellena datos faltantes."
    )

    if not projects:
        st.warning("No hay proyectos disponibles.")
    else:
        names = [p.get("Proyecto") for p in projects]
        c1, c2 = st.columns(2)
        with c1:
            selected = st.selectbox("Proyecto", names, key="audit_project_v2")
        with c2:
            range_label = st.selectbox("Rango temporal", RANGE_LABELS, key="audit_range_v2")

        project_data = next(p for p in projects if p.get("Proyecto") == selected)

        if st.button("🚀 Generar informe completo", key="audit_run_v2", use_container_width=True):
            with st.spinner("Procesando análisis óptico, serie temporal, SAR, VIIRS y trazabilidad..."):
                try:
                    report, temporal, charts = ejecutar_analisis_canonico(
                        project_data,
                        range_label,
                        save=True,
                    )
                    _store_analysis("audit", report, temporal, charts, project_data)
                    st.success(f"Informe generado · ID {report['analysis_id'][:12]}")
                    save_warning = st.session_state.get("_biocore_save_warning")
                    if save_warning:
                        st.warning(save_warning)
                except Exception as exc:
                    st.error(f"No fue posible generar el informe: {exc}")

        _render_stored_analysis("audit")

if st.session_state.get("admin_mode"):
    with tab_db:
        st.title("📊 Base de datos")
        sub_users, sub_reports = st.tabs(["Usuarios", "Reportes canónicos"])

        with sub_users:
            try:
                rows = supabase.table("usuarios").select("*").execute().data or []
                if rows:
                    safe_rows = []
                    for r in rows:
                        r = dict(r)
                        r.pop("password_cliente", None)
                        safe_rows.append(r)
                    st.dataframe(pd.DataFrame(safe_rows), use_container_width=True, hide_index=True)
                else:
                    st.info("No hay usuarios registrados.")
            except Exception as exc:
                st.error(f"Error cargando usuarios: {exc}")

        with sub_reports:
            try:
                rows = (
                    supabase.table("historial_reportes")
                    .select("proyecto,tipo,created_at,analysis_id,schema_version,method_version,estado,source_data_date")
                    .order("created_at", desc=True)
                    .limit(200)
                    .execute().data or []
                )
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(f"Error cargando reportes: {exc}")

    with tab_clientes:
        st.title("👥 Gestión de clientes")
        nuevo, editar, eliminar = st.tabs(["➕ Nuevo", "✏️ Editar", "🗑️ Eliminar"])

        with nuevo:
            with st.form("new_client_v2"):
                proyecto = st.text_input("Proyecto *")
                tipo = st.selectbox("Tipo *", ["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"])
                coords_text = st.text_area("Coordenadas JSON *", placeholder='[[lon,lat],[lon,lat],[lon,lat]]')
                baseline = st.number_input("Año de línea base", min_value=2000, max_value=datetime.now().year, value=2015)
                email = st.text_input("Correo")
                telegram_id = st.text_input("ID Telegram")
                c1, c2 = st.columns(2)
                with c1:
                    report_time = st.time_input("Hora reporte", value=time(8, 0))
                with c2:
                    frequency = st.selectbox("Frecuencia", ["Diario", "Semanal"])
                password = st.text_input("Contraseña *", type="password")
                submitted = st.form_submit_button("Registrar", use_container_width=True)

            if submitted:
                try:
                    if not proyecto or not coords_text or not password:
                        raise ValueError("Completa los campos obligatorios.")
                    coords = limpiar_coordenadas(json.loads(coords_text))
                    record = {
                        "Proyecto": proyecto.strip(),
                        "Tipo": tipo,
                        "Coordenadas": json.dumps(coords),
                        "email_cliente": email.strip().lower() or None,
                        "password_cliente": hash_password(password),
                        "id_telegram": telegram_id.strip() or None,
                        "ano_linea_base": int(baseline),
                        "hora_reporte": report_time.strftime("%H:%M"),
                        "frecuencia_reporte": frequency,
                    }
                    supabase.table("usuarios").insert(record).execute()
                    st.success("Cliente registrado.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"No fue posible registrar: {exc}")

        with editar:
            all_names = [p.get("Proyecto") for p in _accessible_projects()]
            if not all_names:
                st.info("No hay clientes registrados.")
            else:
                selected_edit = st.selectbox("Cliente", all_names, key="edit_client_name")
                current = _project_by_name(selected_edit) or {}
                with st.form("edit_client_v2"):
                    tipos = ["MINERIA", "GLACIAR", "BOSQUE", "HUMEDAL", "AGRICOLA"]
                    current_type = current.get("Tipo") if current.get("Tipo") in tipos else tipos[0]
                    tipo = st.selectbox("Tipo", tipos, index=tipos.index(current_type))
                    raw_coords = current.get("Coordenadas")
                    if not isinstance(raw_coords, str):
                        raw_coords = json.dumps(raw_coords or [])
                    coords_text = st.text_area("Coordenadas JSON", value=raw_coords)
                    baseline = st.number_input(
                        "Año de línea base",
                        min_value=2000,
                        max_value=datetime.now().year,
                        value=int(current.get("ano_linea_base") or 2015),
                    )
                    email = st.text_input("Correo", value=current.get("email_cliente") or "")
                    telegram_id = st.text_input("ID Telegram", value=str(current.get("id_telegram") or ""))
                    password = st.text_input("Nueva contraseña (opcional)", type="password")
                    submitted = st.form_submit_button("Guardar cambios", use_container_width=True)

                if submitted:
                    try:
                        coords = limpiar_coordenadas(json.loads(coords_text))
                        update = {
                            "Tipo": tipo,
                            "Coordenadas": json.dumps(coords),
                            "ano_linea_base": int(baseline),
                            "email_cliente": email.strip().lower() or None,
                            "id_telegram": telegram_id.strip() or None,
                        }
                        if password.strip():
                            update["password_cliente"] = hash_password(password)
                        supabase.table("usuarios").update(update).eq("Proyecto", selected_edit).execute()
                        st.success("Cliente actualizado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"No fue posible actualizar: {exc}")

        with eliminar:
            all_names = [p.get("Proyecto") for p in _accessible_projects()]
            if not all_names:
                st.info("No hay clientes registrados.")
            else:
                selected_del = st.selectbox("Cliente", all_names, key="delete_client_name")
                confirm = st.checkbox(f"Confirmo eliminar {selected_del}")
                if st.button("Eliminar cliente", disabled=not confirm, use_container_width=True):
                    try:
                        supabase.table("usuarios").delete().eq("Proyecto", selected_del).execute()
                        st.success("Cliente eliminado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"No fue posible eliminar: {exc}")

else:
    with tab_historial:
        st.title("📨 Historial de análisis")
        project_name = st.session_state.get("proyecto_cliente")
        try:
            rows = (
                supabase.table("historial_reportes")
                .select("proyecto,tipo,created_at,analysis_id,schema_version,method_version,estado,source_data_date,payload_json")
                .eq("proyecto", project_name)
                .order("created_at", desc=True)
                .limit(100)
                .execute().data or []
            )
        except Exception as exc:
            rows = []
            st.error(f"No fue posible cargar el historial: {exc}")

        if not rows:
            st.info("Aún no hay análisis canónicos guardados.")
        else:
            display = [
                {k: r.get(k) for k in ["created_at", "source_data_date", "analysis_id", "estado", "method_version"]}
                for r in rows
            ]
            st.dataframe(pd.DataFrame(display), use_container_width=True, hide_index=True)

            canonical_rows = [r for r in rows if isinstance(r.get("payload_json"), dict)]
            if canonical_rows:
                options = {
                    f"{str(r.get('created_at') or '')[:19]} · {str(r.get('analysis_id') or '')[:12]}": r
                    for r in canonical_rows
                }
                selected_label = st.selectbox("Abrir un análisis", list(options.keys()))
                selected_report = options[selected_label]["payload_json"]
                if verify_analysis_id(selected_report):
                    render_analysis_dashboard(selected_report)
                else:
                    st.error("El registro seleccionado no supera la verificación de integridad.")

    with tab_config:
        st.title("⚙️ Reportes automáticos por Telegram")
        mostrar_formulario_reportes()
        st.markdown("---")
        mostrar_resumen_reportes()

with tab_guia:
    mostrar_guia_v2()

st.markdown("---")
st.markdown(
    "<div style='text-align:center;color:#64748b;font-size:.82rem;padding:12px;'>"
    "BioCore Intelligence © 2026 · Vigilancia ambiental satelital explicable"
    "</div>",
    unsafe_allow_html=True,
)
