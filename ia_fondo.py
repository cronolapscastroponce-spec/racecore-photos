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


# Estilos para «IA completa». Con «Variado» se elige uno distinto cada vez.
ESTILOS = {
    "marca": ("Marca", "cartel de competición moderno como el de la marca: tipografía sans serif muy gruesa, en "
              "cursiva y en MAYÚSCULAS, blanca con sombra suave; las palabras destacadas en el color principal; una "
              "raya fina inclinada del color principal bajo el título; el mensaje destacado dentro de una franja "
              "inclinada del color principal, como un brochazo de pintura con los bordes rasgados; cada dato con una "
              "barra vertical amarilla delante. Foto con mucho contraste y colores vivos."),
    "neon": ("Neón nocturno", "ambiente nocturno con luces de neón y reflejos en el asfalto; los textos brillan como "
             "tubos de neón (en blanco y en el color principal) con resplandor; estética cyberpunk moderna, tonos "
             "oscuros con acentos de color intensos."),
    "retro": ("Retro años 80", "estética synthwave de los años 80: cielo en degradado morado y naranja, sol retro, "
              "rejilla en perspectiva, tipografía cromada y brillante con contorno; colores saturados."),
    "revista": ("Portada de revista", "portada de revista de motor: el título enorme queda en parte DETRÁS del piloto "
                "y del kart (efecto de profundidad), textos elegantes en blanco, composición limpia de fotografía "
                "profesional."),
    "velocidad": ("Velocidad", "sensación de velocidad extrema: desenfoque de movimiento en el fondo, líneas de "
                  "velocidad y estelas de luz del color principal; tipografía muy inclinada y dinámica."),
    "tv": ("Gráficos de TV", "gráficos de retransmisión de carreras tipo Fórmula 1: paneles geométricos "
           "semitransparentes, cortes en diagonal, tipografía técnica condensada, aspecto oficial y moderno."),
    "grunge": ("Urbano grunge", "estilo urbano y callejero: texturas de pintura y spray, salpicaduras, cinta "
               "adhesiva, tipografía estarcida o hecha a brocha; contraste alto y aspecto rebelde."),
    "vintage": ("Cartel vintage", "cartel de carreras de los años 60-70: aspecto de litografía, colores algo "
                "apagados, textura de papel viejo, tipografía clásica de rótulo antiguo."),
    "comic": ("Cómic", "estilo cómic y pop art: contornos negros marcados, tramas de puntos, colores planos muy vivos, "
              "tipografía de cómic gruesa."),
    "minimal": ("Minimalista", "diseño minimalista y elegante: mucho espacio limpio, tipografía sans serif grande y "
                "fina, muy pocos elementos, aspecto premium."),
}


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


def prompt_cartel(titulo, franja, datos, color, ambiente, zona_logo, zona_redes, estilo="marca",
                  lista=None, zona_lista=None):
    """Instrucciones para que la IA haga el cartel con los textos exactos.

    titulo y franja: listas de líneas (lo que va entre *asteriscos* en color);
    datos: lista de textos cortos; zona_logo: (ancho %, alto %) libre arriba a la
    izquierda o None; zona_redes: alto % libre abajo o None; estilo: clave de ESTILOS;
    lista: líneas de un horario que escribe la IA, o zona_lista: (desde %, hasta %) del
    alto que se deja vacío en el centro porque el horario lo pone después app.py.
    """
    p = [
        "Convierte la foto adjunta en un cartel publicitario para redes sociales de un circuito de karting, "
        "con acabado profesional.",
        "Mantén el mismo circuito, kart y piloto de la foto (que se reconozcan), con el protagonista en el centro; "
        "la luz, el ambiente y el tratamiento de la imagen pueden cambiar según el estilo.",
        f"ESTILO: {ESTILOS.get(estilo, ESTILOS['marca'])[1]}",
        f"Color principal de la marca: {nombre_color(color)}. Úsalo en los elementos destacados.",
        "",
        "TEXTOS: escribe EXACTAMENTE estos textos, letra por letra, con sus tildes y signos. "
        "No añadas ni cambies ninguna palabra, número o precio, y no pongas ningún otro texto:",
    ]
    if titulo:
        p.append("- Título principal, grande y protagonista, una línea por renglón:")
        for linea in titulo:
            marcado = [t for i, t in enumerate(linea.split("*")) if i % 2 == 1 and t.strip()]
            nota = f" (resalta en el color principal: {', '.join(_comillas(m) for m in marcado)})" if marcado else ""
            p.append(f"    {_comillas(linea)}{nota}")
    if franja:
        p.append("- Mensaje destacado, muy visible, en la parte de abajo (la última línea más grande):")
        p += [f"    {_comillas(l)}" for l in franja]
    if datos:
        p.append("- Datos, en letra más pequeña, cerca del mensaje destacado:")
        p += [f"    {_comillas(d)}" for d in datos]
    if lista:
        p.append("- Un horario en el centro, muy legible (lista o tabla limpia sobre un fondo que haga contraste), "
                 "con estas líneas EXACTAS y en este orden. Copia cada hora dígito a dígito. Las que acaban en «:» "
                 "son los días: van destacadas.")
        p += [f"    {_comillas(l)}" for l in lista]
    p.append("")
    if zona_lista:
        p.append(f"- Deja VACÍA y oscura la zona central, entre el {zona_lista[0]} % y el {zona_lista[1]} % del alto, "
                 "de lado a lado: sin texto ni elementos importantes, ahí se pondrá después un horario. El título va "
                 f"arriba, por encima del {zona_lista[0]} %, y el mensaje destacado y los datos, abajo, por debajo del "
                 f"{zona_lista[1]} %.")
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
