"""Panel de redes de Racecore.

Banco de fotos por categorías, publicaciones programadas y un programador que
deja la imagen (foto + texto superpuesto) y el texto del post listos a su hora.
Tú publicas a mano; el panel solo prepara.

Arranque: doble clic en 2_probar.bat  (o: .venv\\Scripts\\python app.py --abrir)
Panel:    http://localhost:5000
"""
import logging
import os
import random
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

import requests
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from jinja2 import DictLoader
from PIL import Image, ImageDraw, ImageFont, ImageOps

import ia_fondo

BASE = Path(__file__).resolve().parent
DB = BASE / "racecore.db"
DIR_FOTOS = BASE / "fotos"
DIR_MINIS = BASE / "miniaturas"
DIR_GEN = BASE / "generadas"
DIR_FUENTES = BASE / "fuentes"
LOGO = BASE / "logo.png"
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

# Ajustes editables desde el panel (si están vacíos se mira la variable de entorno)
AJUSTES_ENV = {
    "openai_key": "OPENAI_API_KEY",
    "openai_calidad": "OPENAI_IMAGE_QUALITY",
    "telegram_token": "TELEGRAM_TOKEN",
    "telegram_chat": "TELEGRAM_CHAT_ID",
}

log = logging.getLogger("racecore")
ESTADO = {"ultima_comprobacion": None}
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
    prompt_ia TEXT NOT NULL DEFAULT '');
-- estado: generando | lista | publicada | descartada | error
CREATE TABLE IF NOT EXISTS generadas (
    id INTEGER PRIMARY KEY, publicacion_id INTEGER NOT NULL, ocurrencia TEXT NOT NULL,
    prueba INTEGER NOT NULL DEFAULT 0, estado TEXT NOT NULL DEFAULT 'generando',
    archivo TEXT, texto TEXT NOT NULL DEFAULT '', foto_id INTEGER,
    con_ia INTEGER NOT NULL DEFAULT 0, aviso TEXT NOT NULL DEFAULT '', creada TEXT NOT NULL);
-- una sola imagen programada por publicación y hora (las pruebas no cuentan)
CREATE UNIQUE INDEX IF NOT EXISTS generadas_unica ON generadas(publicacion_id, ocurrencia) WHERE prueba = 0;
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
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
    occ = ocurrencias(pub, ahora, ahora + timedelta(days=8))
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
    if pub["modo"] == "fecha":
        try:
            fecha = date.fromisoformat(pub["fecha"]).strftime("%d/%m/%Y")
        except ValueError:
            fecha = "sin fecha"
        return f"{fecha} · {pub['hora']}"
    dias = [DIAS_CORTOS[int(x)] for x in pub["dias"].split(",") if x.strip().isdigit()]
    return f"{' '.join(dias) or 'ningún día'} · {pub['hora']}"


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


def superponer(img, pub, occ):
    """Título, subtítulo, pie (etiqueta de color) y logo sobre el fondo."""
    W, H = img.size
    d = ImageDraw.Draw(img)
    acento = color_rgb(pub["color"])
    story = H / W > 1.6
    margen = int(W * 0.07)
    base = H - (int(H * 0.14) if story else margen)   # stories: libre la zona de la interfaz
    ancho_txt = W - 2 * margen

    titulo = variables(pub["titulo"], occ).strip()
    sub = variables(pub["subtitulo"], occ).strip()
    pie = variables(pub["pie"], occ).strip()

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


def elegir_foto(pub_id, fotos, evitar=None):
    """Una al azar, sin repetir «evitar» ni, si no se indica, la última usada en esta publicación."""
    if not fotos:
        return None
    if evitar is None:
        ultima = consulta("SELECT foto_id FROM generadas WHERE publicacion_id = ? AND foto_id IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1", (pub_id,))
        evitar = ultima[0]["foto_id"] if ultima else None
    return random.choice([f for f in fotos if f["id"] != evitar] or fotos)


def componer(pub, occ, evitar=None):
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
        if pub["prompt_ia"].strip():
            clave = ajuste("openai_key")
            if not clave:
                avisos.append("IA: falta la clave de OpenAI (menú Ajustes). Se ha usado la foto original.")
            else:
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
    return img, (foto["id"] if foto else None), con_ia, " ".join(avisos)


# ---------------------------------------------------------------- generación y programador

def reclamar(pub_id, occ, prueba):
    """Crea la fila «generando». Devuelve su id, o None si esa hora ya estaba generada."""
    cur = ejecutar("INSERT OR IGNORE INTO generadas (publicacion_id, ocurrencia, prueba, creada) "
                   "VALUES (?, ?, ?, ?)", (pub_id, occ.strftime("%Y-%m-%d %H:%M"), int(prueba), ahora_txt()))
    return cur.lastrowid if cur.rowcount else None


def generar(gen_id, pub, occ, enviar=False):
    try:
        anterior = consulta("SELECT archivo, foto_id FROM generadas WHERE id = ?", (gen_id,))
        img, foto_id, con_ia, aviso = componer(pub, occ, anterior[0]["foto_id"] if anterior else None)
        archivo = f"{pub['id']}_{occ:%Y%m%d_%H%M}_{gen_id}_{uuid.uuid4().hex[:6]}.jpg"
        img.save(DIR_GEN / archivo, "JPEG", quality=92, optimize=True)
        texto = variables(pub["texto"], occ)
        cur = ejecutar("UPDATE generadas SET estado = 'lista', archivo = ?, texto = ?, foto_id = ?, con_ia = ?, "
                       "aviso = ? WHERE id = ? AND estado = 'generando'",
                       (archivo, texto, foto_id, int(con_ia), aviso, gen_id))
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
    for pub in consulta("SELECT * FROM publicaciones WHERE activa = 1"):
        antelacion = timedelta(minutes=max(0, pub["antelacion"]))
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
    while True:
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
        SELECT g.*, p.nombre FROM generadas g LEFT JOIN publicaciones p ON p.id = g.publicacion_id
        WHERE g.estado IN ('generando', 'lista', 'error') ORDER BY g.ocurrencia, g.id""")
    hechas = consulta("""
        SELECT g.*, p.nombre FROM generadas g LEFT JOIN publicaciones p ON p.id = g.publicacion_id
        WHERE g.estado = 'publicada' ORDER BY g.ocurrencia DESC LIMIT 12""")

    def preparar(g):
        occ = datetime.strptime(g["ocurrencia"], "%Y-%m-%d %H:%M")
        return dict(g) | {"cuando": cuando_txt(occ), "nombre": g["nombre"] or "(publicación borrada)"}

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
    ejecutar("UPDATE generadas SET estado = 'generando', aviso = '' WHERE id = ?", (gen_id,))
    en_segundo_plano(gen_id, pub[0], datetime.strptime(g["ocurrencia"], "%Y-%m-%d %H:%M"))
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
         "titulo": "", "subtitulo": "", "pie": "", "texto": "", "color": "#e10600", "prompt_ia": ""}


@app.route("/publicaciones")
def publicaciones():
    cats = {c["id"]: c["nombre"] for c in consulta("SELECT * FROM categorias")}
    filas = []
    for p in consulta("SELECT * FROM publicaciones ORDER BY activa DESC, nombre"):
        occ = proxima(p) if p["activa"] else None
        genera = occ - timedelta(minutes=p["antelacion"]) if occ else None
        filas.append(dict(p) | {
            "programacion": resumen_programacion(p),
            "categoria": cats.get(p["categoria_id"], "sin categoría"),
            "formato_txt": FORMATOS.get(p["formato"], FORMATOS["post"])[0],
            "proxima": cuando_txt(occ) if occ else "",
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
    color = f.get("color", "#e10600").strip()
    if not (len(color) == 7 and color.startswith("#")):
        color = "#e10600"
    datos = {
        "nombre": f.get("nombre", "").strip() or "Sin nombre",
        "activa": 1 if f.get("activa") else 0,
        "modo": "fecha" if f.get("modo") == "fecha" else "semanal",
        "dias": ",".join(dias),
        "fecha": f.get("fecha", "").strip(),
        "hora": hora,
        "antelacion": antelacion,
        "categoria_id": categoria_id,
        "formato": f.get("formato") if f.get("formato") in FORMATOS else "post",
        "titulo": f.get("titulo", "").strip(),
        "subtitulo": f.get("subtitulo", "").strip(),
        "pie": f.get("pie", "").strip(),
        "texto": f.get("texto", "").strip(),
        "color": color,
        "prompt_ia": f.get("prompt_ia", "").strip(),
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
                           dias_marcados=set(str(pub["dias"]).split(",")))


@app.post("/publicaciones/<int:pub_id>/generar")
def generar_ahora(pub_id):
    pub = pub_o_404(pub_id)
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
        for clave in ("openai_key", "telegram_token"):
            nuevo = request.form.get(clave, "").strip()
            if request.form.get(f"quitar_{clave}"):
                guardar_ajuste(clave, "")
            elif nuevo:
                guardar_ajuste(clave, nuevo)
        guardar_ajuste("telegram_chat", request.form.get("telegram_chat", "").strip())
        calidad = request.form.get("openai_calidad", "medium")
        guardar_ajuste("openai_calidad", calidad if calidad in ("low", "medium", "high") else "medium")
        flash("Ajustes guardados.", "ok")
        return redirect(url_for("ajustes"))

    def oculta(clave):
        v = ajuste(clave)
        return f"configurada (…{v[-4:]})" if v else ""

    ultima = ESTADO["ultima_comprobacion"]
    return render_template(
        "ajustes.html", openai_key=oculta("openai_key"), telegram_token=oculta("telegram_token"),
        telegram_chat=ajuste("telegram_chat"), calidad=ajuste("openai_calidad", "medium"),
        ultima=ultima.strftime("%H:%M:%S") if ultima else "", carpeta=BASE, hay_logo=LOGO.exists(),
        fuentes=[n for n in ("Titulo.ttf", "Texto.ttf") if (DIR_FUENTES / n).exists()])


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
    <b>{{ g.nombre }}</b>
    {% if g.prueba %}<span class="chip prueba">Prueba</span>{% endif %}
    {% if g.con_ia %}<span class="chip ia">IA</span>{% endif %}
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
{% for g in hechas %}<a href="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" target="_blank" title="{{ g.nombre }} · {{ g.cuando }}"><img loading="lazy" src="{{ url_for('archivo', carpeta='generadas', nombre=g.archivo) }}" alt=""></a>{% endfor %}
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
      {% if p.prompt_ia %}<span class="chip ia">IA</span>{% endif %}
      <div class="ayuda">{{ p.programacion }} · {{ p.formato_txt }} · fotos: {{ p.categoria }}</div>
      {% if p.proxima %}<div class="ayuda">Próxima: <b>{{ p.proxima }}</b> (la imagen se prepara el {{ p.genera }})</div>
      {% elif p.activa %}<div class="ayuda">Sin próximas fechas.</div>{% endif %}
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
    <label><input type="radio" name="modo" value="semanal" {{ 'checked' if pub.modo != 'fecha' }} onchange="modo()"> Días de la semana</label>
    <label><input type="radio" name="modo" value="fecha" {{ 'checked' if pub.modo == 'fecha' }} onchange="modo()"> Fecha concreta</label>
  </div>
  <div id="semanal" class="dias">
    {% for d in dias %}<label><input type="checkbox" name="dias" value="{{ loop.index0 }}" {{ 'checked' if loop.index0|string in dias_marcados }}> {{ d }}</label>{% endfor %}
  </div>
  <div id="fecha"><label>Fecha</label><input type="date" name="fecha" value="{{ pub.fecha }}"></div>
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
  <label>Título</label><input type="text" name="titulo" value="{{ pub.titulo }}" placeholder="Ej: Tanda nocturna">
  <label>Subtítulo</label><input type="text" name="subtitulo" value="{{ pub.subtitulo }}" placeholder="Ej: Este {dia} a las {hora}">
  <div class="dos">
    <div><label>Pie (etiqueta de color)</label><input type="text" name="pie" value="{{ pub.pie }}" placeholder="Ej: 25 € · Reserva ya"></div>
    <div><label>Color de acento</label><input type="color" name="color" value="{{ pub.color }}"></div>
  </div>
  <label>Prompt IA (opcional)</label>
  <textarea name="prompt_ia" placeholder="Vacío = la foto tal cual. Ej: atardecer dorado, luz cálida, ambiente de competición">{{ pub.prompt_ia }}</textarea>
  <div class="ayuda">La IA solo cambia el fondo a partir de una de tus fotos. El texto lo pone siempre el panel.</div>
  <div class="ayuda">En título, subtítulo y pie no uses emojis: en la imagen salen como cuadrados. En el texto del post sí.</div>
</div>

<div class="caja">
  <b>Texto del post</b>
  <textarea name="texto" style="min-height:140px" placeholder="El texto que pegarás en Instagram/Facebook">{{ pub.texto }}</textarea>
  <div class="ayuda">En título, subtítulo, pie y texto puedes usar {dia} (sábado), {fecha} (27 de septiembre) y {hora} (18:00).</div>
</div>

<div class="fila">
  <button class="principal">Guardar</button>
  <button name="y_generar" value="1">Guardar y generar prueba</button>
  <a class="boton" href="{{ url_for('publicaciones') }}">Cancelar</a>
</div>
</form>
<script>
function modo() {
  const fecha = document.querySelector('input[name=modo][value=fecha]').checked;
  document.getElementById('fecha').hidden = !fecha;
  document.getElementById('semanal').hidden = fecha;
}
modo();
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
<form method="post">
<div class="caja">
  <b>IA (OpenAI)</b>
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
<form method="post" action="{{ url_for('probar_telegram') }}" style="margin-top:10px"><button>Probar Telegram</button></form>

<h2>Estado</h2>
<div class="caja">
  <div>Programador: {% if ultima %}<span class="chip on">funcionando</span> última comprobación {{ ultima }}{% else %}<span class="chip">arrancando…</span>{% endif %}</div>
  <div>Logo: {% if hay_logo %}<span class="chip on">logo.png encontrado</span>{% else %}<span class="chip">no hay</span> <span class="ayuda">pon un archivo logo.png en la carpeta del panel</span>{% endif %}</div>
  <div>Fuentes propias: {% if fuentes %}<span class="chip on">{{ fuentes|join(', ') }}</span>{% else %}<span class="chip">las de Windows</span> <span class="ayuda">opcional: Titulo.ttf y Texto.ttf en la carpeta «fuentes»</span>{% endif %}</div>
  <div class="ayuda" style="margin-top:8px">Carpeta del panel: {{ carpeta }}</div>
</div>
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
