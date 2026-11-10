"""Las imágenes de una unidad que llevan texto: inventario y lectura (punto 6).

Ninguna imagen de las unidades de muestra es SVG, así que no hay texto que se pueda editar
como texto: está pintado. El primer paso es saber cuáles lo llevan y de qué tipo, porque
de eso depende qué se puede rehacer solo y qué va a diseño:

| Tipo | Qué se hace |
|---|---|
| `rotulos_fondo_liso` | Se rehace automáticamente: borrar el rótulo y escribir la traducción |
| `rotulos_sobre_dibujo` | A diseño en la primera iteración: borrar sobre un dibujo deja manchas |
| `texto_en_foto` | A diseño |
| territorial | A diseño aunque sea de fondo liso: no basta con traducir, hay que rehacerla |
| `sin_texto`, `no_traducible` | Nada |

La lectura la hace el asistente y se guarda por paquete: sirve para todos los trabajos
que salgan de él y no se repite.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from . import bd

MINIMO = (150, 100)          # por debajo, iconos y viñetas
MAXIMO_PX = 1400             # lo que se envía a leer; la imagen original no se toca
TIPOS_AUTOMATICOS = {"rotulos_fondo_liso"}


def _ruta(unidad: Path) -> Path:
    return bd.DATOS / "lecturas" / unidad.name / "imagenes_unidad.json"


def lecturas(unidad: Path) -> dict[str, dict]:
    """Lo leído de las imágenes de la unidad: huella del fichero → lectura."""
    ruta = _ruta(unidad)
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def guardar(unidad: Path, datos: dict[str, dict]) -> None:
    ruta = _ruta(unidad)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


def inventario(unidad: Path) -> list[dict]:
    """Las imágenes que usa la unidad, sin repetir y sin las muy pequeñas.

    Una imagen «la usa» la unidad si su nombre aparece en `components.json`. Se identifica
    por la huella de su contenido: la misma imagen con dos nombres se lee una vez.
    """
    from PIL import Image

    if (unidad / "components.json").is_file():
        componentes = (unidad / "components.json").read_text(encoding="utf-8")
    else:  # fuente de Author: los nombres están en los XML de la unidad y sus actividades
        from maker_paquete import abrir_unidad
        componentes = abrir_unidad(unidad).texto_xml()
    vistas, resultado = set(), []
    for f in sorted((unidad / "resources").rglob("*")):
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp") or f.name not in componentes:
            continue
        datos = f.read_bytes()
        huella = hashlib.sha1(datos).hexdigest()[:16]
        if huella in vistas:
            continue
        try:
            with Image.open(BytesIO(datos)) as im:
                ancho, alto = im.size
        except Exception:
            continue
        if ancho < MINIMO[0] or alto < MINIMO[1]:
            continue
        vistas.add(huella)
        resultado.append({"fichero": str(f.relative_to(unidad)).replace("\\", "/"),
                          "huella": huella, "ancho": ancho, "alto": alto})
    return resultado


def _huella_visual(ruta: Path) -> int | None:
    """dHash de 16×16: el mismo dibujo a otro tamaño o recomprimido da casi lo mismo."""
    from PIL import Image
    try:
        with Image.open(ruta) as im:
            g = im.convert("L").resize((17, 16), Image.LANCZOS)
    except Exception:
        return None
    px = list(g.getdata())
    bits = 0
    for y in range(16):
        for x in range(16):
            bits = (bits << 1) | (px[y * 17 + x] > px[y * 17 + x + 1])
    return bits


def _iguales(a: Path, b: Path) -> bool:
    """Si dos imágenes son la misma figura, píxel a píxel, al tamaño de la pequeña.

    El dHash no basta: la versión del alumno y la del profesor de un mismo esquema dan
    casi la misma huella, y en la del profesor van las respuestas. Se compara a 200 px de
    ancho con un desenfoque ligero, que iguala el suavizado de las líneas finas a tamaños
    distintos. Medido en «As plantas» y «A Prehistoria»: las copias de verdad dan entre el
    0 y el 0,3 % de píxeles muy distintos; alumno y profesor, el 1,2 %.
    """
    import numpy as np
    from PIL import Image, ImageFilter
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            alto = max(1, round(200 * ia.height / ia.width))

            def gris(im):
                return np.asarray(im.convert("L").resize((200, alto), Image.LANCZOS)
                                  .filter(ImageFilter.GaussianBlur(1)), dtype=np.int16)
            ga, gb = gris(ia), gris(ib)
    except Exception:
        return False
    return float((np.abs(ga - gb) > 64).mean()) <= 0.005


def grupos(unidad: Path, imagenes: list[dict] | None = None) -> dict[str, list[str]]:
    """Las copias de una misma imagen: huella de la principal → huellas de todas.

    Las unidades llevan la misma figura varias veces, a distinto tamaño y en carpetas
    distintas (la galería ampliada, la lección, la versión de impresión), y cada sitio cita
    la suya. Son ficheros distintos, pero para el editor son una sola imagen. Se agrupan
    por parecido visual (dHash) y proporción; la principal es la más grande, que es la que
    mejor se rehace y de la que se sacan las demás.
    """
    imagenes = imagenes if imagenes is not None else inventario(unidad)
    datos = [(i, _huella_visual(unidad / i["fichero"])) for i in imagenes]
    datos.sort(key=lambda d: -(d[0]["ancho"] * d[0]["alto"]))
    resultado: dict[str, list[str]] = {}
    principales: list[tuple[dict, int]] = []
    for imagen, dh in datos:
        proporcion = imagen["ancho"] / imagen["alto"]
        sitio = None
        if dh is not None:
            for p, pdh in principales:
                if (abs(p["ancho"] / p["alto"] - proporcion) <= 0.02 * proporcion
                        and bin(pdh ^ dh).count("1") <= 24
                        and _iguales(unidad / p["fichero"], unidad / imagen["fichero"])):
                    sitio = p["huella"]
                    break
        if sitio:
            resultado[sitio].append(imagen["huella"])
        else:
            resultado[imagen["huella"]] = [imagen["huella"]]
            if dh is not None:
                principales.append((imagen, dh))
    return resultado


def para_leer(unidad: Path, imagen: dict) -> bytes:
    """La imagen en PNG, reducida a lo que hace falta para leerla y con fondo blanco."""
    from PIL import Image

    with Image.open(unidad / imagen["fichero"]) as im:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            fondo = Image.new("RGB", im.size, "white")
            fondo.paste(im, mask=im.split()[-1])
            im = fondo
        else:
            im = im.convert("RGB")
        im.thumbnail((MAXIMO_PX, MAXIMO_PX))
        salida = BytesIO()
        im.save(salida, format="PNG")
        return salida.getvalue()


def destino(lectura: dict) -> str:
    """Qué se hace con una imagen según su lectura: «nada», «automático» o «diseño»."""
    if lectura.get("tipo") in ("sin_texto", "no_traducible") and not lectura.get("territorial"):
        return "nada"
    if lectura.get("territorial"):
        return "diseño"
    return "automático" if lectura.get("tipo") in TIPOS_AUTOMATICOS else "diseño"


def huella_fichero(ruta: Path) -> str:
    return hashlib.sha1(ruta.read_bytes()).hexdigest()[:16]


def _ruta_comprobaciones(unidad: Path) -> Path:
    return bd.DATOS / "lecturas" / unidad.name / "comprobaciones_imagenes.json"


def comprobaciones(unidad: Path) -> dict[str, dict]:
    """Las relecturas de imágenes rehechas, por huella de la imagen rehecha.

    La reescritura es determinista: la misma imagen con las mismas traducciones da el
    mismo fichero, así que su comprobación vale mientras no cambie.
    """
    ruta = _ruta_comprobaciones(unidad)
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def guardar_comprobaciones(unidad: Path, datos: dict[str, dict]) -> None:
    ruta = _ruta_comprobaciones(unidad)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")


def veredicto(comprobacion: dict) -> dict:
    """Qué dice la relectura, calculado en código: «bien», «revisar» o «mal».

    Mal si un rótulo no se lee como debe o quedan restos del idioma de origen; revisar si
    solo hay defectos visuales. La coincidencia la mide el código, no el modelo.
    """
    from difflib import SequenceMatcher
    fallos = [l for l in comprobacion.get("leidos", [])
              if SequenceMatcher(None, _normal(l.get("leido", "")), _normal(l.get("esperado", ""))).ratio() < 0.85]
    restos = [r for r in comprobacion.get("restos_origen", []) if r.strip()]
    defectos = [d for d in comprobacion.get("defectos", []) if d.strip()]
    estado = "mal" if fallos or restos else "revisar" if defectos else "bien"
    return {"estado": estado, "fallos": fallos, "restos": restos, "defectos": defectos}


def rotulos_validos(lectura: dict) -> list[dict]:
    """Los rótulos que son texto de verdad: al menos dos letras.

    En «Los Invertebrados» la abeja tiene un rótulo «Á», cortado en el propio original
    (debería decir «Ás», alas). No hay nada que traducir ahí: decide una persona.
    """
    import re
    return [r for r in lectura.get("rotulos", [])
            if len(re.findall(r"[^\W\d_]", texto_rotulo(r))) >= 2]


def texto_rotulo(rotulo: dict) -> str:
    """El texto de un rótulo tal como se traduce: sus líneas unidas."""
    return " ".join(" ".join(rotulo.get("lineas", [])).split())


# ------------------------------------------------------------------- localizar

_LENGUAS_OCR = {"gl": "glg", "es": "spa", "ca": "cat", "en": "eng", "eu": "eus"}


def _normal(texto: str) -> str:
    import re
    return re.sub(r"[\W_]+", "", (texto or "").lower())


def _ocr(ruta: Path, idioma: str, pagina: bool = False) -> list[dict]:
    """Las palabras que ve Tesseract, con su caja en píxeles de la imagen original.

    Se amplía antes: en una imagen de 310 px Tesseract no leía casi nada. Con `pagina`, solo
    la pasada de segmentación automática de página (ver `localizar`).
    """
    import csv
    import io
    import subprocess
    import tempfile

    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(ruta) as im:
        im = im.convert("RGB")
    factor = max(1.0, min(4.0, 1800 / max(im.size)))
    if factor > 1:
        im = im.resize((int(im.width * factor), int(im.height * factor)), Image.LANCZOS)
    palabras = []
    # Dos pasadas: la imagen tal cual y en negativo. Tesseract lee mal el texto claro sobre
    # fondo de color —la banda de flechas de Redi, Spallanzani y Pasteur—, y en negativo
    # pasa a ser texto oscuro sobre claro.
    # Y una tercera con solo los píxeles blancos, en negro sobre blanco: el texto blanco
    # sobre una banda de color («Cabalo moderno» sobre verde) no salía en ninguna de las dos.
    a = np.asarray(im).astype(np.int16)
    blancos = (a.min(axis=2) > 175) & ((a.max(axis=2) - a.min(axis=2)) < 45)
    solo_blancos = Image.fromarray(np.where(blancos, 0, 255).astype(np.uint8))
    pasadas = ((im, "11"), (ImageOps.invert(ImageOps.grayscale(im)), "11"), (solo_blancos, "11"))
    if pagina:
        pasadas = ((im, "3"),)
    for pasada, (version, psm) in enumerate(pasadas, start=3 if pagina else 0):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            version.save(tmp.name)
        try:
            salida = subprocess.run(["tesseract", tmp.name, "-", "-l", _LENGUAS_OCR.get(idioma, "spa"),
                                     "--psm", psm, "tsv"], capture_output=True, text=True,
                                    timeout=180).stdout
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        for r in csv.DictReader(io.StringIO(salida), delimiter="\t"):
            # Los signos sueltos («|» es el borde de un recuadro) no cuentan como palabra.
            if not _normal(r.get("text") or "") or float(r.get("conf") or -1) < 20:
                continue
            x, y, w, h = (int(r[k]) / factor for k in ("left", "top", "width", "height"))
            palabras.append({"texto": r["text"], "caja": (x, y, x + w, y + h),
                             "linea": (pasada, r["block_num"], r["par_num"], r["line_num"])})
    return palabras


def localizar(ruta: Path, rotulos: list[dict], idioma: str, pagina: bool = False) -> list[dict]:
    """Dónde está cada rótulo que leyó el asistente, línea a línea.

    Se buscan tramos de palabras seguidas de una misma línea de Tesseract cuyo texto se
    parezca al de la línea del rótulo. Solo se acepta lo que casa con la lectura del
    asistente: el ruido de Tesseract no se toma nunca por texto. Las líneas siguientes de
    un rótulo se buscan justo debajo de la anterior, para no emparejar el «Caldo» de un
    matraz con el «de carne» de otro.

    Con `pagina` se añade una lectura con segmentación automática de página: en la versión
    de 720 px del árbol de las plantas, el modo de texto disperso solo leía el pie, y el de
    página lo lee todo. Es el último recurso (ver `_reescribir`): en el árbol grande daba a
    «Carriza» una caja más alta que la del asistente y dejaba de caber.
    """
    palabras = _ocr(ruta, idioma)
    if pagina:
        palabras += _ocr(ruta, idioma, pagina=True)
    return _localizar(ruta, rotulos, palabras)


def _localizar(ruta: Path, rotulos: list[dict], palabras: list[dict]) -> list[dict]:
    from difflib import SequenceMatcher

    import numpy as np
    from PIL import Image

    with Image.open(ruta) as im:
        px = np.array(im.convert("RGB"))
    alto_img, ancho_img = px.shape[:2]

    def extender(caja):
        """La caja de una línea leída a medias, completada por la tinta de los lados.

        Tesseract ve «reprodutivo)» y no «(Illamento»: se sigue la tinta de la misma
        franja mientras los huecos no pasen de un espacio entre palabras.
        """
        x0, y0, x1, y1 = (int(round(v)) for v in caja)
        fondo, _ = _fondo(px, caja, 1)
        columnas = (_distancia(px[max(0, y0):y1, :], fondo) > 70).any(axis=0)
        hueco = max(3, int(0.7 * (y1 - y0)))

        def hasta(x, paso):
            ultimo, vacio = x, 0
            while 0 <= x + paso < ancho_img:
                x += paso
                if columnas[x]:
                    ultimo, vacio = x, 0
                else:
                    vacio += 1
                    if vacio > hueco:
                        break
            return ultimo
        return (hasta(x0, -1), y0, hasta(min(x1, ancho_img) - 1, 1) + 1, y1)

    def debajo(anterior, objetivo: str, previo: str):
        """Una línea que Tesseract no ha visto, buscada por la tinta bajo la anterior."""
        x0, y0, x1, y1 = (int(round(v)) for v in anterior)
        h, w = y1 - y0, x1 - x0
        ya, yb = int(y1 + 0.05 * h), int(min(alto_img, y1 + 1.8 * h))
        xa, xb = int(max(0, x0 - w)), int(min(ancho_img, x1 + w))
        if yb - ya < 3:
            return None
        fondo, _ = _fondo(px, anterior, 1)
        tinta = _distancia(px[ya:yb, xa:xb], fondo) > 70
        filas = np.nonzero(tinta.any(axis=1))[0]
        # Franjas de filas con tinta; la primera con altura de texto. Una línea guía que
        # sale del rótulo es una franja de uno o dos píxeles y no cuenta.
        franjas, inicio = [], None
        for i, f in enumerate(filas):
            if inicio is None:
                inicio = f
            if i + 1 == len(filas) or filas[i + 1] - f > 1:
                franjas.append((inicio, f))
                inicio = None
        franja = next(((a, b) for a, b in franjas if 0.5 * h <= b - a + 1 <= 1.5 * h), None)
        if franja is None:
            return None
        r0, r1 = franja
        cols = np.nonzero(tinta[r0:r1 + 1].any(axis=0))[0] + xa
        centro = (x0 + x1) / 2
        semilla = cols[np.argmin(np.abs(cols - centro))]
        caja = extender((semilla, ya + r0, semilla + 1, ya + r1 + 1))
        esperado = w / max(1, len(_normal(previo))) * len(_normal(objetivo))
        return caja if abs((caja[2] - caja[0]) - esperado) <= 0.35 * esperado else None

    por_linea: dict[tuple, list[int]] = {}
    for i, p in enumerate(palabras):
        por_linea.setdefault(p["linea"], []).append(i)
    candidatos = []
    for indices in por_linea.values():
        indices.sort(key=lambda i: palabras[i]["caja"][0])
        for a in range(len(indices)):
            # Sin tope de palabras: el pie de los minerales del resumen de Biología son
            # catorce en una línea, y con un tope de diez quedaba «1. Ouro. 2. Galena.».
            for b in range(a + 1, len(indices) + 1):
                trozo = [palabras[i] for i in indices[a:b]]
                caja = (min(p["caja"][0] for p in trozo), min(p["caja"][1] for p in trozo),
                        max(p["caja"][2] for p in trozo), max(p["caja"][3] for p in trozo))
                candidatos.append({"normal": _normal(" ".join(p["texto"] for p in trozo)),
                                   "caja": caja, "indices": set(indices[a:b])})
    # Bloques de dos a cuatro líneas seguidas, para el rótulo que la lectura da en una
    # línea y en la imagen ocupa varias: el pie de los silicatos del resumen de Biología.
    lineas_ocr = []
    for clave, indices in por_linea.items():
        cajas_l = [palabras[i]["caja"] for i in indices]
        lineas_ocr.append({"pasada": clave[0], "indices": indices,
                           "normal": _normal(" ".join(palabras[i]["texto"] for i in indices)),
                           "caja": (min(c[0] for c in cajas_l), min(c[1] for c in cajas_l),
                                    max(c[2] for c in cajas_l), max(c[3] for c in cajas_l))})

    def siguiente(linea):
        x0, y0, x1, y1 = linea["caja"]
        alto = y1 - y0
        abajo = [m for m in lineas_ocr if m["pasada"] == linea["pasada"] and m is not linea
                 and y1 - 0.3 * alto <= m["caja"][1] <= y1 + 1.0 * alto
                 and abs(m["caja"][0] - x0) <= 1.5 * alto
                 and 0.6 * alto <= m["caja"][3] - m["caja"][1] <= 1.6 * alto]
        return min(abajo, key=lambda m: m["caja"][1], default=None)

    for linea in lineas_ocr:
        cadena = [linea]
        while len(cadena) < 4:
            otra = siguiente(cadena[-1])
            if otra is None or any(otra is l for l in cadena):
                break
            cadena.append(otra)
            candidatos.append({
                "normal": "".join(l["normal"] for l in cadena),
                "caja": (min(l["caja"][0] for l in cadena), cadena[0]["caja"][1],
                         max(l["caja"][2] for l in cadena), cadena[-1]["caja"][3]),
                "partes": [(l["caja"], l["normal"]) for l in cadena],
                "indices": {i for l in cadena for i in l["indices"]}})

    usados: set[int] = set()
    cajas_usadas: list[tuple] = []

    def ya_usada(caja) -> bool:
        # Las dos pasadas de Tesseract dan la misma palabra dos veces: una caja que se
        # solapa con otra ya asignada es la misma palabra.
        for u in cajas_usadas:
            ancho = min(caja[2], u[2]) - max(caja[0], u[0])
            alto = min(caja[3], u[3]) - max(caja[1], u[1])
            if ancho > 0 and alto > 0 and ancho * alto > 0.3 * (caja[2] - caja[0]) * (caja[3] - caja[1]):
                return True
        return False

    resultado = []
    for rotulo in rotulos:
        cajas: list = []
        textos: list[str] = []
        for k, linea in enumerate(rotulo["lineas"]):
            objetivo, mejor, puntos = _normal(linea), None, 0.72
            anterior = cajas[-1] if cajas else None
            if k > 0 and anterior is None:
                cajas.append(None)
                textos.append(linea)
                continue
            parcial = None
            for c in candidatos:
                if c["indices"] & usados or ya_usada(c["caja"]):
                    continue
                if anterior is not None:
                    alto = anterior[3] - anterior[1]
                    solapa = min(anterior[2], c["caja"][2]) - max(anterior[0], c["caja"][0])
                    # Debajo de la anterior, sin exigir que empiece tras su borde: la caja de
                    # «Semente núa,» bajaba con la coma y «sen froito» quedaba un píxel dentro.
                    if not (anterior[1] + 0.6 * alto <= c["caja"][1] <= anterior[3] + 1.6 * alto and solapa > 0):
                        continue
                parecido = SequenceMatcher(None, objetivo, c["normal"]).ratio()
                if parecido > puntos:
                    mejor, puntos = c, parecido
                # Un trozo de la línea, leído bien: se completa después por la tinta.
                if (len(c["normal"]) >= 4 and c["normal"] in objetivo
                        and len(c["normal"]) >= 0.35 * len(objetivo)
                        and (parcial is None or len(c["normal"]) > len(parcial["normal"]))):
                    parcial = c
            if mejor and mejor.get("partes"):
                # Un bloque: cada línea de la imagen pasa a ser una línea del rótulo.
                trozos = _repartir(linea, [n for _, n in mejor["partes"]])
                if trozos:
                    cajas += [c for c, _ in mejor["partes"]]
                    textos += trozos
                    usados |= mejor["indices"]
                    cajas_usadas += [c for c, _ in mejor["partes"]]
                    continue
                mejor = None
            caja = mejor["caja"] if mejor else None
            if caja is None and parcial is not None:
                ampliada = extender(parcial["caja"])
                esperado = ((parcial["caja"][2] - parcial["caja"][0])
                            / len(parcial["normal"]) * len(objetivo))
                if abs((ampliada[2] - ampliada[0]) - esperado) <= 0.3 * esperado:
                    caja, mejor = ampliada, parcial
            if caja is None and anterior is not None:
                caja = debajo(anterior, linea, rotulo["lineas"][k - 1])
                if caja is not None and ya_usada(caja):
                    caja = None
            cajas.append(caja)
            textos.append(linea)
            if mejor:
                usados |= mejor["indices"]
            if caja is not None:
                cajas_usadas.append(caja)
        resultado.append({"lineas": textos, "cajas": cajas,
                          "ok": bool(cajas) and all(c is not None for c in cajas)})
    return resultado


# Sube cuando cambia cómo se afinan las cajas del asistente: las guardadas con otra versión
# no se usan y se vuelven a pedir. Las de la versión 1 (sin versión) se ajustaban solo a
# la tinta y se comían los marcos de los recuadros; las de la 2 tomaban el borde de un
# recuadro («|») por una palabra y se estiraban hasta él.
VERSION_CAJAS = 3


def cajas_vigentes(lectura: dict) -> dict:
    return (lectura.get("cajas") or {}) if lectura.get("cajas_version") == VERSION_CAJAS else {}


def cajas_coherentes(cajas: list) -> bool:
    """Las líneas de un rótulo, una debajo de otra y de alto parecido.

    Dos líneas que se solapan más de un cuarto de su alto, o una el doble de alta que la
    otra, no son líneas de texto: son cajas mal medidas, y escribir encima monta un
    renglón sobre otro.
    """
    if not cajas or not all(cajas) or any(len(c) != 4 for c in cajas):
        return False
    altos = [c[3] - c[1] for c in cajas]
    if min(altos) <= 2 or max(altos) > 2 * min(altos):
        return False
    for a, b in zip(cajas, cajas[1:]):
        if a[3] - b[1] > 0.25 * min(a[3] - a[1], b[3] - b[1]):
            return False
    return True


def afinar(ruta: Path, caja: list[float], texto: str, idioma: str) -> tuple[int, int, int, int] | None:
    """La caja exacta de una línea, a partir de la aproximada del asistente.

    El asistente sabe qué rótulo es y dónde está más o menos; Tesseract, que en la imagen
    entera no lo encuentra, lo lee bien en un recorte pequeño y ampliado. Se busca en el
    recorte el tramo de palabras que más se parece al texto de la línea. Si no aparece, se
    ajusta la caja a la tinta.
    """
    import csv
    import io
    import subprocess
    import tempfile
    from difflib import SequenceMatcher

    from PIL import Image

    if len(caja) != 4:
        return None
    with Image.open(ruta) as im:
        im = im.convert("RGB")
    x0, y0, x1, y1 = (float(v) for v in caja)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    h, w = max(4.0, y1 - y0), max(4.0, x1 - x0)
    objetivo = _normal(texto)

    def leer(recorte, origen, factor, psm):
        """Lo que Tesseract lee en un recorte: el tramo que más se parece y su caja."""
        mejor, puntos = None, 0.0
        for version in (recorte, recorte.convert("L").point(lambda v: 255 - v)):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                version.save(tmp.name)
            try:
                salida = subprocess.run(["tesseract", tmp.name, "-", "-l", _LENGUAS_OCR.get(idioma, "spa"),
                                         "--psm", psm, "tsv"], capture_output=True, text=True, timeout=60).stdout
            except (OSError, subprocess.SubprocessError):
                salida = ""
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            # Sin las «palabras» que son solo signos: el borde de un recuadro se lee como
            # «|», y como al comparar se quitan los signos, el tramo con él valía lo mismo
            # que sin él y la caja se estiraba hasta el marco.
            palabras = [r for r in csv.DictReader(io.StringIO(salida), delimiter="\t")
                        if _normal(r.get("text") or "") and float(r.get("conf") or -1) >= 10]
            for i in range(len(palabras)):
                for j in range(i + 1, min(len(palabras), i + 12) + 1):
                    trozo = palabras[i:j]
                    if len({(p["block_num"], p["par_num"], p["line_num"]) for p in trozo}) > 1:
                        break
                    parecido = SequenceMatcher(None, objetivo, _normal(" ".join(p["text"] for p in trozo))).ratio()
                    if parecido > puntos:
                        cs = [(int(p["left"]), int(p["top"]), int(p["left"]) + int(p["width"]),
                               int(p["top"]) + int(p["height"])) for p in trozo]
                        mejor, puntos = (origen[0] + min(c[0] for c in cs) / factor,
                                         origen[1] + min(c[1] for c in cs) / factor,
                                         origen[0] + max(c[2] for c in cs) / factor,
                                         origen[1] + max(c[3] for c in cs) / factor), parecido
        return mejor, puntos

    # Tesseract es caprichoso con el recorte: con el vecino de arriba dentro, en el árbol
    # de las plantas no leía «Ximnospermas»; con otro recorte y otra ampliación, al 92 %.
    # Se prueba de más ajustado a más holgado y se para en cuanto lee bien.
    mejor, puntos = None, 0.6
    for holgura_x, holgura_y, psm in ((0.15, 0.25, "7"), (0.35, 0.5, "6"), (0.6, 0.8, "11")):
        a0, b0 = int(max(0, x0 - holgura_x * w - 0.5 * h)), int(max(0, y0 - holgura_y * h))
        a1, b1 = int(min(im.width, x1 + holgura_x * w + 0.5 * h)), int(min(im.height, y1 + holgura_y * h))
        if a1 - a0 < 4 or b1 - b0 < 4:
            continue
        base = im.crop((a0, b0, a1, b1))
        for factor in sorted({2.0, 3.0, max(1.0, min(8.0, 90 / h))}):
            recorte = base.resize((int(base.width * factor), int(base.height * factor)), Image.LANCZOS)
            caja_leida, parecido = leer(recorte, (a0, b0), factor, psm)
            if parecido > puntos:
                mejor, puntos = caja_leida, parecido
            if puntos >= 0.9:
                break
        if puntos >= 0.9:
            break
    if mejor:
        return tuple(int(round(v)) for v in mejor)
    return ajustar_a_tinta(ruta, caja)


def ajustar_a_tinta(ruta: Path, caja: list[float]) -> tuple[int, int, int, int] | None:
    """La caja aproximada de una línea (la del asistente), ajustada a las letras.

    Se busca la tinta en la caja ampliada un poco por cada lado, respecto al color de su
    contorno, y se recorta a ella. Si la tinta se sale mucho de lo esperado —una línea del
    dibujo que cruza— o no hay, se queda la caja tal cual: la relectura dirá si vale.
    """
    import numpy as np
    from PIL import Image

    with Image.open(ruta) as im:
        px = np.array(im.convert("RGB"))
    alto_img, ancho_img = px.shape[:2]
    if len(caja) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in caja)
    x0, x1 = sorted((max(0.0, x0), min(float(ancho_img), x1)))
    y0, y1 = sorted((max(0.0, y0), min(float(alto_img), y1)))
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    h = y1 - y0
    a0, b0 = int(max(0, x0 - 0.4 * h)), int(max(0, y0 - 0.3 * h))
    a1, b1 = int(min(ancho_img, x1 + 0.4 * h)), int(min(alto_img, y1 + 0.3 * h))
    fondo, _ = _fondo(px, (a0, b0, a1, b1), 1)
    tinta = _distancia(px[b0:b1, a0:a1], fondo) > 70
    # Fuera los marcos: una fila o una columna casi entera de tinta es el borde del
    # recuadro del rótulo, no una letra.
    tinta[tinta.mean(axis=1) > 0.6, :] = False
    tinta[:, tinta.mean(axis=0) > 0.6] = False
    filas, columnas = np.nonzero(tinta.any(axis=1))[0], np.nonzero(tinta.any(axis=0))[0]
    original = (int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1)))
    if not len(filas) or not len(columnas):
        return original
    ajustada = (a0 + int(columnas[0]), b0 + int(filas[0]), a0 + int(columnas[-1]) + 1, b0 + int(filas[-1]) + 1)
    w, wa = x1 - x0, ajustada[2] - ajustada[0]
    ha = ajustada[3] - ajustada[1]
    # El asistente tiende a dar cajas holgadas, sobre todo de alto: se admite que la tinta
    # ocupe bastante menos, pero no que se salga mucho (eso es el dibujo de al lado).
    if 0.5 * w <= wa <= 1.35 * w and 0.3 * h <= ha <= 1.5 * h:
        return ajustada
    return original


def _repartir(linea: str, partes: list[str]) -> list[str] | None:
    """Parte el texto de la lectura en las líneas que tiene en la imagen.

    `partes` es lo que Tesseract lee en cada línea, normalizado. Se corta por palabras, en
    el punto en que lo acumulado se parece más a lo leído hasta esa línea.
    """
    palabras = linea.split()
    if len(palabras) < len(partes):
        return None
    resultado, desde = [], 0
    objetivo = 0
    for n, parte in enumerate(partes[:-1]):
        objetivo += len(parte)
        restantes = len(partes) - n - 1
        mejor = min(range(desde + 1, len(palabras) - restantes + 1),
                    key=lambda h: abs(len(_normal(" ".join(palabras[:h]))) - objetivo))
        resultado.append(" ".join(palabras[desde:mejor]))
        desde = mejor
    resultado.append(" ".join(palabras[desde:]))
    return resultado


# ------------------------------------------------------------------- reescribir

# Fuentes candidatas para el texto de las infografías: la letra original suele ser una
# humanista estrecha, tipo Myriad Condensed. Se elige la que mejor encaja con el ancho, el
# alto y el grosor de la tinta de cada rótulo.
_FAMILIAS = [("PTN57F", "PTN77F"), ("PTS55F", "PTS75F"), ("NotoSans-Regular", "NotoSans-Bold")]
_CANDIDATAS = [f for par in _FAMILIAS for f in par]
# Lo que baja de la línea base. Los paréntesis también: sin ellos, el pie «CRIPTÓGAMAS
# (PLANTAS SEN FLOR)», pegado al borde inferior, ponía la base en el fondo del paréntesis
# y el texto nuevo se salía de la imagen a cualquier tamaño.
_DESCENDENTES = set("gjpqyçQ,;()[]{}")


def _rutas_candidatas() -> list[str]:
    from .pdf_texto import fuente
    return [str(fuente(n)) for n in _CANDIDATAS if fuente(n)]


def _distancia(px, color):
    import numpy as np
    return np.abs(px.astype(np.int16) - np.asarray(color, dtype=np.int16)).max(axis=-1)


def _render(ruta: str, texto: str, tamano: float):
    """El texto pintado en blanco sobre negro, como máscara de tinta."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    t = max(4, int(round(tamano)))
    f = ImageFont.truetype(ruta, t)
    lienzo = Image.new("L", (int(f.getlength(texto)) + 4 * t + 8, int(t * 2.2) + 8), 0)
    ImageDraw.Draw(lienzo).text((2 * t, int(t * 1.4)), texto, font=f, fill=255, anchor="ls")
    return np.asarray(lienzo) > 110


def _trazo(mascara) -> float:
    """El grosor del trazo: la mediana de los tramos horizontales de tinta.

    Distingue la negrita mejor que la cantidad de tinta, que depende también de lo
    apretadas que vayan las letras.
    """
    import numpy as np
    tramos = []
    for fila in mascara:
        if not fila.any():
            continue
        d = np.diff(np.concatenate(([0], fila.astype(np.int8), [0])))
        tramos += list(np.nonzero(d == -1)[0] - np.nonzero(d == 1)[0])
    return float(np.median(tramos)) if tramos else 1.0


def _calibrar(px, caja, texto: str, fondo,
              estricto: bool = False) -> tuple[dict[str, tuple[float, float]], tuple]:
    """Cómo encaja cada fuente candidata con un rótulo —error y cuerpo— y su color.

    `estricto`, para las cajas que vienen del asistente: si ninguna fuente encaja, la caja
    no es la del texto. Las de Tesseract son cajas de palabras y no hace falta.
    """
    import numpy as np
    from .pdf_texto import _tinta
    x0, y0, x1, y1 = (int(round(v)) for v in caja)
    zona = px[y0:y1, x0:x1]
    lejos = _distancia(zona, fondo)
    tinta = lejos > 70
    if tinta.sum() < 5:
        raise ValueError("no se ve tinta en la caja del rótulo")
    ys, xs = np.nonzero(tinta)
    ancho, alto = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    # Con el mismo umbral relativo que la fuente dibujada: el 45 % del contraste.
    trazo = _trazo(lejos > 0.45 * lejos.max())
    color = tuple(int(c) for c in np.median(zona[lejos >= np.percentile(lejos[tinta], 60)], axis=0))
    encajes = {}
    for ruta in _rutas_candidatas():
        iz, de = _tinta(ruta, texto)
        if de - iz <= 0:
            continue
        tamano = ancho / (de - iz)
        m = _render(ruta, texto, tamano)
        if not m.any():
            continue
        ry, _ = np.nonzero(m)
        alto_r = ry.max() - ry.min() + 1
        encajes[ruta] = (abs(alto_r - alto) / alto + abs(_trazo(m) - trazo) / trazo, tamano)
    if not encajes:
        raise ValueError("no hay ninguna fuente candidata instalada")
    # Si ninguna fuente se parece a la tinta (alto y trazo), la caja no es la del texto
    # —se ha comido el marco o un trozo del dibujo— y las letras saldrían a otro tamaño.
    if estricto and min(e for e, _ in encajes.values()) > 0.6:
        raise ValueError("la caja no se corresponde con el texto: ninguna letra encaja con su tinta")
    return encajes, color


def _elegir_letra(medidas: list[dict]) -> list[tuple[str, float]]:
    """Una sola familia para toda la imagen, y el peso decidido por grupos de estilo.

    Elegida rótulo a rótulo, la misma leyenda salía una vez en PT Sans y otra en Noto
    Sans Bold, y en la negrita pasaba lo mismo: el antialias de un JPEG engaña a la medida
    del grosor de un rótulo suelto. Los rótulos del mismo color y cuerpo parecido son del
    mismo estilo; su peso se decide sumando la medida de todos.
    """
    from .pdf_texto import fuente
    familias = [(str(fuente(n)), str(fuente(b))) for n, b in _FAMILIAS if fuente(n) and fuente(b)]

    def coste(familia):
        normal, negrita = familia
        return sum(min(m["encajes"].get(normal, (9, 0))[0], m["encajes"].get(negrita, (9, 0))[0])
                   for m in medidas)
    normal, negrita = min(familias, key=coste)

    grupos: list[list[int]] = []
    for i, m in enumerate(medidas):
        cuerpo = m["encajes"].get(normal, (9, 1))[1]
        for g in grupos:
            r = medidas[g[0]]
            if (max(abs(a - b) for a, b in zip(m["color"], r["color"])) < 40
                    and abs(cuerpo - r["encajes"].get(normal, (9, 1))[1]) <= 0.18 * cuerpo):
                g.append(i)
                break
        else:
            grupos.append([i])
    peso = {}
    for g in grupos:
        en = sum(medidas[i]["encajes"].get(normal, (9, 0))[0] for i in g)
        eb = sum(medidas[i]["encajes"].get(negrita, (9, 0))[0] for i in g)
        for i in g:
            peso[i] = negrita if eb < en else normal
    return [(peso[i], m["encajes"].get(peso[i], (9, 12.0))[1]) for i, m in enumerate(medidas)]


def _fondo(px, caja, margen: int):
    """El color de alrededor de una caja y qué parte de ese contorno es de ese color."""
    import numpy as np
    alto, ancho = px.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in caja)
    a0, b0 = max(0, x0 - margen - 2), max(0, y0 - margen - 2)
    a1, b1 = min(ancho, x1 + margen + 2), min(alto, y1 + margen + 2)
    zona = px[b0:b1, a0:a1].astype(np.int16)
    anillo = np.ones(zona.shape[:2], bool)
    anillo[2:-2, 2:-2] = False
    muestra = zona[anillo]
    color = np.median(muestra, axis=0)
    liso = (np.abs(muestra - color).max(axis=1) < 35).mean()
    return tuple(int(c) for c in color), float(liso)


def _dentro(caja, alto: int, ancho: int):
    """La caja, si cabe en la imagen. Abajo se tolera lo que ocupa el hueco de los
    descendentes (el 30 % del alto): un pie pegado al borde no los tiene, pero la caja
    que se calcula para la línea sí."""
    x0, y0, x1, y1 = (int(round(v)) for v in caja)
    if y1 > alto and y1 - alto <= 0.3 * (y1 - y0):
        y1 = alto
    if x0 < 0 or y0 < 0 or x1 > ancho or y1 > alto:
        return None
    return x0, y0, x1, y1


def _es_libre(px, caja, fondo) -> bool:
    alto, ancho = px.shape[:2]
    dentro = _dentro(caja, alto, ancho)
    if dentro is None:
        return False
    x0, y0, x1, y1 = dentro
    zona = px[y0:y1, x0:x1]
    # Unos pocos píxeles, no un porcentaje: en una caja grande el 1 % era el brazo entero
    # del esqueleto del gorila, y el rótulo se montaba encima.
    return zona.size == 0 or int((_distancia(zona, fondo) > 40).sum()) <= 4


def _es_libre_fuera(px, caja, fondo, excluida) -> bool:
    """Como `_es_libre`, sin contar lo que cae dentro de `excluida` (el rótulo borrado)."""
    alto, ancho = px.shape[:2]
    dentro = _dentro(caja, alto, ancho)
    if dentro is None:
        return False
    x0, y0, x1, y1 = dentro
    zona = _distancia(px[y0:y1, x0:x1], fondo) > 40
    e0, f0, e1, f1 = (int(round(v)) for v in excluida)
    zona[max(0, f0 - y0):max(0, f1 - y0), max(0, e0 - x0):max(0, e1 - x0)] = False
    return int(zona.sum()) <= 4


def _solapa(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _partir(texto: str, ruta: str, tamano: float, ancho: float) -> list[str]:
    from .pdf_texto import _tinta
    lineas, actual = [], ""
    for palabra in texto.split():
        prueba = f"{actual} {palabra}" if actual else palabra
        iz, de = _tinta(ruta, prueba)
        if actual and (de - iz) * tamano > ancho:
            lineas.append(actual)
            actual = palabra
        else:
            actual = prueba
    if actual:
        lineas.append(actual)
    return lineas


def _medir_rotulo(px, rotulo: dict) -> dict:
    """Fondo, color y encaje de cada fuente de un rótulo ya localizado, antes de tocar nada."""
    cajas = rotulo["cajas"]
    x0, y0 = min(c[0] for c in cajas), min(c[1] for c in cajas)
    x1, y1 = max(c[2] for c in cajas), max(c[3] for c in cajas)
    margen = max(2, int(0.15 * max(c[3] - c[1] for c in cajas)))
    fondo, liso = _fondo(px, (x0, y0, x1, y1), margen)
    modo = "liso"
    dentro, parte_dentro = _dominante(px, (x0, y0, x1, y1))
    if liso >= 0.85 and parte_dentro >= 0.4 and max(abs(a - b) for a, b in zip(dentro, fondo)) > 40:
        # El contorno es liso, pero el rótulo va sobre otra cosa: una banda de color más
        # estrecha que la caja («Estame» en verde sobre la flor). Pintar la caja del color
        # del contorno borraba la banda y escribía el texto en verde sobre blanco.
        fondo, modo = dentro, "relleno"
    elif liso < 0.85:
        # El contorno no es de un color —una línea guía que toca el rótulo, la banda de
        # color en la que va, un dibujo al lado—, pero dentro de la caja casi siempre
        # domina uno: ese es el fondo, y las letras se borran con relleno, no pintando
        # la caja entera.
        dominante, parte = _dominante(px, (x0, y0, x1, y1))
        if parte < 0.4:
            raise ValueError(f"fondo no liso: solo el {liso:.0%} del contorno es de un color "
                             f"y dentro del rótulo ninguno pasa del {parte:.0%}")
        fondo, modo = dominante, "relleno"
    larga = max(range(len(cajas)), key=lambda i: cajas[i][2] - cajas[i][0])
    encajes, color = _calibrar(px, cajas[larga], rotulo["lineas"][larga], fondo,
                               estricto=rotulo.get("del_asistente", False))
    return {"fondo": fondo, "color": color, "encajes": encajes, "margen": margen, "modo": modo}


def _dominante(px, caja) -> tuple[tuple[int, int, int], float]:
    """El color que más ocupa una caja y qué parte ocupa (los de menos de 35 de distancia)."""
    import numpy as np
    x0, y0, x1, y1 = (int(round(v)) for v in caja)
    zona = px[max(0, y0):y1, max(0, x0):x1].reshape(-1, 3).astype(np.int16)
    if not len(zona):
        return (255, 255, 255), 0.0
    cubos = (zona // 24)
    claves, cuentas = np.unique(cubos[:, 0] * 10000 + cubos[:, 1] * 100 + cubos[:, 2], return_counts=True)
    clave = claves[np.argmax(cuentas)]
    del_cubo = zona[(cubos[:, 0] * 10000 + cubos[:, 1] * 100 + cubos[:, 2]) == clave]
    color = np.median(del_cubo, axis=0)
    parte = float((np.abs(zona - color).max(axis=1) < 35).mean())
    return tuple(int(c) for c in color), parte


def _borrar_con_relleno(px, cajas, fondo) -> None:
    """Borra las letras de un rótulo reconstruyendo lo que hay detrás (OpenCV, Telea).

    Solo las letras, dentro de la caja de cada línea y un píxel más: la línea guía que
    llega hasta el rótulo y el dibujo de al lado no se tocan, y el hueco se rellena desde
    su borde, así que una banda de color o un degradado siguen siéndolo.
    """
    import cv2
    import numpy as np
    alto, ancho = px.shape[:2]
    mascara = np.zeros((alto, ancho), np.uint8)
    for c in cajas:
        x0, y0, x1, y1 = (int(round(v)) for v in c)
        x0, y0, x1, y1 = max(0, x0 - 1), max(0, y0 - 1), min(ancho, x1 + 1), min(alto, y1 + 1)
        zona = px[y0:y1, x0:x1]
        mascara[y0:y1, x0:x1] = (_distancia(zona, fondo) > 40).astype(np.uint8) * 255
    # El antialias del borde de cada letra también es letra.
    mascara = cv2.dilate(mascara, np.ones((3, 3), np.uint8), iterations=2)
    radio = max(3, int(0.25 * max(c[3] - c[1] for c in cajas)))
    px[:] = cv2.inpaint(px, mascara, radio, cv2.INPAINT_TELEA)


def _contenedor(px, caja, fondo) -> dict | None:
    """El recuadro o la banda que rodea un rótulo ya borrado, si lo hay.

    Desde la caja del texto se busca hacia los cuatro lados el primer píxel que no es del
    fondo, en varias filas y columnas: tiene que estar a la misma distancia en todas (un
    borde recto) y cerca del texto. Si ese píxel es un marco (unos pocos píxeles de otro
    color), el recuadro lo incluye; si es ya lo de fuera (una banda de color sin marco), el
    borde es la propia banda. Devuelve el rectángulo exterior y el color de fuera.
    """
    import numpy as np
    alto, ancho = px.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in caja)
    h = max(4, y1 - y0)

    def lejos(p):
        return int(np.abs(np.asarray(p, dtype=np.int16) - np.asarray(fondo, dtype=np.int16)).max()) > 40

    def buscar(desde, hasta, paso, fijo, horizontal):
        for v in range(desde, hasta, paso):
            if not 0 <= v < (ancho if horizontal else alto):
                return None
            if lejos(px[fijo, v] if horizontal else px[v, fijo]):
                return v
        return None

    filas = [y0 + (y1 - y0) * k // 8 for k in (1, 3, 4, 5, 7)]
    # Arriba y abajo se mira a los lados, no en el centro: ahí es donde suele llegar la
    # línea que une el recuadro con el de arriba, y se tomaba por lo de fuera.
    columnas = [x0 + (x1 - x0) * k // 8 for k in (1, 2, 6, 7)]
    # Se empieza un poco fuera de la caja: si está medida un píxel corta, lo primero que se
    # encontraba era el borde de una letra.
    sx, sy = max(2, h // 5), max(2, h // 6)
    izq = [buscar(x0 - sx, x0 - 3 * h, -1, y, True) for y in filas]
    der = [buscar(x1 + sx, x1 + 3 * h, 1, y, True) for y in filas]
    arr = [buscar(y0 - sy, y0 - 2 * h, -1, x, False) for x in columnas]
    aba = [buscar(y1 + sy, y1 + 2 * h, 1, x, False) for x in columnas]

    def recto(valores):
        """El borde, si la mayoría de las muestras lo encuentran en el mismo sitio."""
        validos = [v for v in valores if v is not None]
        if not validos:
            return None
        mediana = int(np.median(validos))
        cerca = [v for v in validos if abs(v - mediana) <= 2]
        return mediana if len(cerca) > len(valores) / 2 else None
    L, R, T, B = recto(izq), recto(der), recto(arr), recto(aba)
    if None in (L, R, T, B):
        return None

    def borde(v, paso, fijo, horizontal):
        """Grosor del marco desde `v` hacia fuera, y el color de lo que hay detrás."""
        color = px[fijo, v] if horizontal else px[v, fijo]
        k = 0
        # El marco sigue mientras no se vuelva al color de dentro: con antialias, sus
        # píxeles no son todos del mismo gris.
        while k < 8:
            w = v + paso * k
            if not 0 <= w < (ancho if horizontal else alto):
                break
            if not lejos(px[fijo, w] if horizontal else px[w, fijo]):
                break
            k += 1
        if k >= 8:          # no es un marco: es lo de fuera, y la banda acaba en v
            return v - paso, color
        w = v + paso * k
        fuera = (px[fijo, w] if horizontal else px[w, fijo]) if 0 <= w < (ancho if horizontal else alto) else color
        return v + paso * (k - 1), fuera

    ym, xm = (y0 + y1) // 2, x0 + (x1 - x0) // 8
    Lo, fuera = borde(L, -1, ym, True)
    Ro, _ = borde(R, 1, ym, True)
    To, _ = borde(T, -1, xm, False)
    Bo, _ = borde(B, 1, xm, False)
    # Por dentro: lo que queda entre los bordes, que es donde puede ir el texto.
    return {"caja": (Lo, To, Ro, Bo), "interior": (L + 1, T + 1, R - 1, B - 1),
            "fuera": tuple(int(c) for c in fuera)}


def _ensanchar(px, contenedor: dict, delta: int, centro: int) -> bool:
    """Ensancha el recuadro `delta` píxeles, repitiendo su columna central. Si no hay sitio
    libre a los lados (fondo de fuera, sin nada encima), no toca nada y devuelve False."""
    import numpy as np
    alto, ancho = px.shape[:2]
    Lo, To, Ro, Bo = (int(round(v)) for v in contenedor["caja"])
    centro, delta = int(round(centro)), int(delta)
    izq, der = delta // 2, delta - delta // 2
    if Lo - izq < 0 or Ro + der >= ancho:
        return False
    for a, b in ((Lo - izq, Lo), (Ro + 1, Ro + 1 + der)):
        zona = px[max(0, To - 1):min(alto, Bo + 2), a:b]
        if int((_distancia(zona, contenedor["fuera"]) > 40).sum()) > 4:
            return False
    region = px[To:Bo + 1, Lo:Ro + 1].copy()
    medio = min(max(1, centro - Lo), region.shape[1] - 2)
    nueva = np.concatenate([region[:, :medio], np.repeat(region[:, medio:medio + 1], delta, axis=1),
                            region[:, medio:]], axis=1)
    px[To:Bo + 1, Lo - izq:Ro + der + 1] = nueva
    return True


def _rehacer_rotulo(px, rotulo: dict, nuevo: str, ocupado: list, medida: dict,
                    ruta: str, tamano: float) -> dict:
    """Borra un rótulo de `px` y escribe `nuevo` en su sitio. Devuelve lo que ha hecho."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from .pdf_texto import _tinta

    cajas = rotulo["cajas"]
    x0, y0 = min(c[0] for c in cajas), min(c[1] for c in cajas)
    x1, y1 = max(c[2] for c in cajas), max(c[3] for c in cajas)
    fondo, color, margen = medida["fondo"], medida["color"], medida["margen"]

    # Alineación: en varias líneas, qué borde se repite; en una, según dónde esté.
    alto_img, ancho_img = px.shape[:2]
    centros = [(c[0] + c[2]) / 2 for c in cajas]
    if len(cajas) > 1:
        izq, der = np.ptp([c[0] for c in cajas]), np.ptp([c[2] for c in cajas])
        alineacion = ("centro" if np.ptp(centros) < min(izq, der) - 1
                      else "izquierda" if izq <= der else "derecha")
    else:
        alineacion = ("izquierda" if x0 < 0.08 * ancho_img else
                      "derecha" if x1 > 0.92 * ancho_img else "centro")
    eje = float(np.mean(centros))

    relleno = medida.get("modo") == "relleno"
    # El recuadro o la banda en que va, si va en uno: se busca antes de borrar, porque el
    # borrado no puede tocar su marco (en el árbol de las plantas se comía el borde de
    # arriba de los recuadros de dos líneas), y el texto nuevo no puede salirse de él.
    contenedor = _contenedor(px, (x0, y0, x1, y1), fondo)
    tope = contenedor["interior"] if contenedor else (0, 0, ancho_img, alto_img)
    if relleno:
        _borrar_con_relleno(px, cajas, fondo)
    else:
        # Borrar: la caja del rótulo, con un pequeño margen, del color del fondo.
        b = (max(0, x0 - margen, tope[0]), max(0, y0 - margen, tope[1]),
             min(ancho_img, x1 + margen, tope[2] + 1), min(alto_img, y1 + margen, tope[3] + 1))
        zona = px[int(b[1]):int(b[3]), int(b[0]):int(b[2])]
        # Las líneas rectas que cruzan la zona entera (el marco de un recuadro que no se ha
        # podido detectar) se respetan. Una letra no llega al margen de arriba y abajo, así
        # que nunca la cruza entera: con un umbral del 60 % se conservaban los palos de la
        # «d» y la «t» y el texto no se borraba.
        tinta = _distancia(zona, fondo) > 40
        linea = np.zeros_like(tinta)
        if tinta.size:
            linea[tinta.mean(axis=1) > 0.95, :] = True
            linea[:, tinta.mean(axis=0) > 0.95] = True
        zona[~(linea & tinta)] = fondo
        # Y alrededor, el halo que deja el JPEG: solo los píxeles casi del color del fondo,
        # para no tocar las líneas del dibujo. La relectura veía «marcas tenues del borrado».
        h2 = 2 * margen + 2
        a0, a1 = max(0, x0 - h2, tope[0]), min(ancho_img, x1 + h2, tope[2] + 1)
        c0, c1 = max(0, y0 - h2, tope[1]), min(alto_img, y1 + h2, tope[3] + 1)
        halo = px[int(c0):int(c1), int(a0):int(a1)]
        cerca = _distancia(halo, fondo) < 60
        halo[cerca] = fondo
    viejo = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    marco = list(contenedor["interior"]) if contenedor else None
    if marco and alineacion == "centro":
        # Centrado en su recuadro, no en la caja del texto antiguo, que puede estar
        # medida algo desplazada.
        eje = (marco[0] + marco[2]) / 2
    # Lo que el texto deja libre hasta el borde: un tercio del que dejaba el original, entre
    # 2 px y medio cuerpo. Pedir el mismo hacía imposible cualquier texto más largo.
    aire = (int(max(2, min(0.35 * min(x0 - marco[0], marco[2] - x1), 0.5 * (y1 - y0))))
            if marco else 0)

    def libre(caja) -> bool:
        if marco is not None:
            a0, b0, a1, b1 = caja
            if (a0 < marco[0] + aire or a1 > marco[2] - aire or b0 < marco[1]
                    or b1 > marco[3] + 1 + 0.3 * (b1 - b0)):
                return False
        if not relleno:
            return _es_libre(px, caja, fondo)
        # Con relleno, lo que había donde estaba el rótulo ya está borrado; fuera de él solo
        # se admite fondo: el texto nuevo no puede pisar la línea guía ni el dibujo.
        return _es_libre_fuera(px, caja, fondo, viejo)

    bases = [c[3] - (0.21 * tamano if set(l) & _DESCENDENTES else 0)
             for c, l in zip(cajas, rotulo["lineas"])]
    paso = (bases[-1] - bases[0]) / (len(bases) - 1) if len(bases) > 1 else 1.25 * tamano
    ancho_orig = max(c[2] - c[0] for c in cajas)

    def colocar(lineas, t):
        n0, n = len(bases), len(lineas)
        if n == n0:
            bs = list(bases)
        elif n < n0:
            centro = (bases[0] + bases[-1]) / 2
            bs = [centro + paso * (i - (n - 1) / 2) for i in range(n)]
        else:
            bs = list(bases) + [bases[-1] + paso * (i + 1) for i in range(n - n0)]
        res = []
        for linea, base in zip(lineas, bs):
            iz, de = _tinta(ruta, linea)
            w = (de - iz) * t
            x = x0 if alineacion == "izquierda" else x1 - w if alineacion == "derecha" else eje - w / 2
            res.append((linea, x - iz * t, base, (x, base - 0.78 * t, x + w, base + 0.24 * t)))
        return res

    a_su_tamano = ((1.0, 0, 1.0, "reescrito"), (1.0, 0, 1.15, "reescrito"), (1.0, 1, 1.0, "una línea más"))
    todas = a_su_tamano + ((0.95, 0, 1.15, "reducido"), (0.9, 0, 1.15, "reducido"),
                           (0.9, 1, 1.1, "una línea más"), (0.85, 0, 1.2, "reducido"),
                           (0.8, 1, 1.2, "reducido"))

    def probar(opciones=todas):
        for escala, extra, holgura, estado in opciones:
            t = tamano * escala
            lineas = _partir(nuevo, ruta, t, ancho_orig * holgura)
            if len(lineas) > len(bases) + extra:
                continue
            propuesta = colocar(lineas, t)
            if all(libre(c[3]) and not any(_solapa(c[3], o) for o in ocupado)
                   for c in propuesta):
                return t, propuesta, estado
        return None

    ampliado = 0

    def ensanchar_y_probar(opciones):
        """Si va dentro de un recuadro o de una banda, se ensancha (por igual a los dos
        lados, repitiendo su columna central) y se vuelve a probar. Lo que se toca fuera del
        recuadro tiene que ser fondo: si hay otro recuadro al lado, no se hace."""
        nonlocal x0, x1, ancho_orig, eje, viejo, ampliado
        if contenedor is None:
            return None
        antes, marco_antes, eje_antes = px.copy(), list(marco), eje
        for parte in (0.15, 0.3, 0.5):
            delta = max(2, int(round(parte * ancho_orig)))
            if not _ensanchar(px, contenedor, delta, (x0 + x1) // 2):
                px[:] = antes
                return None
            izq = delta // 2
            x0, x1 = x0 - izq, x1 + (delta - izq)
            marco[0], marco[2] = marco_antes[0] - izq, marco_antes[2] + (delta - izq)
            ancho_orig += delta
            eje = (marco[0] + marco[2]) / 2 if alineacion == "centro" else eje_antes
            viejo = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)
            hecho = probar(opciones)
            if hecho is not None:
                ampliado = delta
                return hecho
            px[:] = antes
            x0, x1 = x0 + izq, x1 - (delta - izq)
            marco[:] = marco_antes
            eje = eje_antes
            ancho_orig -= delta
            viejo = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)
        return None

    # Por orden: tal cual; ensanchando el recuadro con la letra a su tamaño; reduciendo; y
    # ensanchando y reduciendo. Ensanchar respeta más el original que achicar la letra.
    elegido = (probar(a_su_tamano) or ensanchar_y_probar(a_su_tamano)
               or probar(todas) or ensanchar_y_probar(todas))
    if elegido is None:
        return {"estado": "no cabe", "detalle": "ni reduciendo un 20 %, ni con una línea más, "
                                                "ni ensanchando su recuadro"}
    t, propuesta, estado = elegido
    if ampliado:
        estado = "recuadro ampliado"
    imagen = Image.fromarray(px)
    dibujo = ImageDraw.Draw(imagen)
    letra = ImageFont.truetype(ruta, max(4, int(round(t))))
    for linea, x, base, caja in propuesta:
        dibujo.text((x, base), linea, font=letra, fill=color, anchor="ls")
        ocupado.append(caja)
    px[:] = np.asarray(imagen)
    return {"estado": estado, "fuente": Path(ruta).stem, "tamano": round(t, 1),
            "tamano_original": round(tamano, 1), "lineas": len(propuesta)}


def reescribir(origen: Path, salida: Path, lectura: dict, traducciones: dict[str, str],
               idioma_origen: str) -> dict:
    """Ver `_reescribir`. Un fallo inesperado en una imagen la deja como estaba y la manda
    a diseño, con el error en el detalle: no puede tumbar el proceso del trabajo entero."""
    try:
        return _reescribir(origen, salida, lectura, traducciones, idioma_origen)
    except Exception as e:  # noqa: BLE001
        return {"estado": "error", "detalle": f"fallo al rehacerla ({type(e).__name__}: {e})"}


def _reescribir(origen: Path, salida: Path, lectura: dict, traducciones: dict[str, str],
                idioma_origen: str) -> dict:
    """Rehace una imagen de rótulos sobre fondo liso con sus rótulos traducidos.

    Todo o nada: si un rótulo no se localiza, no cabe o su fondo no es liso, la imagen no
    se escribe y va a diseño. Una imagen a medio traducir es peor que la original.
    """
    import numpy as np
    from PIL import Image

    rotulos = [r for r in rotulos_validos(lectura) if r.get("lineas")]
    pares = [(r, texto_rotulo(r), traducciones.get(texto_rotulo(r))) for r in rotulos]
    faltan = [t for _, t, n in pares if not n]
    if faltan:
        return {"estado": "sin traducir",
                "detalle": f"{len(faltan)} rótulos sin traducción, por ejemplo «{faltan[0]}»"}
    cambiar = [(r, n) for r, t, n in pares if _normal(n) != _normal(t)]
    if not cambiar:
        return {"estado": "sin cambios"}
    situados = localizar(origen, [r for r, _ in cambiar], idioma_origen)
    # Lo que no encuentra Tesseract, con las cajas que dio el asistente (`cajas`, ya
    # afinadas). Solo eso: lo que Tesseract ha encontrado en la imagen entera es más
    # preciso. En el árbol de las plantas, las del asistente mandaban y salían letras
    # enormes con el marco del recuadro borrado.
    del_asistente = cajas_vigentes(lectura)
    for s, (r, _) in zip(situados, cambiar):
        cajas = del_asistente.get(texto_rotulo(r))
        if not s["ok"] and cajas and len(cajas) == len(r["lineas"]) and cajas_coherentes(cajas):
            s.update({"lineas": list(r["lineas"]), "cajas": [tuple(c) for c in cajas], "ok": True,
                      "del_asistente": True})
    # Y lo que aún falte, con la lectura de página de Tesseract (ver `localizar`).
    de_pagina: list = []

    def pagina() -> list:
        if not de_pagina:
            de_pagina.extend(localizar(origen, [r for r, _ in cambiar], idioma_origen, pagina=True))
        return de_pagina

    if not all(s["ok"] for s in situados):
        for i, (s, o) in enumerate(zip(situados, pagina())):
            if not s["ok"] and o["ok"]:
                situados[i] = o
    perdidos = [" / ".join(s["lineas"]) for s in situados if not s["ok"]]
    if perdidos:
        return {"estado": "no localizado",
                "detalle": f"no se encuentra «{perdidos[0]}» en la imagen"
                           + (f" ni otros {len(perdidos) - 1}" if len(perdidos) > 1 else "")}
    with Image.open(origen) as im:
        formato = im.format
        alfa = im.getchannel("A") if "A" in im.getbands() else None
        px = np.array(im.convert("RGB"))
    medidas = []
    for i, s in enumerate(situados):
        try:
            medidas.append(_medir_rotulo(px, s))
        except ValueError as e:
            # Una caja del asistente que no casa con la tinta: en las versiones pequeñas
            # del árbol, la lectura de página de Tesseract sí da la buena.
            o = pagina()[i] if s.get("del_asistente") else None
            try:
                if not (o and o["ok"]):
                    raise e
                medidas.append(_medir_rotulo(px, o))
                situados[i] = o
            except ValueError as e2:
                return {"estado": "fondo no liso" if "fondo" in str(e2) else "no medido",
                        "detalle": f"«{' / '.join(s['lineas'])}»: {e2}"}
    letras = _elegir_letra(medidas)
    hechos, ocupado = [], []
    for s, (_, nuevo), medida, (ruta, tamano) in zip(situados, cambiar, medidas, letras):
        hecho = _rehacer_rotulo(px, s, nuevo, ocupado, medida, ruta, tamano)
        hecho["original"], hecho["nuevo"] = " / ".join(s["lineas"]), nuevo
        hechos.append(hecho)
        if hecho["estado"] not in ("reescrito", "reducido", "una línea más", "recuadro ampliado"):
            return {"estado": hecho["estado"],
                    "detalle": f"«{hecho['original']}»: {hecho.get('detalle', '')}", "rotulos": hechos}
    resultado = Image.fromarray(px)
    if alfa is not None:
        resultado.putalpha(alfa)
    salida.parent.mkdir(parents=True, exist_ok=True)
    if (formato or "").upper() == "JPEG":
        resultado.convert("RGB").save(salida, "JPEG", quality=93, subsampling=0)
    else:
        resultado.save(salida, formato or "PNG")
    return {"estado": "reescrita", "rotulos": hechos}
