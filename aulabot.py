"""
=======================================================================
 AULABOT  -  programa de la laptop
 Colegio Charles Babbage  |  Fractal-BOTS
-----------------------------------------------------------------------
 Boton 1  ->  empieza a grabar la clase
 Boton 2  ->  termina, transcribe con Whisper, resume con Gemini y sube
 Boton 3  ->  abre la camara y toma asistencia leyendo los QR
 Boton 4  ->  escucha una pregunta y la responde en voz alta

 Sin internet sigue funcionando: todo queda en una base local y se sube
 solo apenas vuelve la conexion.

     pip install -r requirements.txt
     python aulabot.py
=======================================================================
"""

import configparser
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
import wave
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import serial
from serial.tools import list_ports

BASE = Path(__file__).resolve().parent
DIR_AUDIO = BASE / "audio"
DIR_AUDIO.mkdir(exist_ok=True)
RUTA_DB = BASE / "aulabot.db"
RUTA_LOG = BASE / "aulabot.log"

BAUDIOS = 115200
MUESTREO = 16000  # Hz, lo que espera Whisper

DIAS = {1: "lunes", 2: "martes", 3: "miercoles", 4: "jueves",
        5: "viernes", 6: "sabado", 7: "domingo"}


# ------------------------------------------------------------------ log
_lock_log = threading.Lock()


def log(msg, etiqueta="  "):
    linea = f"{datetime.now():%H:%M:%S} {etiqueta} {msg}"
    with _lock_log:
        try:
            print(linea)
        except UnicodeEncodeError:
            print(linea.encode("ascii", "ignore").decode())
        try:
            with open(RUTA_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except Exception:
            pass


def ahora_iso():
    return datetime.now(timezone.utc).isoformat()


def sin_tildes(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if unicodedata.category(c) != "Mn").lower()


def normalizar_dispositivo(valor):
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in ("auto", "default"):
        return None
    try:
        return int(texto)
    except ValueError:
        return texto


# =====================================================================
#  La caja (ESP32 por USB)
# =====================================================================
class Caja:
    def __init__(self, puerto_cfg="auto"):
        self.ser = None
        self.puerto = None
        self.eventos = queue.Queue()
        self.vivo = threading.Event()
        self._cfg = puerto_cfg
        self._ultimo_vu = 0.0

    # -- busqueda ------------------------------------------------------
    VID_PID = {(0x10C4, 0xEA60), (0x1A86, 0x7523),
               (0x1A86, 0x55D4), (0x0403, 0x6001)}

    def _probar(self, dispositivo):
        try:
            s = serial.Serial(dispositivo, BAUDIOS, timeout=0.4)
        except Exception:
            return None
        time.sleep(2.0)                      # la ESP32 se reinicia al abrir
        s.reset_input_buffer()
        for _ in range(4):
            try:
                s.write(b"WHO\n")
            except Exception:
                break
            fin = time.time() + 0.8
            while time.time() < fin:
                linea = s.readline().decode("utf-8", "ignore").strip()
                if "HELLO AULABOT" in linea:
                    return s
        s.close()
        return None

    def conectar(self):
        if self._cfg and self._cfg != "auto":
            self.ser = self._probar(self._cfg)
            self.puerto = self._cfg if self.ser else None
        else:
            puertos = sorted(list_ports.comports(),
                             key=lambda p: (p.vid, p.pid) not in self.VID_PID)
            for p in puertos:
                s = self._probar(p.device)
                if s:
                    self.ser, self.puerto = s, p.device
                    break
        if not self.ser:
            return False
        self.vivo.set()
        threading.Thread(target=self._escuchar, daemon=True).start()
        threading.Thread(target=self._latido, daemon=True).start()
        log(f"caja conectada en {self.puerto}", "[OK]")
        return True

    # -- hilos ---------------------------------------------------------
    def _escuchar(self):
        while self.vivo.is_set():
            try:
                linea = self.ser.readline().decode("utf-8", "ignore").strip()
            except Exception:
                self.vivo.clear()
                break
            if linea.startswith("EV BTN "):
                boton = linea.split()[-1]
                log(f"boton {boton}", "[>]")
                self.eventos.put(boton)

    def _latido(self):
        while self.vivo.is_set():
            self.enviar("PING")
            time.sleep(2.0)

    # -- salida --------------------------------------------------------
    def enviar(self, cmd):
        if not self.ser:
            return
        try:
            self.ser.write((cmd + "\n").encode("utf-8", "ignore"))
        except Exception:
            self.vivo.clear()

    def estado(self, e):
        self.enviar(f"ST {e}")

    def mensaje(self, t):
        self.enviar("MSG " + t[:24])

    def titulo(self, t):
        self.enviar("TIT " + t[:10])

    def cronometro(self, seg):
        self.enviar(f"TMR {int(seg)}")

    def vu(self, nivel):
        self.enviar(f"VU {max(0, min(100, int(nivel)))}")

    def nube(self, hay):
        self.enviar(f"NET {1 if hay else 0}")

    def cerrar(self):
        self.vivo.clear()
        time.sleep(0.3)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass


# =====================================================================
#  Base local (SQLite): copia de los datos + cola de subida
# =====================================================================
class Local:
    def __init__(self):
        self.con = sqlite3.connect(RUTA_DB, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self._crear()

    def _crear(self):
        with self.lock:
            self.con.executescript("""
            CREATE TABLE IF NOT EXISTS cursos(
                id TEXT PRIMARY KEY, nombre TEXT);
            CREATE TABLE IF NOT EXISTS estudiantes(
                id TEXT PRIMARY KEY, curso_id TEXT, nombres TEXT,
                apellidos TEXT, codigo_qr TEXT, activo INTEGER);
            CREATE TABLE IF NOT EXISTS horarios(
                id TEXT PRIMARY KEY, curso_id TEXT, dia_semana INTEGER,
                orden INTEGER, materia TEXT, hora_inicio TEXT, hora_fin TEXT);
            CREATE TABLE IF NOT EXISTS clases(
                id TEXT PRIMARY KEY, curso_id TEXT, tema TEXT, estado TEXT,
                inicio TEXT, fin TEXT, duracion_seg INTEGER, resumen TEXT,
                temas TEXT, tareas TEXT);
            CREATE TABLE IF NOT EXISTS asistencias(
                clase_id TEXT, estudiante_id TEXT, hora TEXT, metodo TEXT,
                PRIMARY KEY(clase_id, estudiante_id));
            CREATE TABLE IF NOT EXISTS pendientes(
                n INTEGER PRIMARY KEY AUTOINCREMENT, metodo TEXT, tabla TEXT,
                params TEXT, cuerpo TEXT, prefer TEXT, creado TEXT);
            """)
            self.con.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def e(self, sql, args=()):
        with self.lock:
            self.con.execute(sql, args)
            self.con.commit()

    def guardar_catalogo(self, cursos, estudiantes, horarios):
        with self.lock:
            c = self.con
            c.execute("DELETE FROM cursos")
            c.executemany("INSERT INTO cursos VALUES(?,?)",
                          [(x["id"], x["nombre"]) for x in cursos])
            c.execute("DELETE FROM estudiantes")
            c.executemany("INSERT INTO estudiantes VALUES(?,?,?,?,?,?)",
                          [(x["id"], x["curso_id"], x["nombres"], x["apellidos"],
                            x["codigo_qr"], 1 if x.get("activo", True) else 0)
                           for x in estudiantes])
            c.execute("DELETE FROM horarios")
            c.executemany("INSERT INTO horarios VALUES(?,?,?,?,?,?,?)",
                          [(x["id"], x["curso_id"], x["dia_semana"], x["orden"],
                            x["materia"], x.get("hora_inicio"), x.get("hora_fin"))
                           for x in horarios])
            c.commit()

    def encolar(self, metodo, tabla, params, cuerpo, prefer):
        self.e("INSERT INTO pendientes(metodo,tabla,params,cuerpo,prefer,creado)"
               " VALUES(?,?,?,?,?,?)",
               (metodo, tabla, json.dumps(params or {}),
                json.dumps(cuerpo) if cuerpo is not None else None,
                prefer or "", ahora_iso()))

    def pendientes(self):
        return self.q("SELECT * FROM pendientes ORDER BY n")

    def borrar_pendiente(self, n):
        self.e("DELETE FROM pendientes WHERE n=?", (n,))


# =====================================================================
#  Supabase (REST, entrando como el docente)
# =====================================================================
class Nube:
    def __init__(self, cfg, local):
        self.url = cfg["url"].rstrip("/")
        self.apikey = cfg["clave_publica"]
        self.correo = cfg["correo_docente"]
        self.clave = cfg["clave_docente"]
        self.local = local
        self.token = None
        self.refresh = None
        self.vence = 0
        self.hay = False

    # -- sesion --------------------------------------------------------
    def entrar(self):
        try:
            r = requests.post(
                f"{self.url}/auth/v1/token?grant_type=password",
                headers={"apikey": self.apikey, "Content-Type": "application/json"},
                json={"email": self.correo, "password": self.clave}, timeout=12)
            if r.status_code != 200:
                log(f"no pude entrar a Supabase: {r.text[:120]}", "[!]")
                self.hay = False
                return False
            d = r.json()
            self.token = d["access_token"]
            self.refresh = d.get("refresh_token")
            self.vence = time.time() + d.get("expires_in", 3600) - 120
            self.hay = True
            return True
        except Exception as ex:
            self.hay = False
            log(f"sin internet ({type(ex).__name__})", "[!]")
            return False

    def _vigente(self):
        if not self.token:
            return self.entrar()
        if time.time() < self.vence:
            return True
        try:
            r = requests.post(
                f"{self.url}/auth/v1/token?grant_type=refresh_token",
                headers={"apikey": self.apikey, "Content-Type": "application/json"},
                json={"refresh_token": self.refresh}, timeout=12)
            if r.status_code == 200:
                d = r.json()
                self.token = d["access_token"]
                self.refresh = d.get("refresh_token", self.refresh)
                self.vence = time.time() + d.get("expires_in", 3600) - 120
                return True
        except Exception:
            pass
        return self.entrar()

    def _cab(self, prefer=None):
        h = {"apikey": self.apikey, "Authorization": f"Bearer {self.token}",
             "Content-Type": "application/json"}
        if prefer:
            h["Prefer"] = prefer
        return h

    # -- operaciones ---------------------------------------------------
    def pedir(self, tabla, params):
        """Lectura. Devuelve [] si no hay internet."""
        if not self._vigente():
            return []
        try:
            r = requests.get(f"{self.url}/rest/v1/{tabla}",
                             headers=self._cab(), params=params, timeout=15)
            r.raise_for_status()
            self.hay = True
            return r.json()
        except Exception as ex:
            self.hay = False
            log(f"lectura de {tabla} fallo: {type(ex).__name__}", "[!]")
            return []

    def escribir(self, metodo, tabla, params=None, cuerpo=None, prefer=None):
        """Escritura con reintento. Si no hay internet, queda en la cola."""
        if self._vigente():
            try:
                r = requests.request(metodo, f"{self.url}/rest/v1/{tabla}",
                                     headers=self._cab(prefer), params=params,
                                     json=cuerpo, timeout=15)
                if r.status_code < 300:
                    self.hay = True
                    return True
                log(f"{metodo} {tabla} -> {r.status_code} {r.text[:120]}", "[!]")
            except Exception:
                self.hay = False
        self.local.encolar(metodo, tabla, params, cuerpo, prefer)
        return False

    def sincronizar(self):
        """Sube lo que quedo pendiente. Devuelve cuantas subio."""
        pend = self.local.pendientes()
        if not pend:
            return 0
        if not self._vigente():
            return 0
        subidas = 0
        for p in pend:
            try:
                r = requests.request(
                    p["metodo"], f"{self.url}/rest/v1/{p['tabla']}",
                    headers=self._cab(p["prefer"] or None),
                    params=json.loads(p["params"] or "{}"),
                    json=json.loads(p["cuerpo"]) if p["cuerpo"] else None,
                    timeout=20)
                if r.status_code < 300 or r.status_code == 409:
                    self.local.borrar_pendiente(p["n"])
                    subidas += 1
                else:
                    log(f"pendiente {p['n']} -> {r.status_code}", "[!]")
                    break
            except Exception:
                self.hay = False
                break
        if subidas:
            log(f"{subidas} cambios subidos a Supabase", "[OK]")
        return subidas


# =====================================================================
#  Gemini
# =====================================================================
class Gemini:
    RAIZ = "https://generativelanguage.googleapis.com/v1beta"
    PREFERIDOS = ["gemini-2.0-flash", "gemini-1.5-flash",
                  "gemini-flash-latest", "gemini-2.5-flash-lite",
                  "gemini-2.5-flash"]

    def __init__(self, api_key, modelo="auto"):
        self.key = (api_key or "").strip()
        self.modelo = None if modelo in ("", "auto") else modelo

    def listo(self):
        return bool(self.key) and not self.key.startswith("PON_AQUI")

    def elegir_modelo(self):
        if self.modelo or not self.listo():
            return self.modelo
        try:
            r = requests.get(f"{self.RAIZ}/models", params={"key": self.key}, timeout=15)
            r.raise_for_status()
            disponibles = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                           if "generateContent" in m.get("supportedGenerationMethods", [])]
            for p in self.PREFERIDOS:
                if p in disponibles:
                    self.modelo = p
                    break
            else:
                rapidos = [d for d in disponibles if "flash" in d]
                self.modelo = (rapidos or disponibles or [None])[0]
            if self.modelo:
                log(f"modelo de Gemini: {self.modelo}", "[OK]")
        except Exception as ex:
            log(f"no pude listar modelos de Gemini ({type(ex).__name__})", "[!]")
        return self.modelo

    def preguntar(self, prompt, json_puro=False, timeout=90):
        if not self.listo():
            return None

        modelos = []
        if self.modelo:
            modelos.append(self.modelo)
        modelos.extend(p for p in self.PREFERIDOS if p not in modelos)

        try:
            r = requests.get(f"{self.RAIZ}/models", params={"key": self.key}, timeout=15)
            if r.status_code == 200:
                disponibles = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                               if "generateContent" in m.get("supportedGenerationMethods", [])]
                for p in disponibles:
                    if p not in modelos:
                        modelos.append(p)
        except Exception:
            pass

        for modelo in modelos:
            if not modelo:
                continue
            cfg = {"temperature": 0.3, "maxOutputTokens": 2048}
            if json_puro:
                cfg["responseMimeType"] = "application/json"
            try:
                r = requests.post(
                    f"{self.RAIZ}/models/{modelo}:generateContent",
                    params={"key": self.key},
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": cfg},
                    timeout=timeout)
                if r.status_code == 200:
                    self.modelo = modelo
                    partes = r.json()["candidates"][0]["content"]["parts"]
                    return "".join(p.get("text", "") for p in partes).strip()
                texto = r.text[:160]
                if r.status_code in (400, 404):
                    log(f"Gemini modelo {modelo} no disponible; probando otro... ({texto})", "[!]")
                    continue
                log(f"Gemini {modelo} {r.status_code}: {texto}", "[!]")
                return None
            except Exception as ex:
                log(f"Gemini fallo con {modelo} ({type(ex).__name__})", "[!]")
                continue

        if not self.modelo:
            self.modelo = None
        return None


# =====================================================================
#  Microfono
# =====================================================================
class Microfono:
    def __init__(self, dispositivo="auto"):
        import sounddevice as sd
        self.sd = sd
        self.dispositivo = normalizar_dispositivo(dispositivo)
        self.stream = None
        self.trozos = []
        self.nivel = 0.0
        self.t0 = None

    def _callback(self, datos, frames, tiempo, estado):
        import numpy as np
        self.trozos.append(datos.copy())
        self.nivel = float(np.sqrt(np.mean(np.square(datos))))

    def abrir(self):
        self.trozos = []
        self.t0 = time.time()
        self.stream = self.sd.InputStream(
            samplerate=MUESTREO, channels=1, dtype="float32",
            device=self.dispositivo, callback=self._callback, blocksize=1024)
        self.stream.start()

    def cerrar(self, ruta):
        import numpy as np
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if not self.trozos:
            return 0.0
        datos = np.concatenate(self.trozos, axis=0).flatten()
        pcm = np.clip(datos, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        with wave.open(str(ruta), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MUESTREO)
            w.writeframes(pcm.tobytes())
        dur = len(pcm) / MUESTREO
        self.trozos = []
        return dur

    def grabar_segundos(self, segundos, ruta):
        self.abrir()
        time.sleep(segundos)
        return self.cerrar(ruta)

    def vu_0_100(self):
        # el RMS de voz ronda 0.01 - 0.2; lo estiramos para que se vea bonito
        return min(100, int(self.nivel * 600))


# =====================================================================
#  Whisper
# =====================================================================
class Transcriptor:
    def __init__(self, modelo="small", computo="int8", idioma="es"):
        self.nombre = modelo
        self.computo = computo
        self.idioma = idioma
        self.modelo = None

    def cargar(self):
        if self.modelo:
            return
        from faster_whisper import WhisperModel
        log(f"cargando Whisper '{self.nombre}' (la primera vez se descarga)...")
        self.modelo = WhisperModel(self.nombre, device="auto",
                                   compute_type=self.computo)
        log("Whisper listo", "[OK]")

    def transcribir(self, ruta):
        self.cargar()
        import numpy as np
        with wave.open(str(ruta), "rb") as w:
            crudo = w.readframes(w.getnframes())
        audio = np.frombuffer(crudo, dtype=np.int16).astype(np.float32) / 32768.0
        segmentos, _ = self.modelo.transcribe(
            audio, language=self.idioma, vad_filter=True,
            beam_size=1, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segmentos).strip()


# =====================================================================
#  Voz
# =====================================================================
class Voz:
    def __init__(self):
        self.motor = None

    def _motor(self):
        if self.motor:
            return self.motor
        import pyttsx3
        m = pyttsx3.init()
        for v in m.getProperty("voices"):
            etiqueta = sin_tildes(f"{v.name} {getattr(v, 'id', '')}")
            if "spanish" in etiqueta or "espan" in etiqueta or "-es" in etiqueta:
                m.setProperty("voice", v.id)
                break
        m.setProperty("rate", 175)
        self.motor = m
        return m

    def decir(self, texto):
        try:
            m = self._motor()
            m.say(texto)
            m.runAndWait()
        except Exception as ex:
            log(f"no pude hablar ({type(ex).__name__})", "[!]")


# =====================================================================
#  Lector de QR
# =====================================================================
def leer_qr(indice_camara, segundos, al_leer, caja=None):
    """Abre la camara y llama a al_leer(codigo) por cada QR nuevo."""
    import cv2
    cap = cv2.VideoCapture(indice_camara, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(indice_camara)
    if not cap.isOpened():
        log("no pude abrir la camara", "[!]")
        return 0

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    detector = cv2.QRCodeDetector()
    try:
        from pyzbar.pyzbar import decode as zbar
    except Exception:
        zbar = None

    vistos, fin, ultimo_texto = set(), time.time() + segundos, ""
    ventana = "AulaBot - asistencia (ESC para cerrar)"
    cv2.namedWindow(ventana, cv2.WINDOW_NORMAL)

    while time.time() < fin:
        ok, cuadro = cap.read()
        if not ok:
            break

        codigos = []
        if zbar:
            for z in zbar(cuadro):
                codigos.append(z.data.decode("utf-8", "ignore").strip())
        if not codigos:
            try:
                txt, puntos, _ = detector.detectAndDecode(cuadro)
                if txt:
                    codigos.append(txt.strip())
            except Exception:
                pass

        for c in codigos:
            if c and c not in vistos:
                vistos.add(c)
                ultimo_texto = al_leer(c) or ""
                fin = time.time() + segundos    # se renueva el tiempo

        cv2.rectangle(cuadro, (0, 0), (cuadro.shape[1], 70), (20, 20, 20), -1)
        cv2.putText(cuadro, ultimo_texto or "Acerca tu codigo QR",
                    (20, 46), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        cv2.imshow(ventana, cuadro)
        if cv2.waitKey(1) & 0xFF == 27:
            break

    cap.release()
    cv2.destroyAllWindows()
    return len(vistos)


# =====================================================================
#  Programa
# =====================================================================
class AulaBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.local = Local()
        self.nube = Nube(cfg["supabase"], self.local)
        self.gemini = Gemini(cfg["gemini"]["api_key"], cfg["gemini"]["modelo"])
        self.caja = Caja(cfg["serial"]["puerto"])
        self.mic = Microfono(cfg["audio"]["dispositivo"])
        self.whisper = Transcriptor(cfg["whisper"]["modelo"],
                                    cfg["whisper"]["computo"],
                                    cfg["whisper"]["idioma"])
        self.voz = Voz()
        self.clase_id = None
        self.clase_curso = None
        self.t_inicio = None
        self.ocupado = False

    # -- catalogo ------------------------------------------------------
    def refrescar_catalogo(self):
        cursos = self.nube.pedir("cursos", {"select": "id,nombre"})
        est = self.nube.pedir("estudiantes",
                              {"select": "id,curso_id,nombres,apellidos,codigo_qr,activo"})
        hor = self.nube.pedir("horarios", {"select": "*"})
        if cursos and est:
            self.local.guardar_catalogo(cursos, est, hor)
            log(f"catalogo al dia: {len(est)} alumnos, {len(cursos)} cursos", "[OK]")
        else:
            n = self.local.q("SELECT COUNT(*) c FROM estudiantes")[0]["c"]
            log(f"trabajando con la copia local ({n} alumnos)", "[!]")

    def curso_de_ahora(self):
        dia = datetime.now().isoweekday()
        hs = self.local.q("SELECT * FROM horarios WHERE dia_semana=? ORDER BY orden",
                          (dia,))
        if hs:
            ahora = datetime.now().strftime("%H:%M:%S")
            con_hora = [h for h in hs if h["hora_inicio"]]
            for h in con_hora:
                if h["hora_inicio"] <= ahora <= (h["hora_fin"] or "23:59:59"):
                    return h["curso_id"]
            if con_hora:
                return min(con_hora,
                           key=lambda h: abs(
                               (datetime.strptime(h["hora_inicio"][:5], "%H:%M").hour * 60
                                + datetime.strptime(h["hora_inicio"][:5], "%H:%M").minute)
                               - (datetime.now().hour * 60 + datetime.now().minute))
                           )["curso_id"]
            hoy = datetime.now().strftime("%Y-%m-%d")
            ya = {c["curso_id"] for c in
                  self.local.q("SELECT curso_id FROM clases WHERE inicio LIKE ?",
                               (hoy + "%",))}
            for h in hs:
                if h["curso_id"] not in ya:
                    return h["curso_id"]
            return hs[0]["curso_id"]

        objetivo = sin_tildes(self.cfg["clase"]["curso_por_defecto"])
        for c in self.local.q("SELECT * FROM cursos"):
            if sin_tildes(c["nombre"]) == objetivo:
                return c["id"]
        todos = self.local.q("SELECT * FROM cursos")
        return todos[0]["id"] if todos else None

    def nombre_curso(self, curso_id):
        r = self.local.q("SELECT nombre FROM cursos WHERE id=?", (curso_id,))
        return r[0]["nombre"] if r else "Curso"

    # -- estado de la caja + tabla estado_dispositivo -------------------
    def marcar_estado(self, estado, clase_id=None):
        self.nube.escribir("PATCH", "estado_dispositivo", {"id": "eq.1"},
                           {"estado": estado, "clase_id": clase_id,
                            "origen": "esp32", "actualizado_en": ahora_iso()})

    # -- boton 1: empezar ----------------------------------------------
    def empezar(self):
        if self.clase_id:
            self.caja.mensaje("Ya esta grabando")
            return
        curso = self.curso_de_ahora()
        if not curso:
            self.caja.estado("ERROR")
            self.caja.mensaje("No hay cursos cargados")
            return

        self.clase_id = str(uuid.uuid4())
        self.clase_curso = curso
        self.t_inicio = datetime.now()
        inicio = ahora_iso()

        try:
            self.mic.abrir()
        except Exception as ex:
            log(f"no pude abrir el microfono: {ex}", "[!]")
            self.clase_id = None
            self.caja.estado("ERROR")
            self.caja.mensaje("Revisa el microfono")
            return

        fila = {"id": self.clase_id, "curso_id": curso, "estado": "grabando",
                "inicio": inicio, "origen": "esp32"}
        self.local.e("INSERT OR REPLACE INTO clases(id,curso_id,estado,inicio)"
                     " VALUES(?,?,?,?)", (self.clase_id, curso, "grabando", inicio))
        self.nube.escribir("POST", "clases", {"on_conflict": "id"}, fila,
                           "resolution=merge-duplicates")
        self.marcar_estado("grabando", self.clase_id)

        self.caja.estado("GRABANDO")
        self.caja.cronometro(0)
        self.caja.mensaje(self.nombre_curso(curso)[:24])
        log(f"grabando {self.nombre_curso(curso)}", "[REC]")

    # -- boton 2: terminar ---------------------------------------------
    def terminar(self):
        if not self.clase_id:
            self.caja.mensaje("No hay clase grabando")
            return
        clase_id, curso = self.clase_id, self.clase_curso
        self.clase_id = None
        ruta = DIR_AUDIO / f"clase_{datetime.now():%Y%m%d_%H%M%S}.wav"
        dur = self.mic.cerrar(ruta)
        fin = ahora_iso()

        self.caja.estado("PROCESANDO")
        self.caja.cronometro(-1)
        self.marcar_estado("procesando", clase_id)
        self.nube.escribir("PATCH", "clases", {"id": f"eq.{clase_id}"},
                           {"estado": "procesando", "fin": fin,
                            "duracion_seg": int(dur), "audio_ruta": ruta.name,
                            "actualizado_en": fin})
        log(f"grabacion terminada: {int(dur)} s -> {ruta.name}", "[OK]")

        threading.Thread(target=self._procesar,
                         args=(clase_id, curso, ruta, dur, fin), daemon=True).start()

    def _procesar(self, clase_id, curso, ruta, dur, fin):
        self.ocupado = True
        try:
            self.caja.mensaje("Transcribiendo...")
            texto = self.whisper.transcribir(ruta)
            if not texto:
                texto = "(no se escucho nada en la grabacion)"
            log(f"transcripcion: {len(texto)} caracteres", "[OK]")

            self.nube.escribir("POST", "transcripciones", {"on_conflict": "clase_id"},
                               {"clase_id": clase_id, "texto": texto},
                               "resolution=merge-duplicates")

            self.caja.mensaje("Resumiendo con IA...")
            datos = self._resumir(texto, self.nombre_curso(curso))

            self.caja.estado("SUBIENDO")
            cambios = {"estado": "completada", "fin": fin, "duracion_seg": int(dur),
                       "tema": datos["tema"], "resumen": datos["resumen"],
                       "temas": datos["temas"], "tareas": datos["tareas"],
                       "modelo_transcripcion": f"whisper-{self.whisper.nombre}",
                       "modelo_resumen": self.gemini.modelo or "local",
                       "actualizado_en": ahora_iso()}
            self.local.e("UPDATE clases SET estado=?,fin=?,duracion_seg=?,tema=?,"
                         "resumen=?,temas=?,tareas=? WHERE id=?",
                         ("completada", fin, int(dur), datos["tema"], datos["resumen"],
                          json.dumps(datos["temas"]), json.dumps(datos["tareas"]),
                          clase_id))
            self.nube.escribir("PATCH", "clases", {"id": f"eq.{clase_id}"}, cambios)
            self.marcar_estado("libre", None)
            self.nube.sincronizar()

            log(f"clase lista: {datos['tema']}", "[OK]")
            self.caja.estado("LIBRE")
            self.caja.mensaje("Clase guardada")
        except Exception as ex:
            log(f"error procesando: {ex}", "[X]")
            self.nube.escribir("PATCH", "clases", {"id": f"eq.{clase_id}"},
                               {"estado": "error", "error_detalle": str(ex)[:300],
                                "actualizado_en": ahora_iso()})
            self.marcar_estado("error", clase_id)
            self.caja.estado("ERROR")
            self.caja.mensaje("Fallo el resumen")
        finally:
            self.ocupado = False

    def _resumir(self, texto, curso):
        vacio = {"tema": f"Clase de {curso}", "resumen": texto[:900],
                 "temas": [], "tareas": []}
        if not self.gemini.listo():
            return vacio
        prompt = (
            "Eres el asistente del profesor de robotica e inteligencia artificial "
            f"del colegio Charles Babbage. Abajo esta la transcripcion automatica de "
            f"una clase de {curso}. La transcripcion puede tener errores de audio; "
            "interpreta con sentido comun y no inventes nada que no se haya dicho.\n\n"
            "Devuelve SOLO un JSON con esta forma exacta:\n"
            '{"tema": "titulo corto de la clase, maximo 10 palabras",\n'
            ' "resumen": "2 a 4 frases en espanol claro, dirigidas a los alumnos",\n'
            ' "temas": ["entre 2 y 5 temas puntuales"],\n'
            ' "tareas": ["deberes o trabajos que el profesor pidio; [] si no pidio"]}\n\n'
            f"TRANSCRIPCION:\n{texto[:24000]}")
        bruto = self.gemini.preguntar(prompt, json_puro=True)
        if not bruto:
            return vacio
        try:
            m = re.search(r"\{.*\}", bruto, re.S)
            d = json.loads(m.group(0) if m else bruto)
            return {
                "tema": str(d.get("tema") or vacio["tema"])[:120],
                "resumen": str(d.get("resumen") or "")[:2000],
                "temas": [str(t)[:80] for t in (d.get("temas") or [])][:6],
                "tareas": [str(t)[:160] for t in (d.get("tareas") or [])][:6],
            }
        except Exception:
            return vacio

    # -- boton 3: asistencia por QR ------------------------------------
    def asistencia(self):
        clase_id, curso = self.clase_id, self.clase_curso
        if not clase_id:
            hoy = datetime.now().strftime("%Y-%m-%d")
            r = self.local.q("SELECT id,curso_id FROM clases WHERE inicio LIKE ?"
                             " ORDER BY inicio DESC LIMIT 1", (hoy + "%",))
            if r:
                clase_id, curso = r[0]["id"], r[0]["curso_id"]
        if not clase_id:
            self.caja.estado("ERROR")
            self.caja.mensaje("Primero empieza la clase")
            return

        self.caja.estado("QR")
        marcados = []

        def al_leer(codigo):
            fila = self.local.q(
                "SELECT id,nombres,apellidos FROM estudiantes WHERE codigo_qr=?",
                (codigo.strip(),))
            if not fila:
                self.caja.mensaje("QR no reconocido")
                return "QR no reconocido"
            e = fila[0]
            nombre = f"{e['nombres'].split()[0].title()} {e['apellidos'].split()[0].title()}"
            if e["id"] in marcados:
                return f"{nombre} ya estaba"
            marcados.append(e["id"])
            self.local.e("INSERT OR REPLACE INTO asistencias VALUES(?,?,?,?)",
                         (clase_id, e["id"], ahora_iso(), "qr"))
            self.nube.escribir(
                "POST", "asistencias", {"on_conflict": "clase_id,estudiante_id"},
                {"clase_id": clase_id, "estudiante_id": e["id"],
                 "hora": ahora_iso(), "metodo": "qr"},
                "resolution=merge-duplicates")
            log(f"asistencia: {nombre}", "[QR]")
            self.caja.mensaje(f"Listo {nombre}"[:24])
            return f"Presente: {nombre}"

        leer_qr(int(self.cfg["camara"]["indice"]),
                int(self.cfg["camara"]["segundos"]), al_leer, self.caja)

        self.nube.sincronizar()
        total = len(self.local.q("SELECT 1 FROM estudiantes WHERE curso_id=?", (curso,)))
        self.caja.estado("GRABANDO" if self.clase_id else "LIBRE")
        self.caja.mensaje(f"{len(marcados)} de {total} presentes")
        log(f"asistencia cerrada: {len(marcados)} alumnos", "[OK]")

    # -- boton 4: pregunta ---------------------------------------------
    def pregunta(self):
        if self.ocupado:
            self.caja.mensaje("Espera, estoy resumiendo")
            return
        grabando = self.clase_id is not None
        if grabando:
            self.caja.mensaje("Pregunta mas tarde")
            return

        self.caja.estado("PREGUNTA")
        self.caja.mensaje("Habla ahora")
        ruta = DIR_AUDIO / "pregunta.wav"
        try:
            self.mic.grabar_segundos(int(self.cfg["audio"]["segundos_pregunta"]), ruta)
        except Exception as ex:
            log(f"microfono ocupado: {ex}", "[!]")
            self.caja.estado("ERROR")
            return

        self.caja.estado("PROCESANDO")
        self.caja.mensaje("Pensando...")
        try:
            pregunta = self.whisper.transcribir(ruta)
        except Exception:
            pregunta = ""
        log(f"pregunta: {pregunta or '(no se entendio)'}", "[?]")
        if not pregunta:
            self.voz.decir("No te escuche bien, intenta de nuevo.")
            self.caja.estado("LIBRE")
            return

        respuesta = self._normalizar_respuesta_voz(self._responder(pregunta))
        log(f"respuesta: {respuesta}", "[IA]")
        self.caja.estado("PREGUNTA")
        self.caja.mensaje(respuesta[:24])
        self.voz.decir(respuesta)
        self.caja.estado("LIBRE")
        self.caja.mensaje("Boton 1: empezar clase")

    def _contexto(self):
        hoy = datetime.now()
        dia = hoy.isoweekday()
        partes = [f"Hoy es {DIAS[dia]} {hoy.day} de "
                  f"{['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][hoy.month-1]} "
                  f"de {hoy.year}, son las {hoy:%H:%M}."]

        hs = self.local.q("SELECT * FROM horarios ORDER BY dia_semana, orden")
        if hs:
            lineas = []
            for h in hs:
                hora = f" a las {h['hora_inicio'][:5]}" if h["hora_inicio"] else ""
                lineas.append(f"- {DIAS[h['dia_semana']]}: {self.nombre_curso(h['curso_id'])}"
                              f", {h['materia']}{hora}")
            partes.append("Horario del laboratorio de robotica:\n" + "\n".join(lineas))

        cl = self.local.q("SELECT * FROM clases WHERE estado='completada'"
                          " ORDER BY inicio DESC LIMIT 6")
        if cl:
            lineas = []
            for c in cl:
                tareas = json.loads(c["tareas"] or "[]")
                lineas.append(
                    f"- {self.nombre_curso(c['curso_id'])}, {(c['inicio'] or '')[:10]}: "
                    f"{c['tema'] or 'sin tema'}. "
                    f"Deberes: {'; '.join(tareas) if tareas else 'ninguno'}")
            partes.append("Ultimas clases registradas:\n" + "\n".join(lineas))
        return "\n\n".join(partes)

    def _normalizar_respuesta_voz(self, texto):
        t = re.sub(r"\s+", " ", str(texto or "")).strip()
        if not t:
            return "No tengo ese dato."
        t = t.replace("\n", " ")
        if t.lower() in {"no tengo ese dato", "no tengo ese dato.", "no tengo ese dato en la base local."}:
            return "No tengo ese dato en la base local."
        return t[:260]

    def _responder(self, pregunta):
        texto = (pregunta or "").strip()
        p = sin_tildes(texto)
        hoy = datetime.now()

        # Respuestas deterministas desde la base local: evitan respuestas aleatorias
        if any(k in p for k in ("deber", "tarea", "trabajo", "homework")):
            cl = self.local.q("SELECT * FROM clases WHERE estado='completada'"
                              " ORDER BY inicio DESC LIMIT 1")
            tareas = []
            if cl:
                try:
                    tareas = json.loads(cl[0]["tareas"] or "[]")
                except Exception:
                    tareas = []
            if tareas:
                return "El deber de la ultima clase fue: " + "; ".join(tareas) + "."
            return "La ultima clase no dejo deberes."

        if any(k in p for k in ("clase", "horario", "toca", "curso", "materia")):
            hs = self.local.q("SELECT * FROM horarios WHERE dia_semana=? ORDER BY orden",
                              (hoy.isoweekday(),))
            if hs:
                return ("Hoy toca " +
                        ", ".join(self.nombre_curso(h["curso_id"]) for h in hs) + ".")
            return "Hoy no hay clases en el laboratorio."

        if any(k in p for k in ("fecha", "dia", "hoy", "hora", "tiempo")):
            return f"Hoy es {DIAS[hoy.isoweekday()]} {hoy.day}, y son las {hoy:%H:%M}."

        # Si no hay dato local claro, entonces usa Gemini con prompt muy restrictivo.
        ctx = self._contexto()
        if self.gemini.listo():
            prompt = (
                "Eres AulaBot, el asistente del laboratorio de robotica del colegio "
                "Charles Babbage. Responde SOLO con la informacion que aparece en los "
                "datos de abajo. Si no aparece, responde exactamente: No tengo ese dato. "
                "No inventes, no hagas listas largas, no des respuestas generales. "
                "Responde en 1 o 2 frases cortas, en espanol de Ecuador.\n\n"
                f"DATOS:\n{ctx}\n\nPREGUNTA DEL PROFESOR O DEL ALUMNO:\n{pregunta}")
            r = self.gemini.preguntar(prompt, timeout=40)
            if r:
                respuesta = self._normalizar_respuesta_voz(r)
                if respuesta.lower() not in {"no tengo ese dato en la base local."}:
                    return respuesta

        return "No tengo ese dato en la base local. Pregunta por la fecha, el horario o los deberes."

    # -- hilos de fondo -------------------------------------------------
    def _hilo_vu(self):
        while True:
            if self.clase_id:
                self.caja.cronometro((datetime.now() - self.t_inicio).total_seconds())
                self.caja.vu(self.mic.vu_0_100())
            time.sleep(0.4)

    def _hilo_sync(self):
        while True:
            time.sleep(20)
            try:
                antes = self.nube.hay
                self.nube.sincronizar()
                if self.nube.hay != antes:
                    self.caja.nube(self.nube.hay)
            except Exception:
                pass

    # -- arranque -------------------------------------------------------
    def correr(self):
        print("\n" + "=" * 62)
        print("  AULABOT  -  Charles Babbage  |  Fractal-BOTS")
        print("=" * 62 + "\n")

        log("buscando la caja...")
        if not self.caja.conectar():
            log("no encontre la ESP32. Revisa el cable USB y cierra el "
                "monitor serie del Arduino IDE.", "[X]")
            return 1

        self.caja.titulo("Conectando")
        self.caja.mensaje("Entrando a Supabase")
        if self.nube.entrar():
            log("sesion de docente abierta", "[OK]")
            self.refrescar_catalogo()
            self.nube.sincronizar()
        else:
            log("arrancando en modo sin internet", "[!]")
            self.refrescar_catalogo()
        self.caja.nube(self.nube.hay)

        if self.gemini.listo():
            self.gemini.elegir_modelo()
        else:
            log("sin clave de Gemini: los resumenes seran solo la transcripcion", "[!]")

        self.caja.mensaje("Cargando Whisper")
        try:
            self.whisper.cargar()
        except Exception as ex:
            log(f"no pude cargar Whisper: {ex}", "[X]")
            log("corre el programa una vez con internet para que lo descargue.", "[!]")

        threading.Thread(target=self._hilo_vu, daemon=True).start()
        threading.Thread(target=self._hilo_sync, daemon=True).start()

        self.marcar_estado("libre", None)
        self.caja.estado("LIBRE")
        log("todo listo. Presiona el boton 1 de la caja.", "[OK]")
        print()

        acciones = {"INICIO": self.empezar, "PARAR": self.terminar,
                    "QR": self.asistencia, "PREGUNTA": self.pregunta}
        try:
            while True:
                try:
                    boton = self.caja.eventos.get(timeout=1.0)
                except queue.Empty:
                    if not self.caja.vivo.is_set():
                        log("se perdio la caja. Reconecta el USB y vuelve a correr.", "[X]")
                        break
                    continue
                fn = acciones.get(boton)
                if fn:
                    try:
                        fn()
                    except Exception as ex:
                        log(f"error en el boton {boton}: {ex}", "[X]")
                        self.caja.estado("ERROR")
        except KeyboardInterrupt:
            pass
        finally:
            if self.clase_id:
                self.terminar()
                time.sleep(1)
            self.marcar_estado("libre", None)
            self.caja.estado("LIBRE")
            self.caja.cerrar()
            log("hasta luego.")
        return 0


# =====================================================================
def leer_config():
    ruta = BASE / "config.ini"
    if not ruta.exists():
        print("Falta config.ini junto a aulabot.py")
        sys.exit(1)
    cp = configparser.ConfigParser()
    cp.read(ruta, encoding="utf-8")
    return {s: dict(cp[s]) for s in cp.sections()}


def listar_dispositivos():
    import sounddevice as sd
    print("\nMicrofonos:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:2d}  {d['name']}")
    print("\nCamaras (indices que responden):")
    try:
        import cv2
        for i in range(4):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                print(f"  {i:2d}  disponible")
                cap.release()
    except Exception:
        print("  (instala opencv-python)")
    print()


if __name__ == "__main__":
    if "--dispositivos" in sys.argv:
        listar_dispositivos()
        sys.exit(0)
    sys.exit(AulaBot(leer_config()).correr())
