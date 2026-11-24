
import os
import csv
import json
import math
import time
import random
import sqlite3
import threading

from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st


# ============================================================
# 1. CONFIGURACIÓN GENERAL
# ============================================================

BROKER_IP = os.getenv("MQTT_BROKER", "broker.hivemq.com")
BROKER_PORT = int(os.getenv("MQTT_PORT", "1883"))

TOPIC = os.getenv(
    "MQTT_TOPIC",
    "cybertwin/mazda_bt50/obd2"
)

MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

MQTT_TLS = os.getenv("MQTT_TLS", "false").lower() in (
    "1", "true", "yes"
)

MQTT_QOS = int(os.getenv("MQTT_QOS", "1"))

BASE_DIR = (
    os.path.dirname(os.path.abspath(__file__))
    if "__file__" in globals()
    else os.getcwd()
)

ARCHIVO_DB = os.path.join(BASE_DIR, "historial_mqtt.db")
ARCHIVO_CSV = os.path.join(BASE_DIR, "historial_obd2.csv")

ZONA_HORARIA = ZoneInfo("America/Bogota")

INTERVALO_ACTUALIZACION = "1s"

MAX_MOSTRAR_VIVO = 120
MAX_PUNTOS_BLOQUE = 360
MAX_RAM_DIA = 60000

TIMEOUT_ESP32_SEGUNDOS = 6


# ============================================================
# 2. DEFINICIÓN DE LOS PARÁMETROS OBD-II
# ============================================================

PARAMETROS = {
    "rpm": {
        "nombre": "Revoluciones del motor",
        "unidad": "rpm",
        "alias": ["rpm", "engine_rpm", "engine_speed"]
    },
    "acelerador": {
        "nombre": "Posición del acelerador",
        "unidad": "%",
        "alias": [
            "acelerador", "accelerator",
            "accelerator_position",
            "accelerator_pedal_position"
        ]
    },
    "carga_motor": {
        "nombre": "Carga del motor",
        "unidad": "%",
        "alias": ["carga_motor", "engine_load", "load"]
    },
    "map": {
        "nombre": "Presión MAP",
        "unidad": "kPa",
        "alias": ["map", "intake_map", "manifold_pressure"]
    },
    "maf": {
        "nombre": "Flujo de aire MAF",
        "unidad": "g/s",
        "alias": ["maf", "mass_air_flow"]
    },
    "temp_refrigerante": {
        "nombre": "Temperatura del refrigerante",
        "unidad": "°C",
        "alias": [
            "temp_refrigerante",
            "coolant_temperature",
            "coolant_temp",
            "temperatura_refrigerante"
        ]
    },
    "presion_combustible": {
        "nombre": "Presión del riel de combustible",
        "unidad": "kPa",
        "alias": [
            "presion_combustible",
            "fuel_rail_pressure",
            "fuel_pressure"
        ]
    },
    "temp_admision": {
        "nombre": "Temperatura del aire de admisión",
        "unidad": "°C",
        "alias": [
            "temp_admision",
            "intake_air_temperature",
            "intake_temp"
        ]
    },
    "velocidad": {
        "nombre": "Velocidad del vehículo",
        "unidad": "km/h",
        "alias": ["velocidad", "vehicle_speed", "speed"]
    },
    "mariposa": {
        "nombre": "Posición de la mariposa",
        "unidad": "%",
        "alias": [
            "mariposa", "throttle_position",
            "throttle"
        ]
    },
    "presion_barometrica": {
        "nombre": "Presión barométrica",
        "unidad": "kPa",
        "alias": [
            "presion_barometrica",
            "barometric_pressure",
            "barometric"
        ]
    },
    "avance_encendido": {
        "nombre": "Avance de encendido",
        "unidad": "°",
        "alias": [
            "avance_encendido",
            "timing_advance",
            "ignition_timing"
        ]
    },
    "tiempo_motor": {
        "nombre": "Tiempo de funcionamiento del motor",
        "unidad": "s",
        "alias": [
            "tiempo_motor",
            "engine_runtime",
            "runtime"
        ]
    }
}

CLAVES_PARAMETROS = list(PARAMETROS.keys())

COLUMNAS_SQL = [
    "rpm",
    "acelerador",
    "carga_motor",
    "map",
    "maf",
    "temp_refrigerante",
    "presion_combustible",
    "temp_admision",
    "velocidad",
    "mariposa",
    "presion_barometrica",
    "avance_encendido",
    "tiempo_motor"
]


# ============================================================
# 3. CONFIGURACIÓN DE STREAMLIT
# ============================================================

st.set_page_config(
    page_title="Mazda BT-50 | Telemetría OBD-II",
    page_icon="🚙",
    layout="wide"
)

st.markdown(
    """
    <style>
    div[data-testid="stRadio"] > div[role="radiogroup"] {
        display: flex;
        flex-wrap: wrap;
        gap: 0.45rem;
        max-height: 165px;
        overflow-y: auto;
        padding: 0.75rem;
        background-color: rgba(128,128,128,0.08);
        border-radius: 10px;
    }
    </style>
    """,
    unsafe_allow_html=True
)


# ============================================================
# 4. UTILIDADES
# ============================================================

def obtener_ahora():
    return datetime.now(ZONA_HORARIA)


def formato_hora(fecha):
    return fecha.strftime("%I:%M:%S%p").lstrip("0").lower()


def convertir_numero(valor):
    if valor is None or isinstance(valor, bool):
        return None

    try:
        numero = float(valor)

        if math.isfinite(numero):
            return numero

    except (ValueError, TypeError):
        pass

    return None


def buscar_clave(diccionario, alias):
    for clave in alias:
        if clave in diccionario:
            return diccionario[clave]

    return None


def obtener_timestamp(objeto):
    """
    Intenta utilizar la marca temporal enviada por la ESP32.
    Si no existe o no puede interpretarse, utiliza la hora local.
    """

    posibles = [
        objeto.get("timestamp"),
        objeto.get("datetime"),
        objeto.get("fecha_hora")
    ]

    for valor in posibles:
        if not valor:
            continue

        try:
            fecha = pd.to_datetime(valor, errors="coerce")

            if pd.isna(fecha):
                continue

            fecha = fecha.to_pydatetime()

            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=ZONA_HORARIA)
            else:
                fecha = fecha.astimezone(ZONA_HORARIA)

            return fecha

        except Exception:
            continue

    return obtener_ahora()


# ============================================================
# 5. PROCESAMIENTO DEL JSON OBD-II
# ============================================================

def procesar_payload(payload):
    """
    Convierte el JSON de la ESP32 en un registro uniforme.

    Admite:
    - Valores directamente en el objeto JSON.
    - Valores dentro de values, pids o data.
    - Indicadores de validez dentro de valid o validity.
    - Valores nulos cuando un PID no está disponible.

    Los nombres definitivos se podrán ajustar cuando tengamos
    el primer JSON real de la ESP32.
    """

    texto = payload.decode("utf-8").strip()

    objeto = json.loads(texto)

    if not isinstance(objeto, dict):
        raise ValueError("El mensaje MQTT debe contener un objeto JSON.")

    datos = objeto.get(
        "values",
        objeto.get("pids", objeto.get("data", objeto))
    )

    if not isinstance(datos, dict):
        datos = objeto

    validez = objeto.get(
        "valid",
        objeto.get("validity", {})
    )

    if not isinstance(validez, dict):
        validez = {}

    registro = {
        "Momento": obtener_timestamp(objeto),
        "device_id": objeto.get("device_id", "Desconocido"),
        "boot_id": objeto.get("boot_id", ""),
        "reading_id": objeto.get("reading_id", ""),
        "source": "REAL",
        "elm_connected": objeto.get("elm_connected", None),
        "hz": convertir_numero(objeto.get("hz")),
        "ts_ms": convertir_numero(objeto.get("ts_ms"))
    }

    for clave, configuracion in PARAMETROS.items():

        valor_original = buscar_clave(
            datos,
            configuracion["alias"]
        )

        valor = convertir_numero(valor_original)

        # Algunos formatos utilizan una clave individual
        # como rpm_valid o valid_rpm.
        validez_individual = buscar_clave(
            validez,
            configuracion["alias"]
        )

        if validez_individual is None:
            validez_individual = objeto.get(
                f"{clave}_valid",
                objeto.get(f"valid_{clave}", None)
            )

        if isinstance(validez_individual, str):
            validez_individual = (
                validez_individual.lower() in
                ("true", "1", "yes", "ok")
            )

        # Si el JSON no proporciona bandera de validez,
        # se considera válido únicamente si existe un número.
        if validez_individual is None:
            valido = valor is not None
        else:
            valido = bool(validez_individual) and valor is not None

        registro[clave] = valor if valido else None
        registro[f"{clave}_valid"] = valido

    return registro


# ============================================================
# 6. GENERADOR DE DATOS SIMULADOS
# ============================================================

def generar_datos_simulados():
    """
    Genera valores ficticios para probar la interfaz.
    No representa mediciones reales de una Mazda BT-50.
    """

    rpm = random.randint(750, 2600)

    velocidad = random.randint(0, 90)

    carga = min(
        100,
        max(5, velocidad * 0.65 + random.uniform(5, 25))
    )

    return {
        "Momento": obtener_ahora(),
        "device_id": "ESP32_SIMULADA",
        "boot_id": "SIMULACION",
        "reading_id": str(time.time_ns()),
        "source": "SIMULADO",
        "elm_connected": True,
        "hz": 0.5,
        "ts_ms": None,

        "rpm": float(rpm),
        "acelerador": random.uniform(5, 55),
        "carga_motor": float(carga),
        "map": random.uniform(95, 180),
        "maf": random.uniform(3, 35),
        "temp_refrigerante": random.uniform(75, 95),
        "presion_combustible": random.uniform(250, 1200),
        "temp_admision": random.uniform(20, 45),
        "velocidad": float(velocidad),
        "mariposa": random.uniform(5, 45),
        "presion_barometrica": random.uniform(90, 103),
        "avance_encendido": random.uniform(5, 25),
        "tiempo_motor": random.uniform(100, 15000),

        **{
            f"{clave}_valid": True
            for clave in CLAVES_PARAMETROS
        }
    }


# ============================================================
# 7. RECOLECTOR MQTT Y BASE DE DATOS
# ============================================================

class RecolectorMQTT:

    def __init__(self):

        self.lock = threading.RLock()

        self.fecha_actual = obtener_ahora().strftime("%Y-%m-%d")

        self.historial_hoy = deque(maxlen=MAX_RAM_DIA)

        self.mqtt_connected = False
        self.mqtt_error = None

        self.ultimo_mensaje_ts = None
        self.ultimo_topic = None
        self.ultimo_payload = None
        self.ultimo_error_payload = None

        self.mensajes_recibidos = 0
        self.mensajes_invalidos = 0
        self.mensajes_reales = 0
        self.mensajes_simulados = 0

        self.total_historico = 0
        self.conteo_por_fecha = {}

        self.ultima_hora = None
        self.ultimo_device_id = None
        self.ultimo_elm_connected = None

        self.db_conn = sqlite3.connect(
            ARCHIVO_DB,
            check_same_thread=False
        )

        self.db_conn.execute("PRAGMA journal_mode=WAL")
        self.db_conn.execute("PRAGMA synchronous=NORMAL")
        self.db_conn.execute("PRAGMA busy_timeout=5000")

        self._inicializar_db()
        self._cargar_memoria()
        self._iniciar_cliente()

    def _inicializar_db(self):

        definiciones = [
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "fecha TEXT NOT NULL",
            "hora TEXT NOT NULL",
            "momento_iso TEXT NOT NULL",
            "source TEXT NOT NULL",
            "device_id TEXT",
            "boot_id TEXT",
            "reading_id TEXT",
            "elm_connected INTEGER",
            "hz REAL",
            "ts_ms REAL"
        ]

        for clave in CLAVES_PARAMETROS:
            definiciones.append(f"{clave} REAL")
            definiciones.append(f"{clave}_valid INTEGER NOT NULL DEFAULT 0")

        definiciones.append("payload TEXT")

        sql = f"""
        CREATE TABLE IF NOT EXISTS obd_lecturas (
            {",".join(definiciones)}
        )
        """

        with self.lock:
            self.db_conn.execute(sql)

            self.db_conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_obd_fecha
                ON obd_lecturas(fecha)
                """
            )

            self.db_conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_obd_momento
                ON obd_lecturas(momento_iso)
                """
            )

            self.db_conn.commit()

    def _cargar_memoria(self):

        with self.lock:

            cur = self.db_conn.cursor()

            cur.execute(
                """
                SELECT fecha, COUNT(*)
                FROM obd_lecturas
                GROUP BY fecha
                """
            )

            resumen = cur.fetchall()

            self.conteo_por_fecha = {}
            self.total_historico = 0

            for fecha, cantidad in resumen:
                self.conteo_por_fecha[fecha] = int(cantidad)
                self.total_historico += int(cantidad)

            self.conteo_por_fecha.setdefault(
                self.fecha_actual,
                0
            )

            cur.execute(
                """
                SELECT *
                FROM obd_lecturas
                WHERE fecha = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (self.fecha_actual, MAX_RAM_DIA)
            )

            filas = cur.fetchall()

            nombres = [
                descripcion[0]
                for descripcion in cur.description
            ]

            for fila in reversed(filas):

                registro = dict(zip(nombres, fila))

                registro["Momento"] = datetime.fromisoformat(
                    registro["momento_iso"]
                )

                registro["Fecha"] = registro["fecha"]
                registro["Hora"] = registro["hora"]

                for clave in CLAVES_PARAMETROS:
                    registro[clave] = registro[clave]
                    registro[f"{clave}_valid"] = bool(
                        registro[f"{clave}_valid"]
                    )

                self.historial_hoy.append(registro)

            self.fecha_actual = obtener_ahora().strftime("%Y-%m-%d")

    def guardar_registro(self, registro, payload=""):

        momento = registro["Momento"]

        fecha = momento.strftime("%Y-%m-%d")
        hora = formato_hora(momento)

        columnas = [
            "fecha",
            "hora",
            "momento_iso",
            "source",
            "device_id",
            "boot_id",
            "reading_id",
            "elm_connected",
            "hz",
            "ts_ms"
        ]

        valores = [
            fecha,
            hora,
            momento.isoformat(),
            registro.get("source", "REAL"),
            registro.get("device_id"),
            registro.get("boot_id"),
            str(registro.get("reading_id", "")),
            (
                None if registro.get("elm_connected") is None
                else int(bool(registro["elm_connected"]))
            ),
            registro.get("hz"),
            registro.get("ts_ms")
        ]

        for clave in CLAVES_PARAMETROS:
            columnas.append(clave)
            valores.append(registro.get(clave))

            columnas.append(f"{clave}_valid")
            valores.append(
                int(bool(registro.get(f"{clave}_valid", False)))
            )

        columnas.append("payload")
        valores.append(payload[:4000])

        placeholders = ",".join(["?"] * len(columnas))

        sql = f"""
        INSERT INTO obd_lecturas ({",".join(columnas)})
        VALUES ({placeholders})
        """

        with self.lock:

            try:

                self.db_conn.execute(sql, valores)
                self.db_conn.commit()

                registro["Fecha"] = fecha
                registro["Hora"] = hora

                self.historial_hoy.append(registro)

                self.total_historico += 1

                self.conteo_por_fecha[fecha] = (
                    self.conteo_por_fecha.get(fecha, 0) + 1
                )

                if registro["source"] == "REAL":
                    self.mensajes_reales += 1
                else:
                    self.mensajes_simulados += 1

                self.ultimo_mensaje_ts = time.monotonic()
                self.ultima_hora = hora
                self.ultimo_device_id = registro.get("device_id")
                self.ultimo_elm_connected = registro.get("elm_connected")

            except Exception as e:
                self.mqtt_error = f"Error SQLite: {e}"

    def obtener_lecturas_fecha(self, fecha):

        with self.lock:

            cur = self.db_conn.cursor()

            cur.execute(
                """
                SELECT *
                FROM obd_lecturas
                WHERE fecha = ?
                ORDER BY id ASC
                """,
                (fecha,)
            )

            filas = cur.fetchall()

            nombres = [
                descripcion[0]
                for descripcion in cur.description
            ]

        if not filas:
            return pd.DataFrame()

        registros = []

        for fila in filas:

            registro = dict(zip(nombres, fila))

            registro["Momento"] = datetime.fromisoformat(
                registro["momento_iso"]
            )

            registro["Fecha"] = registro["fecha"]
            registro["Hora"] = registro["hora"]

            for clave in CLAVES_PARAMETROS:
                registro[f"{clave}_valid"] = bool(
                    registro[f"{clave}_valid"]
                )

            registros.append(registro)

        return pd.DataFrame(registros)

    def _iniciar_cliente(self):

        def on_connect(client, userdata, flags, reason_code, properties):

            with self.lock:

                if not getattr(
                    reason_code,
                    "is_failure",
                    bool(reason_code)
                ):

                    client.subscribe(TOPIC, qos=MQTT_QOS)

                    self.mqtt_connected = True
                    self.mqtt_error = None

                else:

                    self.mqtt_connected = False

                    self.mqtt_error = (
                        f"Broker rechazó conexión: {reason_code}"
                    )

        def on_disconnect(
            client,
            userdata,
            disconnect_flags,
            reason_code,
            properties
        ):

            with self.lock:
                self.mqtt_connected = False

                if getattr(
                    reason_code,
                    "is_failure",
                    bool(reason_code)
                ):

                    self.mqtt_error = (
                        f"Desconexión MQTT: {reason_code}"
                    )

        def on_message(client, userdata, msg):

            texto = msg.payload.decode(
                "utf-8",
                errors="replace"
            )

            try:

                registro = procesar_payload(msg.payload)

                if registro["source"] != "REAL":
                    registro["source"] = "REAL"

                self.guardar_registro(registro, texto)

                with self.lock:

                    self.mensajes_recibidos += 1
                    self.ultimo_topic = msg.topic
                    self.ultimo_payload = texto[:1000]
                    self.ultimo_error_payload = None

            except Exception as e:

                with self.lock:

                    self.mensajes_invalidos += 1
                    self.ultimo_error_payload = str(e)

        try:

            cliente = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )

            cliente.on_connect = on_connect
            cliente.on_disconnect = on_disconnect
            cliente.on_message = on_message

            cliente.reconnect_delay_set(
                min_delay=1,
                max_delay=30
            )

            if MQTT_USERNAME:
                cliente.username_pw_set(
                    MQTT_USERNAME,
                    MQTT_PASSWORD
                )

            if MQTT_TLS:
                cliente.tls_set()

            cliente.connect(
                BROKER_IP,
                BROKER_PORT,
                60
            )

            cliente.loop_start()

            self.client = cliente

        except Exception as e:

            self.mqtt_error = str(e)


@st.cache_resource
def obtener_recolector():
    return RecolectorMQTT()


recolector = obtener_recolector()


# ============================================================
# 8. CONSULTA HISTÓRICA
# ============================================================

@st.cache_data(ttl=10)
def cargar_dia(fecha, conteo):
    return recolector.obtener_lecturas_fecha(fecha)


def exportar_csv(df):

    if df.empty:
        return b""

    columnas = [
        "Fecha",
        "Hora",
        "Momento",
        "source",
        "device_id",
        "elm_connected"
    ]

    for clave in CLAVES_PARAMETROS:
        columnas.extend([
            clave,
            f"{clave}_valid"
        ])

    columnas = [
        c for c in columnas
        if c in df.columns
    ]

    return df[columnas].to_csv(
        index=False
    ).encode("utf-8-sig")


# ============================================================
# 9. ESTADO DE LA INTERFAZ
# ============================================================

if "fecha_seleccionada" not in st.session_state:
    st.session_state.fecha_seleccionada = obtener_ahora().strftime("%Y-%m-%d")

if "simulacion_activa" not in st.session_state:
    st.session_state.simulacion_activa = False

if "grafica_pausada" not in st.session_state:
    st.session_state.grafica_pausada = False


# ============================================================
# 10. ENCABEZADO
# ============================================================

st.title("🚙 Mazda BT-50 | Dashboard OBD-II")

st.markdown(
    """
    Sistema de monitoreo de variables del motor mediante una ESP32,
    adaptador ELM327, Bluetooth, MQTT y almacenamiento histórico.

    Permite visualizar parámetros OBD-II, consultar registros anteriores
    y analizar el comportamiento del vehículo.
    """
)

st.divider()


# ============================================================
# 11. CONTROL DE SIMULACIÓN
# ============================================================

st.subheader("🧪 Modo de pruebas")

col_sim, col_info = st.columns([1, 3])

with col_sim:

    st.session_state.simulacion_activa = st.toggle(
        "Activar datos simulados",
        value=st.session_state.simulacion_activa,
        help="Genera lecturas ficticias para probar el dashboard."
    )

with col_info:

    if st.session_state.simulacion_activa:

        st.warning(
            "Modo de simulación activo. Los registros generados "
            "son ficticios y se guardan identificados como SIMULADO."
        )

    else:

        st.info(
            "Modo de simulación desactivado. "
            "El dashboard espera mensajes MQTT reales."
        )


# ============================================================
# 12. DASHBOARD PRINCIPAL
# ============================================================

@st.fragment(run_every=INTERVALO_ACTUALIZACION)
def dashboard():

    fecha_hoy = obtener_ahora().strftime("%Y-%m-%d")

    # --------------------------------------------------------
    # Generar una lectura simulada por actualización
    # --------------------------------------------------------

    if st.session_state.simulacion_activa:

        registro_simulado = generar_datos_simulados()

        recolector.guardar_registro(
            registro_simulado,
            "REGISTRO SIMULADO"
        )

    # --------------------------------------------------------
    # Obtener información del recolector
    # --------------------------------------------------------

    with recolector.lock:

        mqtt_conectado = recolector.mqtt_connected

        mqtt_error = recolector.mqtt_error

        ultimo_ts = recolector.ultimo_mensaje_ts

        ultima_hora = recolector.ultima_hora

        ultimo_device_id = recolector.ultimo_device_id

        ultimo_elm = recolector.ultimo_elm_connected

        ultimo_topic = recolector.ultimo_topic

        ultimo_payload = recolector.ultimo_payload

        ultimo_error_payload = recolector.ultimo_error_payload

        mensajes_recibidos = recolector.mensajes_recibidos

        mensajes_invalidos = recolector.mensajes_invalidos

        mensajes_reales = recolector.mensajes_reales

        mensajes_simulados = recolector.mensajes_simulados

        total_global = recolector.total_historico

        conteos = dict(recolector.conteo_por_fecha)

        lista_hoy = list(recolector.historial_hoy)

    conteos.setdefault(fecha_hoy, 0)

    fechas = sorted(conteos.keys(), reverse=True)

    # --------------------------------------------------------
    # Estado de la ESP32
    # --------------------------------------------------------

    if ultimo_ts is not None:

        segundos_sin_datos = time.monotonic() - ultimo_ts

        esp32_conectada = (
            segundos_sin_datos <= TIMEOUT_ESP32_SEGUNDOS
        )

    else:

        segundos_sin_datos = None
        esp32_conectada = False

    # --------------------------------------------------------
    # Selección de fecha
    # --------------------------------------------------------

    st.subheader("📅 Historial y selección de jornada")

    if st.session_state.fecha_seleccionada not in fechas:
        st.session_state.fecha_seleccionada = fecha_hoy

    fecha_elegida = st.selectbox(
        "Seleccionar fecha:",
        options=fechas,
        index=fechas.index(st.session_state.fecha_seleccionada),
        format_func=lambda f: (
            f"{f} — {conteos.get(f, 0):,} registros"
        ),
        key="fecha_obd_selector"
    )

    st.session_state.fecha_seleccionada = fecha_elegida

    es_hoy = fecha_elegida == fecha_hoy

    if es_hoy:

        df = pd.DataFrame(lista_hoy)

    else:

        df = cargar_dia(
            fecha_elegida,
            conteos.get(fecha_elegida, 0)
        )

    total_dia = len(df)

    # --------------------------------------------------------
    # Exportación CSV
    # --------------------------------------------------------

    st.subheader("📥 Exportación de datos")

    if not df.empty:

        col_csv1, col_csv2 = st.columns(2)

        with col_csv1:

            st.download_button(
                "📅 Descargar jornada seleccionada",
                data=exportar_csv(df),
                file_name=f"historial_obd2_{fecha_elegida}.csv",
                mime="text/csv",
                use_container_width=True
            )

        with col_csv2:

            st.download_button(
                "🌐 Descargar historial completo",
                data=exportar_csv(
                    recolector.obtener_lecturas_fecha(fecha_elegida)
                ),
                file_name=f"historial_obd2_{fecha_elegida}.csv",
                mime="text/csv",
                use_container_width=True
            )

        st.caption(
            f"Registros de la jornada: {total_dia:,} | "
            f"Registros históricos totales: {total_global:,}"
        )

    else:

        st.info("Todavía no existen registros para esta fecha.")

    # --------------------------------------------------------
    # Estado de conexión
    # --------------------------------------------------------

    st.divider()

    st.subheader("📡 Estado del sistema")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Broker MQTT",
        "Conectado 🟢" if mqtt_conectado else "Desconectado 🔴"
    )

    c2.metric(
        "ESP32",
        "Conectada 🟢" if esp32_conectada else "Sin datos 🔴",
        delta=(
            f"Último: {ultima_hora}"
            if ultima_hora
            else "Esperando lecturas"
        )
    )

    if ultimo_elm is True:
        elm_estado = "Conectado 🟢"
    elif ultimo_elm is False:
        elm_estado = "Desconectado 🔴"
    else:
        elm_estado = "Sin información ⚪"

    c3.metric("ELM327", elm_estado)

    c4.metric(
        "Dispositivo",
        ultimo_device_id or "Sin identificar"
    )

    if not es_hoy:
        st.caption(
            "Los estados de conexión corresponden a la recepción actual, "
            "no a la fecha histórica seleccionada."
        )

    # --------------------------------------------------------
    # Últimas lecturas
    # --------------------------------------------------------

    st.divider()

    st.subheader("📊 Parámetros del vehículo")

    if df.empty:

        st.warning(
            f"No hay datos para {fecha_elegida}. "
            f"Esperando información en `{TOPIC}`."
        )

        return

    ultima = df.iloc[-1]

    columnas_kpi = st.columns(4)

    for i, clave in enumerate(CLAVES_PARAMETROS):

        valor = ultima.get(clave)

        valido = bool(
            ultima.get(f"{clave}_valid", False)
        )

        unidad = PARAMETROS[clave]["unidad"]

        nombre = PARAMETROS[clave]["nombre"]

        if valido and pd.notna(valor):

            texto = f"{float(valor):.2f} {unidad}"

        else:

            texto = "N/D"

        columnas_kpi[i % 4].metric(
            nombre,
            texto
        )

    st.caption(
        f"Última lectura registrada: {ultima.get('Hora', '--')} | "
        f"Origen: {ultima.get('source', 'Desconocido')}"
    )

    # --------------------------------------------------------
    # Selección de variable
    # --------------------------------------------------------

    st.divider()

    st.subheader("📈 Análisis de variables")

    nombres_variables = {
        clave: PARAMETROS[clave]["nombre"]
        for clave in CLAVES_PARAMETROS
    }

    variable = st.selectbox(
        "Selecciona el parámetro que deseas analizar:",
        options=CLAVES_PARAMETROS,
        format_func=lambda x: nombres_variables[x],
        key="variable_obd"
    )

    nombre_variable = PARAMETROS[variable]["nombre"]
    unidad = PARAMETROS[variable]["unidad"]

    # --------------------------------------------------------
    # Preparar datos válidos
    # --------------------------------------------------------

    df_variable = df.copy()

    df_variable[variable] = pd.to_numeric(
        df_variable[variable],
        errors="coerce"
    )

    df_variable = df_variable.dropna(
        subset=[variable]
    )

    if df_variable.empty:

        st.warning(
            f"No existen lecturas válidas para {nombre_variable}."
        )

        return

    # --------------------------------------------------------
    # Estadísticas
    # --------------------------------------------------------

    st.subheader(f"📐 Estadísticas: {nombre_variable}")

    serie = df_variable[variable]

    a, b, c, d, e = st.columns(5)

    a.metric("Último valor", f"{serie.iloc[-1]:.2f} {unidad}")

    b.metric("Mínimo", f"{serie.min():.2f} {unidad}")

    c.metric("Máximo", f"{serie.max():.2f} {unidad}")

    d.metric("Promedio", f"{serie.mean():.2f} {unidad}")

    e.metric("Mediana", f"{serie.median():.2f} {unidad}")

    # --------------------------------------------------------
    # Gráfica
    # --------------------------------------------------------

    st.divider()

    st.subheader(f"📈 Gráfica de {nombre_variable}")

    col_escala, col_media = st.columns(2)

    with col_escala:

        escala = st.selectbox(
            "Intervalo temporal:",
            [
                "Todos los datos",
                "Últimas 30 lecturas",
                "Últimas 120 lecturas",
                "Últimas 500 lecturas"
            ],
            key="escala_obd"
        )

    with col_media:

        mostrar_media = st.checkbox(
            "Mostrar media móvil",
            value=False,
            key="media_obd"
        )

    if escala == "Últimas 30 lecturas":
        df_plot = df_variable.tail(30).copy()

    elif escala == "Últimas 120 lecturas":
        df_plot = df_variable.tail(120).copy()

    elif escala == "Últimas 500 lecturas":
        df_plot = df_variable.tail(500).copy()

    else:
        df_plot = df_variable.copy()

    if len(df_plot) > MAX_PUNTOS_BLOQUE:

        salto = math.ceil(
            len(df_plot) / MAX_PUNTOS_BLOQUE
        )

        df_plot = df_plot.iloc[::salto].copy()

    df_plot["Momento_Plot"] = (
        pd.to_datetime(df_plot["Momento"])
        .dt.tz_localize(None)
    )

    df_plot = df_plot.sort_values("Momento_Plot")

    if mostrar_media and len(df_plot) > 1:

        df_plot["Media móvil"] = (
            df_plot[variable]
            .rolling(window=5, min_periods=1)
            .mean()
        )

    grafica_base = alt.Chart(df_plot).encode(
        x=alt.X(
            "Momento_Plot:T",
            title="Hora",
            axis=alt.Axis(
                format="%I:%M:%S%p",
                labelAngle=0
            )
        ),
        y=alt.Y(
            f"{variable}:Q",
            title=f"{nombre_variable} ({unidad})",
            scale=alt.Scale(zero=False)
        )
    )

    puntos = grafica_base.mark_circle(
        size=65
    ).encode(
        tooltip=[
            alt.Tooltip("Fecha:N"),
            alt.Tooltip("Hora:N"),
            alt.Tooltip(
                f"{variable}:Q",
                title=nombre_variable,
                format=".2f"
            ),
            alt.Tooltip("source:N", title="Origen")
        ]
    )

    linea = grafica_base.mark_line(
        strokeWidth=2
    )

    grafica = linea + puntos

    if mostrar_media and len(df_plot) > 1:

        media = alt.Chart(df_plot).mark_line(
            strokeDash=[6, 4],
            strokeWidth=3
        ).encode(
            x="Momento_Plot:T",
            y=alt.Y(
                "Media móvil:Q",
                title=f"{nombre_variable} ({unidad})"
            )
        )

        grafica = grafica + media

    st.altair_chart(
        grafica.properties(height=400).interactive(),
        use_container_width=True
    )

    # --------------------------------------------------------
    # Tabla histórica
    # --------------------------------------------------------

    st.divider()

    st.subheader("📋 Registro de lecturas")

    columnas_tabla = [
        "Fecha",
        "Hora",
        "source",
        "device_id"
    ]

    for clave in CLAVES_PARAMETROS:

        columnas_tabla.extend([
            clave,
            f"{clave}_valid"
        ])

    columnas_tabla = [
        c for c in columnas_tabla
        if c in df.columns
    ]

    tabla = df[columnas_tabla].iloc[::-1].copy()

    tabla = tabla.rename(
        columns={
            clave: (
                f"{PARAMETROS[clave]['nombre']} "
                f"({PARAMETROS[clave]['unidad']})"
            )
            for clave in CLAVES_PARAMETROS
        }
    )

    st.dataframe(
        tabla,
        use_container_width=True,
        hide_index=True,
        height=400
    )

    # --------------------------------------------------------
    # Diagnóstico MQTT
    # --------------------------------------------------------

    st.divider()

    with st.expander(
        "🛠️ Diagnóstico MQTT y recepción",
        expanded=False
    ):

        x1, x2, x3 = st.columns(3)

        x1.metric(
            "Mensajes válidos",
            f"{mensajes_recibidos:,}"
        )

        x2.metric(
            "Mensajes inválidos",
            f"{mensajes_invalidos:,}"
        )

        x3.metric(
            "Registros simulados",
            f"{mensajes_simulados:,}"
        )

        st.write(f"**Broker:** `{BROKER_IP}:{BROKER_PORT}`")

        st.write(f"**Tópico:** `{TOPIC}`")

        st.write(
            f"**Último tópico recibido:** "
            f"`{ultimo_topic or 'Sin mensajes'}`"
        )

        st.write(
            f"**Último payload recibido:** "
            f"`{ultimo_payload or 'Sin mensajes'}`"
        )

        st.write(
            f"**Mensajes reales guardados:** {mensajes_reales:,}"
        )

        st.write(
            f"**Mensajes simulados guardados:** {mensajes_simulados:,}"
        )

        if ultimo_error_payload:
            st.error(
                f"Último mensaje descartado: {ultimo_error_payload}"
            )

        if mqtt_error:
            st.error(mqtt_error)

        st.caption(
            "La conexión MQTT y la recepción de datos son procesos "
            "independientes. La ESP32 se considera activa cuando llegan "
            "mensajes recientes."
        )


# ============================================================
# 13. EJECUCIÓN
# ============================================================

dashboard()
