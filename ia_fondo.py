"""Imágenes con IA (API de imágenes de OpenAI) a partir de una foto del banco.

Dos usos:
- generar_fondo(): la IA solo rehace el FONDO y el texto lo pone app.py con Pillow.
- generar_cartel(): la IA hace el cartel entero CON los textos, dejando libres
  el hueco del logo y la barra de redes, que los pone app.py encima.

Si algo va mal lanzan una excepción con el motivo y app.py usa su diseño
propio, así la publicación sale igualmente.
"""
import base64
import colorsys
import io
import os

from PIL import Image, ImageOps

MODELO = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")

# Reglas que se añaden SIEMPRE al prompt cuando la IA solo hace el fondo
REGLAS = """
Reglas obligatorias:
- Parte de la foto adjunta: mismo circuito de karting, mismos karts y ambiente. Resultado fotográfico y realista.
- NO añadas ningún texto, letra, número, logotipo, cartel legible ni marca de agua.
- El protagonista (kart, piloto) en el centro de la imagen.
- Deja la franja de arriba y el tercio de abajo sencillos y con poco detalle: ahí irán el logo y el texto.
""".strip()


def nombre_color(hexa):
    """«#e8195a» -> «#e8195a (rosa fucsia)»: a la IA le ayuda tener el nombre además del código."""
    try:
        r, g, b = (int(hexa.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return hexa
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if s < 0.15:
        nombre = "blanco" if l > 0.85 else "negro" if l < 0.15 else "gris"
    else:
        tonos = [(15, "rojo"), (40, "naranja"), (65, "amarillo"), (160, "verde"), (200, "turquesa"),
                 (255, "azul"), (290, "morado"), (345, "rosa fucsia"), (360, "rojo")]
        nombre = next(n for limite, n in tonos if h * 360 <= limite)
    return f"{hexa} ({nombre})"


def _comillas(texto):
    return "«" + texto.replace("*", "") + "»"


def prompt_cartel(titulo, franja, datos, color, ambiente, zona_logo, zona_redes):
    """Instrucciones para que la IA haga el cartel con los textos exactos.

    titulo y franja: listas de líneas (lo que va entre *asteriscos* en color);
    datos: lista de textos cortos; zona_logo: (ancho %, alto %) libre arriba a la
    izquierda o None; zona_redes: alto % libre abajo o None.
    """
    p = [
        "Convierte la foto adjunta en un cartel publicitario para redes sociales de un circuito de karting.",
        "Mantén el mismo circuito, kart y piloto de la foto: tiene que seguir pareciendo una foto real, con mucho "
        "contraste, colores vivos, fondo algo desenfocado y el protagonista en el centro.",
        "Estilo: cartel de competición moderno. Tipografía sans serif muy gruesa, en cursiva y en MAYÚSCULAS, "
        "blanca con una sombra suave.",
        f"Color principal: {nombre_color(color)}. Detalles en amarillo.",
        "",
        "TEXTOS: escribe EXACTAMENTE estos textos, letra por letra, con sus tildes y signos. "
        "No añadas ni cambies ninguna palabra, número o precio, y no pongas ningún otro texto:",
    ]
    if titulo:
        p.append("- Título enorme en la parte de arriba, una línea por renglón:")
        for linea in titulo:
            marcado = [t for i, t in enumerate(linea.split("*")) if i % 2 == 1 and t.strip()]
            nota = f" (en color principal: {', '.join(_comillas(m) for m in marcado)})" if marcado else ""
            p.append(f"    {_comillas(linea)}{nota}")
        p.append("  Debajo del título, una raya fina inclinada del color principal.")
    if franja:
        p.append("- En la parte de abajo, una franja ancha e inclinada del color principal, como un brochazo de "
                 "pintura con los bordes rasgados, y dentro en blanco (la última línea más grande):")
        p += [f"    {_comillas(l)}" for l in franja]
    if datos:
        p.append("- Debajo de la franja, en letra más pequeña, cada dato con una barra vertical amarilla delante:")
        p += [f"    {_comillas(d)}" for d in datos]
    p.append("")
    if zona_logo:
        p.append(f"- Deja COMPLETAMENTE VACÍA la esquina de arriba a la izquierda (el {zona_logo[0]} % del ancho y el "
                 f"{zona_logo[1]} % del alto): ahí se pondrá el logo después. No dibujes ningún logotipo ni marca.")
    else:
        p.append("- No dibujes ningún logotipo ni marca.")
    if zona_redes:
        p.append(f"- Deja la franja de abajo del todo (el último {zona_redes} % del alto) oscura y SIN NADA: ahí se "
                 "pondrán las redes sociales después.")
    p.append("- No escribas redes sociales, webs, @, teléfonos ni iconos.")
    if ambiente.strip():
        p += ["", f"Ambiente de la foto: {ambiente.strip()}"]
    return "\n".join(p)


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


def _editar(ruta_foto, prompt, ancho, alto, clave, calidad, modelo):
    """Llama a la API y devuelve una PIL.Image RGB de ancho x alto. Lanza excepción si falla."""
    from openai import OpenAI

    cliente = OpenAI(api_key=clave, timeout=300, max_retries=1)
    resp = cliente.images.edit(
        model=modelo,
        image=("foto.jpg", _foto_como_jpeg(ruta_foto), "image/jpeg"),
        prompt=prompt,
        size=_tamano_api(modelo, ancho, alto),
        quality=calidad,
    )
    datos = base64.b64decode(resp.data[0].b64_json)
    img = Image.open(io.BytesIO(datos)).convert("RGB")
    return ImageOps.fit(img, (ancho, alto), Image.LANCZOS)


def generar_fondo(ruta_foto, prompt, ancho, alto, clave, calidad="medium", modelo=MODELO):
    return _editar(ruta_foto, f"{prompt.strip()}\n\n{REGLAS}", ancho, alto, clave, calidad, modelo)


def generar_cartel(ruta_foto, prompt, ancho, alto, clave, calidad="medium", modelo=MODELO):
    return _editar(ruta_foto, prompt, ancho, alto, clave, calidad, modelo)
