"""Fondo generado con IA (API de OpenAI) a partir de una foto del banco.

Solo toca el FONDO: el texto lo sigue superponiendo app.py con Pillow.
Si no hay prompt o la API falla (sin clave, sin saldo, timeout...) devuelve
None y app.py usa la foto original, así la publicación sale igualmente.

Prueba rápida de la API key (sin tocar el panel):
    python ia_fondo.py fotos/alguna.jpg "atardecer, ambiente de carrera" story
"""
import base64
import io
import logging
import os
import sys

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Reglas que se añaden SIEMPRE al prompt de cada publicación
REGLAS = """
Reglas obligatorias:
- Parte de la foto adjunta: mismo circuito de karting, mismos karts y ambiente. Resultado fotográfico y realista.
- NO añadas ningún texto, letra, número, logotipo, cartel legible ni marca de agua.
- Deja el tercio inferior de la imagen sencillo y con poco detalle: ahí se pondrá el texto después.
- Deja despejada la esquina superior derecha: ahí irá el logo.
""".strip()

FORMATOS = {
    "post": (1080, 1080),
    "vertical": (1080, 1350),
    "story": (1080, 1920),
}


def _tamano_api(modelo, ancho, alto):
    """Tamaño a pedir a la API con la misma proporción que el destino."""
    if modelo.startswith("gpt-image-1"):
        # Modelos antiguos: solo tamaños fijos
        return "1024x1024" if ancho == alto else ("1024x1536" if alto > ancho else "1536x1024")
    # gpt-image-2 y posteriores: cualquier WxH múltiplo de 16
    w = -(-ancho // 16) * 16
    h = round(w * alto / ancho / 16) * 16
    return f"{w}x{h}"


def _foto_como_jpeg(ruta_foto, lado_max=1536):
    """Foto orientada y reducida (no hace falta mandar 12 MP a la API)."""
    with Image.open(ruta_foto) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((lado_max, lado_max), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def generar_fondo(ruta_foto, prompt, ancho, alto):
    """Devuelve una PIL.Image RGB de ancho x alto, o None si no procede o falla."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    clave = os.environ.get("OPENAI_API_KEY", "").strip()
    if not clave:
        log.warning("IA: falta OPENAI_API_KEY en .env; uso la foto original")
        return None

    modelo = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2").strip()
    calidad = os.environ.get("OPENAI_IMAGE_QUALITY", "medium").strip()  # low / medium / high

    try:
        from openai import OpenAI

        cliente = OpenAI(api_key=clave, timeout=240, max_retries=1)
        resp = cliente.images.edit(
            model=modelo,
            image=("foto.jpg", _foto_como_jpeg(ruta_foto), "image/jpeg"),
            prompt=f"{prompt}\n\n{REGLAS}",
            size=_tamano_api(modelo, ancho, alto),
            quality=calidad,
        )
        datos = base64.b64decode(resp.data[0].b64_json)
        img = Image.open(io.BytesIO(datos)).convert("RGB")
        return ImageOps.fit(img, (ancho, alto), Image.LANCZOS)
    except Exception as e:  # nunca tumbar el programador por la IA
        log.warning("IA: fallo generando fondo (%s); uso la foto original", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass

    if len(sys.argv) < 3:
        sys.exit('Uso: python ia_fondo.py FOTO "PROMPT" [post|vertical|story]')
    formato = sys.argv[3] if len(sys.argv) > 3 else "post"
    if formato not in FORMATOS:
        sys.exit(f"Formato no válido: {formato} (usa post, vertical o story)")

    fondo = generar_fondo(sys.argv[1], sys.argv[2], *FORMATOS[formato])
    if fondo is None:
        sys.exit("No se generó el fondo (mira el aviso de arriba).")
    fondo.save("prueba_ia.png")
    print("OK -> prueba_ia.png")
