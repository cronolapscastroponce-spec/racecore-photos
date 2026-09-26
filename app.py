"""Panel de redes de Racecore.

Banco de fotos por categorías, publicaciones programadas y un programador que
deja la imagen (foto + texto superpuesto) y el texto del post listos a su hora.
Tú publicas a mano; el panel solo prepara.
Las publicaciones en modo «Eventos» se programan respecto a los eventos que el
panel lee de Racecore y de CKS por la red local (o que se crean a mano).

Arranque: doble clic en 2_probar.bat  (o: .venv\\Scripts\\python app.py --abrir)
Panel:    http://localhost:5000
"""
import io
import json
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
CADA_SINCRONIZAR = 600              # segundos entre lecturas de Racecore y CKS
# Programas de cronometraje de los que se leen los eventos (los dos dan el mismo JSON)
FUENTES = {
    "racecore": {"nombre": "Racecore", "ruta": "/api/publico/eventos", "puerto": 8100},
    "cks": {"nombre": "CKS", "ruta": "/api/publico/competiciones", "puerto": 8090},
}
HORAS_TANDAS = 168                  # fotos de tandas de CKS: se guardan las de la última semana
MAX_TANDAS_CKS = 40                 # CKS da como mucho las 40 tandas más recientes
LADO_MAX_TANDA = 2400               # (llegan a 1280-1920 px; así no se agrandan)
MOMENTOS = {"antes": "Antes del evento", "dia": "El mismo día", "despues": "Después del evento"}
# Plantilla de horario de apertura
ACTIVIDADES = ["Kart Rental", "Entrenos Motos", "Entrenos Karting"]
# Deportes en TV para la cafetería: calendarios y guía de TV de Marca (horas de España)
MARCA = {
    "f1": ("F1", "https://www.marca.com/motor/formula1/calendario.html", 12 * 3600),
    "motogp": ("MotoGP", "https://www.marca.com/motor/motogp/calendario.html", 12 * 3600),
    "tv": ("Guía de TV", "https://www.marca.com/programacion-tv.html", 2 * 3600),
}
# Lo que dura cada cosa, para saber si acaba antes de cerrar (minutos)
DURACION = {("f1", "Carrera"): 120, ("f1", "Clasificación"): 60, ("f1", "Sprint"): 60,
            ("f1", "Clasificación sprint"): 45, ("motogp", "Carrera"): 60, ("motogp", "Sprint"): 30,
            ("motogp", "Clasificación"): 45, ("futbol", ""): 115}
OCUPA = {"manana": "Mañana (hasta las {corte})", "todo": "Todo el día", "tarde": "Tarde (desde las {corte})",
         "nada": "No ocupa la pista"}
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
ESTADO = {"ultima_comprobacion": None, "fuentes": {f: {"ok": None, "error": "", "n": None} for f in FUENTES},
          "tandas": {"ok": None, "error": "", "nuevas": 0, "total": None, "no_publicables": None},
          "deportes": {f: {"ok": None, "error": "", "n": None} for f in MARCA}}
DIBUJO = threading.Lock()   # las fuentes de Pillow no se deben usar en dos hilos a la vez


# ---------------------------------------------------------------- base de datos

ESQUEMA = """
CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS fotos (
    id INTEGER PRIMARY KEY, categoria_id INTEGER NOT NULL, archivo TEXT NOT NULL, subida TEXT NOT NULL,
    origen TEXT NOT NULL DEFAULT '', ext_url TEXT NOT NULL DEFAULT '', ext_fecha TEXT NOT NULL DEFAULT '');
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
    filtro TEXT NOT NULL DEFAULT '', condicion TEXT NOT NULL DEFAULT '', lista TEXT NOT NULL DEFAULT '',
    categorias_extra TEXT NOT NULL DEFAULT '', horario TEXT NOT NULL DEFAULT '', deportes TEXT NOT NULL DEFAULT '');
-- estado: generando | lista | publicada | descartada | error
CREATE TABLE IF NOT EXISTS generadas (
    id INTEGER PRIMARY KEY, publicacion_id INTEGER NOT NULL, ocurrencia TEXT NOT NULL,
    prueba INTEGER NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'generando',
    archivo TEXT, texto TEXT NOT NULL DEFAULT '', foto_id INTEGER,
    con_ia INTEGER NOT NULL DEFAULT 0, aviso TEXT NOT NULL DEFAULT '', creada TEXT NOT NULL,
    estilo TEXT NOT NULL DEFAULT '', evento_id INTEGER NOT NULL DEFAULT 0, telegram TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
-- Sesiones de F1 y MotoGP y emisiones de la guía de TV leídas de Marca
CREATE TABLE IF NOT EXISTS emisiones (
    id INTEGER PRIMARY KEY, fuente TEXT NOT NULL, fecha TEXT NOT NULL, hora TEXT NOT NULL,
    deporte TEXT NOT NULL DEFAULT '', competicion TEXT NOT NULL DEFAULT '', titulo TEXT NOT NULL DEFAULT '',
    sesion TEXT NOT NULL DEFAULT '', canal TEXT NOT NULL DEFAULT '');
-- Eventos leídos de Racecore o CKS (origen 'racecore' o 'cks') o creados a mano ('manual').
-- Solo lo necesario para las publicaciones: nada de datos personales salvo el podio.
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY, origen TEXT NOT NULL, ext_id TEXT NOT NULL,
    nombre TEXT NOT NULL DEFAULT '', campeonato TEXT NOT NULL DEFAULT '',
    fecha TEXT NOT NULL DEFAULT '', fecha_txt TEXT NOT NULL DEFAULT '', hora TEXT NOT NULL DEFAULT '',
    plazas INTEGER, inscritos INTEGER, precio TEXT NOT NULL DEFAULT '', abierta INTEGER NOT NULL DEFAULT 0,
    enlace TEXT NOT NULL DEFAULT '', web TEXT NOT NULL DEFAULT '', horarios TEXT NOT NULL DEFAULT '',
    resultados TEXT NOT NULL DEFAULT '', ganadores TEXT NOT NULL DEFAULT '', actualizado TEXT NOT NULL DEFAULT '',
    categoria_id INTEGER, pilotos TEXT NOT NULL DEFAULT '', en_fuente INTEGER NOT NULL DEFAULT 1,
    ocupa TEXT NOT NULL DEFAULT 'manana', UNIQUE (origen, ext_id));
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
                                    "condicion": "TEXT NOT NULL DEFAULT ''",
                                    "lista": "TEXT NOT NULL DEFAULT ''",
                                    "categorias_extra": "TEXT NOT NULL DEFAULT ''",
                                    "horario": "TEXT NOT NULL DEFAULT ''",
                                    "deportes": "TEXT NOT NULL DEFAULT ''"},
                  "generadas": {"estilo": "TEXT NOT NULL DEFAULT ''",
                                "evento_id": "INTEGER NOT NULL DEFAULT 0",
                                "telegram": "TEXT NOT NULL DEFAULT ''"},
                  "fotos": {"origen": "TEXT NOT NULL DEFAULT ''", "ext_url": "TEXT NOT NULL DEFAULT ''",
                            "ext_fecha": "TEXT NOT NULL DEFAULT ''"},
                  "eventos": {"categoria_id": "INTEGER", "pilotos": "TEXT NOT NULL DEFAULT ''",
                              "en_fuente": "INTEGER NOT NULL DEFAULT 1",
                              "ocupa": "TEXT NOT NULL DEFAULT 'manana'"}}
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
        if resumen_filtro(pub["filtro"]):
            extra.append(f"solo {resumen_filtro(pub['filtro'])}")
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
# El panel lee los eventos de Racecore y CKS por la red local (solo lectura, con token) y
# las publicaciones en modo «Eventos» se programan respecto a la fecha de cada uno:
# «cada 2 días de 14 a 3 días antes», «1 día después»... Los datos del evento
# ({evento}, {inscritos}, {horarios}...) se ponen en los textos antes de dibujar.

SINCRONIZAR = threading.Lock()


class ErrorFuente(Exception):
    """Fallo al leer Racecore o CKS, con un mensaje que se entiende."""


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
    """Horarios como texto: una línea por tanda («09:00-09:10 · Entrenos · Cat A»)."""
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


PARTICULAS = {"de", "del", "la", "las", "los", "y", "san", "da", "van", "von"}


def nombre_para_redes(p, formato):
    """{"nombre": "Ana", "apellidos": "de la Fuente Ruiz"} -> «Ana de la Fuente» (o «Ana F.»)."""
    nombre, apellidos = txt(p.get("nombre")) or txt(p.get("piloto")), txt(p.get("apellidos")).split()
    if not apellidos and " " in nombre and not txt(p.get("apellidos")):  # llegó el nombre entero
        nombre, apellidos = nombre.split()[0], nombre.split()[1:]
    primero = []
    for palabra in apellidos:  # el primer apellido, con sus «de la...»
        primero.append(palabra)
        if palabra.lower() not in PARTICULAS:
            break
    if formato == "inicial" and primero:
        return f"{nombre} {primero[-1][0].upper()}."
    return " ".join([nombre] + primero).strip()


def texto_pilotos(pilotos, formato):
    """Inscritos para las publicaciones: uno por línea, por orden alfabético y, si hay varias
    categorías, agrupados con su nombre delante («SENIOR:»)."""
    grupos = {}
    for p in pilotos:
        nombre = nombre_para_redes(p, formato)
        if nombre:
            grupos.setdefault(txt(p.get("categoria")), []).append(nombre)
    varias = len([c for c in grupos if c]) > 1
    lineas = []
    for cat in sorted(grupos, key=str.casefold):
        if varias:
            lineas.append(f"{(cat or 'Otros').upper()}:")
        lineas += sorted(grupos[cat], key=str.casefold)
    return "\n".join(lineas)


def evento_de_fuente(e):
    """Del JSON de Racecore o CKS solo se guarda lo que hace falta para las publicaciones."""
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
        # solo el nombre para redes (nada más de cada piloto)
        "pilotos": texto_pilotos([p for p in (e.get("pilotos") or []) if isinstance(p, dict)],
                                 ajuste("nombres_formato", "completo")),
    }


def url_fuente(fuente):
    """«192.168.1.50» -> «http://192.168.1.50:8100» (8090 en CKS). Vacío si no está configurado."""
    url = ajuste(f"{fuente}_url").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    partes = urlsplit(url)
    try:
        puerto = partes.port
    except ValueError:
        puerto = None
    host = partes.netloc if puerto else f"{partes.hostname}:{FUENTES[fuente]['puerto']}"
    return f"{partes.scheme}://{host}"


def leer_fuente(fuente):
    base, nombre, ruta = url_fuente(fuente), FUENTES[fuente]["nombre"], FUENTES[fuente]["ruta"]
    try:
        r = requests.get(f"{base}{ruta}", headers={"X-Token": ajuste(f"{fuente}_token")}, timeout=10)
    except requests.RequestException:
        raise ErrorFuente(f"No hay conexión con {base}. Mira que el PC de {nombre} esté encendido, "
                          f"en la misma red y con {nombre} abierto.") from None
    if r.status_code == 404:
        raise ErrorFuente(f"{nombre} todavía no tiene la dirección para el panel ({ruta}).")
    if r.status_code == 401:
        raise ErrorFuente(f"{nombre} dice que el token no es correcto.")
    if r.status_code == 403:
        raise ErrorFuente(f"En {nombre} falta generar el token de lectura.")
    if r.status_code != 200:
        raise ErrorFuente(f"{nombre} ha respondido con un error ({r.status_code}).")
    try:
        eventos = r.json()["eventos"]
    except (ValueError, KeyError, TypeError):
        eventos = None
    if not isinstance(eventos, list):
        raise ErrorFuente(f"La respuesta de {nombre} no tiene el formato esperado.")
    return [e for e in eventos
            if isinstance(e, dict) and e.get("id") is not None and e.get("tipo") != "campeonato"]


def sincronizar(fuente="racecore"):
    """Lee Racecore o CKS y actualiza la tabla eventos. Devuelve cuántos hay (None si no está configurado)."""
    estado = ESTADO["fuentes"][fuente]
    if not ajuste(f"{fuente}_url"):
        estado.update(error="", n=None)
        return None
    with SINCRONIZAR:
        try:
            eventos = leer_fuente(fuente)
        except ErrorFuente as e:
            estado["error"] = str(e)
            raise
        marca = ahora_txt()
        c = conectar()
        try:
            for e in eventos:
                fila = evento_de_fuente(e) | {"origen": fuente, "ext_id": str(e["id"]), "actualizado": marca,
                                              "en_fuente": 1}
                cols = list(fila)
                c.execute(f"INSERT INTO eventos ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
                          f"ON CONFLICT(origen, ext_id) DO UPDATE SET "
                          f"{', '.join(f'{k} = excluded.{k}' for k in cols)}", [fila[k] for k in cols])
            # Los que ya no manda (borrados, o pasados hace días) se ocultan y no se programan.
            # No se borran: si vuelven, conservan las fotos elegidas. Pasados 60 días, sí se borran.
            vistos = [str(e["id"]) for e in eventos]
            no_vistos = f" AND ext_id NOT IN ({', '.join('?' for _ in vistos)})" if vistos else ""
            c.execute(f"UPDATE eventos SET en_fuente = 0 WHERE origen = ?{no_vistos}", [fuente] + vistos)
            limite = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute("DELETE FROM eventos WHERE origen = ? AND en_fuente = 0 AND actualizado < ?",
                      (fuente, limite))
            c.commit()
        finally:
            c.close()
        estado.update(ok=datetime.now(), error="", n=len(eventos))
        return len(eventos)


def sincronizar_seguro():
    """Para el programador: si Racecore o CKS fallan se queda anotado y se siguen usando los últimos datos."""
    for fuente, datos in FUENTES.items():
        try:
            sincronizar(fuente)
        except ErrorFuente as e:
            log.warning("%s: %s", datos["nombre"], e)
        except Exception as e:
            ESTADO["fuentes"][fuente]["error"] = f"Error leyendo {datos['nombre']}: {e}"
            log.exception(datos["nombre"])


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


def leer_filtro(texto):
    """Filtro de eventos guardado -> {"campeonatos": [...], "eventos": [...], "texto": "..."}.

    Se guarda en JSON. Antes era solo un texto: se sigue entendiendo como «que contengan».
    """
    filtro = {"campeonatos": [], "eventos": [], "texto": ""}
    texto = (texto or "").strip()
    try:
        datos = json.loads(texto) if texto else {}
    except ValueError:
        datos = None
    if not isinstance(datos, dict):
        return filtro | {"texto": texto}
    for clave in ("campeonatos", "eventos"):
        filtro[clave] = [txt(x) for x in (datos.get(clave) or []) if txt(x)]
    filtro["texto"] = txt(datos.get("texto"))
    return filtro


def resumen_filtro(texto):
    f = leer_filtro(texto)
    return ", ".join(f["campeonatos"] + f["eventos"] + ([f"«{f['texto']}»"] if f["texto"] else []))


def eventos_de(pub, eventos=None):
    """Eventos a los que se aplica la publicación: todos, o los de los campeonatos y eventos
    marcados, o los que contienen el texto del filtro."""
    eventos = consulta("SELECT * FROM eventos WHERE en_fuente = 1") if eventos is None else eventos
    f = leer_filtro(pub["filtro"])
    if not (f["campeonatos"] or f["eventos"] or f["texto"]):
        return list(eventos)
    campeonatos = {c.casefold() for c in f["campeonatos"]}
    nombres = {n.casefold() for n in f["eventos"]}
    texto = f["texto"].casefold()
    return [e for e in eventos
            if e["campeonato"].casefold() in campeonatos or e["nombre"].casefold() in nombres
            or (texto and texto in f"{e['nombre']} {e['campeonato']}".casefold())]


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
        "{pilotos}": ev["pilotos"],
    }


def con_evento(pub, ev, occ):
    """La publicación con los datos del evento ya puestos en sus textos."""
    valores = valores_evento(ev, occ)
    datos = dict(pub)
    if datos["diseno"] == "lista" and not datos["lista"].strip():
        datos["lista"] = "{pilotos}"
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia", "lista"):
        for clave, valor in valores.items():
            datos[campo] = (datos[campo] or "").replace(clave, valor or "")
    datos["nombre"] = f"{pub['nombre']} · {ev['nombre']}"
    if ev["categoria_id"]:  # fotos elegidas para este evento (motos, alquiler...)
        datos["categoria_id"], datos["categorias_extra"] = ev["categoria_id"], ""
    datos["_aviso"] = ""
    if datos["diseno"] == "lista" and not datos["lista"].strip():
        datos["_aviso"] = "La lista de nombres está vacía: en Eventos mira si llegan los nombres de los inscritos. "
    if ev["origen"] in FUENTES and ev["actualizado"]:
        leido = datetime.strptime(ev["actualizado"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - leido > timedelta(hours=3):
            datos["_aviso"] += (f"Ojo: datos de {FUENTES[ev['origen']]['nombre']} del {leido:%d/%m %H:%M} "
                                "(no se han podido actualizar). "
                                "Revisa los números antes de publicar.")
    return datos


# ---------------------------------------------------------------- horario de apertura
# Plantilla para fines de semana, puentes y fiestas: qué hay abierto cada día y a qué horas.
# Los eventos de esos días (Racecore, CKS o a mano) quitan las horas que ocupan la pista.

def minutos(hhmm, defecto=None):
    h = leer_hora(hhmm)
    return int(h[:2]) * 60 + int(h[3:]) if h else defecto


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def unir(cosas):
    """["a", "b", "c"] -> «a, b y c»"""
    return " y ".join([", ".join(cosas[:-1]), cosas[-1]]) if len(cosas) > 1 else "".join(cosas)


def leer_horario(texto):
    """Configuración guardada (JSON) -> dict, o None si la publicación no usa horario."""
    try:
        cfg = json.loads(texto) if (texto or "").strip() else None
    except ValueError:
        cfg = None
    if not isinstance(cfg, dict):
        return None
    cfg.setdefault("dias", "finde")
    cfg["apertura"] = leer_hora(cfg.get("apertura")) or "10:00"
    cfg["cierre"] = leer_hora(cfg.get("cierre")) or "20:00"
    cfg["actividades"] = [a for a in cfg.get("actividades") or [] if isinstance(a, dict) and txt(a.get("nombre"))]
    return cfg


def dias_horario(cfg, occ):
    """Los días que abarca: el fin de semana siguiente a la publicación o unas fechas (14 como mucho)."""
    if cfg["dias"] == "fechas":
        d1 = leer_fecha(cfg.get("desde"))
        d2 = leer_fecha(cfg.get("hasta")) or d1
        if not d1:
            return []
        d1, d2 = sorted((d1, d2))
        return [d1 + timedelta(days=i) for i in range(min(14, (d2 - d1).days + 1))]
    sabado = occ.date() + timedelta(days=(5 - occ.weekday()) % 7)
    return [sabado, sabado + timedelta(days=1)]


def texto_dias(dias):
    """«sábado 11 y domingo 12 de octubre», «del 30 de abril al 3 de mayo»..."""
    if not dias:
        return ""
    d1, d2 = dias[0], dias[-1]
    if len(dias) == 1:
        return f"{DIAS[d1.weekday()]} {d1.day} de {MESES[d1.month - 1]}"
    if len(dias) == 2:
        mes1 = "" if d1.month == d2.month else f" de {MESES[d1.month - 1]}"
        return f"{DIAS[d1.weekday()]} {d1.day}{mes1} y {DIAS[d2.weekday()]} {d2.day} de {MESES[d2.month - 1]}"
    mes1 = "" if d1.month == d2.month else f" de {MESES[d1.month - 1]}"
    return f"del {d1.day}{mes1} al {d2.day} de {MESES[d2.month - 1]}"


def restar(tramos, quitar):
    """Quita el tramo «quitar» (min, min) de una lista de tramos."""
    a, b = quitar
    res = []
    for x, y in tramos:
        if b <= x or a >= y:
            res.append((x, y))
            continue
        if x < a:
            res.append((x, a))
        if b < y:
            res.append((b, y))
    return res


def calcular_horario(cfg, dias, eventos):
    """(texto del horario, nombres de los eventos). Una línea «DÍA:» y debajo qué hay y a qué horas."""
    apertura, cierre = minutos(cfg["apertura"]), minutos(cfg["cierre"])
    corte = minutos(ajuste("corte_eventos", "16:00"), 16 * 60)
    actividades = [a for a in cfg["actividades"] if a.get("activa")]
    lineas, nombres = [], []
    for dia in dias:
        lineas.append(f"{DIAS[dia.weekday()]} {dia.day} de {MESES[dia.month - 1]}:".upper())
        bloqueos = []
        for ev in [e for e in eventos if leer_fecha(e["fecha"]) == dia]:
            nombres.append(ev["nombre"])
            tramo = {"manana": (apertura, corte), "todo": (apertura, cierre),
                     "tarde": (corte, cierre)}.get(ev["ocupa"] or "manana")
            if tramo and tramo[0] < tramo[1]:
                bloqueos.append(tramo)
                lineas.append(f"Evento: {ev['nombre']} · {hhmm(tramo[0])}-{hhmm(tramo[1])}")
            else:
                lineas.append(f"Evento: {ev['nombre']}")
        grupos = {}
        for a in actividades:
            tramos = [(minutos(a.get("desde"), apertura), minutos(a.get("hasta"), cierre))]
            if a.get("evento", True):
                for b in bloqueos:
                    tramos = restar(tramos, b)
            horas = " y ".join(f"{hhmm(x)}-{hhmm(y)}" for x, y in tramos if y - x >= 30)
            if horas:
                grupos.setdefault(horas, []).append(txt(a["nombre"]))
        for horas, quienes in grupos.items():
            lineas.append(f"{unir(quienes)} · {horas}")
        if not grupos and not bloqueos:
            lineas.append("Cerrado")
    return "\n".join(lineas), list(dict.fromkeys(nombres))


def con_horario(pub, occ):
    """La publicación con {horario}, {dias_horario} y {eventos_horario} ya puestos en sus textos."""
    cfg = leer_horario(pub["horario"])
    if not cfg:
        return pub
    dias = dias_horario(cfg, occ)
    horario, nombres = calcular_horario(cfg, dias, consulta("SELECT * FROM eventos WHERE en_fuente = 1"))
    valores = {"{horario}": horario, "{dias_horario}": texto_dias(dias), "{eventos_horario}": unir(nombres)}
    datos = dict(pub)
    if datos["diseno"] == "lista" and not datos["lista"].strip():
        datos["lista"] = "{horario}"
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia", "lista"):
        for clave, valor in valores.items():
            datos[campo] = (datos[campo] or "").replace(clave, valor)
    return datos


# ---------------------------------------------------------------- deportes en TV (cafetería)
# Se leen de Marca: los calendarios de F1 y MotoGP (toda la temporada, con hora de España) y la
# guía de TV (solo hoy y mañana, con el canal). Las publicaciones con «Deportes en TV» ponen en
# {deportes} lo que cae dentro del horario de la cafetería; si no hay nada, no se publican.

DEPORTES_LOCK = threading.Lock()


def descargar_marca(url):
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.raise_for_status()
    m = re.search(rb'charset=["\']?([\w-]+)', r.content[:3000])
    return r.content.decode(m.group(1).decode() if m else "iso-8859-15", errors="replace")


def limpiar_html(trozo):
    import html as html_mod
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_mod.unescape(trozo or ""))).strip()


def tipo_sesion(nombre):
    """«Clasificación carrera», «Parrilla», «Carrera al sprint»... -> nombre corto; None si son libres."""
    n = nombre.lower()
    if "libre" in n or "practice" in n or "entrenamiento" in n:
        return None
    if "sprint" in n:
        return "Clasificación sprint" if "clasif" in n else "Sprint"
    if "clasif" in n or "parrilla" in n:
        return "Clasificación"
    if n.startswith("carrera"):
        return "Carrera"
    return None


def leer_calendario(texto, fuente):
    """Sesiones de un calendario de Marca: [(fecha, hora, gran premio, circuito, sesión)]. En MotoGP, solo MotoGP."""
    res = []
    for bloque in re.split(r'<li class="gran-premio__element"', texto)[1:]:
        titulo = re.search(r'gran-premio__title-item">(.*?)</span>', bloque, flags=re.S)
        fecha_gp = re.search(r'gran-premio__date">\s*(\d{2})-(\d{2})-(\d{4})', bloque)
        if not (titulo and fecha_gp):
            continue
        gp = limpiar_html(titulo.group(1)).rstrip(":").strip()
        anio, mes_gp = int(fecha_gp.group(3)), int(fecha_gp.group(2))
        circuito = re.search(r'gran-premio__circuit-name">(.*?)</h3>', bloque, flags=re.S)
        if fuente == "motogp":  # Moto2 y Moto3 van en otros bloques
            m = re.search(r'category-motogp">(.*?)(?=category-moto2|category-moto3|$)', bloque, flags=re.S)
            bloque = m.group(1) if m else ""
        for nombre, hora, dia, mes in re.findall(
                r'gran-premio__schedule-data">(.*?)</span>\s*<span class="hora">\s*(\d{1,2}:\d{2})\s*</span>'
                r'\s*<span class="fecha">\s*(\d{1,2})/(\d{1,2})', bloque, flags=re.S):
            sesion = tipo_sesion(limpiar_html(nombre).rstrip(":"))
            if not sesion:
                continue
            a = anio - 1 if int(mes) - mes_gp > 6 else anio
            try:
                res.append((date(a, int(mes), int(dia)).isoformat(), leer_hora(hora), gp,
                            limpiar_html(circuito.group(1)) if circuito else "", sesion))
            except ValueError:
                continue
    return res


def leer_guia_tv(texto):
    """Emisiones de la guía de TV de Marca: [(fecha, hora, deporte, competición, título, canal)]."""
    res = []
    for bloque in re.split(r'<span class="title-section-widget">', texto)[1:]:
        cabecera = re.match(r'(.*?)</span>', bloque, flags=re.S)
        dia = leer_fecha(limpiar_html(cabecera.group(1)).lower()) if cabecera else None
        if not dia:
            continue
        for ev in re.findall(r'<li class="dailyevent">(.*?)</li>', bloque, flags=re.S):
            campo = lambda cls: limpiar_html((re.search(  # noqa: E731
                rf'class="{cls}"[^>]*>(.*?)</(?:span|strong|h4)>', ev, flags=re.S) or [None, ""])[1])
            hora = leer_hora(campo("dailyhour"))
            if hora:
                res.append((dia.isoformat(), hora, campo("dailyday"), campo("dailycompetition"),
                            campo("dailyteams"), campo("dailychannel")))
    return res


def actualizar_deportes(fuentes=None, forzar=False):
    """Lee de Marca lo que haga falta (cada fuente tiene su plazo). Devuelve {fuente: nº de filas}."""
    hechas = {}
    with DEPORTES_LOCK:
        for fuente in fuentes or MARCA:
            nombre, url, cada = MARCA[fuente]
            estado = ESTADO["deportes"][fuente]
            ultima = ajuste(f"marca_{fuente}")
            if not forzar and ultima and (datetime.now() - datetime.strptime(ultima, "%Y-%m-%d %H:%M:%S")
                                          ).total_seconds() < cada:
                continue
            try:
                texto = descargar_marca(url)
                filas = leer_guia_tv(texto) if fuente == "tv" else leer_calendario(texto, fuente)
                if not filas:
                    raise ErrorFuente(f"No encuentro nada en la página de {nombre}: puede que Marca la haya cambiado.")
            except ErrorFuente as e:
                estado["error"] = str(e)
                log.warning("Marca %s: %s", fuente, e)
                continue
            except Exception as e:
                estado["error"] = f"No se ha podido leer {nombre} de Marca ({str(e)[:150]})."
                log.warning("Marca %s: %s", fuente, e)
                continue
            c = conectar()
            try:
                if fuente == "tv":  # la guía solo trae hoy y mañana: se sustituyen esos días
                    for dia in {f[0] for f in filas}:
                        c.execute("DELETE FROM emisiones WHERE fuente = 'tv' AND fecha = ?", (dia,))
                    c.executemany("INSERT INTO emisiones (fuente, fecha, hora, deporte, competicion, titulo, canal) "
                                  "VALUES ('tv', ?, ?, ?, ?, ?, ?)", filas)
                else:
                    c.execute("DELETE FROM emisiones WHERE fuente = ?", (fuente,))
                    # (fecha, hora, gran premio, circuito, sesión)
                    c.executemany("INSERT INTO emisiones (fuente, fecha, hora, titulo, competicion, sesion) "
                                  "VALUES (?, ?, ?, ?, ?, ?)", [(fuente, *f) for f in filas])
                limite = (date.today() - timedelta(days=30)).isoformat()
                c.execute("DELETE FROM emisiones WHERE fecha < ?", (limite,))
                c.commit()
            finally:
                c.close()
            guardar_ajuste(f"marca_{fuente}", ahora_txt())
            estado.update(ok=datetime.now(), error="", n=len(filas))
            hechas[fuente] = len(filas)
    return hechas


def actualizar_deportes_seguro():
    """Para el programador: solo si alguna publicación activa usa deportes."""
    usadas = set()
    for p in consulta("SELECT deportes FROM publicaciones WHERE activa = 1 AND deportes != ''"):
        cfg = leer_deportes(p["deportes"])
        usadas |= {f for f in ("f1", "motogp") if cfg and cfg.get(f)} | ({"tv"} if cfg and cfg.get("futbol") else set())
    if usadas:
        try:
            actualizar_deportes(sorted(usadas))
        except Exception:
            log.exception("Deportes")


def leer_deportes(texto):
    try:
        cfg = json.loads(texto) if (texto or "").strip() else None
    except ValueError:
        cfg = None
    if not isinstance(cfg, dict):
        return None
    cfg["apertura"] = leer_hora(cfg.get("apertura")) or "10:00"
    cfg["cierre"] = leer_hora(cfg.get("cierre")) or "20:00"
    cfg["dias"] = min(14, max(1, como_entero(cfg.get("dias")) or 1))
    return cfg


def lista_deportes(cfg, occ):
    """[(fecha, hora, línea)] de lo que cae dentro del horario de la cafetería en los días de la publicación."""
    desde = occ.date()
    dias = [(desde + timedelta(days=i)).isoformat() for i in range(cfg["dias"])]
    apertura, cierre = minutos(cfg["apertura"]), minutos(cfg["cierre"])
    marca = ",".join("?" for _ in dias)
    res = []

    def cabe(hora, dura):
        m = minutos(hora)
        return m is not None and m >= apertura and m + dura <= cierre
    for fuente in ("f1", "motogp"):
        if not cfg.get(fuente):
            continue
        for e in consulta(f"SELECT * FROM emisiones WHERE fuente = ? AND fecha IN ({marca}) ORDER BY fecha, hora",
                          [fuente, *dias]):
            if cabe(e["hora"], DURACION.get((fuente, e["sesion"]), 60)):
                res.append((e["fecha"], e["hora"], f"{e['hora']} · {MARCA[fuente][0]} {e['titulo']} · {e['sesion']}"))
    if cfg.get("futbol"):
        equipos = {x.strip().casefold() for x in (cfg.get("equipos") or "").split(",") if x.strip()}
        canal = (cfg.get("canal") or "").strip().casefold()
        fuera = [x.strip().casefold() for x in (cfg.get("excluir") or "").split(",") if x.strip()]
        for e in consulta(f"SELECT * FROM emisiones WHERE fuente = 'tv' AND fecha IN ({marca}) ORDER BY fecha, hora",
                          dias):
            lados = {x.strip().casefold() for x in e["titulo"].split(" - ")}
            if (e["deporte"].casefold() == "fútbol" and lados & equipos and canal in e["canal"].casefold()
                    and not any(x in e["competicion"].casefold() for x in fuera)
                    and cabe(e["hora"], DURACION[("futbol", "")])):
                res.append((e["fecha"], e["hora"], f"{e['hora']} · {e['titulo']} · {e['canal']}"))
    return sorted(res)


def texto_deportes(filas):
    lineas, dia = [], None
    for fecha, _, linea in filas:
        if fecha != dia:
            dia = fecha
            d = date.fromisoformat(fecha)
            lineas.append(f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}:".upper())
        lineas.append(linea)
    return "\n".join(lineas)


def con_deportes(pub, occ):
    """La publicación con {deportes} y {dias_deportes}. _vacia = no hay nada que publicar."""
    cfg = leer_deportes(pub["deportes"])
    if not cfg:
        return pub
    filas = lista_deportes(cfg, occ)
    dias = [occ.date() + timedelta(days=i) for i in range(cfg["dias"])]
    valores = {"{deportes}": texto_deportes(filas), "{dias_deportes}": texto_dias(dias)}
    datos = dict(pub)
    if datos["diseno"] == "lista" and not datos["lista"].strip():
        datos["lista"] = "{deportes}"
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia", "lista"):
        for clave, valor in valores.items():
            datos[campo] = (datos[campo] or "").replace(clave, valor)
    datos["_vacia"] = not filas
    if not filas:
        datos["_aviso"] = ((datos.get("_aviso") or "") + " No hay nada de deportes que cumpla las condiciones "
                           "(por eso no se publica sola).").strip()
    else:
        viejas = [MARCA[f][0] for f in MARCA if ESTADO["deportes"][f]["error"]]
        if viejas:
            datos["_aviso"] = ((datos.get("_aviso") or "") + f" Ojo: no se ha podido actualizar {unir(viejas)} "
                               "desde Marca; revisa los horarios.").strip()
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


def pintar_lista(img, lineas, y0, y1, acento):
    """Lista de nombres en columnas sobre un recuadro oscuro, centrada entre y0 e y1.
    Las líneas que acaban en «:» (categorías) salen en amarillo."""
    W = img.width
    margen, pad = int(W * 0.05), int(W * 0.03)
    alto = y1 - y0
    if not lineas or alto < W * 0.12:
        return
    n = len(lineas)
    largo = max(fuente_cartel("texto", 100).getlength(sin_marcas(l)) for l in lineas) / 100  # ancho a tamaño 1

    def medidas(cols):
        """(tamaño de letra, columnas, filas, ancho de columna) con ese número de columnas."""
        filas = -(-n // cols)
        ancho_col = (W - 2 * margen - 2 * pad - (cols - 1) * pad) / cols
        return int(min(W * 0.055, (alto - 2 * pad) / filas / 1.3, ancho_col / largo)), cols, filas, ancho_col
    # las columnas que dejan la letra más grande (con muchos nombres, 3 o 4; con pocos, 1)
    tam, cols, filas, ancho_col = max(medidas(c) for c in (1, 2, 3, 4))
    tam = max(14, tam)
    f = fuente_cartel("texto", tam)

    def cabe(linea):
        while len(linea) > 1 and f.getlength(linea) > ancho_col:
            linea = linea[:-2] + "…"
        return linea
    lh = int(tam * 1.3)
    caja = filas * lh - (lh - alto_mayus(f)) + 2 * pad
    arriba = y0 + max(0, (alto - caja) // 2)
    capa = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(capa).rounded_rectangle([margen, arriba, W - margen, arriba + caja],
                                           radius=int(W * 0.025), fill=(0, 0, 0, 170))
    img.paste(capa, (0, 0), capa)
    elementos = []
    for i, linea in enumerate(lineas):
        col, fila = divmod(i, filas)
        x = margen + pad + col * (ancho_col + pad)
        y = arriba + pad + fila * lh + alto_mayus(f)
        elementos.append((x, y, cabe(sin_marcas(linea)), f, AMARILLO if linea.endswith(":") else BLANCO))
    pintar(img, elementos, acento, sombra=max(2, int(W * 0.004)))


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
    # «Cartel con lista»: los nombres van en el centro, así que se deja más hueco
    lista = [l.strip() for l in variables(pub["lista"], occ).split("\n") if l.strip()] \
        if pub["diseno"] == "lista" else []
    minimo = H * (0.45 if lista else 0.1)
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
        if arriba + alto_arriba + minimo <= abajo - alto_abajo:
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
    if lista:
        pintar_lista(img, lista, arriba + alto_arriba + hueco, abajo - alto_abajo - hueco, acento)
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


def ids_categorias(pub):
    """La categoría de fotos de la publicación y las demás que se hayan marcado."""
    ids = [pub["categoria_id"]] + [int(x) for x in (pub["categorias_extra"] or "").split(",") if x.strip().isdigit()]
    return list(dict.fromkeys(i for i in ids if i))


def componer(pub, occ, evitar=None, evitar_estilo=None):
    """(imagen, foto_id, con_ia, aviso, estilo)"""
    _, ancho, alto = FORMATOS.get(pub["formato"], FORMATOS["post"])
    avisos = []
    ids = ids_categorias(pub)
    fotos = consulta(f"SELECT * FROM fotos WHERE categoria_id IN ({', '.join('?' for _ in ids)})", ids) if ids else []
    foto = elegir_foto(pub["id"], fotos, evitar)
    fondo, con_ia = None, False

    if foto is None:
        avisos.append("Las categorías elegidas no tienen fotos: se ha usado un fondo liso." if ids
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
    for campo in ("titulo", "subtitulo", "pie", "texto", "prompt_ia", "lista"):
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
        pub = con_deportes(con_horario(pub, occ), occ)
        img, foto_id, con_ia, aviso, estilo = componer(pub, occ, anterior[0]["foto_id"] if anterior else None,
                                                      (anterior[0]["estilo"] or None) if anterior else None)
        archivo = f"{pub['id']}_{occ:%Y%m%d_%H%M}_{gen_id}_{uuid.uuid4().hex[:6]}.jpg"
        img.save(DIR_GEN / archivo, "JPEG", quality=92, optimize=True)
        texto = variables(pub["texto"], occ)
        if "_aviso" in pub.keys():  # publicaciones de eventos: p. ej. datos de Racecore o CKS sin actualizar
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
            ejecutar("UPDATE generadas SET telegram = ? WHERE id = ?",
                     (avisar_telegram(DIR_GEN / archivo, pub["nombre"], occ, texto), gen_id))
    except Exception as e:
        log.exception("Error generando «%s»", pub["nombre"])
        ejecutar("UPDATE generadas SET estado = 'error', aviso = ? WHERE id = ? AND estado = 'generando'",
                 (str(e)[:500], gen_id))


def en_segundo_plano(gen_id, pub, occ):
    """pub ya con los datos del evento puestos (con_evento), si es de eventos."""
    threading.Thread(target=generar, args=(gen_id, pub, occ), daemon=True).start()


def avisar_telegram(ruta, nombre, occ, texto):
    """Manda la imagen y su texto al móvil. Devuelve «ok», el motivo del fallo, o vacío si no está configurado."""
    token, chat = ajuste("telegram_token"), ajuste("telegram_chat")
    if not (token and chat):
        return ""
    api = f"https://api.telegram.org/bot{token}"

    def comprobar_respuesta(r):
        if not r.ok:
            try:
                motivo = r.json().get("description")
            except ValueError:
                motivo = None
            raise RuntimeError(motivo or f"error {r.status_code}")
    try:
        with open(ruta, "rb") as f:
            comprobar_respuesta(requests.post(f"{api}/sendPhoto", timeout=60, files={"photo": f}, data={
                "chat_id": chat, "caption": f"📣 {nombre} · publicar {cuando_txt(occ)}"}))
        if texto.strip():
            comprobar_respuesta(requests.post(f"{api}/sendMessage", timeout=30,
                                              data={"chat_id": chat, "text": texto[:4096]}))
        return "ok"
    except Exception as e:
        motivo = str(e).replace(token, "***")
        log.warning("Telegram: %s", motivo)
        return motivo[:300]


def comprobar(ahora=None):
    """Genera lo que toque: desde «antelación» minutos antes de la hora hasta poco después."""
    ahora = ahora or datetime.now()
    eventos = None
    for pub in consulta("SELECT * FROM publicaciones WHERE activa = 1"):
        antelacion = timedelta(minutes=max(0, pub["antelacion"]))
        if pub["modo"] == "evento":
            if eventos is None:
                eventos = consulta("SELECT * FROM eventos WHERE en_fuente = 1")
            for occ, ev in ocurrencias_evento(pub, ahora - GRACIA, ahora + antelacion, eventos):
                # si no se cumple la condición (p. ej. aún no hay resultados) se vuelve a mirar después
                if cumple(pub, ev):
                    gen_id = reclamar(pub["id"], occ, prueba=False, evento_id=ev["id"])
                    if gen_id:
                        generar(gen_id, con_evento(pub, ev, occ), occ, enviar=True)
            continue
        for occ in ocurrencias(pub, ahora - GRACIA, ahora + antelacion):
            if occ > ahora - GRACIA:
                # Deportes: si ese día no hay nada que ver en la cafetería, no se publica (se vuelve a mirar)
                if pub["deportes"]:
                    datos = con_deportes(pub, occ)
                    if isinstance(datos, dict) and datos.get("_vacia"):
                        continue
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
            threading.Thread(target=importar_tandas_seguro, daemon=True, name="tandas").start()
            threading.Thread(target=actualizar_deportes_seguro, daemon=True, name="deportes").start()
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
                           n_pruebas=sum(1 for g in filas if g["prueba"] and g["estado"] != "generando"),
                           hay_telegram=bool(ajuste("telegram_token") and ajuste("telegram_chat")),
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


@app.post("/generadas/<int:gen_id>/telegram")
def enviar_telegram(gen_id):
    """Manda (o vuelve a mandar) una imagen al móvil."""
    g = consulta("""SELECT g.*, p.nombre, e.nombre AS evento FROM generadas g
                    LEFT JOIN publicaciones p ON p.id = g.publicacion_id LEFT JOIN eventos e ON e.id = g.evento_id
                    WHERE g.id = ?""", (gen_id,))
    if not g or not g[0]["archivo"]:
        abort(404)
    g = g[0]
    nombre = " · ".join(x for x in (g["nombre"], g["evento"]) if x) or "Publicación"
    resultado = avisar_telegram(DIR_GEN / g["archivo"], nombre,
                                datetime.strptime(g["ocurrencia"], "%Y-%m-%d %H:%M"), g["texto"])
    if not resultado:
        flash("Telegram no está configurado: pon el token y el chat en Ajustes.", "error")
    else:
        ejecutar("UPDATE generadas SET telegram = ? WHERE id = ?", (resultado, gen_id))
        flash("Enviada a Telegram." if resultado == "ok" else f"Telegram ha fallado: {resultado}",
              "ok" if resultado == "ok" else "error")
    return redirect(url_for("listas"))


@app.post("/generadas/borrar-pruebas")
def borrar_pruebas():
    """Quita de la lista todas las pruebas («Generar ahora») que ya estén hechas."""
    pruebas = consulta("SELECT id, archivo FROM generadas WHERE prueba = 1 AND estado IN ('lista', 'error')")
    for g in pruebas:
        if g["archivo"]:
            (DIR_GEN / g["archivo"]).unlink(missing_ok=True)
        ejecutar("UPDATE generadas SET estado = 'descartada', archivo = NULL WHERE id = ?", (g["id"],))
    flash(f"{len(pruebas)} prueba(s) borrada(s).", "ok")
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
         "momento": "antes", "dias_desde": 14, "dias_hasta": 3, "cada": 2, "filtro": "", "condicion": "",
         "lista": "", "categorias_extra": "", "horario": "", "deportes": ""}
DEPORTES_NUEVA = {"f1": False, "motogp": False, "futbol": False, "equipos": "Real Madrid, Barcelona",
                  "canal": "DAZN", "excluir": "Liga F", "dias": 4, "apertura": "10:00", "cierre": "20:00"}
PLANTILLAS_DEPORTES = {
    "motor": {"nombre": "F1 y MotoGP en la cafetería", "activa": 0, "modo": "semanal", "dias": "3", "hora": "12:00",
              "formato": "vertical", "diseno": "lista", "lista": "{deportes}",
              "titulo": "ESTE FINDE\n*EN DIRECTO*", "subtitulo": "F1 y MotoGP\nen nuestra cafetería", "pie": "",
              "texto": "🏎️🏍️ Este fin de semana vive la F1 y MotoGP en la cafetería del circuito:\n\n{deportes}"
                       "\n\n¡Te esperamos!",
              "deportes": json.dumps(DEPORTES_NUEVA | {"f1": True, "motogp": True, "dias": 4}, ensure_ascii=False)},
    "futbol": {"nombre": "Fútbol en la cafetería", "activa": 0, "modo": "semanal", "dias": "0,1,2,3,4,5,6",
               "hora": "10:00", "formato": "vertical", "diseno": "lista", "lista": "{deportes}",
               "titulo": "HOY\n*FÚTBOL*", "subtitulo": "En directo\nen la cafetería", "pie": "",
               "texto": "⚽ Hoy en la cafetería:\n\n{deportes}\n\n¡Ven a verlo con nosotros!",
               "deportes": json.dumps(DEPORTES_NUEVA | {"futbol": True, "dias": 1}, ensure_ascii=False)},
}
# «+ Horario de apertura» en Publicaciones: una publicación ya preparada (en pausa)
PLANTILLA_HORARIO = {
    "nombre": "Horario del fin de semana", "activa": 0, "modo": "semanal", "dias": "3", "hora": "12:00",
    "formato": "vertical", "diseno": "lista", "lista": "{horario}",
    "titulo": "*HORARIO*", "subtitulo": "Fin de semana\n{dias_horario}", "pie": "",
    "texto": "🏁 Horario de este fin de semana ({dias_horario}):\n\n{horario}\n\n¡Te esperamos en pista!",
    "horario": json.dumps({"dias": "finde", "apertura": "10:00", "cierre": "20:00", "actividades": [
        {"nombre": n, "activa": True, "desde": "", "hasta": "", "evento": True} for n in ACTIVIDADES]},
        ensure_ascii=False)}


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
            "categoria": " + ".join(cats.get(i, "?") for i in ids_categorias(p)) or "sin categoría",
            "formato_txt": FORMATOS.get(p["formato"], FORMATOS["post"])[0],
            "proxima": (cuando_txt(occ) + (f" · {ev['nombre']}" if ev else "")) if occ else "",
            "genera": genera.strftime("%d/%m %H:%M") if genera else "",
        })
    return render_template("publicaciones.html", filas=filas)


def guardar_filtro(f):
    """Campeonatos y eventos marcados, y el texto, en JSON (vacío = todos los eventos)."""
    filtro = {"campeonatos": sorted({x.strip() for x in f.getlist("f_campeonato") if x.strip()}),
              "eventos": sorted({x.strip() for x in f.getlist("f_evento") if x.strip()}),
              "texto": f.get("f_texto", "").strip()}
    return json.dumps(filtro, ensure_ascii=False) if any(filtro.values()) else ""


def guardar_horario(f):
    """Horario de apertura del formulario, en JSON (vacío si no se usa)."""
    if not f.get("h_usar"):
        return ""
    actividades = []
    for i in range(len(ACTIVIDADES) + 1):
        nombre = f.get(f"h_nombre_{i}", "").strip()
        if nombre:
            actividades.append({"nombre": nombre, "activa": bool(f.get(f"h_activa_{i}")),
                                "desde": leer_hora(f.get(f"h_desde_{i}")), "hasta": leer_hora(f.get(f"h_hasta_{i}")),
                                "evento": bool(f.get(f"h_evento_{i}"))})
    return json.dumps({"dias": "fechas" if f.get("h_dias") == "fechas" else "finde",
                       "desde": f.get("h_desde", "").strip(), "hasta": f.get("h_hasta", "").strip(),
                       "apertura": leer_hora(f.get("h_apertura")) or "10:00",
                       "cierre": leer_hora(f.get("h_cierre")) or "20:00", "actividades": actividades},
                      ensure_ascii=False)


def guardar_deportes(f):
    """Deportes en TV del formulario, en JSON (vacío si no se usa)."""
    if not f.get("d_usar"):
        return ""
    return json.dumps({"f1": bool(f.get("d_f1")), "motogp": bool(f.get("d_motogp")), "futbol": bool(f.get("d_futbol")),
                       "equipos": f.get("d_equipos", "").strip(), "canal": f.get("d_canal", "").strip(),
                       "excluir": f.get("d_excluir", "").strip(),
                       "dias": min(14, max(1, como_entero(f.get("d_dias")) or 1)),
                       "apertura": leer_hora(f.get("d_apertura")) or "10:00",
                       "cierre": leer_hora(f.get("d_cierre")) or "20:00"}, ensure_ascii=False)


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
        "diseno": f.get("diseno") if f.get("diseno") in ("sencillo", "ia", "lista") else "cartel",
        "lista": f.get("lista", "").strip().replace("\r", ""),
        "estilo_ia": f.get("estilo_ia") if f.get("estilo_ia") in ia_fondo.ESTILOS else "variado",
        "momento": f.get("momento") if f.get("momento") in MOMENTOS else "antes",
        "dias_desde": entero("dias_desde", 14, 0, 365),
        "dias_hasta": entero("dias_hasta", 3, 0, 365),
        "cada": entero("cada", 1, 1, 60),
        "filtro": guardar_filtro(f),
        "condicion": f.get("condicion") if f.get("condicion") in CONDICIONES else "",
        "categorias_extra": ",".join(sorted({x for x in f.getlist("categorias_extra") if x.isdigit()
                                             and x != str(categoria_id)}, key=int)),
        "horario": guardar_horario(f),
        "deportes": guardar_deportes(f),
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
    if not pub_id and request.args.get("plantilla") == "horario":
        pub.update(PLANTILLA_HORARIO)
    if not pub_id and request.args.get("plantilla") in PLANTILLAS_DEPORTES:
        pub.update(PLANTILLAS_DEPORTES[request.args["plantilla"]])
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
    # Para marcar: los campeonatos y los eventos de ahora (sin los ya pasados), más los ya marcados
    filtro = leer_filtro(pub["filtro"])
    todos = consulta("SELECT nombre, campeonato, fecha FROM eventos WHERE en_fuente = 1 ORDER BY fecha, nombre")
    desde = date.today() - timedelta(days=10)
    campeonatos = sorted({e["campeonato"] for e in todos if e["campeonato"]} | set(filtro["campeonatos"]),
                         key=str.casefold)
    sueltos = list(dict.fromkeys([e["nombre"] for e in todos if e["nombre"] and
                                  (leer_fecha(e["fecha"]) or desde) >= desde] + filtro["eventos"]))
    # Horario de apertura: lo guardado, o lo normal (10 a 20 y las tres actividades) para empezar
    horario = leer_horario(pub["horario"])
    filas_h = list((horario or {}).get("actividades") or
                   [{"nombre": n, "activa": True, "desde": "", "hasta": "", "evento": True} for n in ACTIVIDADES])
    filas_h += [{"nombre": "", "activa": False, "desde": "", "hasta": "", "evento": True}] * \
        (len(ACTIVIDADES) + 1 - len(filas_h))
    vista = ""
    if horario and pub.get("id") and pub["modo"] != "evento":  # cómo quedaría la próxima vez
        occ = ocurrencia_prueba(pub_o_404(pub["id"]))
        vista = calcular_horario(horario, dias_horario(horario, occ),
                                 consulta("SELECT * FROM eventos WHERE en_fuente = 1"))[0]
    deportes = leer_deportes(pub["deportes"])
    vista_dep = ""
    if deportes and pub.get("id") and pub["modo"] != "evento":
        occ = ocurrencia_prueba(pub_o_404(pub["id"]))
        vista_dep = texto_deportes(lista_deportes(deportes, occ)) or \
            f"Nada que cumpla las condiciones para el {cuando_txt(occ)}: ese día no se publica."
    return render_template("publicacion.html", pub=pub, formatos=FORMATOS, dias=DIAS,
                           filtro=filtro, campeonatos=campeonatos, sueltos=sueltos,
                           deportes=deportes or DEPORTES_NUEVA, usa_deportes=bool(deportes), vista_deportes=vista_dep,
                           horario=horario or {"dias": "finde", "apertura": "10:00", "cierre": "20:00"},
                           usa_horario=bool(horario), filas_h=filas_h[:len(ACTIVIDADES) + 1], vista_horario=vista,
                           extras={x for x in (pub["categorias_extra"] or "").split(",") if x},
                           eventos_filtro=[{"n": e["nombre"], "c": e["campeonato"]} for e in todos],
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


@app.post("/publicaciones/<int:pub_id>/duplicar")
def duplicar_publicacion(pub_id):
    """Copia en pausa, para hacer una variante (otro texto, otros eventos...)."""
    datos = {k: v for k, v in dict(pub_o_404(pub_id)).items() if k != "id"}
    datos |= {"nombre": f"{datos['nombre']} (copia)", "activa": 0}
    campos = list(datos)
    nuevo = ejecutar(f"INSERT INTO publicaciones ({', '.join(campos)}) VALUES ({', '.join('?' for _ in campos)})",
                     [datos[c] for c in campos]).lastrowid
    flash("Copia creada en pausa. Cámbiale lo que quieras y actívala.", "ok")
    return redirect(url_for("editar_publicacion", pub_id=nuevo))


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

def estado_fuentes():
    res = []
    for fuente, datos in FUENTES.items():
        estado = ESTADO["fuentes"][fuente]
        res.append({"clave": fuente, "nombre": datos["nombre"], "configurado": bool(ajuste(f"{fuente}_url")),
                    "url": url_fuente(fuente), "error": estado["error"], "n": estado["n"],
                    "ok": estado["ok"].strftime("%d/%m %H:%M") if estado["ok"] else ""})
    return res


def leer_ahora(fuentes=None):
    """Lee Racecore y CKS (o las fuentes indicadas) ahora mismo y lo cuenta con un mensaje."""
    fuentes = [f for f in (fuentes or FUENTES) if ajuste(f"{f}_url")]
    if not fuentes:
        flash("Ni Racecore ni CKS están configurados: pon su dirección y su token en Ajustes.", "error")
    for fuente in fuentes:
        nombre = FUENTES[fuente]["nombre"]
        try:
            flash(f"{nombre}: {sincronizar(fuente)} evento(s) leído(s).", "ok")
        except ErrorFuente as e:
            flash(f"{nombre}: {e}", "error")
        except Exception as e:
            log.exception(nombre)
            flash(f"Error leyendo {nombre}: {e}", "error")


@app.route("/eventos")
def eventos():
    ahora = datetime.now()
    hoy = ahora.date()
    reglas = consulta("SELECT * FROM publicaciones WHERE modo = 'evento' AND activa = 1")
    filas = []
    for ev in consulta("SELECT * FROM eventos WHERE en_fuente = 1"):
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
            "n_nombres": sum(1 for l in ev["pilotos"].splitlines() if l.strip() and not l.strip().endswith(":")),
            "proximas": [(cuando_txt(o), n, c) for o, n, c in proximas[:8]],
        })
    filas.sort(key=lambda e: e["orden"])
    hay_reglas = bool(consulta("SELECT 1 FROM publicaciones WHERE modo = 'evento' LIMIT 1"))
    faltan = sum(1 for e in EJEMPLOS if not consulta("SELECT 1 FROM publicaciones WHERE nombre = ?", (e["nombre"],)))
    return render_template("eventos.html", eventos=filas, fuentes=estado_fuentes(), hay_reglas=hay_reglas,
                           nombres_fuente={f: d["nombre"] for f, d in FUENTES.items()},
                           hay_activas=bool(reglas), faltan_ejemplos=faltan,
                           ocupa={k: v.format(corte=ajuste("corte_eventos", "16:00")) for k, v in OCUPA.items()},
                           categorias=consulta("SELECT * FROM categorias ORDER BY nombre"))


@app.post("/eventos/<int:ev_id>/fotos")
def fotos_evento(ev_id):
    """Categoría de fotos de un evento (vacío = la de cada publicación) y lo que ocupa de la pista."""
    if not consulta("SELECT 1 FROM eventos WHERE id = ?", (ev_id,)):
        abort(404)
    try:
        cat_id = int(request.form.get("categoria_id") or 0) or None
    except ValueError:
        cat_id = None
    if cat_id and not consulta("SELECT 1 FROM categorias WHERE id = ?", (cat_id,)):
        cat_id = None
    ocupa = request.form.get("ocupa") if request.form.get("ocupa") in OCUPA else "manana"
    ejecutar("UPDATE eventos SET categoria_id = ?, ocupa = ? WHERE id = ?", (cat_id, ocupa, ev_id))
    flash("Evento guardado.", "ok")
    return redirect(url_for("eventos"))


@app.post("/eventos/leer")
def leer_eventos():
    leer_ahora()
    return redirect(url_for("ajustes") if request.form.get("volver") == "ajustes" else url_for("eventos"))


EVENTO_NUEVO = {"id": None, "nombre": "", "campeonato": "", "fecha": "", "hora": "", "plazas": None,
                "inscritos": None, "precio": "", "abierta": 1, "enlace": "", "web": "", "horarios": "",
                "resultados": "", "ganadores": "", "pilotos": ""}


def evento_manual_o_404(ev_id):
    fila = consulta("SELECT * FROM eventos WHERE id = ? AND origen = 'manual'", (ev_id,))
    if not fila:
        abort(404)
    return fila[0]


@app.route("/eventos/nuevo", methods=["GET", "POST"])
@app.route("/eventos/<int:ev_id>", methods=["GET", "POST"])
def editar_evento(ev_id=None):
    """Eventos a mano: para lo que no está en Racecore ni en CKS (o mientras no estén conectados)."""
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
            "ganadores": f.get("ganadores", "").strip(),
            "pilotos": "\n".join(l.strip() for l in f.get("pilotos", "").splitlines() if l.strip()),
            "actualizado": ahora_txt(),
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
     "hora": "19:00", "condicion": "", "diseno": "lista", "lista": "{pilotos}",
     "titulo": "*{inscritos}*\nPILOTOS", "subtitulo": "{evento}\n¡Es {faltan}!",
     "pie": "{dia} {fecha} | Desde las {hora}",
     "texto": "✅ ¡Todo listo! {inscritos} pilotos para {evento}, {faltan}.\n\n{pilotos}\n\n"
              "📋 Horarios y más información: {web}"},
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
    t = ESTADO["tandas"]
    return render_template("categoria.html", cat=cat, fotos=lista,
                           subidas=request.args.get("subidas", type=int), malas=request.args.get("malas", type=int),
                           es_tandas=cat_id == categoria_tandas(), tandas=t | {
                               "ok": t["ok"].strftime("%d/%m %H:%M") if t["ok"] else ""})


def miniatura(archivo):
    return Path(archivo).stem + ".jpg"


def guardar_imagen(stream, lado_max=LADO_MAX_FOTO):
    """Guarda la foto ya girada y reducida, y su miniatura. Devuelve el archivo; excepción si no es una imagen."""
    archivo = f"{uuid.uuid4().hex}.jpg"
    try:
        with Image.open(stream) as im:
            im.draft("RGB", (lado_max, lado_max))  # JPEG grandes: decodifica ya reducido
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((lado_max, lado_max), Image.LANCZOS)
            im.save(DIR_FOTOS / archivo, "JPEG", quality=90)
            im.thumbnail((480, 480))
            im.save(DIR_MINIS / miniatura(archivo), "JPEG", quality=85)
    except Exception:
        (DIR_FOTOS / archivo).unlink(missing_ok=True)
        raise
    return archivo


def guardar_foto(cat_id, f):
    """Foto subida a mano. False si no es una imagen válida."""
    if Path(f.filename or "").suffix.lower() not in EXTENSIONES:
        return False
    try:
        archivo = guardar_imagen(f.stream)
    except Exception:
        return False
    ejecutar("INSERT INTO fotos (categoria_id, archivo, subida) VALUES (?, ?, ?)", (cat_id, archivo, ahora_txt()))
    return True


# --- Fotos de las tandas de CKS
# CKS da solo las fotos de tandas de alquiler en las que TODOS los pilotos dieron permiso de imagen
# (y ningún nombre). El panel las copia a una categoría y las borra cuando CKS deja de darlas:
# pasada la semana, o si alguien retira el permiso.

IMPORTAR = threading.Lock()


def fotos_de_tandas(datos):
    """{dirección: inicio de la tanda} de la respuesta de CKS: tandas[] {sesion_id, inicio, pilotos, fotos[]}."""
    urls = {}
    for t in datos.get("tandas") or []:
        if not isinstance(t, dict):
            continue
        inicio = txt(t.get("inicio")).replace("T", " ")[:19]  # «2026-09-27 17:30:12»
        fotos = t.get("fotos")
        if not isinstance(fotos, list):  # por si la lista se llama de otra forma
            fotos = next((v for v in t.values() if isinstance(v, list)), [])
        for f in fotos:
            if isinstance(f, dict):
                f = next((v for k, v in f.items() if k in ("url", "direccion", "src") and isinstance(v, str)),
                         next((v for v in f.values() if isinstance(v, str) and v.startswith("http")), None))
            if isinstance(f, str) and f.startswith(("http://", "https://")) and f not in urls:
                urls[f] = inicio
    return urls


def descargar_foto(url, cat_id, fecha=""):
    datos = io.BytesIO()
    with requests.get(url, timeout=60, stream=True) as r:
        r.raise_for_status()
        for trozo in r.iter_content(65536):
            datos.write(trozo)
            if datos.tell() > 50 * 1024 * 1024:
                raise ValueError("foto demasiado grande")
    datos.seek(0)
    archivo = guardar_imagen(datos, LADO_MAX_TANDA)
    ejecutar("INSERT INTO fotos (categoria_id, archivo, subida, origen, ext_url, ext_fecha) "
             "VALUES (?, ?, ?, 'cks', ?, ?)", (cat_id, archivo, ahora_txt(), url, fecha))


def categoria_tandas():
    try:
        cat_id = int(ajuste("tandas_categoria") or 0)
    except ValueError:
        return None
    return cat_id if cat_id and consulta("SELECT 1 FROM categorias WHERE id = ?", (cat_id,)) else None


def importar_tandas():
    """Trae las fotos nuevas de las tandas y quita las que CKS ya no da. None si no está configurado."""
    cat_id, estado = categoria_tandas(), ESTADO["tandas"]
    if not (cat_id and ajuste("cks_url")):
        estado.update(error="", total=None)
        return None
    if not IMPORTAR.acquire(blocking=False):  # ya se están trayendo
        return None
    try:
        base = url_fuente("cks")
        try:
            r = requests.get(f"{base}/api/publico/fotos", params={"horas": HORAS_TANDAS},
                             headers={"X-Token": ajuste("cks_token")}, timeout=30)
        except requests.RequestException:
            raise ErrorFuente(f"No hay conexión con {base}. Mira que CKS esté abierto y en la misma red.") from None
        if r.status_code == 404:
            raise ErrorFuente("CKS todavía no tiene la dirección de fotos (/api/publico/fotos).")
        if r.status_code in (401, 403):
            raise ErrorFuente("CKS no acepta el token (es el mismo que el de las competiciones).")
        if r.status_code != 200:
            raise ErrorFuente(f"CKS ha respondido con un error ({r.status_code}).")
        try:
            datos = r.json()
        except ValueError:
            datos = None
        if not isinstance(datos, dict) or not isinstance(datos.get("tandas"), list):
            raise ErrorFuente("La respuesta de fotos de CKS no tiene el formato esperado.")
        urls = fotos_de_tandas(datos)
        ya = {f["ext_url"]: f for f in consulta("SELECT * FROM fotos WHERE origen = 'cks'")}
        nuevas = fallidas = 0
        for url, fecha in urls.items():
            if url in ya:
                continue
            try:
                descargar_foto(url, cat_id, fecha)
                nuevas += 1
            except Exception as e:
                fallidas += 1
                log.warning("Foto de tanda %s: %s", url, e)
        # Las que CKS ya no da (más de una semana, permiso retirado o foto borrada) se borran.
        # Si CKS no pudo mirar alguna tanda en la web, esta vez no se borra nada, por si acaso.
        # Si la lista viene cortada (40 tandas), las anteriores a la más antigua de la lista no
        # se pueden comprobar: esas se borran al cumplir la semana.
        cortada = len(datos["tandas"]) >= MAX_TANDAS_CKS
        desde = min((f for f in urls.values() if f), default="") if cortada else ""
        semana = (datetime.now() - timedelta(hours=HORAS_TANDAS)).strftime("%Y-%m-%d %H:%M:%S")
        if not como_entero(datos.get("sin_conexion")):
            for url, foto in ya.items():
                fecha = foto["ext_fecha"]
                comprobable = not cortada or not fecha or fecha >= desde
                if url not in urls and (comprobable or fecha < semana):
                    borrar_archivos_foto(foto)
                    ejecutar("DELETE FROM fotos WHERE id = ?", (foto["id"],))
        estado.update(ok=datetime.now(), nuevas=nuevas, total=len(urls),
                      no_publicables=como_entero(datos.get("no_publicables")),
                      error=f"{fallidas} foto(s) no se han podido bajar; se reintenta en 10 minutos." if fallidas else "")
        return nuevas
    except ErrorFuente as e:
        estado["error"] = str(e)
        raise
    finally:
        IMPORTAR.release()


def importar_tandas_seguro():
    try:
        importar_tandas()
    except ErrorFuente as e:
        log.warning("Fotos de tandas: %s", e)
    except Exception as e:
        ESTADO["tandas"]["error"] = f"Error trayendo las fotos de las tandas: {e}"
        log.exception("Fotos de tandas")


def traer_tandas_ahora():
    try:
        n = importar_tandas()
        if n is None:
            flash("Las fotos de las tandas no están activadas (Ajustes → Racecore y CKS) o ya se están trayendo.", "error")
        else:
            t = ESTADO["tandas"]
            flash(f"Fotos de tandas: {n} nueva(s); {t['total']} en total de la última semana"
                  + (f"; {t['no_publicables']} tanda(s) sin permiso de imagen" if t["no_publicables"] else "")
                  + (f". {t['error']}" if t["error"] else "."), "error" if t["error"] else "ok")
    except ErrorFuente as e:
        flash(f"Fotos de tandas: {e}", "error")
    except Exception as e:
        log.exception("Fotos de tandas")
        flash(f"Error trayendo las fotos de las tandas: {e}", "error")


@app.post("/fotos/tandas")
def traer_tandas():
    traer_tandas_ahora()
    cat_id = categoria_tandas()
    return redirect(url_for("categoria", cat_id=cat_id) if cat_id else url_for("fotos"))


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
    for p in consulta("SELECT id, categorias_extra FROM publicaciones WHERE categorias_extra != ''"):
        resto = ",".join(x for x in p["categorias_extra"].split(",") if x.strip() != str(cat_id))
        ejecutar("UPDATE publicaciones SET categorias_extra = ? WHERE id = ?", (resto, p["id"]))
    ejecutar("UPDATE eventos SET categoria_id = NULL WHERE categoria_id = ?", (cat_id,))
    if categoria_tandas() == cat_id:
        guardar_ajuste("tandas_categoria", "")
    ejecutar("DELETE FROM categorias WHERE id = ?", (cat_id,))
    flash("Categoría borrada.", "ok")
    return redirect(url_for("fotos"))


# --- Ajustes

@app.route("/ajustes", methods=["GET", "POST"])
def ajustes():
    if request.method == "POST":
        antes = {f: (ajuste(f"{f}_url"), ajuste(f"{f}_token")) for f in FUENTES}
        formato_antes = ajuste("nombres_formato", "completo")
        for f in FUENTES:
            guardar_ajuste(f"{f}_url", request.form.get(f"{f}_url", "").strip())
        guardar_ajuste("nombres_formato", "inicial" if request.form.get("nombres_formato") == "inicial" else "completo")
        guardar_ajuste("corte_eventos", leer_hora(request.form.get("corte_eventos")) or "16:00")
        tandas_antes = categoria_tandas()
        elegida = request.form.get("tandas_categoria", "")
        if elegida == "nueva":
            existe = consulta("SELECT id FROM categorias WHERE nombre = 'Tandas CKS'")
            elegida = str(existe[0]["id"] if existe else
                          ejecutar("INSERT INTO categorias (nombre) VALUES ('Tandas CKS')").lastrowid)
        guardar_ajuste("tandas_categoria", elegida if elegida.isdigit() else "")
        for clave in ("openai_key", "telegram_token", *(f"{f}_token" for f in FUENTES)):
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
        # se prueba enseguida la conexión que haya cambiado (y si cambia el formato, se rehacen los nombres)
        cambiadas = [f for f in FUENTES if ajuste(f"{f}_url") and (
            (ajuste(f"{f}_url"), ajuste(f"{f}_token")) != antes[f] or ajuste("nombres_formato", "completo") != formato_antes)]
        if cambiadas:
            leer_ahora(cambiadas)
        if categoria_tandas() and (categoria_tandas() != tandas_antes or "cks" in cambiadas):
            traer_tandas_ahora()
        return redirect(url_for("ajustes"))

    def oculta(clave):
        v = ajuste(clave)
        return f"configurada (…{v[-4:]})" if v else ""

    ultima = ESTADO["ultima_comprobacion"]
    return render_template(
        "ajustes.html", openai_key=oculta("openai_key"), telegram_token=oculta("telegram_token"),
        conexiones=[f | {"url_ajuste": ajuste(f"{f['clave']}_url"), "token": oculta(f"{f['clave']}_token")}
                    for f in estado_fuentes()],
        nombres_formato=ajuste("nombres_formato", "completo"), corte_eventos=ajuste("corte_eventos", "16:00"),
        tandas_categoria=categoria_tandas(), categorias=consulta("SELECT * FROM categorias ORDER BY nombre"),
        deportes=[{"nombre": MARCA[f][0], "error": ESTADO["deportes"][f]["error"], "n": ESTADO["deportes"][f]["n"],
                   "ok": ajuste(f"marca_{f}")[8:10] + "/" + ajuste(f"marca_{f}")[5:7] + " " + ajuste(f"marca_{f}")[11:16]
                   if ajuste(f"marca_{f}") else ""} for f in MARCA],
        tandas=ESTADO["tandas"] | {"ok": ESTADO["tandas"]["ok"].strftime("%d/%m %H:%M") if ESTADO["tandas"]["ok"] else ""},
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


@app.post("/deportes/actualizar")
def actualizar_deportes_ahora():
    hechas = actualizar_deportes(forzar=True)
    errores = [f"{MARCA[f][0]}: {ESTADO['deportes'][f]['error']}" for f in MARCA if f not in hechas]
    if hechas:
        flash("Marca: " + ", ".join(f"{MARCA[f][0]} {n}" for f, n in hechas.items()) + " leídos.", "ok")
    for e in errores:
        flash(e, "error")
    return redirect(request.referrer or url_for("ajustes"))


@app.post("/ajustes/telegram/detectar")
def detectar_telegram():
    """Busca el chat del último mensaje que le han mandado al bot y lo guarda como Chat ID."""
    token = ajuste("telegram_token")
    if not token:
        flash("Primero pon el token del bot y guarda.", "error")
        return redirect(url_for("ajustes"))
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        r.raise_for_status()
        chats = [(u.get("message") or u.get("channel_post") or {}).get("chat") for u in r.json().get("result", [])]
        chats = [c for c in chats if c and c.get("id")]
    except Exception as e:
        flash(f"Telegram ha fallado: {str(e).replace(token, '***')}", "error")
        return redirect(url_for("ajustes"))
    if not chats:
        flash("No encuentro ningún mensaje: abre tu bot en Telegram, mándale «hola» y vuelve a pulsar el botón.",
              "error")
        return redirect(url_for("ajustes"))
    chat = chats[-1]
    guardar_ajuste("telegram_chat", str(chat["id"]))
    nombre = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
    flash(f"Chat encontrado: {nombre or chat['id']}. Ahora pulsa «Probar Telegram».", "ok")
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
<div class="fila" style="justify-content:space-between"><h1>Listas para publicar</h1>
{% if n_pruebas %}<form class="enlinea" method="post" action="{{ url_for('borrar_pruebas') }}" onsubmit="return confirm('¿Borrar las {{ n_pruebas }} pruebas?')"><button class="peligro">Borrar las pruebas ({{ n_pruebas }})</button></form>{% endif %}
</div>
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
    {% if g.telegram == 'ok' %}<span class="chip on">Enviada a Telegram</span>{% endif %}
    <div class="ayuda">Publicar: {{ g.cuando }}</div>
    {% if g.aviso %}<div class="aviso">{{ g.aviso }}</div>{% endif %}
    {% if g.telegram and g.telegram != 'ok' %}<div class="aviso">Telegram no la ha recibido: {{ g.telegram }}</div>{% endif %}
    {% if g.estado == 'lista' %}
    <textarea readonly id="texto{{ g.id }}">{{ g.texto }}</textarea>
    <div class="fila">
      <a class="boton principal" href="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo, descargar=1) }}">Descargar imagen</a>
      <button type="button" onclick="copiar({{ g.id }}, this)">Copiar texto</button>
      <button type="button" class="compartir" hidden onclick="compartir('{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}', {{ g.id }})">Compartir</button>
      {% if hay_telegram %}<form class="enlinea" method="post" action="{{ url_for('enviar_telegram', gen_id=g.id) }}"><button>{{ 'Reenviar' if g.telegram == 'ok' else 'Enviar' }} a Telegram</button></form>{% endif %}
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
<div class="fila"><a class="boton" href="{{ url_for('editar_publicacion', plantilla='horario') }}">+ Horario de apertura</a>
<a class="boton" href="{{ url_for('editar_publicacion', plantilla='motor') }}">+ F1 y MotoGP</a>
<a class="boton" href="{{ url_for('editar_publicacion', plantilla='futbol') }}">+ Fútbol</a>
<a class="boton principal" href="{{ url_for('editar_publicacion') }}">+ Nueva publicación</a></div></div>
{% for p in filas %}
<div class="caja">
  <div class="fila" style="justify-content:space-between">
    <div>
      <b>{{ p.nombre }}</b>
      {% if p.activa %}<span class="chip on">Activa</span>{% else %}<span class="chip">Pausada</span>{% endif %}
      {% if p.modo == 'evento' %}<span class="chip">Eventos</span>{% endif %}
      {% if p.horario %}<span class="chip">Horario</span>{% endif %}
      {% if p.deportes %}<span class="chip">Deportes</span>{% endif %}
      {% if p.prompt_ia %}<span class="chip ia">IA</span>{% endif %}
      <div class="ayuda">{{ p.programacion }} · {{ p.formato_txt }} · fotos: {{ p.categoria }}</div>
      {% if p.proxima %}<div class="ayuda">Próxima: <b>{{ p.proxima }}</b> (la imagen se prepara el {{ p.genera }})</div>
      {% elif p.activa %}<div class="ayuda">{{ 'Sin eventos próximos.' if p.modo == 'evento' else 'Sin próximas fechas.' }}</div>{% endif %}
    </div>
    <div class="fila">
      <a class="boton" href="{{ url_for('editar_publicacion', pub_id=p.id) }}">Editar</a>
      <form class="enlinea" method="post" action="{{ url_for('generar_ahora', pub_id=p.id) }}"><button>Generar ahora</button></form>
      <form class="enlinea" method="post" action="{{ url_for('duplicar_publicacion', pub_id=p.id) }}"><button>Duplicar</button></form>
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
table.actividades { border-collapse:collapse; width:100%; min-width:520px; }
table.actividades th { color:var(--suave); font-size:13px; font-weight:normal; text-align:left; padding:4px; }
table.actividades td { padding:4px; }
pre.vista { background:#0d0e10; border:1px solid var(--borde); border-radius:8px; padding:10px; white-space:pre-wrap; font:inherit; }
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
    <div class="ayuda">Se publica sola para cada evento de la pestaña Eventos (los de Racecore, los de CKS y los creados a mano).</div>
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
    <label>¿Para qué eventos? <span class="ayuda">Si no marcas nada, para todos.</span></label>
    {% if campeonatos %}<div class="ayuda">Campeonatos (también sus carreras futuras):</div>
    <div class="opciones">{% for c in campeonatos %}<label><input type="checkbox" name="f_campeonato" value="{{ c }}" {{ 'checked' if c in filtro.campeonatos }}> {{ c }}</label>{% endfor %}</div>{% endif %}
    {% if sueltos %}<div class="ayuda" style="margin-top:8px">Eventos sueltos:</div>
    <div class="opciones">{% for n in sueltos %}<label><input type="checkbox" name="f_evento" value="{{ n }}" {{ 'checked' if n in filtro.eventos }}> {{ n }}</label>{% endfor %}</div>{% endif %}
    <label>O los que contengan este texto (opcional)</label>
    <input type="text" name="f_texto" value="{{ filtro.texto }}" placeholder="Ej: MOTO" autocomplete="off">
    <div class="aviso" id="aplica"></div>
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
  {% if categorias|length > 1 %}
  <label>Y también fotos de (opcional)</label>
  <div class="opciones">{% for c in categorias %}<label><input type="checkbox" name="categorias_extra" value="{{ c.id }}" {{ 'checked' if c.id|string in extras }}> {{ c.nombre }}</label>{% endfor %}</div>
  <div class="ayuda">Cada vez coge una foto al azar de todas las categorías marcadas.</div>
  {% endif %}
  <div class="dos">
    <div><label>Diseño</label>
      <select name="diseno">
        <option value="cartel" {{ 'selected' if pub.diseno not in ('sencillo', 'ia', 'lista') }}>Cartel (título grande, franja de color, datos y redes)</option>
        <option value="lista" {{ 'selected' if pub.diseno == 'lista' }}>Cartel con lista (nombres en el centro)</option>
        <option value="sencillo" {{ 'selected' if pub.diseno == 'sencillo' }}>Sencillo (texto abajo)</option>
        {% if hay_ia or pub.diseno == 'ia' %}<option value="ia" {{ 'selected' if pub.diseno == 'ia' }}>IA completa (la IA pone los textos; logo y redes, el panel)</option>{% endif %}
      </select></div>
    <div><label>Color de acento</label><input type="color" name="color" value="{{ pub.color }}"></div>
  </div>
  <div id="caja_lista">
    <label>Lista en el centro de la imagen</label>
    <textarea name="lista" placeholder="{pilotos}">{{ pub.lista }}</textarea>
    <div class="ayuda">Una línea por nombre. Con {pilotos} (o vacío, en publicaciones de eventos) salen los inscritos del evento. Las líneas que acaban en «:» salen en amarillo (categorías). Si son muchos, salen en 2, 3 o 4 columnas.</div>
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
  <label style="color:var(--texto); margin:0"><input type="checkbox" name="h_usar" value="1" id="h_usar" {{ 'checked' if usa_horario }}> <b>Horario de apertura</b> <span class="ayuda">(fines de semana, puentes, fiestas…)</span></label>
  <div id="caja_horario">
    <div class="opciones">
      <label><input type="radio" name="h_dias" value="finde" {{ 'checked' if horario.dias != 'fechas' }}> El fin de semana siguiente a la publicación (sábado y domingo)</label>
      <label><input type="radio" name="h_dias" value="fechas" {{ 'checked' if horario.dias == 'fechas' }}> Estas fechas</label>
    </div>
    <div class="dos" id="h_fechas">
      <div><label>Desde</label><input type="date" name="h_desde" value="{{ horario.desde or '' }}"></div>
      <div><label>Hasta</label><input type="date" name="h_hasta" value="{{ horario.hasta or '' }}"></div>
    </div>
    <div class="dos">
      <div><label>Apertura</label><input type="time" name="h_apertura" value="{{ horario.apertura }}"></div>
      <div><label>Cierre</label><input type="time" name="h_cierre" value="{{ horario.cierre }}"></div>
    </div>
    <label>Qué hay abierto (deja las horas vacías para usar las de apertura y cierre)</label>
    <div style="overflow-x:auto"><table class="actividades">
      <tr><th></th><th>Actividad</th><th>Desde</th><th>Hasta</th><th title="Si hay un evento, se quitan sus horas">Se corta con eventos</th></tr>
      {% for a in filas_h %}<tr>
        <td><input type="checkbox" name="h_activa_{{ loop.index0 }}" value="1" {{ 'checked' if a.activa }}></td>
        <td><input type="text" name="h_nombre_{{ loop.index0 }}" value="{{ a.nombre }}" placeholder="Otra actividad"></td>
        <td><input type="time" name="h_desde_{{ loop.index0 }}" value="{{ a.desde or '' }}"></td>
        <td><input type="time" name="h_hasta_{{ loop.index0 }}" value="{{ a.hasta or '' }}"></td>
        <td style="text-align:center"><input type="checkbox" name="h_evento_{{ loop.index0 }}" value="1" {{ 'checked' if a.evento }}></td>
      </tr>{% endfor %}
    </table></div>
    <div class="ayuda">Si esos días hay un evento (Racecore, CKS o a mano), se quitan las horas que ocupa. Lo que ocupa cada evento se elige en la pestaña <a href="{{ url_for('eventos') }}">Eventos</a> (normalmente, la mañana).</div>
    <div class="ayuda">Variables: {horario} (el horario día a día; con el diseño «Cartel con lista» sale en el centro), {dias_horario} («sábado 11 y domingo 12 de octubre») y {eventos_horario}.</div>
    {% if vista_horario %}<label>Así saldría la próxima vez</label><pre class="vista">{{ vista_horario }}</pre>{% endif %}
  </div>
</div>

<div class="caja">
  <label style="color:var(--texto); margin:0"><input type="checkbox" name="d_usar" value="1" id="d_usar" {{ 'checked' if usa_deportes }}> <b>Deportes en TV para la cafetería</b> <span class="ayuda">(horarios de Marca)</span></label>
  <div id="caja_deportes">
    <div class="opciones">
      <label><input type="checkbox" name="d_f1" value="1" {{ 'checked' if deportes.f1 }}> F1</label>
      <label><input type="checkbox" name="d_motogp" value="1" {{ 'checked' if deportes.motogp }}> MotoGP</label>
      <label><input type="checkbox" name="d_futbol" value="1" {{ 'checked' if deportes.futbol }}> Fútbol</label>
    </div>
    <div class="ayuda">De la F1 y MotoGP salen la clasificación, el sprint y la carrera (sin libres).</div>
    <div class="tres">
      <div><label>Equipos (fútbol)</label><input type="text" name="d_equipos" value="{{ deportes.equipos }}"></div>
      <div><label>Solo si lo da el canal</label><input type="text" name="d_canal" value="{{ deportes.canal }}" placeholder="Vacío = cualquiera"></div>
      <div><label>Sin estas competiciones</label><input type="text" name="d_excluir" value="{{ deportes.excluir }}" placeholder="Ej: Liga F"></div>
    </div>
    <div class="tres">
      <div><label>Días (desde el de la publicación)</label><input type="number" name="d_dias" min="1" max="14" value="{{ deportes.dias }}"></div>
      <div><label>Abre la cafetería</label><input type="time" name="d_apertura" value="{{ deportes.apertura }}"></div>
      <div><label>Cierra</label><input type="time" name="d_cierre" value="{{ deportes.cierre }}"></div>
    </div>
    <div class="ayuda">Solo sale lo que empieza después de abrir y acaba antes de cerrar (un partido dura unas 2 horas). Si no hay nada, ese día no se publica. La guía de TV de Marca solo trae hoy y mañana: para el fútbol, publica el mismo día (con antelación de un día como mucho).</div>
    <div class="ayuda">Variables: {deportes} (con «Cartel con lista» sale en el centro) y {dias_deportes}.</div>
    {% if vista_deportes %}<label>Así saldría la próxima vez</label><pre class="vista">{{ vista_deportes }}</pre>{% endif %}
  </div>
</div>

<div class="caja">
  <b>Texto del post</b>
  <textarea name="texto" style="min-height:140px" placeholder="El texto que pegarás en Instagram/Facebook">{{ pub.texto }}</textarea>
  <div class="ayuda" id="vars_normal">En título, subtítulo, pie y texto puedes usar {dia} (sábado), {fecha} (27 de septiembre) y {hora} (18:00).</div>
  <div class="ayuda"><b>Varias versiones:</b> escríbelas en la misma casilla separadas por una línea con <b>---</b> (tres guiones) y el panel las va turnando: la 1ª, luego la 2ª, la 3ª… Vale en Título, Franja y Texto del post, y van juntas: la 2ª versión del título sale con la 2ª del texto. «Generar ahora» pasa cada vez a la siguiente, para que las veas todas.</div>
  <div class="ayuda" id="vars_evento">En título, subtítulo, pie y texto puedes usar los datos del evento:
    {evento}, {campeonato}, {dia} {fecha} y {hora} (del evento), {dias} (los que faltan), {faltan} («en 5 días», «mañana», «hoy»),
    {inscritos}, {plazas}, {libres}, {precio}, {enlace} (inscripción), {web}, {horarios}, {resultados}, {ganadores} (para el pie del Cartel) y {pilotos} (nombres de los inscritos, uno por línea).</div>
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
  document.getElementById('caja_lista').hidden = document.querySelector('select[name=diseno]').value !== 'lista';
  document.getElementById('caja_horario').hidden = !document.getElementById('h_usar').checked;
  document.getElementById('caja_deportes').hidden = !document.getElementById('d_usar').checked;
  document.getElementById('h_fechas').hidden = !document.querySelector('input[name=h_dias][value=fechas]').checked;
}
document.querySelectorAll('input[name=modo], select[name=momento], select[name=diseno], #h_usar, input[name=h_dias], #d_usar')
  .forEach(r => r.addEventListener('change', mostrarModo));
// A qué eventos de ahora se aplica (igual que eventos_de() en el servidor)
const EVENTOS = {{ eventos_filtro|tojson }};
function aplica() {
  const marcados = n => [...document.querySelectorAll(`input[name=${n}]:checked`)].map(x => x.value.toLowerCase());
  const camp = marcados('f_campeonato'), evs = marcados('f_evento');
  const t = document.querySelector('input[name=f_texto]').value.trim().toLowerCase();
  const caja = document.getElementById('aplica');
  if (!camp.length && !evs.length && !t) { caja.textContent = 'Se aplica a todos los eventos.'; return; }
  const si = EVENTOS.filter(e => camp.includes(e.c.toLowerCase()) || evs.includes(e.n.toLowerCase())
                              || (t && (e.n + ' ' + e.c).toLowerCase().includes(t))).map(e => e.n);
  caja.textContent = si.length ? 'Ahora se aplica a: ' + si.join(', ') : 'Ahora no coincide con ningún evento.';
}
document.querySelectorAll('input[name=f_campeonato], input[name=f_evento], input[name=f_texto]')
  .forEach(x => { x.addEventListener('change', aplica); x.addEventListener('input', aplica); });
aplica();
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
{% if es_tandas %}<div class="caja">
  <b>Fotos automáticas de las tandas de CKS</b>
  <div class="ayuda">Llegan solas cada 10 minutos: solo tandas en las que todos los pilotos dieron permiso de imagen. Se borran solas a la semana.</div>
  {% if tandas.error %}<div class="aviso">{{ tandas.error }}</div>{% elif tandas.ok %}<div class="ayuda">Última lectura {{ tandas.ok }}{% if tandas.no_publicables %} · {{ tandas.no_publicables }} tanda(s) sin permiso de imagen{% endif %}.</div>{% endif %}
  <form method="post" action="{{ url_for('traer_tandas') }}" style="margin-top:8px"><button>Traer fotos ahora</button></form>
</div>{% endif %}
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
  <b>Racecore y CKS (para la pestaña Eventos)</b>
  <div class="ayuda">El panel lee los eventos por la red del circuito: solo lectura y solo lo necesario para las publicaciones.</div>
  {% for f in conexiones %}
  <div class="dos" style="margin-top:6px">
    <div><label>Dirección del PC de {{ f.nombre }}</label>
      <input type="text" name="{{ f.clave }}_url" value="{{ f.url_ajuste }}" placeholder="Ej: 192.168.1.50  (puerto {{ '8100' if f.clave == 'racecore' else '8090' }} si no pones otro)"></div>
    <div><label>Token de lectura de {{ f.nombre }} {% if f.token %}<span class="chip on">{{ f.token }}</span>{% endif %}</label>
      <input type="password" name="{{ f.clave }}_token" placeholder="{{ 'Déjalo vacío para no cambiarlo' if f.token else 'El que genera ' ~ f.nombre ~ ' en su configuración' }}" autocomplete="off">
      {% if f.token %}<label style="color:var(--texto); margin-top:6px"><input type="checkbox" name="quitar_{{ f.clave }}_token" value="1"> Quitar el token</label>{% endif %}</div>
  </div>
  {% endfor %}
  <label>Fotos de las tandas de alquiler (CKS)</label>
  <select name="tandas_categoria">
    <option value="">No traerlas</option>
    {% if not tandas_categoria %}<option value="nueva">Traerlas a una categoría nueva: «Tandas CKS»</option>{% endif %}
    {% for c in categorias %}<option value="{{ c.id }}" {{ 'selected' if c.id == tandas_categoria }}>Traerlas a la categoría «{{ c.nombre }}»</option>{% endfor %}
  </select>
  <div class="ayuda">Solo llegan fotos de tandas en las que <b>todos</b> los pilotos dieron permiso de imagen, y sin nombres. Se guardan una semana y se borran solas (también si alguien retira el permiso).</div>
  <label>Los eventos «de mañana» ocupan la pista hasta las (para el horario de apertura)</label>
  <input type="time" name="corte_eventos" value="{{ corte_eventos }}" style="max-width:160px">
  <label>Nombres de los pilotos en redes ({pilotos})</label>
  <select name="nombres_formato">
    <option value="completo" {{ 'selected' if nombres_formato != 'inicial' }}>Nombre y primer apellido (Ana Pérez)</option>
    <option value="inicial" {{ 'selected' if nombres_formato == 'inicial' }}>Nombre e inicial del apellido (Ana P.)</option>
  </select>
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
  <div class="ayuda">Si lo configuras, cada imagen programada te llega al móvil con su texto (las pruebas no).</div>
  <div class="ayuda">1) En Telegram, habla con <b>@BotFather</b>: /newbot, ponle un nombre y copia el token aquí. 2) Guarda. 3) Abre tu bot, mándale «hola» y pulsa «Detectar mi chat». 4) «Probar Telegram».</div>
  <label>Token del bot {% if telegram_token %}<span class="chip on">{{ telegram_token }}</span>{% endif %}</label>
  <input type="password" name="telegram_token" placeholder="{{ 'Déjalo vacío para no cambiarlo' if telegram_token else '123456:ABC...' }}" autocomplete="off">
  {% if telegram_token %}<label style="color:var(--texto)"><input type="checkbox" name="quitar_telegram_token" value="1"> Quitar el token</label>{% endif %}
  <label>Chat ID</label>
  <input type="text" name="telegram_chat" value="{{ telegram_chat }}">
</div>
<button class="principal">Guardar ajustes</button>
</form>
<div class="fila" style="margin-top:10px">
  <form class="enlinea" method="post" action="{{ url_for('detectar_telegram') }}"><button>Detectar mi chat</button></form>
  <form class="enlinea" method="post" action="{{ url_for('probar_telegram') }}"><button>Probar Telegram</button></form>
  <form class="enlinea" method="post" action="{{ url_for('leer_eventos') }}"><input type="hidden" name="volver" value="ajustes"><button>Probar Racecore y CKS</button></form>
  <form class="enlinea" method="post" action="{{ url_for('actualizar_deportes_ahora') }}"><button>Leer deportes de Marca</button></form>
</div>

<h2>Estado</h2>
<div class="caja">
  <div>Programador: {% if ultima %}<span class="chip on">funcionando</span> última comprobación {{ ultima }}{% else %}<span class="chip">arrancando…</span>{% endif %}</div>
  {% for f in conexiones %}
  <div>{{ f.nombre }}: {% if not f.configurado %}<span class="chip">sin configurar</span>
    {% elif f.error %}<span class="chip error">sin conexión</span> <span class="ayuda">{{ f.error }}</span>
    {% elif f.ok %}<span class="chip on">conectado</span> última lectura {{ f.ok }} · {{ f.n }} evento(s)
    {% else %}<span class="chip">pendiente</span>{% endif %}</div>
  {% endfor %}
  {% if tandas_categoria %}<div>Fotos de tandas: {% if tandas.error %}<span class="chip error">aviso</span> <span class="ayuda">{{ tandas.error }}</span>
    {% elif tandas.ok %}<span class="chip on">funcionando</span> última lectura {{ tandas.ok }} · {{ tandas.total }} foto(s) de la última semana{% if tandas.no_publicables %} · {{ tandas.no_publicables }} tanda(s) sin permiso{% endif %}
    {% else %}<span class="chip">pendiente</span>{% endif %}</div>{% endif %}
  <div>Deportes (Marca): {% for d in deportes %}{{ d.nombre }} {% if d.error %}<span class="chip error">fallo</span> <span class="ayuda">{{ d.error }}</span>{% elif d.ok %}<span class="chip on">{{ d.ok }}</span>{% else %}<span class="chip">sin leer</span>{% endif %}{{ ' · ' if not loop.last }}{% endfor %}
    <div class="ayuda">Se lee solo si hay alguna publicación activa con «Deportes en TV».</div></div>
  <div>Fuentes propias: {% if fuentes %}<span class="chip on">{{ fuentes|join(', ') }}</span>{% else %}<span class="chip">las incluidas</span> <span class="ayuda">opcional: Titulo.ttf y Texto.ttf en la carpeta «fuentes»</span>{% endif %}</div>
  <div class="ayuda" style="margin-top:8px">Carpeta del panel: {{ carpeta }}</div>
</div>
{% endblock %}"""

PLANTILLAS["eventos.html"] = """{% extends "base.html" %}
{% block contenido %}
<div class="fila" style="justify-content:space-between"><h1>Eventos</h1>
<div class="fila">
  <form class="enlinea" method="post" action="{{ url_for('leer_eventos') }}"><button>Actualizar ahora</button></form>
  <a class="boton" href="{{ url_for('editar_evento') }}">+ Evento a mano</a>
</div></div>
<div class="caja">
  {% for f in fuentes %}
  <div style="margin-bottom:6px"><b>{{ f.nombre }}</b>
  {% if not f.configurado %}<span class="chip">sin configurar</span> <span class="ayuda">pon su dirección y su token en <a href="{{ url_for('ajustes') }}">Ajustes</a></span>
  {% elif f.error %}<span class="chip error">sin conexión</span>
  <div class="aviso">{{ f.error }}{% if f.ok %} Se usan los datos leídos el {{ f.ok }}.{% endif %}</div>
  {% elif f.ok %}<span class="chip on">conectado</span> <span class="ayuda">última lectura {{ f.ok }} · {{ f.n }} evento(s)</span>
  {% else %}<span class="chip">leyendo…</span>{% endif %}</div>
  {% endfor %}
  <div class="ayuda">El panel los lee cada 10 minutos (también puedes crear eventos a mano). Solo guarda lo necesario para las publicaciones: nombre, fecha, horarios, plazas, inscritos, precio, enlaces, el podio y el nombre para redes de cada inscrito.</div>
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
      <span class="chip">{{ nombres_fuente.get(ev.origen, 'a mano') }}</span>
      {% if ev.pasado %}<span class="chip">ya pasó</span>{% endif %}
      {% if ev.cuando %}<div class="ayuda">{{ ev.cuando }}{% if ev.campeonato %} · {{ ev.campeonato }}{% endif %}</div>
      {% else %}<div class="aviso">{% if ev.fecha_txt %}No entiendo la fecha «{{ ev.fecha_txt }}»{% else %}No tiene fecha{% endif %}: para este evento no se programa nada.</div>{% endif %}
      <div class="ayuda">Inscritos: {{ ev.inscritos_txt }} · Nombres: {{ ev.n_nombres if ev.n_nombres else ('no llegan de ' ~ nombres_fuente[ev.origen] if ev.origen in nombres_fuente else '—') }} · Precio: {{ ev.precio or '—' }} · Inscripción {{ 'abierta' if ev.abierta else 'cerrada' }}</div>
      <form method="post" action="{{ url_for('fotos_evento', ev_id=ev.id) }}" class="fila" style="margin-top:6px">
        <span class="ayuda">Fotos:</span>
        <select name="categoria_id" onchange="this.form.submit()" style="width:auto">
          <option value="">las de cada publicación</option>
          {% for c in categorias %}<option value="{{ c.id }}" {{ 'selected' if c.id == ev.categoria_id }}>{{ c.nombre }}</option>{% endfor %}
        </select>
        <span class="ayuda">Ocupa la pista:</span>
        <select name="ocupa" onchange="this.form.submit()" style="width:auto">
          {% for clave, t in ocupa.items() %}<option value="{{ clave }}" {{ 'selected' if clave == (ev.ocupa or 'manana') }}>{{ t }}</option>{% endfor %}
        </select>
        <noscript><button>Guardar</button></noscript>
      </form>
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
{% endif %}{% if ev.pilotos %}
Nombres para redes:
{{ ev.pilotos }}
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
<div class="vacio">No hay eventos.<br>Conecta Racecore o CKS en Ajustes, o crea uno con «+ Evento a mano».</div>
{% endfor %}
{% if hay_reglas and faltan_ejemplos %}
<form method="post" action="{{ url_for('crear_ejemplos') }}" style="margin-top:20px">
  <button>Volver a crear las de ejemplo que faltan ({{ faltan_ejemplos }})</button>
</form>
{% endif %}
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
  <label>Inscritos (uno por línea, para {pilotos})</label>
  <textarea name="pilotos" placeholder="Ana Pérez&#10;Luis Gómez">{{ ev.pilotos }}</textarea>
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
