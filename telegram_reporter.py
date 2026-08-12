import streamlit as st
from supabase import create_client, Client
import os
from datetime import datetime
import pytz

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

# Zona horaria de Chile
TIMEZONE_CHILE = pytz.timezone("America/Santiago")

# Mapeo de días en español a índices (0=Lunes, 6=Domingo)
DIAS_SEMANA = {
    "Lunes": 0,
    "Martes": 1,
    "Miércoles": 2,
    "Jueves": 3,
    "Viernes": 4,
    "Sábado": 5,
    "Domingo": 6,
}

DIAS_SEMANA_REVERSE = {v: k for k, v in DIAS_SEMANA.items()}

# ============================================================================
# INICIALIZACIÓN DE SUPABASE
# ============================================================================

@st.cache_resource
def init_supabase_client() -> Client:
    """Inicializa el cliente de Supabase una sola vez."""
    try:
        supabase_url = st.secrets["connections"]["supabase"]["url"]
        supabase_key = st.secrets["connections"]["supabase"]["key"]
    except:
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
    
    if not supabase_url or not supabase_key:
        st.error("❌ Falta configurar SUPABASE_URL o SUPABASE_KEY en secrets")
        st.stop()
    
    return create_client(supabase_url, supabase_key)

# ============================================================================
# FUNCIONES DE LOGICA
# ============================================================================

def obtener_reporte_existente(chat_id: str, nombre_empresa: str) -> dict | None:
    """
    Obtiene la configuración actual de un cliente si existe.
    """
    try:
        if not chat_id or not chat_id.strip():
            return None
            
        supabase = init_supabase_client()
        response = supabase.table("clientes_reportes").select("*").eq(
            "chat_id", int(chat_id)
        ).eq("nombre_empresa", nombre_empresa).single().execute()
        
        return response.data if response.data else None
    except Exception as e:
        return None

def guardar_reporte(
    nombre_empresa: str,
    chat_id: str,
    frecuencia: str,
    hora_reporte: int,
    dia_semana_texto: str | None = None,
) -> bool:
    """
    Inserta o actualiza la configuración de reportes (UPSERT).
    """
    try:
        # Convertir chat_id a entero
        chat_id_int = int(chat_id)
        
        # Mapear día de la semana si es semanal
        dia_semana_int = None
        if frecuencia == "semanal" and dia_semana_texto:
            dia_semana_int = DIAS_SEMANA.get(dia_semana_texto)
        
        # Convertir frecuencia a minúsculas
        frecuencia_lower = frecuencia.lower()
        
        # Preparar datos para UPSERT
        data = {
            "nombre_empresa": nombre_empresa,
            "chat_id": chat_id_int,
            "frecuencia": frecuencia_lower,
            "hora_reporte": hora_reporte,
            "dia_semana": dia_semana_int,
        }
        
        supabase = init_supabase_client()
        
        # Usar UPSERT (si existe, actualiza; si no, crea)
        response = supabase.table("clientes_reportes").upsert(
            data,
            on_conflict="chat_id,nombre_empresa"
        ).execute()
        
        if response.data:
            return True
        else:
            st.error("❌ Error al guardar los datos. Respuesta vacía de Supabase.")
            return False
    
    except ValueError:
        st.error(f"❌ El Chat ID debe ser un número válido. Recibiste: {chat_id}")
        return False
    except Exception as e:
        st.error(f"❌ Error al guardar la configuración: {str(e)}")
        return False

# ============================================================================
# COMPONENTES DE UI (FORMULARIO)
# ============================================================================

def mostrar_formulario_reportes():
    """
    Muestra el formulario Streamlit para configurar reportes automáticos.
    """
    st.header("🤖 Configurar Reportes Automáticos por Telegram")
    
    st.markdown("""
    ---
    ⏱️ **Nota de Zona Horaria:** Todos los horarios se guardan en **Hora de Chile (UTC-3)**.
    El backend de GitHub Actions consultará esta tabla cada hora para enviar reportes automáticos.
    """)
    
    # Crear formulario con st.form
    with st.form("form_reportes_telegram", clear_on_submit=True):
        
        # ===== Campo: Chat ID de Telegram =====
        st.markdown("### 💬 Tu Chat ID de Telegram")
        col1, col2 = st.columns([3, 1])
        
        with col1:
            chat_id = st.text_input(
                "Chat ID",
                placeholder="Ej: 123456789",
                help="ID numérico de tu conversación privada con el bot"
            )
        
        with col2:
            with st.expander("❓ ¿Cómo obtenerlo?"):
                st.markdown("""
                **Opción 1: @userinfobot**
                1. Abre Telegram
                2. Busca: `@userinfobot`
                3. Envía cualquier mensaje
                4. El bot te responderá tu User ID
                
                **Opción 2: @GetIdsBot**
                1. Busca: `@GetIdsBot`
                2. Inicia el bot
                3. Tu ID aparecerá automáticamente
                """)
        
        # ===== Campo: Frecuencia =====
        st.markdown("### 📅 Frecuencia de Reportes")
        frecuencia = st.radio(
            "¿Con qué frecuencia recibirás los reportes?",
            options=["Diario", "Semanal"],
            horizontal=True,
        )
        
        # ===== Campo: Hora del Reporte =====
        st.markdown("### 🕐 Hora del Reporte (Zona Horaria Chile)")
        hora_reporte = st.slider(
            "Selecciona la hora (formato 24h)",
            min_value=0,
            max_value=23,
            value=9,
            step=1,
            format="%d:00",
        )
        st.caption(f"📍 Horario seleccionado: {hora_reporte:02d}:00 hrs (Chile UTC-3)")
        
        # ===== Campo Condicional: Día de la Semana =====
        dia_semana_texto = None
        if frecuencia == "Semanal":
            st.markdown("### 📆 Día de la Semana")
            dia_semana_texto = st.selectbox(
                "Selecciona el día en que deseas recibir tu reporte",
                options=list(DIAS_SEMANA.keys()),
                index=0,
            )
            st.caption(f"📍 Recibirás tu reporte cada {dia_semana_texto}")
        
        st.markdown("---")
        
        # ===== Botón Submit =====
        submitted = st.form_submit_button(
            "✅ Guardar Configuración",
            use_container_width=True,
            type="primary"
        )
    
    # Procesar envío del formulario
    if submitted:
        # Obtener nombre de empresa desde session state
        proyecto_cliente = st.session_state.get('proyecto_cliente')
        
        if not proyecto_cliente:
            st.error("❌ No se pudo identificar tu proyecto.")
            return
        
        # Validaciones básicas
        if not chat_id or not chat_id.strip():
            st.error("❌ Por favor, ingresa tu Chat ID de Telegram.")
            return
        
        # Validar que sea un número
        try:
            int(chat_id)
        except ValueError:
            st.error(f"❌ El Chat ID debe ser un número. Recibiste: {chat_id}")
            return
        
        # Si es semanal, validar que haya seleccionado un día
        if frecuencia == "Semanal" and not dia_semana_texto:
            st.error("❌ Por favor, selecciona un día de la semana.")
            return
        
        # Guardar en Supabase
        if guardar_reporte(
            nombre_empresa=proyecto_cliente,
            chat_id=chat_id.strip(),
            frecuencia=frecuencia.lower(),
            hora_reporte=hora_reporte,
            dia_semana_texto=dia_semana_texto
        ):
            st.success(
                f"""
                ✅ **¡Configuración guardada correctamente!**
                
                📊 Empresa: {proyecto_cliente}
                💬 Chat ID: {chat_id}
                📅 Frecuencia: {frecuencia}
                🕐 Hora: {hora_reporte:02d}:00 (Chile)
                {f"📆 Día: {dia_semana_texto}" if frecuencia == "semanal" else ""}
                
                Comenzarás a recibir reportes en tu próximo horario programado.
                """
            )
            st.balloons()
        else:
            st.error("❌ No se pudo guardar la configuración. Intenta de nuevo.")

def mostrar_resumen_reportes():
    """
    Muestra un resumen de las configuraciones guardadas.
    """
    st.subheader("📋 Mi Configuración Actual")
    
    proyecto_cliente = st.session_state.get('proyecto_cliente')
    
    if not proyecto_cliente:
        st.warning("No se pudo cargar tu configuración.")
        return
    
    try:
        supabase = init_supabase_client()
        response = supabase.table("clientes_reportes").select("*").eq(
            "nombre_empresa", proyecto_cliente
        ).execute()
        
        if response.data and len(response.data) > 0:
            import pandas as pd
            
            datos = []
            for row in response.data:
                dia_nombre = DIAS_SEMANA_REVERSE.get(row.get("dia_semana"), "-")
                datos.append({
                    "Chat ID": row.get("chat_id", "-"),
                    "Frecuencia": row.get("frecuencia", "-").capitalize(),
                    "Hora": f"{row.get('hora_reporte', '-'):02d}:00",
                    "Día Semana": dia_nombre if row.get("frecuencia") == "semanal" else "-",
                })
            
            df = pd.DataFrame(datos)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("📭 Aún no tienes configuración guardada. ¡Usa el formulario de arriba para crear una!")
    
    except Exception as e:
        st.warning(f"⚠️ No se pudo cargar el resumen: {str(e)}")

# ============================================================================
# PUNTO DE ENTRADA PRINCIPAL
# ============================================================================

if __name__ == "__main__":
    mostrar_formulario_reportes()
    st.markdown("---")
    mostrar_resumen_reportes()
