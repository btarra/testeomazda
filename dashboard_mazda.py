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
MUESTRAS_POR_REGIMEN = 30


# ============================================================
# 2. DEFINICIÓN DE LOS 8 PARÁMETROS OBD-II (MAZDA BT-50 DIÉSEL)
# ============================================================

PARAMETROS = {
    "rpm": {
        "nombre": "Revoluciones del motor",
        "unidad": "rpm",
        "alias": ["rpm", "engine_rpm", "engine_speed"],
        "valid_alias": ["rpm_valid", "valid_rpm"]
    },
    "acelerador": {
        "nombre": "Posición del acelerador",
        "unidad": "%",
        "alias": [
            "accel_pos_d", "acelerador", "accelerator",
            "accelerator_position", "accelerator_pedal_position"
        ],
        "valid_alias": ["accel_pos_d_valid", "acelerador_valid", "valid_acelerador"]
    },
    "carga_motor": {
        "nombre": "Carga del motor",
        "unidad": "%",
        "alias": ["engine_load", "carga_motor", "load"],
        "valid_alias": ["engine_load_valid", "carga_motor_valid", "valid_carga_motor"]
    },
    "map": {
        "nombre": "Presión MAP",
        "unidad": "kPa",
        "alias": ["intake_map", "map", "manifold_pressure"],
        "valid_alias": ["intake_map_valid", "map_valid", "valid_map"]
    },
    "maf": {
        "nombre": "Flujo de aire MAF",
        "unidad": "g/s",
        "alias": ["maf", "mass_air_flow"],
        "valid_alias": ["maf_valid", "valid_maf"]
    },
    "temp_refrigerante": {
        "nombre": "Temperatura del refrigerante",
        "unidad": "°C",
        "alias": [
            "coolant_temp", "temp_refrigerante",
            "coolant_temperature", "temperatura_refrigerante"
        ],
        "valid_alias": [
            "coolant_temp_valid", "temp_refrigerante_valid",
            "valid_temp_refrigerante"
        ]
    },
    "presion_combustible": {
        "nombre": "Presión del riel de combustible",
        "unidad": "kPa",
        "alias": [
            "fuel_rail_kpa", "presion_combustible",
            "fuel_rail_pressure", "fuel_pressure"
        ],
        "valid_alias": [
            "fuel_rail_valid", "fuel_rail_kpa_valid",
            "presion_combustible_valid", "valid_presion_combustible"
        ]
    },
    "temp_admision": {
        "nombre": "Temperatura del aire de admisión",
        "unidad": "°C",
        "alias": [
            "intake_air_temp", "temp_admision",
            "intake_air_temperature", "intake_temp"
        ],
        "valid_alias": [
            "intake_air_valid", "intake_air_temp_valid",
            "temp_admision_valid", "valid_temp_admision"
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
    "temp_admision"
]

REGIMENES_IFAC = {
    "ralenti": {
        "nombre": "1. Ralentí estable (~715–725 RPM, Refrigerante > 75 °C)",
        "corto": "Ralentí estable",
        "descripcion": "Motor en marcha mínima estable entre 715 y 725 RPM con temperatura de refrigerante superior a 75 °C."
    },
    "rampa": {
        "nombre": "2. Aceleración progresiva en rampa (10 %–50 % pedal hasta ~2000 RPM)",
        "corto": "Aceleración en rampa",
        "descripcion": "Apertura progresiva del pedal acelerador entre 10 % y 50 % alcanzando hasta ~2000 RPM."
    },
    "sostenido": {
        "nombre": "3. Régimen sostenido (~2500 RPM)",
        "corto": "Sostenido 2500 RPM",
        "descripcion": "Velocidad de giro sostenida alrededor de 2500 RPM (rango operativo 2450–2550 RPM)."
    }
}


# ============================================================
# 3. CONFIGURACIÓN DE STREAMLIT
# ============================================================

st.set_page_config(
    page_title="Mazda BT-50 Diésel | Telemetría OBD-II",
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
# 4. UTILIDADES Y EVALUACIÓN DE RÉGIMEN EXPERIMENTAL
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


def evaluar_regimen(regimen_key, registro):
    """
    Evalúa si una trama cumple:
    1) Validez simultánea de los 8 PIDs.
    2) Condiciones físicas del régimen experimental (IFAC 2027).
    Retorna: (todos_pids_validos, cumple_fisica, detalle_texto)
    """
    todos_validos = all(
        bool(registro.get(f"{clave}_valid", False)) and registro.get(clave) is not None
        for clave in CLAVES_PARAMETROS
    )

    if not todos_validos:
        faltantes = [
            PARAMETROS[k]["nombre"]
            for k in CLAVES_PARAMETROS
            if not (bool(registro.get(f"{k}_valid", False)) and registro.get(k) is not None)
        ]
        return False, False, f"PIDs incompletos ({len(faltantes)} sin dato)"

    rpm = float(registro["rpm"])
    pedal = float(registro["acelerador"])
    temp_ref = float(registro["temp_refrigerante"])

    if regimen_key == "ralenti":
        ok_rpm = 715.0 <= rpm <= 725.0
        ok_temp = temp_ref > 75.0
        cumple = ok_rpm and ok_temp
        detalle = (
            f"RPM: {rpm:.1f} (meta 715–725) | "
            f"Refrigerante: {temp_ref:.1f} °C (meta > 75 °C)"
        )
        return True, cumple, detalle

    if regimen_key == "rampa":
        ok_pedal = 10.0 <= pedal <= 50.0
        ok_rpm = 720.0 <= rpm <= 2050.0
        cumple = ok_pedal and ok_rpm
        detalle = (
            f"Pedal: {pedal:.1f} % (meta 10–50 %) | "
            f"RPM: {rpm:.1f} (meta ≤ 2000)"
        )
        return True, cumple, detalle

    if regimen_key == "sostenido":
        ok_rpm = 2450.0 <= rpm <= 2550.0
        cumple = ok_rpm
        detalle = f"RPM: {rpm:.1f} (meta ~2500 ± 50)"
        return True, cumple, detalle

    return True, False, "Régimen desconocido"


# ============================================================
# 5. PROCESAMIENTO DEL JSON OBD-II (8 PIDS ESP32)
# ============================================================

def procesar_payload(payload):
    """
    Convierte el JSON emitido por el firmware ESP32 (8 PIDs depurados)
    en un registro uniforme para SQLite y Streamlit.
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

        validez_individual = buscar_clave(
            validez,
            configuracion["alias"]
        )

        if validez_individual is None:
            validez_individual = buscar_clave(
                objeto,
                configuracion.get("valid_alias", [f"{clave}_valid"])
            )

        if isinstance(validez_individual, str):
            validez_individual = (
                validez_individual.lower() in
                ("true", "1", "yes", "ok")
            )

        if validez_individual is None:
            valido = valor is not None
        else:
            valido = bool(validez_individual) and valor is not None

        registro[clave] = valor if valido else None
        registro[f"{clave}_valid"] = valido

    return registro


# ============================================================
# 6. GENERADOR DE DATOS SIMULADOS (8 PIDS MAZDA BT-50 DIÉSEL)
# ============================================================

def generar_datos_simulados(regimen_objetivo=None):
    """
    Genera valores ficticios dentro de los regímenes experimentales
    de la Mazda BT-50 Diésel. Si hay una captura activa, ajusta la
    simulación al régimen seleccionado para validar el protocolo.
    """
    if regimen_objetivo == "ralenti":
        rpm = random.uniform(716.0, 724.0)
        acelerador = 0.0
        temp_ref = random.uniform(77.0, 86.0)
    elif regimen_objetivo == "rampa":
        acelerador = random.uniform(12.0, 48.0)
        rpm = 750.0 + (acelerador / 50.0) * 1250.0 + random.uniform(-20, 20)
        temp_ref = random.uniform(78.0, 88.0)
    elif regimen_objetivo == "sostenido":
        rpm = random.uniform(2475.0, 2525.0)
        acelerador = random.uniform(35.0, 46.0)
        temp_ref = random.uniform(80.0, 91.0)
    else:
        rpm = random.uniform(715.0, 2550.0)
        acelerador = max(0.0, min(55.0, (rpm - 715.0) / 35.0 + random.uniform(-2, 3)))
        temp_ref = random.uniform(76.0, 92.0)

    carga = min(100.0, max(12.0, (rpm / 2600.0) * 75.0 + random.uniform(-4, 6)))

    return {
        "Momento": obtener_ahora(),
        "device_id": "ESP32_SIMULADA",
        "boot_id": "SIMULACION",
        "reading_id": str(time.time_ns()),
        "source": "SIMULADO",
        "elm_connected": True,
        "hz": round(random.uniform(10.2, 12.4), 2),
        "ts_ms": None,

        "rpm": float(rpm),
        "acelerador": float(acelerador),
        "carga_motor": float(carga),
        "map": random.uniform(98.0, 185.0),
        "maf": random.uniform(6.5, 48.0),
        "temp_refrigerante": float(temp_ref),
        "presion_combustible": random.uniform(24000.0, 95000.0),
        "temp_admision": random.uniform(22.0, 42.0),

        **{
            f"{clave}_valid": True
            for clave in CLAVES_PARAMETROS
        }
    }


# ============================================================
# 7. RECOLECTOR MQTT, BASE DE DATOS Y PROTOCOLO 90 MUESTRAS
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
        self.ultimo_hz = None

        # Estado de captura guiada (Protocolo 90 muestras IFAC 2027)
        self.captura_activa = False
        self.regimen_activo = "ralenti"
        self.validar_condiciones_fisicas = True
        self.conteo_regimenes = {
            "ralenti": 0,
            "rampa": 0,
            "sostenido": 0
        }
        self.estado_captura_msg = "Listo para iniciar grabación por régimen."

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

        sql_general = f"""
        CREATE TABLE IF NOT EXISTS obd_lecturas (
            {",".join(definiciones)}
        )
        """

        # Tabla dedicada para el dataset limpio de 90 muestras (IFAC 2027)
        def_ifac = [
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "regimen TEXT NOT NULL",
            "regimen_nombre TEXT NOT NULL",
            "muestra_num INTEGER NOT NULL",
            "fecha TEXT NOT NULL",
            "hora TEXT NOT NULL",
            "momento_iso TEXT NOT NULL",
            "ts_ms REAL",
            "hz REAL",
            "reading_id TEXT",
            "device_id TEXT",
            "source TEXT NOT NULL"
        ]

        for clave in CLAVES_PARAMETROS:
            def_ifac.append(f"{clave} REAL")
            def_ifac.append(f"{clave}_valid INTEGER NOT NULL DEFAULT 0")

        sql_ifac = f"""
        CREATE TABLE IF NOT EXISTS muestras_ifac (
            {",".join(def_ifac)}
        )
        """

        with self.lock:
            self.db_conn.execute(sql_general)
            self.db_conn.execute(sql_ifac)

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

            # Cargar conteo actual de muestras IFAC por régimen
            cur.execute(
                """
                SELECT regimen, COUNT(*)
                FROM muestras_ifac
                GROUP BY regimen
                """
            )
            for reg, cant in cur.fetchall():
                if reg in self.conteo_regimenes:
                    self.conteo_regimenes[reg] = int(cant)

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
                    registro[clave] = registro.get(clave)
                    registro[f"{clave}_valid"] = bool(
                        registro.get(f"{clave}_valid", 0)
                    )

                self.historial_hoy.append(registro)

            self.fecha_actual = obtener_ahora().strftime("%Y-%m-%d")

    def iniciar_captura_regimen(self, regimen_key, validar_fisica=True):
        with self.lock:
            if regimen_key not in REGIMENES_IFAC:
                return
            actual = self.conteo_regimenes.get(regimen_key, 0)
            if actual >= MUESTRAS_POR_REGIMEN:
                self.captura_activa = False
                self.estado_captura_msg = (
                    f"El régimen '{REGIMENES_IFAC[regimen_key]['corto']}' "
                    f"ya tiene {MUESTRAS_POR_REGIMEN}/{MUESTRAS_POR_REGIMEN} muestras."
                )
                return

            self.regimen_activo = regimen_key
            self.validar_condiciones_fisicas = validar_fisica
            self.captura_activa = True
            self.estado_captura_msg = (
                f"Grabando {REGIMENES_IFAC[regimen_key]['corto']} "
                f"({actual}/{MUESTRAS_POR_REGIMEN})..."
            )

    def detener_captura(self):
        with self.lock:
            self.captura_activa = False
            self.estado_captura_msg = "Captura pausada por el usuario."

    def limpiar_regimen(self, regimen_key):
        with self.lock:
            self.captura_activa = False
            self.db_conn.execute(
                "DELETE FROM muestras_ifac WHERE regimen = ?",
                (regimen_key,)
            )
            self.db_conn.commit()
            self.conteo_regimenes[regimen_key] = 0
            self.estado_captura_msg = (
                f"Muestras reiniciadas para '{REGIMENES_IFAC[regimen_key]['corto']}'."
            )

    def _procesar_muestra_ifac_si_aplica(self, registro, fecha, hora):
        """
        Evalúa en tiempo real (dentro del lock) si la trama debe guardarse
        en el dataset experimental de 90 muestras.
        """
        if not self.captura_activa:
            return

        reg = self.regimen_activo
        actual = self.conteo_regimenes.get(reg, 0)

        if actual >= MUESTRAS_POR_REGIMEN:
            self.captura_activa = False
            return

        todos_validos, cumple_fisica, detalle = evaluar_regimen(reg, registro)

        if not todos_validos:
            self.estado_captura_msg = f"Esperando trama completa (8/8 PIDs): {detalle}"
            return

        if self.validar_condiciones_fisicas and not cumple_fisica:
            self.estado_captura_msg = f"Fuera de ventana operativa -> {detalle}"
            return

        nuevo_num = actual + 1

        columnas = [
            "regimen",
            "regimen_nombre",
            "muestra_num",
            "fecha",
            "hora",
            "momento_iso",
            "ts_ms",
            "hz",
            "reading_id",
            "device_id",
            "source"
        ]

        valores = [
            reg,
            REGIMENES_IFAC[reg]["corto"],
            nuevo_num,
            fecha,
            hora,
            registro["Momento"].isoformat(),
            registro.get("ts_ms"),
            registro.get("hz"),
            str(registro.get("reading_id", "")),
            registro.get("device_id"),
            registro.get("source", "REAL")
        ]

        for clave in CLAVES_PARAMETROS:
            columnas.append(clave)
            valores.append(registro.get(clave))
            columnas.append(f"{clave}_valid")
            valores.append(int(bool(registro.get(f"{clave}_valid", False))))

        placeholders = ",".join(["?"] * len(columnas))
        sql = f"""
        INSERT INTO muestras_ifac ({",".join(columnas)})
        VALUES ({placeholders})
        """

        self.db_conn.execute(sql, valores)
        self.conteo_regimenes[reg] = nuevo_num

        if nuevo_num >= MUESTRAS_POR_REGIMEN:
            self.captura_activa = False
            self.estado_captura_msg = (
                f"✅ ¡Completado! {MUESTRAS_POR_REGIMEN}/{MUESTRAS_POR_REGIMEN} "
                f"muestras válidas capturadas en '{REGIMENES_IFAC[reg]['corto']}'."
            )
        else:
            self.estado_captura_msg = (
                f"Grabando '{REGIMENES_IFAC[reg]['corto']}': "
                f"{nuevo_num}/{MUESTRAS_POR_REGIMEN} muestras válidas."
            )

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
            str(registro.get("boot_id", "")),
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

                # Evaluar también si corresponde al protocolo de 90 muestras
                self._procesar_muestra_ifac_si_aplica(registro, fecha, hora)

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
                self.ultimo_hz = registro.get("hz")

            except Exception as e:
                self.mqtt_error = f"Error SQLite: {e}"

    def obtener_muestras_ifac(self):
        with self.lock:
            cur = self.db_conn.cursor()
            cur.execute(
                """
                SELECT *
                FROM muestras_ifac
                ORDER BY
                    CASE regimen
                        WHEN 'ralenti' THEN 1
                        WHEN 'rampa' THEN 2
                        WHEN 'sostenido' THEN 3
                        ELSE 4
                    END,
                    muestra_num ASC
                """
            )
            filas = cur.fetchall()
            nombres = [d[0] for d in cur.description]

        if not filas:
            return pd.DataFrame()

        return pd.DataFrame([dict(zip(nombres, f)) for f in filas])

    def obtener_lecturas_fecha(self, fecha=None):

        with self.lock:

            cur = self.db_conn.cursor()

            if fecha:
                cur.execute(
                    """
                    SELECT *
                    FROM obd_lecturas
                    WHERE fecha = ?
                    ORDER BY id ASC
                    """,
                    (fecha,)
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM obd_lecturas
                    ORDER BY id ASC
                    """
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
                    registro.get(f"{clave}_valid", 0)
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
# 8. CONSULTA HISTÓRICA Y EXPORTACIÓN CSV
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
        "ts_ms",
        "hz",
        "reading_id",
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


def exportar_csv_ifac(df_ifac):
    if df_ifac.empty:
        return b""

    columnas = [
        "regimen",
        "regimen_nombre",
        "muestra_num",
        "fecha",
        "hora",
        "momento_iso",
        "ts_ms",
        "hz",
        "reading_id",
        "device_id",
        "source"
    ] + CLAVES_PARAMETROS

    columnas = [c for c in columnas if c in df_ifac.columns]
    return df_ifac[columnas].to_csv(index=False).encode("utf-8-sig")


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

st.title("🚙 Mazda BT-50 Diésel | Dashboard OBD-II (8 PIDs)")

st.markdown(
    """
    Sistema de telemetría remota de las **8 variables dinámicas depuradas**
    del banco diésel Mazda BT-50 mediante **ESP32 + ELM327 + MQTT**.

    Incluye control automatizado para el protocolo experimental de **90 muestras**
    (30 por régimen) requerido para el artículo **IFAC AMEST 2027** y la co-simulación FMU.
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
        help="Genera lecturas ficticias de los 8 PIDs para probar el dashboard y el protocolo de 90 muestras."
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
            "El dashboard espera mensajes MQTT reales desde la ESP32."
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
        with recolector.lock:
            reg_hint = (
                recolector.regimen_activo
                if recolector.captura_activa
                else None
            )

        registro_simulado = generar_datos_simulados(reg_hint)

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
        ultimo_hz = recolector.ultimo_hz

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

        captura_activa = recolector.captura_activa
        regimen_activo = recolector.regimen_activo
        validar_fisica = recolector.validar_condiciones_fisicas
        conteo_reg = dict(recolector.conteo_regimenes)
        estado_captura_msg = recolector.estado_captura_msg

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
    # Estado de conexión
    # --------------------------------------------------------

    st.subheader("📡 Estado del sistema")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Broker MQTT",
        "Conectado 🟢" if mqtt_conectado else "Desconectado 🔴"
    )

    delta_esp32 = "Esperando lecturas"
    if ultima_hora:
        hz_txt = f" ({ultimo_hz:.1f} Hz)" if ultimo_hz is not None else ""
        delta_esp32 = f"Último: {ultima_hora}{hz_txt}"

    c2.metric(
        "ESP32",
        "Conectada 🟢" if esp32_conectada else "Sin datos 🔴",
        delta=delta_esp32
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

    # --------------------------------------------------------
    # PROTOCOLO EXPERIMENTAL: TOMA DE LAS 90 MUESTRAS (IFAC 2027)
    # --------------------------------------------------------

    st.divider()
    st.subheader("🎯 Protocolo Experimental: Captura de las 90 Muestras (IFAC 2027)")

    total_90 = sum(conteo_reg.values())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "1. Ralentí estable",
        f"{conteo_reg['ralenti']} / {MUESTRAS_POR_REGIMEN}",
        delta="715–725 RPM | Ref > 75 °C"
    )
    m2.metric(
        "2. Aceleración rampa",
        f"{conteo_reg['rampa']} / {MUESTRAS_POR_REGIMEN}",
        delta="10–50 % pedal → ~2000 RPM"
    )
    m3.metric(
        "3. Régimen sostenido",
        f"{conteo_reg['sostenido']} / {MUESTRAS_POR_REGIMEN}",
        delta="~2500 RPM"
    )
    m4.metric(
        "Total Dataset IFAC",
        f"{total_90} / {MUESTRAS_POR_REGIMEN * 3}",
        delta="Listo para FMU" if total_90 >= 90 else "En progreso"
    )

    st.progress(min(1.0, total_90 / float(MUESTRAS_POR_REGIMEN * 3)))

    col_reg_sel, col_reg_ctrl = st.columns([2, 2])

    with col_reg_sel:
        claves_reg = list(REGIMENES_IFAC.keys())
        regimen_elegido = st.selectbox(
            "Régimen operativo a grabar:",
            options=claves_reg,
            index=claves_reg.index(regimen_activo),
            format_func=lambda k: REGIMENES_IFAC[k]["nombre"],
            disabled=captura_activa,
            key="selector_regimen_ifac"
        )

        validar_check = st.checkbox(
            "Validar automáticamente condiciones físicas (RPM, pedal % y refrigerante > 75 °C)",
            value=validar_fisica,
            disabled=captura_activa,
            key="check_validar_fisica_ifac"
        )

        # Semáforo en vivo de condiciones del régimen elegido
        if lista_hoy:
            ultima_muestra = lista_hoy[-1]
            ok_8, ok_cond, texto_cond = evaluar_regimen(regimen_elegido, ultima_muestra)
            if ok_8 and ok_cond:
                st.success(f"En ventana óptima: {texto_cond}")
            elif ok_8:
                st.warning(f"8/8 PIDs válidos, pero fuera de rango del régimen: {texto_cond}")
            else:
                st.error(f"Trama incompleta: {texto_cond}")

    with col_reg_ctrl:
        st.caption(REGIMENES_IFAC[regimen_elegido]["descripcion"])

        b1, b2, b3 = st.columns(3)

        with b1:
            if not captura_activa:
                if st.button(
                    "▶️ Grabar 30",
                    use_container_width=True,
                    type="primary",
                    key="btn_iniciar_ifac"
                ):
                    recolector.iniciar_captura_regimen(
                        regimen_elegido,
                        validar_fisica=validar_check
                    )
                    st.rerun()
            else:
                if st.button(
                    "⏹️ Detener",
                    use_container_width=True,
                    key="btn_detener_ifac"
                ):
                    recolector.detener_captura()
                    st.rerun()

        with b2:
            if st.button(
                "🗑️ Reiniciar régimen",
                use_container_width=True,
                disabled=captura_activa,
                key="btn_limpiar_ifac"
            ):
                recolector.limpiar_regimen(regimen_elegido)
                st.rerun()

        with b3:
            df_ifac = recolector.obtener_muestras_ifac()
            st.download_button(
                f"📥 CSV ({len(df_ifac)}/90)",
                data=exportar_csv_ifac(df_ifac),
                file_name="mazda_bt50_90_muestras_ifac2027.csv",
                mime="text/csv",
                disabled=df_ifac.empty,
                use_container_width=True,
                key="btn_descargar_90_ifac"
            )

        if captura_activa:
            st.info(f"🔴 {estado_captura_msg}")
        else:
            st.caption(f"Estado: {estado_captura_msg}")

    # --------------------------------------------------------
    # Selección de fecha e historial general
    # --------------------------------------------------------

    st.divider()
    st.subheader("📅 Historial continuo y selección de jornada")

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
    # Exportación CSV continuo
    # --------------------------------------------------------

    if not df.empty:

        col_csv1, col_csv2 = st.columns(2)

        with col_csv1:

            st.download_button(
                "📅 Descargar jornada continua seleccionada",
                data=exportar_csv(df),
                file_name=f"mazda_bt50_obd2_{fecha_elegida}.csv",
                mime="text/csv",
                use_container_width=True,
                key="btn_csv_jornada"
            )

        with col_csv2:

            st.download_button(
                "🌐 Descargar historial continuo completo",
                data=exportar_csv(
                    recolector.obtener_lecturas_fecha(None)
                ),
                file_name="mazda_bt50_obd2_completo.csv",
                mime="text/csv",
                use_container_width=True,
                key="btn_csv_completo"
            )

        st.caption(
            f"Registros continuos de la jornada: {total_dia:,} | "
            f"Registros históricos totales: {total_global:,}"
        )

    else:

        st.info("Todavía no existen registros continuos para esta fecha.")

    # --------------------------------------------------------
    # Últimas lecturas (8 KPIs en 2 filas de 4 columnas)
    # --------------------------------------------------------

    st.divider()

    st.subheader("📊 Parámetros del motor en vivo (8 PIDs depurados)")

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

    hz_ultima = ultima.get("hz")
    hz_info = f" | Tasa: {float(hz_ultima):.2f} Hz" if pd.notna(hz_ultima) else ""

    st.caption(
        f"Última lectura registrada: {ultima.get('Hora', '--')}{hz_info} | "
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
        "hz",
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

    renombres = {
        clave: (
            f"{PARAMETROS[clave]['nombre']} "
            f"({PARAMETROS[clave]['unidad']})"
        )
        for clave in CLAVES_PARAMETROS
    }
    renombres["hz"] = "Frecuencia (Hz)"

    tabla = tabla.rename(columns=renombres)

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
