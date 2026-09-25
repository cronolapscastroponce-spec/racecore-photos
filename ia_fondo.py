"""Fondo generado con IA (API de imágenes de OpenAI) a partir de una foto del banco.

Solo toca el FONDO: el texto lo superpone app.py con Pillow (la IA falla con
precios y fechas). Si algo va mal lanza una excepción con el motivo y app.py
usa la foto original, así la publicación sale igualmente.
"""
import base64
import io
import os

from PIL import Image, ImageOps

MODELO = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")

# Reglas que se añaden SIEMPRE al prompt de cada publicación
REGLAS = """
Reglas obligatorias:
- Parte de la foto adjunta: mismo circuito de karting, mismos karts y ambiente. Resultado fotográfico y realista.
- NO añadas ningún texto, letra, número, logotipo, cartel legible ni marca de agua.
- El protagonista (kart, piloto) en el centro de la imagen.
- Deja la franja de arriba y el tercio de abajo sencillos y con poco detalle: ahí irán el logo y el texto.
""".strip()


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


def generar_fondo(ruta_foto, prompt, ancho, alto, clave, calidad="medium", modelo=MODELO):
    """Devuelve una PIL.Image RGB de ancho x alto. Lanza excepción si falla."""
    from openai import OpenAI

    cliente = OpenAI(api_key=clave, timeout=240, max_retries=1)
    resp = cliente.images.edit(
        model=modelo,
        image=("foto.jpg", _foto_como_jpeg(ruta_foto), "image/jpeg"),
        prompt=f"{prompt.strip()}\n\n{REGLAS}",
        size=_tamano_api(modelo, ancho, alto),
        quality=calidad,
    )
    datos = base64.b64decode(resp.data[0].b64_json)
    img = Image.open(io.BytesIO(datos)).convert("RGB")
    return ImageOps.fit(img, (ancho, alto), Image.LANCZOS)
