"""Panel de redes de Racecore.

Banco de fotos por categorías, publicaciones programadas y un programador que
deja la imagen (foto + texto superpuesto) y el texto del post listos a su hora.
Tú publicas a mano; el panel solo prepara.
Las publicaciones en modo «Eventos» se programan respecto a los eventos que el
panel lee de Racecore por la red local (o que se crean a mano).

Arranque: doble clic en 2_probar.bat  (o: .venv\\Scripts\\python app.py --abrir)
Panel:    http://localhost:5000
"""
import logging
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from jinja2 import DictLoader
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

import ia_fondo

BASE = Path(__file__).resolve().parent
DB = BASE / "racecore.db"
DIR_FOTOS = BASE / "fotos"
DIR_MINIS = BASE / "miniaturas"
DIR_GEN = BASE / "generadas"
DIR_FUENTES = BASE / "fuentes"
LOGO = BASE / "logo.png"
BANNER = BASE / "banner_redes.png"
PUERTO = int(os.environ.get("PUERTO", "5000"))

FORMATOS = {
    "post": ("Post 1080x1080", 1080, 1080),
    "vertical": ("Vertical 1080x1350", 1080, 1350),
    "story": ("Story 1080x1920", 1080, 1920),
}
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
DIAS_CORTOS = ["L", "M", "X", "J", "V", "S", "D"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
EXTENSIONES = {".jpg", ".jpeg", ".png", ".webp"}
LADO_MAX_FOTO = 3000                # las fotos se guardan como mucho a este tamaño (sobra para 1080 px)
Image.MAX_IMAGE_PIXELS = 400_000_000  # fotos de móvil de 200 MP
GRACIA = timedelta(minutes=10)      # margen si el PC estaba ocupado justo a la hora
DIAS_CONSERVAR = 60                 # las imágenes generadas se borran pasado este tiempo
CADA_SINCRONIZAR = 600              # segundos entre lecturas de Racecore
RACECORE_PUERTO = 8100
MOMENTOS = {"antes": "Antes del evento", "dia": "El mismo día", "despues": "Después del evento"}
CONDICIONES = {"": "Siempre", "abierta": "Solo con la inscripción abierta", "plazas": "Solo si quedan plazas",
               "resultados": "Solo cuando haya resultados"}

# Ajustes editables desde el panel (si están vacíos se mira la variable de entorno)
AJUSTES_ENV = {
    "openai_key": "OPENAI_API_KEY",
    "openai_calidad": "OPENAI_IMAGE_QUALITY",
    "telegram_token": "TELEGRAM_TOKEN",
    "telegram_chat": "TELEGRAM_CHAT_ID",
}

log = logging.getLogger("racecore")
ESTADO = {"ultima_comprobacion": None, "racecore_ok": None, "racecore_error": "", "racecore_n": None}
DIBUJO = threading.Lock()   # las fuentes de Pillow no se deben usar en dos hilos a la vez


# ---------------------------------------------------------------- base de datos

ESQUEMA = """
CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS fotos (
    id INTEGER PRIMARY KEY, categoria_id INTEGER NOT NULL, archivo TEXT NOT NULL, subida TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS publicaciones (
    id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, activa INTEGER NOT NULL DEFAULT 1,
    modo TEXT NOT NULL DEFAULT 'semanal', dias TEXT NOT NULL DEFAULT '', fecha TEXT NOT NULL DEFAULT '',
    hora TEXT NOT NULL DEFAULT '18:00', antelacion INTEGER NOT NULL DEFAULT 60,
    categoria_id INTEGER, formato TEXT NOT NULL DEFAULT 'post',
    titulo TEXT NOT NULL DEFAULT '', subtitulo TEXT NOT NULL DEFAULT '', pie TEXT NOT NULL DEFAULT '',
    texto TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '#e10600',
    prompt_ia TEXT NOT NULL DEFAULT '', diseno TEXT NOT NULL DEFAULT 'cartel',
    estilo_ia TEXT NOT NULL DEFAULT 'variado',
    momento TEXT NOT NULL DEFAULT 'antes', dias_desde INTEGER NOT NULL DEFAULT 14,
    dias_hasta INTEGER NOT NULL DEFAULT 3, cada INTEGER NOT NULL DEFAULT 2,
    filtro TEXT NOT NULL DEFAULT '', condicion TEXT NOT NULL DEFAULT '');
-- estado: generando | lista | publicada | descartada | error
CREATE TABLE IF NOT EXISTS generadas (
    id INTEGER PRIMARY KEY, publicacion_id INTEGER NOT NULL, ocurrencia TEXT NOT NULL,
    prueba INTEGER NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'generando',
    archivo TEXT, texto TEXT NOT NULL DEFAULT '', foto_id INTEGER,
    con_ia INTEGER NOT NULL DEFAULT 0, aviso TEXT NOT NULL DEFAULT '', creada TEXT NOT NULL,
    estilo TEXT NOT NULL DEFAULT '', evento_id INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
-- Eventos leídos de Racecore (origen 'racecore') o creados a mano ('manual').
-- Solo lo necesario para las publicaciones: nada de datos personales salvo el podio.
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY, origen TEXT NOT NULL, ext_id TEXT NOT NULL,
    nombre TEXT NOT NULL DEFAULT '', campeonato TEXT NOT NULL DEFAULT '',
    fecha TEXT NOT NULL DEFAULT '', fecha_txt TEXT NOT NULL DEFAULT '', hora TEXT NOT NULL DEFAULT '',
    plazas INTEGER, inscritos INTEGER, precio TEXT NOT NULL DEFAULT '', abierta INTEGER NOT NULL DEFAULT 0,
    enlace TEXT NOT NULL DEFAULT '', web TEXT NOT NULL DEFAULT '', horarios TEXT NOT NULL DEFAULT '',
    resultados TEXT NOT NULL DEFAULT '', ganadores TEXT NOT NULL DEFAULT '', actualizado TEXT NOT NULL DEFAULT '',
    UNIQUE (origen, ext_id));
"""


def conectar():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def consulta(sql, args=()):
    c = conectar()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def ejecutar(sql, args=()):
    c = conectar()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur
    finally:
        c.close()


def ajuste(clave, defecto=""):
    fila = consulta("SELECT valor FROM ajustes WHERE clave = ?", (clave,))
    if fila and fila[0]["valor"]:
        return fila[0]["valor"]
    return os.environ.get(AJUSTES_ENV.get(clave, ""), "") or defecto


def guardar_ajuste(clave, valor):
    ejecutar("INSERT INTO ajustes (clave, valor) VALUES (?, ?) "
             "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, valor))


def ahora_txt():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def iniciar():
    for d in (DIR_FOTOS, DIR_MINIS, DIR_GEN, DIR_FUENTES):
        d.mkdir(exist_ok=True)
    c = conectar()
    try:
        c.executescript(ESQUEMA)
        # Columnas añadidas en versiones posteriores
        nuevas = {"publicaciones": {"diseno": "TEXT NOT NULL DEFAULT 'cartel'",
                                    "estilo_ia": "TEXT NOT NULL DEFAULT 'variado'",
                                    "momento": "TEXT NOT NULL DEFAULT 'antes'",
                                    "dias_desde": "INTEGER NOT NULL DEFAULT 14",
                                    "dias_hasta": "INTEGER NOT NULL DEFAULT 3",
                                    "cada": "INTEGER NOT NULL DEFAULT 2",
                                    "filtro": "TEXT NOT NULL DEFAULT ''",
                                    "condicion": "TEXT NOT NULL DEFAULT ''"},
                  "generadas": {"estilo": "TEXT NOT NULL DEFAULT ''",
                                "evento_id": "INTEGER NOT NULL DEFAULT 0"}}
        for tabla, cols in nuevas.items():
            existentes = {f["name"] for f in c.execute(f"PRAGMA table_info({tabla})")}
            for col, tipo in cols.items():
                if col not in existentes:
                    c.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        # Una sola imagen programada por publicación, evento y hora (las pruebas no cuentan).
        # Sustituye al índice antiguo, que no tenía en cuenta el evento.
        c.execute("DROP INDEX IF EXISTS generadas_unica")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS generadas_unica_evento "
                  "ON generadas(publicacion_id, evento_id, ocurrencia) WHERE prueba = 0")
        c.execute("PRAGMA journal_mode=WAL")
        # Lo que se quedó a medias por un reinicio
        c.execute("UPDATE generadas SET estado = 'error', aviso = 'Interrumpido: se reinició el panel' "
                  "WHERE estado = 'generando'")
        c.commit()
    finally:
        c.close()
    if not ajuste("secreto"):
        guardar_ajuste("secreto", secrets.token_hex(32))
    app.secret_key = ajuste("secreto")


# ---------------------------------------------------------------- calendario

def hora_de(pub):
    h, m = (pub["hora"] or "18:00").split(":")[:2]
    return int(h), int(m)


def ocurrencias(pub, desde, hasta):
    """Fechas/horas de publicación entre desde y hasta (ambos incluidos)."""
    h, m = hora_de(pub)
    if pub["modo"] == "evento":  # esas van con ocurrencias_evento()
        return []
    if pub["modo"] == "fecha":
        try:
            dias = [date.fromisoformat(pub["fecha"])]
        except ValueError:
            return []
    else:
        semana = {int(x) for x in pub["dias"].split(",") if x.strip().isdigit()}
        total = (hasta.date() - desde.date()).days + 1
        dias = [desde.date() + timedelta(days=i) for i in range(total)]
        dias = [d for d in dias if d.weekday() in semana]
    res = []
    for d in dias:
        occ = datetime(d.year, d.month, d.day, h, m)
        if desde <= occ <= hasta:
            res.append(occ)
    return res


def proxima(pub, ahora=None):
    ahora = ahora or datetime.now()
    # una fecha concreta puede estar lejos; las semanales se repiten cada 7 días
    hasta = datetime.max if pub["modo"] == "fecha" else ahora + timedelta(days=8)
    occ = ocurrencias(pub, ahora, hasta)
    return occ[0] if occ else None


def ocurrencia_prueba(pub):
    """Para «Generar ahora»: la próxima publicación, o la fecha fija aunque ya pasó, o ahora."""
    occ = proxima(pub)
    if occ:
        return occ
    if pub["modo"] == "fecha":
        try:
            d = date.fromisoformat(pub["fecha"])
            return datetime(d.year, d.month, d.day, *hora_de(pub))
        except ValueError:
            pass
    return datetime.now().replace(second=0, microsecond=0)


def variables(texto, occ):
    return ((texto or "")
            .replace("{dia}", DIAS[occ.weekday()])
            .replace("{fecha}", f"{occ.day} de {MESES[occ.month - 1]}")
            .replace("{hora}", occ.strftime("%H:%M")))


def cuando_txt(occ):
    return f"{DIAS[occ.weekday()]} {occ:%d/%m} · {occ:%H:%M}"


def resumen_programacion(pub):
    if pub["modo"] == "evento":
        extra = [CONDICIONES[pub["condicion"]].lower()] if pub["condicion"] in CONDICIONES and pub["condicion"] else []
        if pub["filtro"]:
            extra.append(f"solo «{pub['filtro']}»")
        return " · ".join([f"Eventos: {cuando_evento(pub)}", pub["hora"], *extra])
    if pub["modo"] == "fecha":
        try:
            fecha = date.fromisoformat(pub["fecha"]).strftime("%d/%m/%Y")
        except ValueError:
            fecha = "sin fecha"
        return f"{fecha} · {pub['hora']}"
    dias = [DIAS_CORTOS[int(x)] for x in pub["dias"].split(",") if x.strip().isdigit()]
    return f"{' '.join(dias) or 'ningún día'} · {pub['hora']}"


# ---------------------------------------------------------------- eventos
# El panel lee los eventos de Racecore por la red local (solo lectura, con token) y
# las publicaciones en modo «Eventos» se programan respecto a la fecha de cada uno:
# «cada 2 días de 14 a 3 días antes», «1 día después»... Los datos del evento
# ({evento}, {inscritos}, {horarios}...) se ponen en los textos antes de dibujar.

SINCRONIZAR = threading.Lock()


class ErrorFuente(Exception):
    """Fallo al leer Racecore, con un mensaje que se entiende."""


def txt(valor):
    return "" if valor is None else str(valor).strip()


def como_entero(valor):
    try:
        return max(0, int(valor))
    except (TypeError, ValueError):
        return None


def leer_fecha(texto):
    """«2026-10-04», «4/10/2026» o «4 de octubre» -> date. None si no se entiende."""
    t = txt(texto).lower()
    if not t:
        return None
    try:
        return date.fromisoformat(t[:10])
    except ValueError:
        pass
    try:
        m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", t)
        if m:
            d, mes, a = map(int, m.groups())
            return date(a + 2000 if a < 100 else a, mes, d)
        m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-z]+)(?:\s+(?:de\s+)?(\d{4}))?", t)
        if m and m.group(2) in MESES:
            d, mes = int(m.group(1)), MESES.index(m.group(2)) + 1
            if m.group(3):
                return date(int(m.group(3)), mes, d)
            hoy = date.today()  # sin año: el más cercano que no esté muy pasado
            f = date(hoy.year, mes, d)
            return f if f >= hoy - timedelta(days=180) else date(hoy.year + 1, mes, d)
    except ValueError:
        pass
    return None


def leer_hora(texto):
    """«9:00», «09.00» o «9h00» -> «09:00». Vacío si no se entiende."""
    m = re.match(r"\s*(\d{1,2})[:.h](\d{2})", txt(texto))
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def texto_horarios(horarios):
    """Horarios de Racecore como texto: una línea por tanda («09:00-09:10 · Entrenos · Cat A»)."""
    lineas = []
    for h in horarios:
        if len(horarios) > 1 and txt(h.get("nombre")):
            lineas.append(txt(h["nombre"]))
        for l in h.get("lineas") or []:
            if not isinstance(l, dict):
                continue
            hora, fin = leer_hora(l.get("hora")) or txt(l.get("hora")), leer_hora(l.get("fin"))
            cats = ", ".join(txt(c) for c in (l.get("cats") or []) if txt(c)) if isinstance(l.get("cats"), list) else ""
            partes = [f"{hora}-{fin}" if hora and fin else hora, txt(l.get("tipo")), cats, txt(l.get("nota"))]
            if any(partes):
                lineas.append(" · ".join(p for p in partes if p))
    return "\n".join(lineas)


def primera_hora(horarios, fecha):
    """La hora de la primera tanda del día del evento."""
    horas = [leer_hora(l.get("hora")) for h in horarios
             if not fecha or leer_fecha(h.get("fecha")) in (None, fecha)
             for l in (h.get("lineas") or []) if isinstance(l, dict)]
    horas = [x for x in horas if x]
    return min(horas) if horas else ""


def podio_por_categoria(sesion):
    """{categoría: [(pos, piloto)]} con los 3 primeros de cada categoría."""
    cats = {}
    podio = [p for p in (sesion.get("podio") or []) if isinstance(p, dict) and txt(p.get("piloto"))]
    for p in sorted(podio, key=lambda p: como_entero(p.get("pos")) or 99):
        cats.setdefault(txt(p.get("categoria")), []).append((como_entero(p.get("pos")), txt(p["piloto"])))
    return {c: ps[:3] for c, ps in cats.items()}


def texto_resultados(resultados):
    """«Final\nSenior: 1º Nombre · 2º Nombre · 3º Nombre», una sesión tras otra."""
    bloques = []
    for s in resultados:
        cats = podio_por_categoria(s)
        if not cats:
            continue
        lineas = [txt(s.get("sesion"))]
        for cat, ps in cats.items():
            nombres = " · ".join(f"{pos}º {nombre}" if pos else nombre for pos, nombre in ps)
            lineas.append(f"{cat}: {nombres}" if cat else nombres)
        bloques.append("\n".join(l for l in lineas if l))
    return "\n\n".join(bloques)


def texto_ganadores(resultados):
    """Ganador de cada categoría en la última sesión con resultados: «Senior: Nombre | Junior: Nombre»."""
    for s in reversed(resultados):
        cats = podio_por_categoria(s)
        if cats:
            return " | ".join(f"{cat}: {ps[0][1]}" if cat else ps[0][1] for cat, ps in cats.items())
    return ""


def evento_de_racecore(e):
    """Del JSON de Racecore solo se guarda lo que hace falta para las publicaciones."""
    horarios = [h for h in (e.get("horarios") or []) if isinstance(h, dict)]
    resultados = [r for r in (e.get("resultados") or []) if isinstance(r, dict)]
    fecha = leer_fecha(e.get("fecha_iso")) or leer_fecha(e.get("fecha"))
    return {
        "nombre": txt(e.get("nombre")), "campeonato": txt(e.get("campeonato")),
        "fecha": fecha.isoformat() if fecha else "", "fecha_txt": txt(e.get("fecha")),
        "hora": primera_hora(horarios, fecha),
        "plazas": como_entero(e.get("plazas")), "inscritos": como_entero(e.get("inscritos")),
        "precio": txt(e.get("precio")), "abierta": 1 if e.get("inscripcion_abierta") else 0,
        "enlace": txt(e.get("url_inscripcion")), "web": txt(e.get("url_portada")),
        "horarios": texto_horarios(horarios), "resultados": texto_resultados(resultados),
        "ganadores": texto_ganadores(resultados),
    }


def url_racecore():
    """«192.168.1.50» -> «http://192.168.1.50:8100». Vacío si no está configurado."""
    url = ajuste("racecore_url").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    partes = urlsplit(url)
    try:
        puerto = partes.port
    except ValueError:
        puerto = None
    host = partes.netloc if puerto else f"{partes.hostname}:{RACECORE_PUERTO}"
    return f"{partes.scheme}://{host}"


def leer_racecore():
    base = url_racecore()
    try:
        r = requests.get(f"{base}/api/publico/eventos", headers={"X-Token": ajuste("racecore_token")}, timeout=10)
    except requests.RequestException:
        raise ErrorFuente(f"No hay conexión con {base}. Mira que el PC de Racecore esté encendido, "
                          "en la misma red y con Racecore abierto.") from None
    if r.status_code == 404:
        raise ErrorFuente("Racecore todavía no tiene la dirección para el panel (/api/publico/eventos).")
    if r.status_code == 401:
        raise ErrorFuente("Racecore dice que el token no es correcto.")
    if r.status_code == 403:
        raise ErrorFuente("En Racecore falta generar el token de lectura.")
    if r.status_code != 200:
        raise ErrorFuente(f"Racecore ha respondido con un error ({r.status_code}).")
    try:
        eventos = r.json()["eventos"]
    except (ValueError, KeyError, TypeError):
        eventos = None
    if not isinstance(eventos, list):
        raise ErrorFuente("La respuesta de Racecore no tiene el formato esperado.")
    return [e for e in eventos
            if isinstance(e, dict) and e.get("id") is not None and e.get("tipo") != "campeonato"]


def sincronizar():
    """Lee Racecore y actualiza la tabla eventos. Devuelve cuántos hay (None si no está configurado)."""
    if not ajuste("racecore_url"):
        ESTADO.update(racecore_error="", racecore_n=None)
        return None
    with SINCRONIZAR:
        try:
            eventos = leer_racecore()
        except ErrorFuente as e:
            ESTADO["racecore_error"] = str(e)
            raise
        marca = ahora_txt()
        c = conectar()
        try:
            for e in eventos:
                fila = evento_de_racecore(e) | {"origen": "racecore", "ext_id": str(e["id"]), "actualizado": marca}
                cols = list(fila)
                c.execute(f"INSERT INTO eventos ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                          f"ON CONFLICT(origen, ext_id) DO UPDATE SET "
                          f"{', '.join(f'{k} = excluded.{k}' for k in cols)}", [fila[k] for k in cols])
            # Los que ya no manda Racecore (borrados, o pasados hace días) se quitan
            c.execute("DELETE FROM eventos WHERE origen = 'racecore' AND actualizado != ?", (marca,))
            c.commit()
        finally:
            c.close()
        ESTADO.update(racecore_ok=datetime.now(), racecore_error="", racecore_n=len(eventos))
        return len(eventos)


def sincronizar_seguro():
    """Para el programador: si Racecore falla se queda anotado y se siguen usando los últimos datos."""
    try:
        sincronizar()
    except ErrorFuente as e:
        log.warning("Racecore: %s", e)
    except Exception as e:
        ESTADO["racecore_error"] = f"Error leyendo Racecore: {e}"
        log.exception("Racecore")


def desfases(pub):
    """Días respecto al evento en los que toca publicar (negativo = antes)."""
    if pub["momento"] == "dia":
        return [0]
    cada = max(1, pub["cada"] or 1)
    cerca, lejos = sorted((max(0, pub["dias_desde"]), max(0, pub["dias_hasta"])))
    if pub["momento"] == "despues":
        return list(range(cerca, lejos + 1, cada))
    return [-d for d in range(lejos, cerca - 1, -cada)]


def cuando_evento(pub):
    """«cada 2 días, de 14 a 3 días antes», «1 día después»..."""
    if pub["momento"] == "dia":
        return "el mismo día"
    cerca, lejos = sorted((pub["dias_desde"], pub["dias_hasta"]))
    lado = "después" if pub["momento"] == "despues" else "antes"
    if cerca == lejos:
        return f"{cerca} día{'' if cerca == 1 else 's'} {lado}"
    rango = f"de {cerca} a {lejos}" if lado == "después" else f"de {lejos} a {cerca}"
    cada = "cada día" if pub["cada"] <= 1 else f"cada {pub['cada']} días"
    return f"{cada}, {rango} días {lado}"


def eventos_de(pub, eventos=None):
    """Eventos a los que se aplica la publicación: todos, o los que contienen el filtro."""
    eventos = consulta("SELECT * FROM eventos") if eventos is None else eventos
    filtro = (pub["filtro"] or "").strip().casefold()
    return [e for e in eventos if not filtro or filtro in f"{e['nombre']} {e['campeonato']}".casefold()]


def ocurrencias_evento(pub, desde, hasta, eventos=None):
    """[(fecha y hora de publicación, evento)] entre desde y hasta, ordenadas."""
    h, m = hora_de(pub)
    res = []
    for ev in eventos_de(pub, eventos):
        f = leer_fecha(ev["fecha"])
        if not f:
            continue
        for d in desfases(pub):
            dia = f + timedelta(days=d)
            occ = datetime(dia.year, dia.month, dia.day, h, m)
            if desde <= occ <= hasta:
                res.append((occ, ev))
    return sorted(res, key=lambda x: (x[0], x[1]["id"]))


def proxima_evento(pub, ahora=None):
    occ = ocurrencias_evento(pub, ahora or datetime.now(), datetime.max)
    return occ[0] if occ else (None, None)


def prueba_evento(pub):
    """Para «Generar ahora»: la próxima publicación con su evento, o la última si ya pasaron todas."""
    todas = ocurrencias_evento(pub, datetime.min, datetime.max)
    futuras = [x for x in todas if x[0] >= datetime.now() - GRACIA]
    return futuras[0] if futuras else (todas[-1] if todas else (None, None))


def plazas_libres(ev):
    if ev["plazas"] and ev["inscritos"] is not None:
        return max(0, ev["plazas"] - ev["inscritos"])
    return None


def cumple(pub, ev):
    """La condición de la publicación: inscripción abierta, plazas libres o resultados."""
    if pub["condicion"] == "abierta":
        return bool(ev["abierta"])
    if pub["condicion"] == "plazas":
        libres = plazas_libres(ev)
        return libres is None or libres > 0
    if pub["condicion"] == "resultados":
        return bool(ev["resultados"].strip())
    return True


def valores_evento(ev, occ):
    """Variables de los textos de una publicación de eventos ({dia}, {fecha} y {hora} son las del evento)."""
    f = leer_fecha(ev["fecha"])
    dias = (f - occ.date()).days if f else None
    if dias is None:
        faltan = ""
    elif dias in (0, 1, -1):
        faltan = {0: "hoy", 1: "mañana", -1: "ayer"}[dias]
    else:
        faltan = f"en {dias} días" if dias > 0 else f"hace {-dias} días"
    numero = lambda v: "" if v is None else str(v)  # noqa: E731
    return {
        "{evento}": ev["nombre"], "{campeonato}": ev["campeonato"],
        "{dia}": DIAS[f.weekday()] if f else "",
        "{fecha}": f"{f.day} de {MESES[f.month - 1]}" if f else ev["fecha_txt"],
        "{hora}": ev["hora"], "{dias}": numero(abs(dias) if dias is not None else None), "{faltan}": faltan,
        "{inscritos}": numero(ev["inscritos"]), "{plazas}": numero(ev["plazas"]),
        "{libres}": numero(plazas_libres(ev)), "{precio}": ev["precio"],
        "{enlace}": ev["enlace"] or ev["web"], "{web}": ev["web"] or ev["enlace"],
        "{horarios}": ev["horarios"], "{resultados}": ev["resultados"], "{ganadores}": ev["ganadores"],
    }


def con_evento(pub, ev, occ):
    """La publicación con los datos del evento ya puestos en sus textos."""
    valores = valores_evento(ev, occ)
    datos = dict(pub)
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia"):
        for clave, valor in valores.items():
            datos[campo] = (datos[campo] or "").replace(clave, valor or "")
    datos["nombre"] = f"{pub['nombre']} · {ev['nombre']}"
    datos["_aviso"] = ""
    if ev["origen"] == "racecore" and ev["actualizado"]:
        leido = datetime.strptime(ev["actualizado"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - leido > timedelta(hours=3):
            datos["_aviso"] = (f"Ojo: datos de Racecore del {leido:%d/%m %H:%M} (no se han podido actualizar). "
                               "Revisa los números antes de publicar.")
    return datos


# ---------------------------------------------------------------- imagen

@lru_cache(maxsize=64)
def fuente(tipo, tam):
    """Fuente propia (fuentes/Titulo.ttf, fuentes/Texto.ttf) o una del sistema."""
    propia = DIR_FUENTES / ("Titulo.ttf" if tipo == "titulo" else "Texto.ttf")
    sistema = {
        "titulo": ["C:/Windows/Fonts/ariblk.ttf", "C:/Windows/Fonts/arialbd.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        "texto": ["C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "etiqueta": ["C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    }[tipo]
    for ruta in [propia, *sistema]:
        if Path(ruta).exists():
            try:
                return ImageFont.truetype(str(ruta), tam)
            except OSError:
                pass
    return ImageFont.load_default(tam)


def color_rgb(hexa):
    hexa = (hexa or "#e10600").lstrip("#")
    try:
        return tuple(int(hexa[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (225, 6, 0)


def partir(d, texto, fnt, ancho):
    """Reparte el texto en líneas que caben en el ancho (respeta los saltos de línea)."""
    lineas = []
    for parrafo in texto.split("\n"):
        linea = ""
        for palabra in parrafo.split():
            prueba = f"{linea} {palabra}".strip()
            if not linea or d.textlength(prueba, font=fnt) <= ancho:
                linea = prueba
            else:
                lineas.append(linea)
                linea = palabra
        lineas.append(linea)
    return lineas


def alto_linea(fnt, factor):
    ascenso, descenso = fnt.getmetrics()
    return int((ascenso + descenso) * factor)


def degradado(img, desde_y):
    """Oscurece de desde_y hacia abajo para que el texto se lea sobre cualquier foto."""
    ancho, alto = img.size
    zona_alto = alto - desde_y
    if zona_alto <= 0:
        return
    mascara = Image.linear_gradient("L").resize((ancho, zona_alto))
    mascara = mascara.point(lambda v: int(235 * (v / 255) ** 1.3))
    zona = img.crop((0, desde_y, ancho, alto))
    img.paste(Image.composite(Image.new("RGB", zona.size, (0, 0, 0)), zona, mascara), (0, desde_y))


def fondo_liso(ancho, alto, color):
    r, g, b = color_rgb(color)
    arriba = (r // 3, g // 3, b // 3)
    mascara = Image.linear_gradient("L").resize((ancho, alto))
    return Image.composite(Image.new("RGB", (ancho, alto), (10, 10, 12)),
                           Image.new("RGB", (ancho, alto), arriba), mascara)


def superponer_sencillo(img, pub, occ):
    """Diseño «Sencillo»: título, subtítulo y pie (etiqueta de color) abajo; logo arriba a la derecha."""
    W, H = img.size
    d = ImageDraw.Draw(img)
    acento = color_rgb(pub["color"])
    story = H / W > 1.6
    margen = int(W * 0.07)
    base = H - (int(H * 0.14) if story else margen)   # stories: libre la zona de la interfaz
    ancho_txt = W - 2 * margen

    titulo = sin_marcas(variables(pub["titulo"], occ)).strip()
    sub = sin_marcas(variables(pub["subtitulo"], occ)).strip()
    pie = sin_marcas(variables(pub["pie"], occ)).strip()

    # Título: se reduce hasta que quepa en 3 líneas
    tam = int(W * 0.095)
    while True:
        f_tit = fuente("titulo", tam)
        l_tit = partir(d, titulo, f_tit, ancho_txt) if titulo else []
        cabe = all(d.textlength(l, font=f_tit) <= ancho_txt for l in l_tit)
        if (len(l_tit) <= 3 and cabe) or tam <= int(W * 0.055):
            break
        tam = int(tam * 0.9)
    f_sub = fuente("texto", int(W * 0.045))
    l_sub = (partir(d, sub, f_sub, ancho_txt) if sub else [])[:4]
    f_pie = fuente("etiqueta", int(W * 0.036))

    lh_tit, lh_sub = alto_linea(f_tit, 1.08), alto_linea(f_sub, 1.25)
    barra_alto, hueco = max(6, int(W * 0.009)), int(W * 0.03)
    pie_pad_x, pie_pad_y = int(f_pie.size * 0.8), int(f_pie.size * 0.5)
    pie_alto = alto_linea(f_pie, 1.0) + 2 * pie_pad_y

    total = 0
    if l_tit:
        total += barra_alto + hueco + lh_tit * len(l_tit)
    if l_sub:
        total += (hueco if total else 0) + lh_sub * len(l_sub)
    if pie:
        total += (int(hueco * 1.3) if total else 0) + pie_alto

    if total:
        y = base - total
        degradado(img, max(0, y - int(H * 0.22)))
        d = ImageDraw.Draw(img)
        if l_tit:
            d.rectangle([margen, y, margen + int(W * 0.12), y + barra_alto], fill=acento)
            y += barra_alto + hueco
            for linea in l_tit:
                d.text((margen, y), linea, font=f_tit, fill=(255, 255, 255))
                y += lh_tit
        if l_sub:
            y += hueco if l_tit else 0
            for linea in l_sub:
                d.text((margen, y), linea, font=f_sub, fill=(232, 232, 232))
                y += lh_sub
        if pie:
            y += int(hueco * 1.3) if (l_tit or l_sub) else 0
            ancho_pie = int(d.textlength(pie, font=f_pie)) + 2 * pie_pad_x
            d.rounded_rectangle([margen, y, margen + ancho_pie, y + pie_alto],
                                radius=pie_alto // 2, fill=acento)
            claro = 0.299 * acento[0] + 0.587 * acento[1] + 0.114 * acento[2] > 160
            d.text((margen + pie_pad_x, y + pie_pad_y), pie, font=f_pie,
                   fill=(0, 0, 0) if claro else (255, 255, 255))

    if LOGO.exists():
        with Image.open(LOGO) as lg:
            lg = lg.convert("RGBA")
            lg.thumbnail((int(W * 0.22), int(H * 0.1)), Image.LANCZOS)
            arriba = int(H * 0.08) if story else margen
            img.paste(lg, (W - margen - lg.width, arriba), lg)
    return img


# ---------------------------------------------------------------- diseño «Cartel»
# Estilo cartel de competición: logo arriba a la izquierda, título enorme en cursiva
# (lo que va entre *asteriscos* sale en color), franja de color tipo brochazo con el
# destacado, datos con barras amarillas y las redes abajo.

BLANCO = (255, 255, 255)
AMARILLO = (247, 197, 0)
REDES = ("facebook", "instagram", "tiktok", "web")
_MEDIDOR = ImageDraw.Draw(Image.new("L", (1, 1)))


@lru_cache(maxsize=128)
def fuente_cartel(tipo, tam):
    """Fuente propia si la hay; si no, las Montserrat incluidas; si no, las de Windows."""
    propia = DIR_FUENTES / ("Titulo.ttf" if tipo == "titulo" else "Texto.ttf")
    incluida = DIR_FUENTES / ("Montserrat-BlackItalic.ttf" if tipo == "titulo" else "Montserrat-ExtraBoldItalic.ttf")
    for ruta in (propia, incluida):
        if ruta.exists():
            try:
                return ImageFont.truetype(str(ruta), tam)
            except OSError:
                pass
    return fuente("titulo" if tipo == "titulo" else "etiqueta", tam)


def trozos(linea):
    """«DE *RECORD*» -> [("DE ", False), ("RECORD", True)]"""
    return [(t, i % 2 == 1) for i, t in enumerate(linea.split("*")) if t]


def sin_marcas(texto):
    return texto.replace("*", "")


def alto_mayus(fnt):
    return -fnt.getbbox("H", anchor="ls")[1]


def partir_equilibrado(texto, fnt, ancho):
    """Como partir(), pero si salen dos líneas las deja de largo parecido (sin palabras sueltas)."""
    lineas = partir(_MEDIDOR, texto, fnt, ancho)
    palabras = texto.split()
    if len(lineas) != 2:
        return lineas
    opciones = [(" ".join(palabras[:i]), " ".join(palabras[i:])) for i in range(1, len(palabras))]
    opciones = [o for o in opciones if max(fnt.getlength(o[0]), fnt.getlength(o[1])) <= ancho]
    if not opciones:
        return lineas
    return list(min(opciones, key=lambda o: max(fnt.getlength(o[0]), fnt.getlength(o[1]))))


def ajustar_tam(texto, tipo, ancho, tam_max, tam_min):
    """Tamaño de letra para que el texto ocupe el ancho, sin pasar de tam_max."""
    largo = fuente_cartel(tipo, 200).getlength(sin_marcas(texto)) or 1
    return max(tam_min, min(tam_max, int(200 * ancho / largo)))


def pintar(img, elementos, color_marca, sombra=0, opacidad=150):
    """Dibuja textos [(x, y_base, texto, fuente, color)] con sombra suave opcional."""
    if sombra:
        mascara = Image.new("L", img.size, 0)
        dm = ImageDraw.Draw(mascara)
        for x, y, texto, fnt, _ in elementos:
            dm.text((x, y), sin_marcas(texto), font=fnt, fill=opacidad, anchor="ls")
        mascara = mascara.filter(ImageFilter.GaussianBlur(sombra))
        mascara = ImageChops.offset(mascara, int(sombra * 0.3), int(sombra * 0.5))
        img.paste((0, 0, 0), (0, 0) + img.size, mascara)
    d = ImageDraw.Draw(img)
    for x, y, texto, fnt, color in elementos:
        for t, marcado in trozos(texto):
            d.text((x, y), t, font=fnt, fill=color_marca if marcado else color, anchor="ls")
            x += fnt.getlength(t)


def oscurecer_cartel(img, fin_arriba, inicio_abajo, vineta=True):
    """Viñeta + oscurecido arriba (título) y abajo (franja, datos, redes)."""
    W, H = img.size
    mascara = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(v * 0.5) if vineta else 0)
    if fin_arriba > 0:
        arriba = Image.linear_gradient("L").rotate(180).resize((W, fin_arriba))
        capa = Image.new("L", (W, H), 0)
        capa.paste(arriba.point(lambda v: int(215 * (v / 255) ** 1.4)), (0, 0))
        mascara = ImageChops.lighter(mascara, capa)
    if inicio_abajo < H:
        abajo = Image.linear_gradient("L").resize((W, H - inicio_abajo))
        capa = Image.new("L", (W, H), 0)
        capa.paste(abajo.point(lambda v: int(240 * (v / 255) ** 0.9)), (0, inicio_abajo))
        mascara = ImageChops.lighter(mascara, capa)
    img.paste((0, 0, 0), (0, 0) + img.size, mascara)


def brochazo(ancho, alto, color, rnd):
    """Franja de color con bordes rasgados, como un trazo de pintura (RGBA)."""
    margen = int(ancho * 0.06)
    capa = Image.new("RGBA", (ancho + 2 * margen, alto), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    x0, x1 = margen, margen + ancho
    pasos = 10
    borde = [(x0 + ancho * i / pasos, rnd.uniform(0, alto * 0.05)) for i in range(pasos + 1)]
    borde += [(x1 - rnd.uniform(0, ancho * 0.035), alto * j / 8) for j in range(1, 8)]
    borde += [(x1 - ancho * i / pasos, alto - rnd.uniform(0, alto * 0.05)) for i in range(pasos + 1)]
    borde += [(x0 + rnd.uniform(0, ancho * 0.035), alto * (8 - j) / 8) for j in range(1, 8)]
    d.polygon(borde, fill=color + (255,))
    # vetas del pincel que se salen por los extremos
    for _ in range(14):
        grosor = alto * rnd.uniform(0.02, 0.07)
        y = rnd.uniform(0, alto - grosor)
        izq = x0 - rnd.uniform(-ancho * 0.02, margen * 0.9)
        der = x1 + rnd.uniform(-ancho * 0.02, margen * 0.9)
        d.rectangle([izq, y, der, y + grosor], fill=color + (rnd.randint(150, 255),))
    # textura: vetas algo más oscuras dentro
    oscuro = tuple(int(c * 0.82) for c in color)
    for _ in range(10):
        grosor = alto * rnd.uniform(0.01, 0.03)
        y = rnd.uniform(alto * 0.08, alto * 0.92)
        a = x0 + rnd.uniform(0, ancho * 0.5)
        d.rectangle([a, y, a + rnd.uniform(ancho * 0.2, ancho * 0.5), y + grosor], fill=oscuro + (70,))
    return capa.filter(ImageFilter.GaussianBlur(0.8))


def icono_red(tipo, lado):
    """Icono blanco de red social (máscara L), dibujado a 4x y reducido para que salga suave."""
    L = lado * 4
    m = Image.new("L", (L, L), 0)
    d = ImageDraw.Draw(m)
    g = max(3, int(L * 0.09))
    if tipo == "facebook":
        d.ellipse([0, 0, L - 1, L - 1], fill=255)
        d.text((L * 0.56, L * 1.0), "f", font=fuente_cartel("titulo", int(L * 0.85)), fill=0, anchor="ms")
    elif tipo == "instagram":
        d.rounded_rectangle([g / 2, g / 2, L - g / 2, L - g / 2], radius=int(L * 0.28), outline=255, width=g)
        d.ellipse([L * 0.29, L * 0.29, L * 0.71, L * 0.71], outline=255, width=g)
        d.ellipse([L * 0.69, L * 0.17, L * 0.81, L * 0.29], fill=255)
    elif tipo == "tiktok":
        d.ellipse([L * 0.12, L * 0.52, L * 0.54, L * 0.94], outline=255, width=int(g * 1.3))
        d.rectangle([L * 0.54 - g * 1.3, L * 0.06, L * 0.54, L * 0.73], fill=255)
        d.arc([L * 0.5, L * -0.2, L * 0.94, L * 0.34], start=90, end=180, fill=255, width=int(g * 1.3))
    else:  # web
        d.ellipse([g / 2, g / 2, L - g / 2, L - g / 2], outline=255, width=g)
        d.ellipse([L * 0.3, g / 2, L * 0.7, L - g / 2], outline=255, width=g)
        d.line([(g, L / 2), (L - g, L / 2)], fill=255, width=g)
        d.line([(L * 0.14, L * 0.3), (L * 0.86, L * 0.3)], fill=255, width=g)
        d.line([(L * 0.14, L * 0.7), (L * 0.86, L * 0.7)], fill=255, width=g)
    return m.resize((lado, lado), Image.LANCZOS)


def logo_ajustado(W, H, escala):
    if not LOGO.exists():
        return None
    with Image.open(LOGO) as lg:
        lg = lg.convert("RGBA")
    lg.thumbnail((int(W * 0.46 * escala), int(min(W * 0.2, H * 0.13) * escala)), Image.LANCZOS)
    return lg


def bloque_logo(W, H, escala):
    lg = logo_ajustado(W, H, escala)
    if lg is None:
        return None
    margen = int(W * 0.05)

    def dibujar(img, y):
        img.paste(lg, (margen, y), lg)
    return lg.height, dibujar


def bloque_titulo(texto, W, acento, escala):
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if not lineas:
        return None
    margen = int(W * 0.06)
    ancho = W - 2 * margen
    tam_max, tam_min = int(W * 0.2 * escala), int(W * 0.05 * escala)
    # Una línea muy larga se parte en dos para que la letra no quede pequeña
    partidas = []
    for l in lineas:
        palabras = l.split()
        if len(palabras) > 1 and ajustar_tam(l, "titulo", ancho, tam_max, 1) < W * 0.11 * escala:
            corte = min(range(1, len(palabras)), key=lambda i: abs(
                len(" ".join(palabras[:i])) - len(" ".join(palabras[i:]))))
            partidas += [" ".join(palabras[:corte]), " ".join(palabras[corte:])]
        else:
            partidas.append(l)
    filas = []
    for l in partidas:
        f = fuente_cartel("titulo", ajustar_tam(l, "titulo", ancho, tam_max, tam_min))
        filas.append((l, f, alto_mayus(f)))
    raya = int(W * 0.05 * escala)
    alto = sum(int(c * 1.3) for _, _, c in filas) - int(filas[-1][2] * 0.3) + raya

    def dibujar(img, y):
        elementos = []
        for l, f, cap in filas:
            y += cap
            elementos.append((margen, y, l, f, BLANCO))
            y += int(cap * 0.3)
        pintar(img, elementos, acento, sombra=max(4, int(W * 0.012)))
        # raya de color inclinada bajo el título
        y += int(raya * 0.45)
        grosor = max(3, int(W * 0.008))
        ImageDraw.Draw(img).polygon([(margen + W * 0.08, y + grosor), (W - margen * 0.4, y - raya * 0.35),
                                     (W - margen * 0.4, y - raya * 0.35 + 2), (margen + W * 0.08, y + 2 * grosor)],
                                    fill=acento)
    return alto, dibujar


def bloque_franja(texto, W, acento, escala, rnd):
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if not lineas:
        return None
    ancho_txt = W * 0.76
    filas = []
    for i, l in enumerate(lineas):
        grande = i == len(lineas) - 1
        tam_max = int(W * (0.12 if grande else 0.072) * escala)
        f = fuente_cartel("titulo", ajustar_tam(l, "titulo", ancho_txt, tam_max, int(W * 0.035)))
        filas.append((l, f, alto_mayus(f)))
    alto_txt = sum(int(c * 1.32) for _, _, c in filas) - int(filas[-1][2] * 0.32)
    pad = int(W * 0.05 * escala)
    alto_franja = alto_txt + 2 * pad
    extra = int(W * 0.04)  # lo que sube/baja al inclinarla
    color_marca = AMARILLO if sum(acento) < 600 else (0, 0, 0)

    def dibujar(img, y):
        capa = brochazo(int(W * 0.9), alto_franja, acento, rnd).rotate(2.5, Image.BICUBIC, expand=True)
        cx, cy = W // 2, y + (alto_franja + 2 * extra) // 2
        img.paste(capa, (cx - capa.width // 2, cy - capa.height // 2), capa)
        ty = cy - alto_txt // 2
        elementos = []
        for l, f, cap in filas:
            ty += cap
            elementos.append((cx - f.getlength(sin_marcas(l)) / 2, ty, l, f, BLANCO))
            ty += int(cap * 0.32)
        pintar(img, elementos, color_marca, sombra=max(3, int(W * 0.006)), opacidad=110)
    return alto_franja + 2 * extra, dibujar


def bloque_datos(texto, W, escala):
    items = [x.strip() for x in texto.split("|") if x.strip()][:3]
    if not items:
        return None
    hueco, barra, sep = int(W * 0.06), max(4, int(W * 0.007)), int(W * 0.022)
    ancho_col = (W - 2 * int(W * 0.06) - hueco * (len(items) - 1)) / len(items) - barra - sep
    tam = int(W * 0.042 * escala)
    while True:
        f = fuente_cartel("texto", tam)
        filas = [partir_equilibrado(sin_marcas(it), f, ancho_col) for it in items]
        if all(len(x) <= 2 for x in filas) or tam <= W * 0.024:
            break
        tam = int(tam * 0.92)
    cap = alto_mayus(f)
    lh = int(cap * 1.5)
    alto = max(len(x) for x in filas) * lh - (lh - cap)
    anchos = [barra + sep + max(f.getlength(l) for l in fl) for fl in filas]
    total = sum(anchos) + hueco * (len(items) - 1)

    def dibujar(img, y):
        d = ImageDraw.Draw(img)
        x = (W - total) / 2
        elementos = []
        for fl, ancho in zip(filas, anchos):
            alto_item = len(fl) * lh - (lh - cap)
            y0 = y + (alto - alto_item) / 2
            d.rectangle([x, y0 - cap * 0.15, x + barra, y0 + alto_item + cap * 0.15], fill=AMARILLO)
            ty = y0
            for l in fl:
                ty += cap
                elementos.append((x + barra + sep, ty, l, f, BLANCO))
                ty += lh - cap
            x += ancho + hueco
        pintar(img, elementos, AMARILLO, sombra=max(3, int(W * 0.006)))
    return alto, dibujar


def bloque_redes(redes, W, escala):
    if not redes:
        return None
    tam = int(W * 0.024 * escala)
    while True:
        f = fuente("texto", tam)
        lado = int(tam * 1.6)
        barra, sep, hueco = max(2, int(tam * 0.12)), int(tam * 0.5), int(tam * 1.4)
        anchos = [lado + 2 * sep + barra + f.getlength(h) for _, h in redes]
        total = sum(anchos) + hueco * (len(redes) - 1)
        if total <= W * 0.94 or tam <= 12:
            break
        tam -= 1

    def dibujar(img, y):
        d = ImageDraw.Draw(img)
        x = (W - total) / 2
        for (tipo, handle), ancho in zip(redes, anchos):
            img.paste(BLANCO, (int(x), y), icono_red(tipo, lado))
            bx = x + lado + sep
            d.rectangle([bx, y + lado * 0.1, bx + barra, y + lado * 0.9], fill=AMARILLO)
            d.text((bx + barra + sep, y + lado / 2), handle, font=f, fill=BLANCO, anchor="lm")
            x += ancho + hueco
    return lado, dibujar


def margenes(W, H):
    """(arriba, abajo) útiles: en stories se deja libre la zona que tapa Instagram."""
    story = H / W > 1.6
    return (int(H * 0.09) if story else int(W * 0.045)), H - (int(H * 0.12) if story else int(W * 0.04))


def redes_configuradas():
    return [(t, ajuste(f"red_{t}")) for t in REDES if ajuste(f"red_{t}")]


def banner_redes(W, H):
    """Banner de redes subido en Ajustes, escalado y colocado abajo: (imagen, x, y). None si no hay."""
    if not BANNER.exists():
        return None
    with Image.open(BANNER) as b:
        b = b.convert("RGBA")
    ancho = int(W * 0.94)
    b = b.resize((ancho, max(1, round(b.height * ancho / b.width))), Image.LANCZOS)
    if b.height > H * 0.12:
        b.thumbnail((ancho, int(H * 0.12)), Image.LANCZOS)
    _, abajo = margenes(W, H)
    return b, (W - b.width) // 2, abajo - b.height


def superponer_cartel(img, pub, occ):
    W, H = img.size
    arriba, abajo = margenes(W, H)
    acento = color_rgb(pub["color"])
    titulo = variables(pub["titulo"], occ).upper()
    franja = variables(pub["subtitulo"], occ).upper()
    datos = variables(pub["pie"], occ).upper()
    redes = redes_configuradas()
    banner = banner_redes(W, H)
    if banner:  # el banner sustituye a las redes dibujadas y todo lo demás va encima
        redes = []
        abajo = banner[2] - int(W * 0.03)

    # Foto con más garra: algo más de contraste y color
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.15)).enhance(1.08)

    # Se reduce todo hasta que quede hueco en medio para la foto
    for escala in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        rnd = random.Random(f"{pub['id']}-{occ}")
        de_arriba = [b for b in (bloque_logo(W, H, escala), bloque_titulo(titulo, W, acento, escala)) if b]
        de_abajo = [b for b in (bloque_franja(franja, W, acento, escala, rnd), bloque_datos(datos, W, escala),
                                bloque_redes(redes, W, escala)) if b]
        hueco = int(W * 0.035 * escala)
        alto_arriba = sum(a for a, _ in de_arriba) + hueco * max(0, len(de_arriba) - 1)
        alto_abajo = sum(a for a, _ in de_abajo) + hueco * max(0, len(de_abajo) - 1)
        if arriba + alto_arriba + H * 0.1 <= abajo - alto_abajo:
            break

    inicio_abajo = abajo - alto_abajo - int(H * 0.15) if de_abajo else H
    if banner:
        inicio_abajo = min(inicio_abajo, banner[2] - int(H * 0.08))
    oscurecer_cartel(img, arriba + alto_arriba + int(H * 0.12) if de_arriba else 0, inicio_abajo)
    y = arriba
    for alto, dibujar in de_arriba:
        dibujar(img, y)
        y += alto + hueco
    y = abajo - alto_abajo
    for alto, dibujar in de_abajo:
        dibujar(img, y)
        y += alto + hueco
    if banner:
        img.paste(banner[0], banner[1:], banner[0])
    return img


def superponer_marca(img):
    """Diseño «IA completa»: los textos ya los puso la IA; aquí solo logo y redes, siempre iguales."""
    W, H = img.size
    arriba, abajo = margenes(W, H)
    banner = banner_redes(W, H)
    redes = None if banner else bloque_redes(redes_configuradas(), W, 1.0)
    if banner:
        oscurecer_cartel(img, 0, banner[2] - int(H * 0.06), vineta=False)
        img.paste(banner[0], banner[1:], banner[0])
    elif redes:
        alto, dibujar = redes
        oscurecer_cartel(img, 0, abajo - alto - int(H * 0.06), vineta=False)
        dibujar(img, abajo - alto)
    logo = bloque_logo(W, H, 1.0)
    if logo:
        logo[1](img, arriba)
    return img


def elegir_estilo(pub, evitar=None):
    """Estilo fijo de la publicación, o con «Variado» uno al azar distinto del último."""
    if pub["estilo_ia"] in ia_fondo.ESTILOS:
        return pub["estilo_ia"]
    if evitar is None:
        ultimo = consulta("SELECT estilo FROM generadas WHERE publicacion_id = ? AND estilo != '' "
                          "ORDER BY id DESC LIMIT 1", (pub["id"],))
        evitar = ultimo[0]["estilo"] if ultimo else None
    return random.choice([e for e in ia_fondo.ESTILOS if e != evitar])


def prompt_ia_cartel(pub, occ, W, H, estilo="marca"):
    """Prompt para «IA completa», con los huecos exactos que luego ocupan el logo y las redes."""
    arriba, abajo = margenes(W, H)
    lg = logo_ajustado(W, H, 1.0)
    banner = banner_redes(W, H)
    redes = bloque_redes(redes_configuradas(), W, 1.0)
    zona_logo = (round((W * 0.05 + lg.width) / W * 100) + 4, round((arriba + lg.height) / H * 100) + 3) if lg else None
    if banner:
        zona_redes = round((H - banner[2]) / H * 100) + 3
    else:
        zona_redes = round((H - abajo + redes[0]) / H * 100) + 3 if redes else None
    lineas = lambda texto: [l.strip() for l in variables(texto, occ).upper().split("\n") if l.strip()]  # noqa: E731
    datos = [x.strip() for x in variables(pub["pie"], occ).upper().split("|") if x.strip()]
    return ia_fondo.prompt_cartel(lineas(pub["titulo"]), lineas(pub["subtitulo"]), datos, pub["color"],
                                  pub["prompt_ia"], zona_logo, zona_redes, estilo)


def superponer(img, pub, occ):
    if pub["diseno"] == "sencillo":
        return superponer_sencillo(img, pub, occ)
    return superponer_cartel(img, pub, occ)  # también si «IA completa» no ha podido usarse


def elegir_foto(pub_id, fotos, evitar=None):
    """Una al azar, sin repetir «evitar» ni, si no se indica, la última usada en esta publicación."""
    if not fotos:
        return None
    if evitar is None:
        ultima = consulta("SELECT foto_id FROM generadas WHERE publicacion_id = ? AND foto_id IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1", (pub_id,))
        evitar = ultima[0]["foto_id"] if ultima else None
    return random.choice([f for f in fotos if f["id"] != evitar] or fotos)


def componer(pub, occ, evitar=None, evitar_estilo=None):
    """(imagen, foto_id, con_ia, aviso, estilo)"""
    _, ancho, alto = FORMATOS.get(pub["formato"], FORMATOS["post"])
    avisos = []
    fotos = consulta("SELECT * FROM fotos WHERE categoria_id = ?", (pub["categoria_id"],))
    foto = elegir_foto(pub["id"], fotos, evitar)
    fondo, con_ia = None, False

    if foto is None:
        avisos.append("La categoría no tiene fotos: se ha usado un fondo liso." if pub["categoria_id"]
                      else "No has elegido categoría de fotos: se ha usado un fondo liso.")
        fondo = fondo_liso(ancho, alto, pub["color"])
    else:
        ruta = DIR_FOTOS / foto["archivo"]
        clave = ajuste("openai_key")
        if pub["diseno"] == "ia":
            if not clave:
                avisos.append("«IA completa» necesita la clave de OpenAI (Ajustes). Se ha usado el diseño Cartel.")
            else:
                try:
                    estilo = elegir_estilo(pub, evitar_estilo)
                    with DIBUJO:
                        prompt = prompt_ia_cartel(pub, occ, ancho, alto, estilo)
                    img = ia_fondo.generar_cartel(ruta, prompt, ancho, alto, clave,
                                                  calidad=ajuste("openai_calidad", "medium"))
                    with DIBUJO:
                        img = superponer_marca(img)
                    return img, foto["id"], True, "", estilo
                except Exception as e:  # la IA nunca debe dejar la publicación sin imagen
                    log.warning("IA: %s", e)
                    avisos.append(f"IA: ha fallado ({str(e)[:300]}). Se ha usado el diseño Cartel.")
        elif pub["prompt_ia"].strip() and clave:  # sin clave de OpenAI la IA simplemente no se usa
            if True:
                try:
                    fondo = ia_fondo.generar_fondo(ruta, pub["prompt_ia"], ancho, alto, clave,
                                                   calidad=ajuste("openai_calidad", "medium"))
                    con_ia = True
                except Exception as e:  # la IA nunca debe dejar la publicación sin imagen
                    log.warning("IA: %s", e)
                    avisos.append(f"IA: ha fallado ({str(e)[:300]}). Se ha usado la foto original.")
        if fondo is None:
            with Image.open(ruta) as im:
                fondo = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (ancho, alto), Image.LANCZOS)

    with DIBUJO:
        img = superponer(fondo, pub, occ)
    return img, (foto["id"] if foto else None), con_ia, " ".join(avisos), ""


# ---------------------------------------------------------------- generación y programador

def versiones(texto):
    """«A\n---\nB» -> ["A", "B"]: versiones separadas por una línea con tres guiones (o más)."""
    partes = [v.strip() for v in re.split(r"^[ \t]*-{3,}[ \t\r]*$", texto or "", flags=re.M)]
    return [v for v in partes if v] or [""]


def con_version(pub, fila):
    """Con varias versiones en los textos, la que toca: se van turnando (1ª, 2ª, 3ª, 1ª...).

    Cuenta las imágenes anteriores de la misma publicación (y del mismo evento). Las pruebas
    no cuentan para las programadas; «Otra foto» mantiene la versión.
    """
    n = consulta("SELECT COUNT(*) AS n FROM generadas WHERE publicacion_id = ? AND evento_id = ? AND id < ?"
                 + ("" if fila["prueba"] else " AND prueba = 0"),
                 (pub["id"], fila["evento_id"], fila["id"]))[0]["n"]
    datos = dict(pub)
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia"):
        opciones = versiones(datos[campo])
        datos[campo] = opciones[n % len(opciones)]
    return datos


def reclamar(pub_id, occ, prueba, evento_id=0):
    """Crea la fila «generando». Devuelve su id, o None si esa hora ya estaba generada."""
    cur = ejecutar("INSERT OR IGNORE INTO generadas (publicacion_id, evento_id, ocurrencia, prueba, creada) "
                   "VALUES (?, ?, ?, ?, ?)",
                   (pub_id, evento_id, occ.strftime("%Y-%m-%d %H:%M"), int(prueba), ahora_txt()))
    return cur.lastrowid if cur.rowcount else None


def generar(gen_id, pub, occ, enviar=False):
    try:
        anterior = consulta("SELECT * FROM generadas WHERE id = ?", (gen_id,))
        if anterior:
            pub = con_version(pub, anterior[0])
        img, foto_id, con_ia, aviso, estilo = componer(pub, occ, anterior[0]["foto_id"] if anterior else None,
                                                      (anterior[0]["estilo"] or None) if anterior else None)
        archivo = f"{pub['id']}_{occ:%Y%m%d_%H%M}_{gen_id}_{uuid.uuid4().hex[:6]}.jpg"
        img.save(DIR_GEN / archivo, "JPEG", quality=92, optimize=True)
        texto = variables(pub["texto"], occ)
        if "_aviso" in pub.keys():  # publicaciones de eventos: p. ej. datos de Racecore sin actualizar
            aviso = " ".join(a for a in (pub["_aviso"], aviso) if a)
        cur = ejecutar("UPDATE generadas SET estado = 'lista', archivo = ?, texto = ?, foto_id = ?, con_ia = ?, "
                       "aviso = ?, estilo = ? WHERE id = ? AND estado = 'generando'",
                       (archivo, texto, foto_id, int(con_ia), aviso, estilo, gen_id))
        if not cur.rowcount:  # la borraron mientras se generaba
            (DIR_GEN / archivo).unlink(missing_ok=True)
            return
        if anterior and anterior[0]["archivo"]:
            (DIR_GEN / anterior[0]["archivo"]).unlink(missing_ok=True)
        log.info("Lista: %s (%s)%s", pub["nombre"], cuando_txt(occ), " con IA" if con_ia else "")
        if enviar:
            avisar_telegram(DIR_GEN / archivo, pub, occ, texto)
    except Exception as e:
        log.exception("Error generando «%s»", pub["nombre"])
        ejecutar("UPDATE generadas SET estado = 'error', aviso = ? WHERE id = ? AND estado = 'generando'",
                 (str(e)[:500], gen_id))


def en_segundo_plano(gen_id, pub, occ):
    """pub ya con los datos del evento puestos (con_evento), si es de eventos."""
    threading.Thread(target=generar, args=(gen_id, pub, occ), daemon=True).start()


def avisar_telegram(ruta, pub, occ, texto):
    token, chat = ajuste("telegram_token"), ajuste("telegram_chat")
    if not (token and chat):
        return
    api = f"https://api.telegram.org/bot{token}"
    try:
        with open(ruta, "rb") as f:
            requests.post(f"{api}/sendPhoto", timeout=60, files={"photo": f}, data={
                "chat_id": chat, "caption": f"📣 {pub['nombre']} · publicar {cuando_txt(occ)}"}).raise_for_status()
        if texto.strip():
            requests.post(f"{api}/sendMessage", timeout=30,
                          data={"chat_id": chat, "text": texto[:4096]}).raise_for_status()
    except Exception as e:
        log.warning("Telegram: %s", str(e).replace(token, "***"))


def comprobar(ahora=None):
    """Genera lo que toque: desde «antelación» minutos antes de la hora hasta poco después."""
    ahora = ahora or datetime.now()
    eventos = None
    for pub in consulta("SELECT * FROM publicaciones WHERE activa = 1"):
        antelacion = timedelta(minutes=max(0, pub["antelacion"]))
        if pub["modo"] == "evento":
            if eventos is None:
                eventos = consulta("SELECT * FROM eventos")
            for occ, ev in ocurrencias_evento(pub, ahora - GRACIA, ahora + antelacion, eventos):
                # si no se cumple la condición (p. ej. aún no hay resultados) se vuelve a mirar después
                if cumple(pub, ev):
                    gen_id = reclamar(pub["id"], occ, prueba=False, evento_id=ev["id"])
                    if gen_id:
                        generar(gen_id, con_evento(pub, ev, occ), occ, enviar=True)
            continue
        for occ in ocurrencias(pub, ahora - GRACIA, ahora + antelacion):
            if occ > ahora - GRACIA:
                gen_id = reclamar(pub["id"], occ, prueba=False)
                if gen_id:
                    generar(gen_id, pub, occ, enviar=True)


def limpiar_antiguas():
    limite = (datetime.now() - timedelta(days=DIAS_CONSERVAR)).strftime("%Y-%m-%d %H:%M:%S")
    for g in consulta("SELECT id, archivo FROM generadas WHERE creada < ?", (limite,)):
        if g["archivo"]:
            (DIR_GEN / g["archivo"]).unlink(missing_ok=True)
        ejecutar("DELETE FROM generadas WHERE id = ?", (g["id"],))


def programador():
    ultimo_limpiado = None
    ultima_lectura = None
    while True:
        if ultima_lectura is None or time.monotonic() - ultima_lectura >= CADA_SINCRONIZAR:
            ultima_lectura = time.monotonic()
            sincronizar_seguro()
        try:
            comprobar()
            if ultimo_limpiado != date.today():
                limpiar_antiguas()
                ultimo_limpiado = date.today()
        except Exception:
            log.exception("Programador")
        ESTADO["ultima_comprobacion"] = datetime.now()
        time.sleep(30)


# ---------------------------------------------------------------- web

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024


def pub_o_404(pub_id):
    fila = consulta("SELECT * FROM publicaciones WHERE id = ?", (pub_id,))
    if not fila:
        abort(404)
    return fila[0]


@app.route("/archivo/<carpeta>/<path:nombre>")
def archivo(carpeta, nombre):
    carpetas = {"fotos": DIR_FOTOS, "miniaturas": DIR_MINIS, "generadas": DIR_GEN}
    if carpeta not in carpetas:
        abort(404)
    return send_from_directory(carpetas[carpeta], nombre, as_attachment=request.args.get("descargar") == "1")


# --- Listas para publicar

@app.route("/")
def listas():
    filas = consulta("""
        SELECT g.*, p.nombre, e.nombre AS evento FROM generadas g
        LEFT JOIN publicaciones p ON p.id = g.publicacion_id LEFT JOIN eventos e ON e.id = g.evento_id
        WHERE g.estado IN ('generando', 'lista', 'error') ORDER BY g.ocurrencia, g.id""")
    hechas = consulta("""
        SELECT g.*, p.nombre, e.nombre AS evento FROM generadas g
        LEFT JOIN publicaciones p ON p.id = g.publicacion_id LEFT JOIN eventos e ON e.id = g.evento_id
        WHERE g.estado = 'publicada' ORDER BY g.ocurrencia DESC LIMIT 12""")

    def preparar(g):
        occ = datetime.strptime(g["ocurrencia"], "%Y-%m-%d %H:%M")
        return dict(g) | {"cuando": cuando_txt(occ), "nombre": g["nombre"] or "(publicación borrada)",
                          "estilo_nombre": ia_fondo.ESTILOS.get(g["estilo"], ("",))[0]}

    return render_template("listas.html", filas=[preparar(g) for g in filas],
                           hechas=[preparar(g) for g in hechas],
                           generando=any(g["estado"] == "generando" for g in filas))


def generada_o_404(gen_id):
    fila = consulta("SELECT * FROM generadas WHERE id = ?", (gen_id,))
    if not fila:
        abort(404)
    return fila[0]


@app.post("/generadas/<int:gen_id>/publicada")
def marcar_publicada(gen_id):
    generada_o_404(gen_id)
    ejecutar("UPDATE generadas SET estado = 'publicada' WHERE id = ?", (gen_id,))
    return redirect(url_for("listas"))


@app.post("/generadas/<int:gen_id>/otra")
def otra_foto(gen_id):
    g = generada_o_404(gen_id)
    pub = consulta("SELECT * FROM publicaciones WHERE id = ?", (g["publicacion_id"],))
    if not pub:
        flash("Esa publicación ya no existe.", "error")
        return redirect(url_for("listas"))
    occ = datetime.strptime(g["ocurrencia"], "%Y-%m-%d %H:%M")
    pub = pub[0]
    if g["evento_id"]:  # se rehace con los datos del evento de ahora (inscritos, resultados...)
        ev = consulta("SELECT * FROM eventos WHERE id = ?", (g["evento_id"],))
        if not ev:
            flash("Ese evento ya no está en el panel.", "error")
            return redirect(url_for("listas"))
        pub = con_evento(pub, ev[0], occ)
    ejecutar("UPDATE generadas SET estado = 'generando', aviso = '' WHERE id = ?", (gen_id,))
    en_segundo_plano(gen_id, pub, occ)
    return redirect(url_for("listas"))


@app.post("/generadas/<int:gen_id>/borrar")
def borrar_generada(gen_id):
    g = generada_o_404(gen_id)
    if g["archivo"]:
        (DIR_GEN / g["archivo"]).unlink(missing_ok=True)
    # Se queda la fila como «descartada» para que el programador no la vuelva a generar
    ejecutar("UPDATE generadas SET estado = 'descartada', archivo = NULL WHERE id = ?", (gen_id,))
    return redirect(url_for("listas"))


# --- Publicaciones

NUEVA = {"id": None, "nombre": "", "activa": 1, "modo": "semanal", "dias": "", "fecha": "",
         "hora": "18:00", "antelacion": 1440, "categoria_id": None, "formato": "post",
         "titulo": "", "subtitulo": "", "pie": "", "texto": "", "color": "#e8195a", "prompt_ia": "",
         "diseno": "cartel", "estilo_ia": "variado",
         "momento": "antes", "dias_desde": 14, "dias_hasta": 3, "cada": 2, "filtro": "", "condicion": ""}


@app.route("/publicaciones")
def publicaciones():
    cats = {c["id"]: c["nombre"] for c in consulta("SELECT * FROM categorias")}
    filas = []
    for p in consulta("SELECT * FROM publicaciones ORDER BY activa DESC, nombre"):
        occ, ev = None, None
        if p["activa"]:
            occ, ev = proxima_evento(p) if p["modo"] == "evento" else (proxima(p), None)
        genera = occ - timedelta(minutes=p["antelacion"]) if occ else None
        filas.append(dict(p) | {
            "programacion": resumen_programacion(p),
            "categoria": cats.get(p["categoria_id"], "sin categoría"),
            "formato_txt": FORMATOS.get(p["formato"], FORMATOS["post"])[0],
            "proxima": (cuando_txt(occ) + (f" · {ev['nombre']}" if ev else "")) if occ else "",
            "genera": genera.strftime("%d/%m %H:%M") if genera else "",
        })
    return render_template("publicaciones.html", filas=filas)


def leer_formulario():
    f = request.form
    errores = []
    dias = sorted({x for x in f.getlist("dias") if x in "0123456"})
    hora = f.get("hora", "").strip()
    try:
        hora = datetime.strptime(hora, "%H:%M").strftime("%H:%M")
    except ValueError:
        errores.append("La hora no es válida.")
        hora = "18:00"
    try:
        antelacion = min(10080, max(0, int(f.get("antelacion") or 0)))
    except ValueError:
        antelacion = 60
    try:
        categoria_id = int(f.get("categoria_id") or 0) or None
    except ValueError:
        categoria_id = None

    def entero(nombre, defecto, minimo, maximo):
        try:
            return min(maximo, max(minimo, int(f.get(nombre) or defecto)))
        except ValueError:
            return defecto
    color = f.get("color", "#e10600").strip()
    if not (len(color) == 7 and color.startswith("#")):
        color = "#e10600"
    datos = {
        "nombre": f.get("nombre", "").strip() or "Sin nombre",
        "activa": 1 if f.get("activa") else 0,
        "modo": f.get("modo") if f.get("modo") in ("fecha", "evento") else "semanal",
        "dias": ",".join(dias),
        "fecha": f.get("fecha", "").strip(),
        "hora": hora,
        "antelacion": antelacion,
        "categoria_id": categoria_id,
        "formato": f.get("formato") if f.get("formato") in FORMATOS else "post",
        "titulo": f.get("titulo", "").strip().replace("\r", ""),
        "subtitulo": f.get("subtitulo", "").strip().replace("\r", ""),
        "pie": f.get("pie", "").strip(),
        "texto": f.get("texto", "").strip(),
        "color": color,
        "prompt_ia": f.get("prompt_ia", "").strip(),
        "diseno": f.get("diseno") if f.get("diseno") in ("sencillo", "ia") else "cartel",
        "estilo_ia": f.get("estilo_ia") if f.get("estilo_ia") in ia_fondo.ESTILOS else "variado",
        "momento": f.get("momento") if f.get("momento") in MOMENTOS else "antes",
        "dias_desde": entero("dias_desde", 14, 0, 365),
        "dias_hasta": entero("dias_hasta", 3, 0, 365),
        "cada": entero("cada", 1, 1, 60),
        "filtro": f.get("filtro", "").strip(),
        "condicion": f.get("condicion") if f.get("condicion") in CONDICIONES else "",
    }
    if datos["modo"] == "semanal" and not dias:
        errores.append("Marca al menos un día de la semana.")
    if datos["modo"] == "fecha":
        try:
            date.fromisoformat(datos["fecha"])
        except ValueError:
            errores.append("Pon la fecha.")
    return datos, errores


@app.route("/publicaciones/nueva", methods=["GET", "POST"])
@app.route("/publicaciones/<int:pub_id>", methods=["GET", "POST"])
def editar_publicacion(pub_id=None):
    pub = dict(pub_o_404(pub_id)) if pub_id else dict(NUEVA)
    if not pub_id and request.args.get("modo") == "evento":
        pub["modo"] = "evento"
    if request.method == "POST":
        datos, errores = leer_formulario()
        if errores:
            for e in errores:
                flash(e, "error")
            pub.update(datos)
        else:
            campos = list(datos)
            if pub_id:
                ejecutar(f"UPDATE publicaciones SET {', '.join(c + ' = ?' for c in campos)} WHERE id = ?",
                         [datos[c] for c in campos] + [pub_id])
            else:
                pub_id = ejecutar(f"INSERT INTO publicaciones ({', '.join(campos)}) "
                                  f"VALUES ({', '.join('?' for _ in campos)})",
                                  [datos[c] for c in campos]).lastrowid
            if request.form.get("y_generar"):
                return generar_ahora(pub_id)
            flash("Guardada.", "ok")
            return redirect(url_for("publicaciones"))
    return render_template("publicacion.html", pub=pub, formatos=FORMATOS, dias=DIAS,
                           categorias=consulta("SELECT * FROM categorias ORDER BY nombre"),
                           dias_marcados=set(str(pub["dias"]).split(",")), hay_ia=bool(ajuste("openai_key")),
                           estilos=ia_fondo.ESTILOS, momentos=MOMENTOS, condiciones=CONDICIONES)


@app.post("/publicaciones/<int:pub_id>/generar")
def generar_ahora(pub_id):
    pub = pub_o_404(pub_id)
    if pub["modo"] == "evento":
        occ, ev = prueba_evento(pub)
        if not ev:
            flash("No hay ningún evento con fecha para probar esta publicación. Mira la pestaña Eventos.", "error")
            return redirect(url_for("publicaciones"))
        en_segundo_plano(reclamar(pub_id, occ, prueba=True, evento_id=ev["id"]), con_evento(pub, ev, occ), occ)
        flash(f"Generando «{pub['nombre']}» con el evento «{ev['nombre']}» ({cuando_txt(occ)})… "
              "aparecerá aquí en unos segundos.", "ok")
        return redirect(url_for("listas"))
    occ = ocurrencia_prueba(pub)
    en_segundo_plano(reclamar(pub_id, occ, prueba=True), pub, occ)
    flash(f"Generando «{pub['nombre']}»… aparecerá aquí en unos segundos"
          f"{' (con IA puede tardar 1-2 minutos)' if pub['prompt_ia'].strip() else ''}.", "ok")
    return redirect(url_for("listas"))


@app.post("/publicaciones/<int:pub_id>/activar")
def activar_publicacion(pub_id):
    pub = pub_o_404(pub_id)
    ejecutar("UPDATE publicaciones SET activa = ? WHERE id = ?", (0 if pub["activa"] else 1, pub_id))
    return redirect(url_for("publicaciones"))


@app.post("/publicaciones/<int:pub_id>/borrar")
def borrar_publicacion(pub_id):
    pub_o_404(pub_id)
    ejecutar("DELETE FROM publicaciones WHERE id = ?", (pub_id,))
    flash("Publicación borrada.", "ok")
    return redirect(url_for("publicaciones"))


# --- Eventos

def estado_racecore():
    ok = ESTADO["racecore_ok"]
    return {"configurado": bool(ajuste("racecore_url")), "url": url_racecore(), "error": ESTADO["racecore_error"],
            "ok": ok.strftime("%d/%m %H:%M") if ok else "", "n": ESTADO["racecore_n"]}


def leer_ahora():
    """Lee Racecore ahora mismo y lo cuenta con un mensaje."""
    try:
        n = sincronizar()
        if n is None:
            flash("Racecore no está configurado: pon su dirección y el token en Ajustes.", "error")
        else:
            flash(f"Racecore: {n} evento(s) leído(s).", "ok")
    except ErrorFuente as e:
        flash(f"Racecore: {e}", "error")
    except Exception as e:
        log.exception("Racecore")
        flash(f"Error leyendo Racecore: {e}", "error")


@app.route("/eventos")
def eventos():
    ahora = datetime.now()
    hoy = ahora.date()
    reglas = consulta("SELECT * FROM publicaciones WHERE modo = 'evento' AND activa = 1")
    filas = []
    for ev in consulta("SELECT * FROM eventos"):
        f = leer_fecha(ev["fecha"])
        if f and f < hoy - timedelta(days=30):
            continue
        proximas = sorted((occ, r["nombre"], CONDICIONES[r["condicion"]].lower() if r["condicion"] in CONDICIONES
                           and r["condicion"] else "")
                          for r in reglas for occ, _ in ocurrencias_evento(r, ahora - GRACIA, datetime.max, [ev]))
        libres = plazas_libres(ev)
        if ev["plazas"]:
            inscritos = f"{ev['inscritos'] if ev['inscritos'] is not None else '?'} de {ev['plazas']}"
            inscritos += f" (quedan {libres})" if libres is not None else ""
        else:
            inscritos = txt(ev["inscritos"]) or "—"
        filas.append(dict(ev) | {
            "orden": (0, f.toordinal()) if f and f >= hoy else ((1, -f.toordinal()) if f else (2, 0)),
            "cuando": f"{DIAS[f.weekday()]} {f:%d/%m/%Y}" + (f" · {ev['hora']}" if ev["hora"] else "") if f else "",
            "pasado": bool(f and f < hoy), "inscritos_txt": inscritos,
            "proximas": [(cuando_txt(o), n, c) for o, n, c in proximas[:8]],
        })
    filas.sort(key=lambda e: e["orden"])
    hay_reglas = bool(consulta("SELECT 1 FROM publicaciones WHERE modo = 'evento' LIMIT 1"))
    return render_template("eventos.html", eventos=filas, racecore=estado_racecore(), hay_reglas=hay_reglas,
                           hay_activas=bool(reglas))


@app.post("/eventos/leer")
def leer_eventos():
    leer_ahora()
    return redirect(url_for("ajustes") if request.form.get("volver") == "ajustes" else url_for("eventos"))


EVENTO_NUEVO = {"id": None, "nombre": "", "campeonato": "", "fecha": "", "hora": "", "plazas": None,
                "inscritos": None, "precio": "", "abierta": 1, "enlace": "", "web": "", "horarios": "",
                "resultados": "", "ganadores": ""}


def evento_manual_o_404(ev_id):
    fila = consulta("SELECT * FROM eventos WHERE id = ? AND origen = 'manual'", (ev_id,))
    if not fila:
        abort(404)
    return fila[0]


@app.route("/eventos/nuevo", methods=["GET", "POST"])
@app.route("/eventos/<int:ev_id>", methods=["GET", "POST"])
def editar_evento(ev_id=None):
    """Eventos a mano: para lo que no está en Racecore (o mientras no esté conectado)."""
    ev = dict(evento_manual_o_404(ev_id)) if ev_id else dict(EVENTO_NUEVO)
    if request.method == "POST":
        f = request.form
        datos = {
            "nombre": f.get("nombre", "").strip() or "Sin nombre", "campeonato": f.get("campeonato", "").strip(),
            "fecha": f.get("fecha", "").strip(), "hora": leer_hora(f.get("hora")),
            "plazas": como_entero(f.get("plazas")), "inscritos": como_entero(f.get("inscritos")),
            "precio": f.get("precio", "").strip(), "abierta": 1 if f.get("abierta") else 0,
            "enlace": f.get("enlace", "").strip(), "web": f.get("web", "").strip(),
            "horarios": f.get("horarios", "").strip().replace("\r", ""),
            "resultados": f.get("resultados", "").strip().replace("\r", ""),
            "ganadores": f.get("ganadores", "").strip(), "actualizado": ahora_txt(),
        }
        if not leer_fecha(datos["fecha"]):
            flash("Pon la fecha del evento.", "error")
            ev.update(datos)
        else:
            campos = list(datos)
            if ev_id:
                ejecutar(f"UPDATE eventos SET {', '.join(c + ' = ?' for c in campos)} WHERE id = ?",
                         [datos[c] for c in campos] + [ev_id])
            else:
                datos |= {"origen": "manual", "ext_id": uuid.uuid4().hex}
                campos = list(datos)
                ejecutar(f"INSERT INTO eventos ({', '.join(campos)}) VALUES ({', '.join('?' for _ in campos)})",
                         [datos[c] for c in campos])
            flash("Evento guardado.", "ok")
            return redirect(url_for("eventos"))
    return render_template("evento.html", ev=ev)


@app.post("/eventos/<int:ev_id>/borrar")
def borrar_evento(ev_id):
    evento_manual_o_404(ev_id)
    ejecutar("DELETE FROM eventos WHERE id = ?", (ev_id,))
    flash("Evento borrado.", "ok")
    return redirect(url_for("eventos"))


# Campaña de ejemplo: se crean en pausa para revisarlas antes de activarlas
EJEMPLOS = [
    {"nombre": "Evento · Recordatorio", "momento": "antes", "dias_desde": 14, "dias_hasta": 3, "cada": 2,
     "hora": "19:00", "condicion": "abierta",
     "titulo": "¡QUEDAN\n*{dias} DÍAS!*", "subtitulo": "{evento}\n¡Apúntate ya!",
     "pie": "{dia} {fecha} | {inscritos} inscritos | {precio}",
     "texto": "🏁 {evento} es {faltan}: {dia} {fecha}.\n\nYa hay {inscritos} pilotos inscritos. "
              "¿Te lo vas a perder?\n\n👉 Inscripciones: {enlace}"},
    {"nombre": "Evento · Llamada a la acción", "momento": "antes", "dias_desde": 10, "dias_hasta": 10, "cada": 1,
     "hora": "12:00", "condicion": "abierta",
     "titulo": "¿TE LO VAS A\n*PERDER?*", "subtitulo": "{evento}\nInscripción abierta",
     "pie": "{dia} {fecha} | {precio}",
     "texto": "🔥 Solo quedan {dias} días para {evento} ({dia} {fecha}).\n\n"
              "Las inscripciones están abiertas. ¡Reserva tu plaza!\n\n👉 {enlace}"},
    {"nombre": "Evento · Horarios", "momento": "antes", "dias_desde": 2, "dias_hasta": 2, "cada": 1,
     "hora": "19:00", "condicion": "",
     "titulo": "*HORARIOS*", "subtitulo": "{evento}\n{dia} {fecha}", "pie": "Empezamos a las {hora}",
     "texto": "⏱️ Horarios de {evento} ({dia} {fecha}):\n\n{horarios}\n\n¡Nos vemos en pista!"},
    {"nombre": "Evento · Inscritos", "momento": "antes", "dias_desde": 1, "dias_hasta": 1, "cada": 1,
     "hora": "19:00", "condicion": "",
     "titulo": "*{inscritos}*\nPILOTOS", "subtitulo": "{evento}\n¡Es {faltan}!",
     "pie": "{dia} {fecha} | Desde las {hora}",
     "texto": "✅ ¡Todo listo! {inscritos} pilotos para {evento}, {faltan}.\n\n"
              "📋 Lista de inscritos y horarios: {web}"},
    {"nombre": "Evento · Resultados", "momento": "despues", "dias_desde": 1, "dias_hasta": 1, "cada": 1,
     "hora": "10:00", "antelacion": 600, "condicion": "resultados",
     "titulo": "*RESULTADOS*", "subtitulo": "{evento}\n¡Enhorabuena!", "pie": "{ganadores}",
     "texto": "🏆 Resultados de {evento}:\n\n{resultados}\n\n"
              "¡Gracias a todos por participar! Clasificación completa: {web}"},
]


@app.post("/eventos/ejemplos")
def crear_ejemplos():
    creadas = 0
    for ejemplo in EJEMPLOS:
        if consulta("SELECT 1 FROM publicaciones WHERE nombre = ?", (ejemplo["nombre"],)):
            continue
        datos = {k: v for k, v in NUEVA.items() if k != "id"} | {
            "modo": "evento", "activa": 0, "formato": "vertical"} | ejemplo
        campos = list(datos)
        ejecutar(f"INSERT INTO publicaciones ({', '.join(campos)}) VALUES ({', '.join('?' for _ in campos)})",
                 [datos[c] for c in campos])
        creadas += 1
    flash(f"He creado {creadas} publicación(es) de ejemplo en pausa. Edítalas, elige la categoría de fotos, "
          "prueba con «Generar ahora» y actívalas." if creadas else "Las publicaciones de ejemplo ya existían.", "ok")
    return redirect(url_for("publicaciones"))


# --- Fotos

@app.route("/fotos")
def fotos():
    cats = consulta("""
        SELECT c.*, COUNT(f.id) AS total, MIN(f.archivo) AS portada
        FROM categorias c LEFT JOIN fotos f ON f.categoria_id = c.id
        GROUP BY c.id ORDER BY c.nombre""")
    return render_template("fotos.html", cats=cats)


@app.post("/categorias")
def crear_categoria():
    nombre = request.form.get("nombre", "").strip()
    if not nombre:
        flash("Pon un nombre.", "error")
    elif consulta("SELECT 1 FROM categorias WHERE nombre = ?", (nombre,)):
        flash("Ya existe una categoría con ese nombre.", "error")
    else:
        cat_id = ejecutar("INSERT INTO categorias (nombre) VALUES (?)", (nombre,)).lastrowid
        return redirect(url_for("categoria", cat_id=cat_id))
    return redirect(url_for("fotos"))


def categoria_o_404(cat_id):
    fila = consulta("SELECT * FROM categorias WHERE id = ?", (cat_id,))
    if not fila:
        abort(404)
    return fila[0]


@app.route("/categorias/<int:cat_id>")
def categoria(cat_id):
    cat = categoria_o_404(cat_id)
    lista = consulta("SELECT * FROM fotos WHERE categoria_id = ? ORDER BY id DESC", (cat_id,))
    return render_template("categoria.html", cat=cat, fotos=lista,
                           subidas=request.args.get("subidas", type=int), malas=request.args.get("malas", type=int))


def miniatura(archivo):
    return Path(archivo).stem + ".jpg"


def guardar_foto(cat_id, f):
    """Guarda la foto ya girada y reducida, y su miniatura. False si no es una imagen válida."""
    if Path(f.filename or "").suffix.lower() not in EXTENSIONES:
        return False
    archivo = f"{uuid.uuid4().hex}.jpg"
    try:
        with Image.open(f.stream) as im:
            im.draft("RGB", (LADO_MAX_FOTO, LADO_MAX_FOTO))  # JPEG grandes: decodifica ya reducido
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((LADO_MAX_FOTO, LADO_MAX_FOTO), Image.LANCZOS)
            im.save(DIR_FOTOS / archivo, "JPEG", quality=90)
            im.thumbnail((480, 480))
            im.save(DIR_MINIS / miniatura(archivo), "JPEG", quality=85)
    except Exception:
        (DIR_FOTOS / archivo).unlink(missing_ok=True)
        return False
    ejecutar("INSERT INTO fotos (categoria_id, archivo, subida) VALUES (?, ?, ?)", (cat_id, archivo, ahora_txt()))
    return True


@app.post("/categorias/<int:cat_id>/subir")
def subir_fotos(cat_id):
    categoria_o_404(cat_id)
    subidas = malas = 0
    for f in request.files.getlist("fotos"):
        if guardar_foto(cat_id, f):
            subidas += 1
        else:
            malas += 1
    if request.headers.get("X-Subida") == "1":  # subida de una en una desde la página
        return {"subidas": subidas, "malas": malas}
    if subidas:
        flash(f"{subidas} foto(s) subida(s).", "ok")
    if malas:
        flash(f"{malas} archivo(s) no válidos: usa fotos JPG, PNG o WEBP.", "error")
    return redirect(url_for("categoria", cat_id=cat_id))


def borrar_archivos_foto(foto):
    (DIR_FOTOS / foto["archivo"]).unlink(missing_ok=True)
    (DIR_MINIS / miniatura(foto["archivo"])).unlink(missing_ok=True)


@app.post("/fotos/<int:foto_id>/borrar")
def borrar_foto(foto_id):
    fila = consulta("SELECT * FROM fotos WHERE id = ?", (foto_id,))
    if not fila:
        abort(404)
    borrar_archivos_foto(fila[0])
    ejecutar("DELETE FROM fotos WHERE id = ?", (foto_id,))
    return redirect(url_for("categoria", cat_id=fila[0]["categoria_id"]))


@app.post("/categorias/<int:cat_id>/borrar")
def borrar_categoria(cat_id):
    categoria_o_404(cat_id)
    for foto in consulta("SELECT * FROM fotos WHERE categoria_id = ?", (cat_id,)):
        borrar_archivos_foto(foto)
    ejecutar("DELETE FROM fotos WHERE categoria_id = ?", (cat_id,))
    ejecutar("UPDATE publicaciones SET categoria_id = NULL WHERE categoria_id = ?", (cat_id,))
    ejecutar("DELETE FROM categorias WHERE id = ?", (cat_id,))
    flash("Categoría borrada.", "ok")
    return redirect(url_for("fotos"))


# --- Ajustes

@app.route("/ajustes", methods=["GET", "POST"])
def ajustes():
    if request.method == "POST":
        antes = (ajuste("racecore_url"), ajuste("racecore_token"))
        guardar_ajuste("racecore_url", request.form.get("racecore_url", "").strip())
        for clave in ("openai_key", "telegram_token", "racecore_token"):
            nuevo = request.form.get(clave, "").strip()
            if request.form.get(f"quitar_{clave}"):
                guardar_ajuste(clave, "")
            elif nuevo:
                guardar_ajuste(clave, nuevo)
        guardar_ajuste("telegram_chat", request.form.get("telegram_chat", "").strip())
        for red in REDES:
            guardar_ajuste(f"red_{red}", request.form.get(f"red_{red}", "").strip())
        calidad = request.form.get("openai_calidad", "medium")
        guardar_ajuste("openai_calidad", calidad if calidad in ("low", "medium", "high") else "medium")
        flash("Ajustes guardados.", "ok")
        if ajuste("racecore_url") and (ajuste("racecore_url"), ajuste("racecore_token")) != antes:
            leer_ahora()  # se prueba la conexión nueva enseguida
        return redirect(url_for("ajustes"))

    def oculta(clave):
        v = ajuste(clave)
        return f"configurada (…{v[-4:]})" if v else ""

    ultima = ESTADO["ultima_comprobacion"]
    return render_template(
        "ajustes.html", openai_key=oculta("openai_key"), telegram_token=oculta("telegram_token"),
        racecore_url=ajuste("racecore_url"), racecore_token=oculta("racecore_token"), racecore=estado_racecore(),
        telegram_chat=ajuste("telegram_chat"), calidad=ajuste("openai_calidad", "medium"),
        ultima=ultima.strftime("%H:%M:%S") if ultima else "", carpeta=BASE,
        marcas_v={t: int(r.stat().st_mtime) if r.exists() else 0 for t, (r, _) in MARCAS.items()},
        redes={r: ajuste(f"red_{r}") for r in REDES},
        fuentes=[n for n in ("Titulo.ttf", "Texto.ttf") if (DIR_FUENTES / n).exists()])


MARCAS = {"logo": (LOGO, "Logo"), "banner": (BANNER, "Banner de redes")}


def quitar_fondo_negro(im):
    """Si la imagen no tiene transparencia y su fondo es negro, el negro pasa a ser transparente."""
    if im.getchannel("A").getextrema()[0] < 250:
        return im, False
    rgb = im.convert("RGB")
    esquinas = [rgb.getpixel((x, y)) for x in (0, rgb.width - 1) for y in (0, rgb.height - 1)]
    if max(max(c) for c in esquinas) > 40:
        return im, False
    r, g, b = rgb.split()
    brillo = ImageChops.lighter(ImageChops.lighter(r, g), b)
    rgb.putalpha(brillo.point(lambda v: 0 if v < 25 else min(255, (v - 25) * 3)))
    return rgb, True


@app.route("/marca/<tipo>.png")
def ver_marca(tipo):
    if tipo not in MARCAS or not MARCAS[tipo][0].exists():
        abort(404)
    return send_from_directory(BASE, MARCAS[tipo][0].name, max_age=0)


@app.post("/ajustes/marca/<tipo>")
def subir_marca(tipo):
    if tipo not in MARCAS:
        abort(404)
    destino, nombre = MARCAS[tipo]
    if request.form.get("quitar"):
        destino.unlink(missing_ok=True)
        flash(f"{nombre}: quitado.", "ok")
        return redirect(url_for("ajustes"))
    try:
        with Image.open(request.files["archivo"].stream) as im:
            im = ImageOps.exif_transpose(im).convert("RGBA")
        im, sin_negro = quitar_fondo_negro(im)
        transparente = im.getchannel("A").getextrema()[0] < 250
        caja = im.getchannel("A").getbbox()  # recorta los bordes vacíos
        if caja:
            im = im.crop(caja)
        im.thumbnail((2000, 2000), Image.LANCZOS)
        im.save(destino, "PNG")
    except Exception:
        flash("Ese archivo no es una imagen válida.", "error")
        return redirect(url_for("ajustes"))
    if sin_negro:
        flash(f"{nombre}: guardado (le he quitado el fondo negro).", "ok")
    elif transparente:
        flash(f"{nombre}: guardado.", "ok")
    else:
        flash(f"{nombre}: guardado, pero no tiene fondo transparente y se verá como un recuadro. "
              "Mejor un PNG sin fondo.", "error")
    return redirect(url_for("ajustes"))


@app.post("/ajustes/telegram")
def probar_telegram():
    token, chat = ajuste("telegram_token"), ajuste("telegram_chat")
    if not (token and chat):
        flash("Falta el token o el chat de Telegram.", "error")
        return redirect(url_for("ajustes"))
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      data={"chat_id": chat, "text": "✅ Panel de redes de Racecore conectado"}).raise_for_status()
        flash("Mensaje de prueba enviado a Telegram.", "ok")
    except Exception as e:
        flash(f"Telegram ha fallado: {str(e).replace(token, '***')}", "error")
    return redirect(url_for("ajustes"))


# ---------------------------------------------------------------- plantillas

PLANTILLAS = {}

PLANTILLAS["base.html"] = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
{% block cabecera %}{% endblock %}
<title>Redes Racecore</title>
<style>
:root { --fondo:#111214; --caja:#1b1c20; --borde:#2c2e34; --texto:#eceff4; --suave:#9aa0ab; --acento:#e10600; --ok:#2e9d5b; }
* { box-sizing:border-box; }
[hidden] { display:none !important; }
body { margin:0; background:var(--fondo); color:var(--texto); font:15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color:inherit; }
nav { display:flex; gap:4px; align-items:center; padding:10px 16px; background:#000; position:sticky; top:0; z-index:5; overflow-x:auto; }
nav b { margin-right:12px; white-space:nowrap; } nav b span { color:var(--acento); }
nav a { text-decoration:none; padding:7px 12px; border-radius:8px; color:var(--suave); white-space:nowrap; }
nav a.activo, nav a:hover { background:var(--caja); color:var(--texto); }
main { max-width:1100px; margin:0 auto; padding:20px 16px 60px; }
h1 { font-size:22px; margin:4px 0 16px; } h2 { font-size:17px; margin:28px 0 10px; color:var(--suave); }
.caja { background:var(--caja); border:1px solid var(--borde); border-radius:12px; padding:16px; margin-bottom:14px; }
.fila { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.boton, button { display:inline-block; border:1px solid var(--borde); background:#26282e; color:var(--texto); padding:8px 14px; border-radius:8px; font:inherit; cursor:pointer; text-decoration:none; }
.boton:hover, button:hover { border-color:#555; }
.principal { background:var(--acento); border-color:var(--acento); color:#fff; font-weight:600; }
.peligro { color:#ff8a80; }
form.enlinea { display:inline; }
input[type=text], input[type=password], input[type=number], input[type=date], input[type=time], select, textarea {
  width:100%; background:#0d0e10; color:var(--texto); border:1px solid var(--borde); border-radius:8px; padding:9px 10px; font:inherit; }
textarea { min-height:90px; resize:vertical; }
label { display:block; margin:14px 0 5px; color:var(--suave); font-size:13px; }
.ayuda { color:var(--suave); font-size:13px; margin-top:4px; }
.chip { display:inline-block; font-size:12px; padding:2px 8px; border-radius:99px; background:#2c2e34; color:var(--suave); margin-left:4px; }
.chip.ia { background:#3b2a6b; color:#d7c8ff; } .chip.on { background:#193d29; color:#8fe0b0; } .chip.prueba { background:#4a3b12; color:#ffd98a; }
.chip.error { background:#4a1717; color:#ff8a80; }
.mensaje { padding:10px 14px; border-radius:8px; margin-bottom:12px; background:#193d29; }
.mensaje.error { background:#4a1717; }
.aviso { background:#3a2f12; color:#ffd98a; border-radius:8px; padding:8px 10px; font-size:13px; margin:8px 0; }
.rejilla { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:10px; }
.rejilla img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:8px; display:block; }
.vacio { color:var(--suave); padding:30px; text-align:center; border:1px dashed var(--borde); border-radius:12px; }
</style></head><body>
<nav><b>RACE<span>CORE</span> · Redes</b>
  <a href="{{ url_for('listas') }}" class="{{ 'activo' if request.endpoint == 'listas' }}">Listas para publicar</a>
  <a href="{{ url_for('publicaciones') }}" class="{{ 'activo' if 'publicacion' in request.endpoint }}">Publicaciones</a>
  <a href="{{ url_for('eventos') }}" class="{{ 'activo' if 'evento' in request.endpoint }}">Eventos</a>
  <a href="{{ url_for('fotos') }}" class="{{ 'activo' if request.endpoint in ('fotos', 'categoria') }}">Fotos</a>
  <a href="{{ url_for('ajustes') }}" class="{{ 'activo' if request.endpoint == 'ajustes' }}">Ajustes</a>
</nav>
<main>
{% for tipo, texto in get_flashed_messages(with_categories=true) %}<div class="mensaje {{ tipo }}">{{ texto }}</div>{% endfor %}
{% block contenido %}{% endblock %}
</main></body></html>"""

PLANTILLAS["listas.html"] = """{% extends "base.html" %}
{% block cabecera %}{% if generando %}<meta http-equiv="refresh" content="5">{% endif %}{% endblock %}
{% block contenido %}
<h1>Listas para publicar</h1>
<style>
.tarjeta { display:grid; grid-template-columns:minmax(0, 320px) 1fr; gap:16px; }
.tarjeta img { width:100%; border-radius:8px; display:block; }
.hueco { aspect-ratio:1; border-radius:8px; background:#0d0e10; display:flex; align-items:center; justify-content:center; color:var(--suave); }
.tarjeta textarea { min-height:120px; margin:8px 0; }
@media (max-width:640px) { .tarjeta { grid-template-columns:1fr; } }
</style>
{% for g in filas %}
<div class="caja tarjeta">
  <div>
    {% if g.estado == 'lista' %}<a href="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" target="_blank"><img loading="lazy" src="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" alt=""></a>
    {% elif g.estado == 'generando' %}<div class="hueco">Generando…</div>
    {% else %}<div class="hueco">No se pudo generar</div>{% endif %}
  </div>
  <div>
    <b>{{ g.nombre }}</b>{% if g.evento %} · {{ g.evento }}{% endif %}
    {% if g.prueba %}<span class="chip prueba">Prueba</span>{% endif %}
    {% if g.con_ia %}<span class="chip ia">IA{{ ' · ' ~ g.estilo_nombre if g.estilo_nombre }}</span>{% endif %}
    <div class="ayuda">Publicar: {{ g.cuando }}</div>
    {% if g.aviso %}<div class="aviso">{{ g.aviso }}</div>{% endif %}
    {% if g.estado == 'lista' %}
    <textarea readonly id="texto{{ g.id }}">{{ g.texto }}</textarea>
    <div class="fila">
      <a class="boton principal" href="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo, descargar=1) }}">Descargar imagen</a>
      <button type="button" onclick="copiar({{ g.id }}, this)">Copiar texto</button>
      <button type="button" class="compartir" hidden onclick="compartir('{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}', {{ g.id }})">Compartir</button>
    </div>
    {% endif %}
    <div class="fila" style="margin-top:10px">
      {% if g.estado == 'lista' %}<form class="enlinea" method="post" action="{{ url_for('marcar_publicada', gen_id=g.id) }}"><button>✔ Ya publicada</button></form>{% endif %}
      {% if g.estado != 'generando' %}<form class="enlinea" method="post" action="{{ url_for('otra_foto', gen_id=g.id) }}"><button>Otra foto</button></form>{% endif %}
      <form class="enlinea" method="post" action="{{ url_for('borrar_generada', gen_id=g.id) }}" onsubmit="return confirm('¿Borrar esta imagen?')"><button class="peligro">Borrar</button></form>
    </div>
  </div>
</div>
{% else %}
<div class="vacio">No hay nada pendiente.<br>Las imágenes aparecen aquí solas antes de su hora, o con «Generar ahora» en Publicaciones.</div>
{% endfor %}

{% if hechas %}
<h2>Publicadas hace poco</h2>
<div class="rejilla">
{% for g in hechas %}<a href="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" target="_blank" title="{{ g.nombre }}{{ ' · ' ~ g.evento if g.evento }} · {{ g.cuando }}"><img loading="lazy" src="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" alt=""></a>{% endfor %}
</div>
{% endif %}

<script>
async function copiar(id, boton) {
  const t = document.getElementById('texto' + id);
  try { await navigator.clipboard.writeText(t.value); }
  catch (e) { t.select(); document.execCommand('copy'); }
  boton.textContent = '¡Copiado!';
  setTimeout(() => boton.textContent = 'Copiar texto', 1500);
}
async function compartir(url, id) {
  const texto = document.getElementById('texto' + id).value;
  try {
    const blob = await (await fetch(url)).blob();
    try { await navigator.clipboard.writeText(texto); } catch (e) {}
    await navigator.share({ files: [new File([blob], 'racecore.jpg', { type: blob.type })], text: texto });
  } catch (e) {
    if (e.name !== 'AbortError') alert('No se pudo compartir. Descarga la imagen y compártela desde la galería.');
  }
}
if (navigator.canShare && navigator.canShare({ files: [new File([''], 'x.jpg', { type: 'image/jpeg' })] }))
  document.querySelectorAll('.compartir').forEach(b => b.hidden = false);
</script>
{% endblock %}"""

PLANTILLAS["publicaciones.html"] = """{% extends "base.html" %}
{% block contenido %}
<div class="fila" style="justify-content:space-between"><h1>Publicaciones</h1>
<a class="boton principal" href="{{ url_for('editar_publicacion') }}">+ Nueva publicación</a></div>
{% for p in filas %}
<div class="caja">
  <div class="fila" style="justify-content:space-between">
    <div>
      <b>{{ p.nombre }}</b>
      {% if p.activa %}<span class="chip on">Activa</span>{% else %}<span class="chip">Pausada</span>{% endif %}
      {% if p.modo == 'evento' %}<span class="chip">Eventos</span>{% endif %}
      {% if p.prompt_ia %}<span class="chip ia">IA</span>{% endif %}
      <div class="ayuda">{{ p.programacion }} · {{ p.formato_txt }} · fotos: {{ p.categoria }}</div>
      {% if p.proxima %}<div class="ayuda">Próxima: <b>{{ p.proxima }}</b> (la imagen se prepara el {{ p.genera }})</div>
      {% elif p.activa %}<div class="ayuda">{{ 'Sin eventos próximos.' if p.modo == 'evento' else 'Sin próximas fechas.' }}</div>{% endif %}
    </div>
    <div class="fila">
      <a class="boton" href="{{ url_for('editar_publicacion', pub_id=p.id) }}">Editar</a>
      <form class="enlinea" method="post" action="{{ url_for('generar_ahora', pub_id=p.id) }}"><button>Generar ahora</button></form>
      <form class="enlinea" method="post" action="{{ url_for('activar_publicacion', pub_id=p.id) }}"><button>{{ 'Pausar' if p.activa else 'Activar' }}</button></form>
      <form class="enlinea" method="post" action="{{ url_for('borrar_publicacion', pub_id=p.id) }}" onsubmit="return confirm('¿Borrar la publicación «{{ p.nombre }}»?')"><button class="peligro">Borrar</button></form>
    </div>
  </div>
</div>
{% else %}
<div class="vacio">Aún no hay publicaciones. Crea la primera con «+ Nueva publicación».</div>
{% endfor %}
{% endblock %}"""

PLANTILLAS["publicacion.html"] = """{% extends "base.html" %}
{% block contenido %}
<h1>{{ 'Editar publicación' if pub.id else 'Nueva publicación' }}</h1>
<style>
.dos { display:grid; grid-template-columns:1fr 1fr; gap:0 16px; }
.tres { display:grid; grid-template-columns:1fr 1fr 1fr; gap:0 16px; }
@media (max-width:640px) { .dos { grid-template-columns:1fr; } }
.dias label { display:inline-flex; gap:4px; align-items:center; margin:6px 10px 0 0; color:var(--texto); font-size:15px; }
.opciones label { display:inline-flex; gap:6px; align-items:center; margin:6px 16px 0 0; color:var(--texto); font-size:15px; }
input[type=color] { width:60px; height:40px; border:none; background:none; padding:0; }
</style>
<form method="post">
<div class="caja">
  <label>Nombre (solo para ti)</label>
  <input type="text" name="nombre" value="{{ pub.nombre }}" placeholder="Ej: Tanda de los viernes" required>
  <div class="opciones"><label><input type="checkbox" name="activa" value="1" {{ 'checked' if pub.activa }}> Activa</label></div>
</div>

<div class="caja">
  <b>¿Cuándo?</b>
  <div class="opciones">
    <label><input type="radio" name="modo" value="semanal" {{ 'checked' if pub.modo != 'fecha' }}> Días de la semana</label>
    <label><input type="radio" name="modo" value="fecha" {{ 'checked' if pub.modo == 'fecha' }}> Fecha concreta</label>
    <label><input type="radio" name="modo" value="evento" {{ 'checked' if pub.modo == 'evento' }}> Eventos</label>
  </div>
  <div id="semanal" class="dias">
    {% for d in dias %}<label><input type="checkbox" name="dias" value="{{ loop.index0 }}" {{ 'checked' if loop.index0|string in dias_marcados }}> {{ d }}</label>{% endfor %}
  </div>
  <div id="fecha"><label>Fecha</label><input type="date" name="fecha" value="{{ pub.fecha }}"></div>
  <div id="evento">
    <div class="ayuda">Se publica sola para cada evento de la pestaña Eventos (los de Racecore y los creados a mano).</div>
    <div class="dos">
      <div><label>¿Cuándo, respecto al evento?</label>
        <select name="momento">{% for clave, t in momentos.items() %}<option value="{{ clave }}" {{ 'selected' if clave == pub.momento }}>{{ t }}</option>{% endfor %}</select></div>
      <div><label>Condición</label>
        <select name="condicion">{% for clave, t in condiciones.items() %}<option value="{{ clave }}" {{ 'selected' if clave == pub.condicion }}>{{ t }}</option>{% endfor %}</select></div>
    </div>
    <div id="rango">
      <div class="tres">
        <div><label>Desde (días)</label><input type="number" name="dias_desde" min="0" max="365" value="{{ pub.dias_desde }}"></div>
        <div><label>Hasta (días)</label><input type="number" name="dias_hasta" min="0" max="365" value="{{ pub.dias_hasta }}"></div>
        <div><label>Cada (días)</label><input type="number" name="cada" min="1" max="60" value="{{ pub.cada }}"></div>
      </div>
      <div class="ayuda">Ej.: antes, de 14 a 3, cada 2 → se publica a 14, 12, 10, 8, 6 y 4 días del evento. Para un solo día pon el mismo número: de 10 a 10. Al día siguiente: después, de 1 a 1.</div>
    </div>
    <label>Solo los eventos que contengan (opcional)</label>
    <input type="text" name="filtro" value="{{ pub.filtro }}" placeholder="Ej: Resistencia. Vacío = todos los eventos">
  </div>
  <div class="dos">
    <div><label>Hora de publicación</label><input type="time" name="hora" value="{{ pub.hora }}" required></div>
    <div><label>Preparar la imagen con antelación (minutos)</label><input type="number" name="antelacion" min="0" max="10080" value="{{ pub.antelacion }}"></div>
  </div>
  <div class="ayuda">1440 = un día antes. La imagen se prepara en cuanto el PC esté encendido dentro de ese margen.</div>
</div>

<div class="caja">
  <b>Imagen</b>
  <div class="dos">
    <div><label>Categoría de fotos</label>
      <select name="categoria_id"><option value="">— elige —</option>
      {% for c in categorias %}<option value="{{ c.id }}" {{ 'selected' if c.id == pub.categoria_id }}>{{ c.nombre }}</option>{% endfor %}
      </select></div>
    <div><label>Formato</label>
      <select name="formato">{% for clave, f in formatos.items() %}<option value="{{ clave }}" {{ 'selected' if clave == pub.formato }}>{{ f[0] }}</option>{% endfor %}</select></div>
  </div>
  <div class="dos">
    <div><label>Diseño</label>
      <select name="diseno">
        <option value="cartel" {{ 'selected' if pub.diseno not in ('sencillo', 'ia') }}>Cartel (título grande, franja de color, datos y redes)</option>
        <option value="sencillo" {{ 'selected' if pub.diseno == 'sencillo' }}>Sencillo (texto abajo)</option>
        {% if hay_ia or pub.diseno == 'ia' %}<option value="ia" {{ 'selected' if pub.diseno == 'ia' }}>IA completa (la IA pone los textos; logo y redes, el panel)</option>{% endif %}
      </select></div>
    <div><label>Color de acento</label><input type="color" name="color" value="{{ pub.color }}"></div>
  </div>
  <label>Título</label>
  <textarea name="titulo" rows="2" style="min-height:0" placeholder="Ej:&#10;{dia}&#10;DE *RECORD !!*">{{ pub.titulo }}</textarea>
  <div class="ayuda">Enter = nueva línea. Lo que pongas *entre asteriscos* sale en el color de acento.</div>
  <label>Franja de color / subtítulo</label>
  <textarea name="subtitulo" rows="2" style="min-height:0" placeholder="Ej:&#10;El mejor tiempo&#10;no paga">{{ pub.subtitulo }}</textarea>
  <div class="ayuda">En «Cartel» va dentro de la franja de color y la última línea sale más grande.</div>
  <label>Datos / pie</label>
  <input type="text" name="pie" value="{{ pub.pie }}" placeholder="Ej: Grupo mínimo 6 corredores | Reserva necesaria">
  <div class="ayuda">En «Cartel», separa cada dato con | (salen con una barra amarilla). En «Sencillo» es la etiqueta de color.</div>
  {% if hay_ia %}
  <label>Estilo (solo con «IA completa»)</label>
  <select name="estilo_ia">
    <option value="variado" {{ 'selected' if pub.estilo_ia not in estilos }}>Variado: cambia cada vez</option>
    {% for clave, e in estilos.items() %}<option value="{{ clave }}" {{ 'selected' if pub.estilo_ia == clave }}>Siempre {{ e[0] }}</option>{% endfor %}
  </select>
  <label>Prompt IA (opcional)</label>
  <textarea name="prompt_ia" placeholder="Ej: atardecer dorado, luz cálida, ambiente de competición">{{ pub.prompt_ia }}</textarea>
  <div class="ayuda">Con «Cartel» o «Sencillo», si lo rellenas la IA rehace solo el fondo y el texto lo pone el panel.</div>
  <div class="ayuda">Con «IA completa», la IA hace el cartel con tus textos y aquí puedes añadir ambiente o detalles. «Otra foto» cambia también de estilo. Revisa los textos antes de publicar y pon calidad Alta en Ajustes.</div>
  {% else %}<input type="hidden" name="prompt_ia" value="{{ pub.prompt_ia }}">
  <input type="hidden" name="estilo_ia" value="{{ pub.estilo_ia }}">{% endif %}
  <div class="ayuda">En la imagen no uses emojis (salen como cuadrados). En el texto del post sí.</div>
</div>

<div class="caja">
  <b>Texto del post</b>
  <textarea name="texto" style="min-height:140px" placeholder="El texto que pegarás en Instagram/Facebook">{{ pub.texto }}</textarea>
  <div class="ayuda" id="vars_normal">En título, subtítulo, pie y texto puedes usar {dia} (sábado), {fecha} (27 de septiembre) y {hora} (18:00).</div>
  <div class="ayuda"><b>Varias versiones:</b> escríbelas en la misma casilla separadas por una línea con <b>---</b> (tres guiones) y el panel las va turnando: la 1ª, luego la 2ª, la 3ª… Vale en Título, Franja y Texto del post, y van juntas: la 2ª versión del título sale con la 2ª del texto. «Generar ahora» pasa cada vez a la siguiente, para que las veas todas.</div>
  <div class="ayuda" id="vars_evento">En título, subtítulo, pie y texto puedes usar los datos del evento:
    {evento}, {campeonato}, {dia} {fecha} y {hora} (del evento), {dias} (los que faltan), {faltan} («en 5 días», «mañana», «hoy»),
    {inscritos}, {plazas}, {libres}, {precio}, {enlace} (inscripción), {web}, {horarios}, {resultados} y {ganadores} (para el pie del Cartel).</div>
</div>

<div class="fila">
  <button class="principal">Guardar</button>
  <button name="y_generar" value="1">Guardar y generar prueba</button>
  <a class="boton" href="{{ url_for('publicaciones') }}">Cancelar</a>
</div>
</form>
<script>
// (no se puede llamar «modo»: dentro del formulario ese nombre es el de los botones de radio)
function mostrarModo() {
  const m = document.querySelector('input[name=modo]:checked').value;
  document.getElementById('fecha').hidden = m !== 'fecha';
  document.getElementById('semanal').hidden = m !== 'semanal';
  document.getElementById('evento').hidden = m !== 'evento';
  document.getElementById('vars_normal').hidden = m === 'evento';
  document.getElementById('vars_evento').hidden = m !== 'evento';
  document.getElementById('rango').hidden = document.querySelector('select[name=momento]').value === 'dia';
}
document.querySelectorAll('input[name=modo], select[name=momento]').forEach(r => r.addEventListener('change', mostrarModo));
mostrarModo();
</script>
{% endblock %}"""

PLANTILLAS["fotos.html"] = """{% extends "base.html" %}
{% block contenido %}
<h1>Banco de fotos</h1>
<form class="caja fila" method="post" action="{{ url_for('crear_categoria') }}">
  <input type="text" name="nombre" placeholder="Nueva categoría (ej: Karts en pista, Grupos, Noche)" style="flex:1; min-width:200px" required>
  <button class="principal">Crear categoría</button>
</form>
<div class="rejilla">
{% for c in cats %}
<a href="{{ url_for('categoria', cat_id=c.id) }}" style="text-decoration:none">
  {% if c.portada %}<img loading="lazy" src="{{ url_for('archivo', carpeta='miniaturas', nombre=c.portada.rsplit('.', 1)[0] ~ '.jpg') }}" alt="">
  {% else %}<div class="vacio" style="aspect-ratio:1; padding:0; display:flex; align-items:center; justify-content:center">vacía</div>{% endif %}
  <div style="margin-top:6px"><b>{{ c.nombre }}</b> <span class="ayuda">{{ c.total }} foto(s)</span></div>
</a>
{% else %}
<div class="vacio" style="grid-column:1/-1">Crea una categoría para empezar a subir fotos.</div>
{% endfor %}
</div>
{% endblock %}"""

PLANTILLAS["categoria.html"] = """{% extends "base.html" %}
{% block contenido %}
<p><a href="{{ url_for('fotos') }}">← Todas las categorías</a></p>
<h1>{{ cat.nombre }} <span class="ayuda">{{ fotos|length }} foto(s)</span></h1>
{% if subidas %}<div class="mensaje">{{ subidas }} foto(s) subida(s).</div>{% endif %}
{% if malas %}<div class="mensaje error">{{ malas }} archivo(s) no se pudieron subir: usa fotos JPG, PNG o WEBP.</div>{% endif %}
<form class="caja" id="subida" method="post" enctype="multipart/form-data" action="{{ url_for('subir_fotos', cat_id=cat.id) }}">
  <b>Subir fotos</b>
  <div class="ayuda">Puedes elegir muchas a la vez: se suben de una en una.</div>
  <div class="fila" style="margin-top:10px">
    <input type="file" name="fotos" accept="image/jpeg,image/png,image/webp" multiple required>
    <button class="principal">Subir</button>
  </div>
  <div id="progreso" class="aviso" hidden></div>
</form>
<script>
document.getElementById('subida').addEventListener('submit', async (ev) => {
  const form = ev.target;
  const archivos = [...form.querySelector('input[type=file]').files];
  if (!archivos.length || !window.fetch) return;
  ev.preventDefault();
  const progreso = document.getElementById('progreso');
  form.querySelector('button').disabled = true;
  progreso.hidden = false;
  window.onbeforeunload = () => true;
  let hechas = 0, subidas = 0, malas = 0;
  const pintar = () => progreso.textContent = `Subiendo ${hechas} de ${archivos.length}… no cierres esta página.`
    + (malas ? ` (${malas} con error)` : '');
  pintar();
  const cola = archivos.slice();
  async function subir() {
    while (cola.length) {
      const datos = new FormData();
      datos.append('fotos', cola.shift());
      try {
        const r = await fetch(form.action, { method: 'POST', body: datos, headers: { 'X-Subida': '1' } });
        const j = await r.json();
        subidas += j.subidas; malas += j.malas;
      } catch (e) { malas++; }
      hechas++; pintar();
    }
  }
  await Promise.all([subir(), subir(), subir()]);
  window.onbeforeunload = null;
  location.href = location.pathname + '?subidas=' + subidas + '&malas=' + malas;
});
</script>
<div class="rejilla">
{% for f in fotos %}
<div>
  <a href="{{ url_for('archivo', carpeta='fotos', nombre=f.archivo) }}" target="_blank"><img loading="lazy" src="{{ url_for('archivo', carpeta='miniaturas', nombre=f.archivo.rsplit('.', 1)[0] ~ '.jpg') }}" alt=""></a>
  <form method="post" action="{{ url_for('borrar_foto', foto_id=f.id) }}" onsubmit="return confirm('¿Borrar esta foto?')" style="margin-top:4px"><button class="peligro" style="width:100%">Borrar</button></form>
</div>
{% else %}
<div class="vacio" style="grid-column:1/-1">Todavía no hay fotos en esta categoría.</div>
{% endfor %}
</div>
<form method="post" action="{{ url_for('borrar_categoria', cat_id=cat.id) }}" onsubmit="return confirm('¿Borrar la categoría «{{ cat.nombre }}» y TODAS sus fotos?')" style="margin-top:30px">
  <button class="peligro">Borrar categoría</button>
</form>
{% endblock %}"""

PLANTILLAS["ajustes.html"] = """{% extends "base.html" %}
{% block contenido %}
<h1>Ajustes</h1>
{% for tipo, titulo, ayuda in [
  ('logo', 'Logo', 'Sale arriba a la izquierda en todas las imágenes. Mejor PNG sin fondo; si tiene fondo negro, se lo quito.'),
  ('banner', 'Banner de redes', 'Sale abajo en «Cartel» e «IA completa». Si lo subes, se usa en vez de los nombres de redes de más abajo. Si tiene fondo negro, se lo quito y recorto lo que sobra.')] %}
<form class="caja" method="post" enctype="multipart/form-data" action="{{ url_for('subir_marca', tipo=tipo) }}">
  <b>{{ titulo }}</b>
  <div class="ayuda">{{ ayuda }}</div>
  {% if marcas_v[tipo] %}<div style="background:#555; display:inline-block; padding:10px; border-radius:8px; margin:10px 0; max-width:100%">
    <img src="{{ url_for('ver_marca', tipo=tipo, v=marcas_v[tipo]) }}" alt="" style="max-height:90px; max-width:100%; display:block"></div>{% endif %}
  <div class="fila" style="margin-top:8px">
    <input type="file" name="archivo" accept="image/png,image/jpeg,image/webp">
    <button class="principal">Subir</button>
    {% if marcas_v[tipo] %}<button name="quitar" value="1" class="peligro" formnovalidate>Quitar</button>{% endif %}
  </div>
</form>
{% endfor %}
<form method="post">
<div class="caja">
  <b>Redes (si no has subido banner de redes)</b>
  <div class="ayuda">Se dibujan abajo con sus iconos. Deja vacías las que no quieras que salgan.</div>
  <div class="dos">
    <div><label>Facebook</label><input type="text" name="red_facebook" value="{{ redes.facebook }}" placeholder="kartingcastroponce"></div>
    <div><label>Instagram</label><input type="text" name="red_instagram" value="{{ redes.instagram }}" placeholder="karting_castroponce"></div>
    <div><label>TikTok</label><input type="text" name="red_tiktok" value="{{ redes.tiktok }}" placeholder="karting_castroponce"></div>
    <div><label>Web</label><input type="text" name="red_web" value="{{ redes.web }}" placeholder="kartingcastroponce.com"></div>
  </div>
</div>
<div class="caja">
  <b>Racecore (para la pestaña Eventos)</b>
  <div class="ayuda">El panel lee los eventos de Racecore por la red del circuito: solo lectura y sin datos personales.</div>
  <label>Dirección del PC de Racecore</label>
  <input type="text" name="racecore_url" value="{{ racecore_url }}" placeholder="Ej: 192.168.1.50  (puerto 8100 si no pones otro)">
  <label>Token de lectura {% if racecore_token %}<span class="chip on">{{ racecore_token }}</span>{% endif %}</label>
  <input type="password" name="racecore_token" placeholder="{{ 'Déjalo vacío para no cambiarlo' if racecore_token else 'El que genera Racecore en sus Ajustes' }}" autocomplete="off">
  {% if racecore_token %}<label style="color:var(--texto)"><input type="checkbox" name="quitar_racecore_token" value="1"> Quitar el token</label>{% endif %}
</div>
<div class="caja">
  <b>IA de OpenAI (opcional, de pago aparte)</b>
  <div class="ayuda">Sin clave, el panel usa tus fotos tal cual. Con clave aparece el campo «Prompt IA» en las publicaciones.</div>
  <label>Clave de la API {% if openai_key %}<span class="chip on">{{ openai_key }}</span>{% endif %}</label>
  <input type="password" name="openai_key" placeholder="{{ 'Déjalo vacío para no cambiarla' if openai_key else 'sk-...' }}" autocomplete="off">
  {% if openai_key %}<label style="color:var(--texto)"><input type="checkbox" name="quitar_openai_key" value="1"> Quitar la clave</label>{% endif %}
  <label>Calidad de imagen</label>
  <select name="openai_calidad">
    {% for v, t in [('low', 'Baja (más barata y rápida)'), ('medium', 'Media'), ('high', 'Alta (más cara y lenta)')] %}
    <option value="{{ v }}" {{ 'selected' if v == calidad }}>{{ t }}</option>{% endfor %}
  </select>
</div>
<div class="caja">
  <b>Telegram (opcional)</b>
  <div class="ayuda">Si lo configuras, cada imagen programada te llega al móvil con su texto.</div>
  <label>Token del bot {% if telegram_token %}<span class="chip on">{{ telegram_token }}</span>{% endif %}</label>
  <input type="password" name="telegram_token" placeholder="{{ 'Déjalo vacío para no cambiarlo' if telegram_token else '123456:ABC...' }}" autocomplete="off">
  {% if telegram_token %}<label style="color:var(--texto)"><input type="checkbox" name="quitar_telegram_token" value="1"> Quitar el token</label>{% endif %}
  <label>Chat ID</label>
  <input type="text" name="telegram_chat" value="{{ telegram_chat }}">
</div>
<button class="principal">Guardar ajustes</button>
</form>
<div class="fila" style="margin-top:10px">
  <form class="enlinea" method="post" action="{{ url_for('probar_telegram') }}"><button>Probar Telegram</button></form>
  <form class="enlinea" method="post" action="{{ url_for('leer_eventos') }}"><input type="hidden" name="volver" value="ajustes"><button>Probar Racecore</button></form>
</div>

<h2>Estado</h2>
<div class="caja">
  <div>Programador: {% if ultima %}<span class="chip on">funcionando</span> última comprobación {{ ultima }}{% else %}<span class="chip">arrancando…</span>{% endif %}</div>
  <div>Racecore: {% if not racecore.configurado %}<span class="chip">sin configurar</span>
    {% elif racecore.error %}<span class="chip error">sin conexión</span> <span class="ayuda">{{ racecore.error }}</span>
    {% elif racecore.ok %}<span class="chip on">conectado</span> última lectura {{ racecore.ok }} · {{ racecore.n }} evento(s)
    {% else %}<span class="chip">pendiente</span>{% endif %}</div>
  <div>Fuentes propias: {% if fuentes %}<span class="chip on">{{ fuentes|join(', ') }}</span>{% else %}<span class="chip">las incluidas</span> <span class="ayuda">opcional: Titulo.ttf y Texto.ttf en la carpeta «fuentes»</span>{% endif %}</div>
  <div class="ayuda" style="margin-top:8px">Carpeta del panel: {{ carpeta }}</div>
</div>
{% endblock %}"""

PLANTILLAS["eventos.html"] = """{% extends "base.html" %}
{% block contenido %}
<div class="fila" style="justify-content:space-between"><h1>Eventos</h1>
<div class="fila">
  <form class="enlinea" method="post" action="{{ url_for('leer_eventos') }}"><button>Actualizar desde Racecore</button></form>
  <a class="boton" href="{{ url_for('editar_evento') }}">+ Evento a mano</a>
</div></div>
<div class="caja">
  <b>Racecore</b>
  {% if not racecore.configurado %}<span class="chip">sin configurar</span>
  <div class="ayuda">Pon la dirección y el token de Racecore en <a href="{{ url_for('ajustes') }}">Ajustes</a>. Mientras tanto puedes crear eventos a mano.</div>
  {% elif racecore.error %}<span class="chip error">sin conexión</span>
  <div class="aviso">{{ racecore.error }}{% if racecore.ok %} Se usan los datos leídos el {{ racecore.ok }}.{% endif %}</div>
  {% elif racecore.ok %}<span class="chip on">conectado</span> <span class="ayuda">última lectura {{ racecore.ok }} · {{ racecore.n }} evento(s)</span>
  {% else %}<span class="chip">leyendo…</span>{% endif %}
  <div class="ayuda">El panel lee Racecore cada 10 minutos. Solo guarda lo necesario para las publicaciones: nombre, fecha, horarios, plazas, número de inscritos, precio, enlaces y el podio.</div>
</div>

{% if not hay_reglas %}
<div class="caja">
  <b>Aún no hay publicaciones para eventos</b>
  <div class="ayuda">Son publicaciones normales con el modo «Eventos»: se preparan solas antes o después de cada evento, con sus datos.</div>
  <div class="ayuda">Las de ejemplo: recordatorio cada 2 días (de 14 a 3 días antes), llamada a la acción (10 días antes), horarios (2 días antes), inscritos (1 día antes) y resultados (al día siguiente).</div>
  <div class="fila" style="margin-top:10px">
    <form class="enlinea" method="post" action="{{ url_for('crear_ejemplos') }}"><button class="principal">Crear las 5 de ejemplo (en pausa)</button></form>
    <a class="boton" href="{{ url_for('editar_publicacion', modo='evento') }}">Crear una desde cero</a>
  </div>
</div>
{% elif not hay_activas %}
<div class="aviso">Ninguna publicación de eventos está activa. Actívalas en <a href="{{ url_for('publicaciones') }}">Publicaciones</a>.</div>
{% endif %}

{% for ev in eventos %}
<div class="caja">
  <div class="fila" style="justify-content:space-between; align-items:flex-start">
    <div>
      <b>{{ ev.nombre }}</b>
      <span class="chip">{{ 'a mano' if ev.origen == 'manual' else 'Racecore' }}</span>
      {% if ev.pasado %}<span class="chip">ya pasó</span>{% endif %}
      {% if ev.cuando %}<div class="ayuda">{{ ev.cuando }}{% if ev.campeonato %} · {{ ev.campeonato }}{% endif %}</div>
      {% else %}<div class="aviso">{% if ev.fecha_txt %}No entiendo la fecha «{{ ev.fecha_txt }}»{% else %}No tiene fecha{% endif %}: para este evento no se programa nada.</div>{% endif %}
      <div class="ayuda">Inscritos: {{ ev.inscritos_txt }} · Precio: {{ ev.precio or '—' }} · Inscripción {{ 'abierta' if ev.abierta else 'cerrada' }}</div>
    </div>
    {% if ev.origen == 'manual' %}<div class="fila">
      <a class="boton" href="{{ url_for('editar_evento', ev_id=ev.id) }}">Editar</a>
      <form class="enlinea" method="post" action="{{ url_for('borrar_evento', ev_id=ev.id) }}" onsubmit="return confirm('¿Borrar el evento «{{ ev.nombre }}»?')"><button class="peligro">Borrar</button></form>
    </div>{% endif %}
  </div>
  {% if ev.proximas %}
  <div class="ayuda" style="margin-top:8px">Se preparará:</div>
  <ul style="margin:4px 0 0; padding-left:20px">{% for cuando, nombre, nota in ev.proximas %}<li>{{ cuando }} · {{ nombre }}{% if nota %} <span class="ayuda">({{ nota }})</span>{% endif %}</li>{% endfor %}</ul>
  {% endif %}
  <details style="margin-top:8px"><summary class="ayuda" style="cursor:pointer">Ver datos</summary>
    <div class="ayuda" style="white-space:pre-line; margin-top:6px">{% if ev.enlace %}Inscripción: {{ ev.enlace }}
{% endif %}{% if ev.web %}Web: {{ ev.web }}
{% endif %}{% if ev.horarios %}
Horarios:
{{ ev.horarios }}
{% endif %}{% if ev.resultados %}
Resultados:
{{ ev.resultados }}
{% endif %}</div>
  </details>
</div>
{% else %}
<div class="vacio">No hay eventos.<br>Conecta Racecore en Ajustes o crea uno con «+ Evento a mano».</div>
{% endfor %}
{% endblock %}"""

PLANTILLAS["evento.html"] = """{% extends "base.html" %}
{% block contenido %}
<h1>{{ 'Editar evento' if ev.id else 'Nuevo evento a mano' }}</h1>
<style>
.dos { display:grid; grid-template-columns:1fr 1fr; gap:0 16px; }
@media (max-width:640px) { .dos { grid-template-columns:1fr; } }
</style>
<form method="post">
<div class="caja">
  <div class="dos">
    <div><label>Nombre</label><input type="text" name="nombre" value="{{ ev.nombre }}" placeholder="Ej: Carrera de Navidad" required></div>
    <div><label>Campeonato (opcional)</label><input type="text" name="campeonato" value="{{ ev.campeonato }}"></div>
    <div><label>Fecha</label><input type="date" name="fecha" value="{{ ev.fecha }}" required></div>
    <div><label>Hora de la primera tanda</label><input type="time" name="hora" value="{{ ev.hora }}"></div>
    <div><label>Plazas</label><input type="number" name="plazas" min="0" value="{{ ev.plazas if ev.plazas is not none }}"></div>
    <div><label>Inscritos</label><input type="number" name="inscritos" min="0" value="{{ ev.inscritos if ev.inscritos is not none }}"></div>
    <div><label>Precio</label><input type="text" name="precio" value="{{ ev.precio }}" placeholder="Ej: 60 €"></div>
    <div><label>&nbsp;</label><label style="color:var(--texto); margin:0"><input type="checkbox" name="abierta" value="1" {{ 'checked' if ev.abierta }}> Inscripción abierta</label></div>
  </div>
  <label>Enlace de inscripción</label><input type="text" name="enlace" value="{{ ev.enlace }}" placeholder="https://...">
  <label>Web del evento</label><input type="text" name="web" value="{{ ev.web }}" placeholder="https://...">
  <label>Horarios</label>
  <textarea name="horarios" placeholder="09:00 · Entrenos&#10;10:30 · Clasificación&#10;12:00 · Final">{{ ev.horarios }}</textarea>
  <label>Resultados</label>
  <textarea name="resultados" placeholder="Final&#10;Senior: 1º Nombre · 2º Nombre · 3º Nombre">{{ ev.resultados }}</textarea>
  <label>Ganadores (para el pie del Cartel)</label>
  <input type="text" name="ganadores" value="{{ ev.ganadores }}" placeholder="Senior: Nombre | Junior: Nombre">
  <div class="ayuda">Sin datos personales: solo lo que vaya a salir en redes.</div>
</div>
<div class="fila">
  <button class="principal">Guardar</button>
  <a class="boton" href="{{ url_for('eventos') }}">Cancelar</a>
</div>
</form>
{% endblock %}"""

app.jinja_loader = DictLoader(PLANTILLAS)


# ---------------------------------------------------------------- arranque

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    from waitress import create_server

    url = f"http://localhost:{PUERTO}"
    if "--abrir" in sys.argv:
        threading.Timer(2, webbrowser.open, (url,)).start()
    try:
        # Solo escucha en este PC: desde fuera se entra por Cloudflare Tunnel (con login)
        servidor = create_server(app, host="127.0.0.1", port=PUERTO, threads=8)
    except OSError:
        log.error("El puerto %s ya está en uso: el panel ya está en marcha. Ábrelo en %s", PUERTO, url)
        sys.exit(1)
    iniciar()
    threading.Thread(target=programador, daemon=True, name="programador").start()
    log.info("Panel en marcha: abre %s en el navegador", url)
    servidor.run()


if __name__ == "__main__":
    main()
