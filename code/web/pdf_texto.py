"""Texto de los PDF maquetados: se reescribe en su sitio, sin rehacer el documento.

Las fichas, la rúbrica y los diagramas que acompañan a una unidad no se pueden regenerar
desde ella, como la versión imprimible: son documentos hechos a mano en otras
herramientas. Lo que se hace es cambiar el texto y dejar todo lo demás tal cual.

Hay dos casos, y el PDF dice cuál es:

- **Texto vectorial** (Illustrator, Word): el texto es texto. Se borra solo el texto
  —el dibujo y las imágenes no se tocan— y se escribe el nuevo en la misma posición, con
  la misma fuente o su equivalente libre.
- **Texto dibujado en una imagen** (los mapas mentales de react-pdf): la página es una
  imagen con el texto ya pintado y, encima, una capa de texto **invisible** que solo sirve
  para seleccionar y buscar. Hay que borrar el texto de la imagen sin tocar las líneas del
  mapa y escribir el nuevo, visible. La capa invisible no coincide del todo con la imagen
  (hasta 10 pt en alguna rama), así que posición, tamaño y color se miden en la imagen.

Nada se da por bueno solo. Si el texto nuevo no cabe se intenta, por este orden, alargar
el subrayado (solo en una hoja del mapa, donde no hay nada a la derecha), reducir la letra
y añadir una línea si encima hay hueco. Si aun así no cabe, se escribe reducido y el
informe lo dice: el editor tiene que mirarlo.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pymupdf

# Resolución a la que se analiza la página: 2 píxeles por punto basta para medir
# baselines y huecos al cuarto de punto.
_ZOOM = 2.0

# La fuente con la que se dibujó el texto de las imágenes no está en el PDF: la capa
# invisible dice Noto Sans, pero lo pintado es Montserrat. Medido sobre las fichas de
# Biología, glifo a glifo.
PERFILES_IMAGEN = {
    "react-pdf": {"cuerpo": "Montserrat-Medium", "titulo": "Montserrat-Bold"},
}

# Fuentes comerciales y su equivalente libre con las mismas métricas.
EQUIVALENTES = {
    "ArialMT": "LiberationSans-Regular", "Arial": "LiberationSans-Regular",
    "Arial-BoldMT": "LiberationSans-Bold", "Arial-Bold": "LiberationSans-Bold",
    "Georgia": "Gelasio-Regular", "Georgia-Bold": "Gelasio-Bold",
    # Aptos no tiene equivalente libre de mismas métricas. Noto Sans Display es la que más
    # se le parece en diseño, y al 97 % ocupa lo mismo: medido sobre 268 líneas del resumen
    # de Biología, con una dispersión del 0,8 %. Cambiar la fuente del resumen está aceptado.
    "Aptos": "NotoSansDisplay-Regular", "Aptos,Bold": "NotoSansDisplay-Bold",
    "Aptos-Bold": "NotoSansDisplay-Bold",
}

# Cuerpo con el que se escribe la fuente equivalente respecto al original.
ESCALAS = {"Aptos": 0.97, "Aptos,Bold": 0.97, "Aptos-Bold": 0.97}

# Fuentes de símbolos: una viñeta o un icono al principio de una línea se deja donde está.
_SIMBOLICAS = re.compile(r"symbol|wingding|webding|dingbat", re.I)
_VINETAS = set("•◦▪■●○►▶✓✔❖")


# ------------------------------------------------------------------- fuentes

def _directorios() -> list[Path]:
    dirs = [os.environ.get("SARELA_FUENTES"), "/usr/share/fonts", "/usr/local/share/fonts",
            "C:/Windows/Fonts",
            str(Path(os.environ.get("LOCALAPPDATA", "-")) / "Microsoft/Windows/Fonts")]
    return [Path(d) for d in dirs if d and Path(d).is_dir()]


@lru_cache(maxsize=None)
def _indice_fuentes() -> dict[str, Path]:
    indice: dict[str, Path] = {}
    for d in _directorios():
        # TrueType antes que OpenType: Debian instala las dos de Montserrat y la probada
        # contra las fichas es la TrueType.
        for f in sorted(d.rglob("*"), key=lambda f: f.suffix.lower() != ".ttf"):
            if f.suffix.lower() in (".ttf", ".otf"):
                indice.setdefault(f.stem.lower(), f)
    return indice


@lru_cache(maxsize=4096)
def _tinta(ruta: str, texto: str) -> tuple[float, float]:
    """Dónde empieza y acaba la tinta de un texto respecto a su origen, a 1 pt.

    El avance tipográfico incluye el margen de cada glifo, y lo que se mide en la imagen
    es tinta: comparar una cosa con la otra hacía que un texto casi igual de largo no
    cupiera por medio punto.
    """
    from PIL import Image, ImageDraw, ImageFont
    cuerpo = 64
    f = ImageFont.truetype(ruta, cuerpo)
    lienzo = Image.new("L", (int(f.getlength(texto)) + 2 * cuerpo, 2 * cuerpo), 0)
    ImageDraw.Draw(lienzo).text((cuerpo // 2, cuerpo // 3), texto, font=f, fill=255)
    caja = lienzo.getbbox()
    if not caja:
        return 0.0, f.getlength(texto) / cuerpo
    return (caja[0] - cuerpo // 2) / cuerpo, (caja[2] - cuerpo // 2) / cuerpo


def fuente(nombre: str) -> Path | None:
    """El fichero de una fuente por su nombre PostScript, sin el prefijo de subconjunto."""
    nombre = nombre.split("+", 1)[-1]
    nombre = EQUIVALENTES.get(nombre, nombre)
    return _indice_fuentes().get(nombre.lower())


# ------------------------------------------------------------------- lectura

@dataclass
class Nodo:
    """Un bloque de texto con sentido propio: un rótulo del mapa, un título, una celda."""
    pagina: int
    lineas: list[str]
    rects: list[pymupdf.Rect]      # de cada línea, en la capa de texto
    bases: list[float]             # baseline de cada línea, en la capa de texto
    tamano: float
    fuente: str
    color: tuple[float, float, float]
    invisible: bool
    medida: dict = field(default_factory=dict)  # lo medido en la imagen, si es de imagen
    en_imagen: bool = False  # leído de la imagen, sin capa de texto: sus medidas son de imagen
    fuentes: dict = field(default_factory=dict)  # {negrita: nombre de la fuente}
    rico: bool = False       # mezcla normal y negrita: las líneas llevan marcas <b>…</b>

    @property
    def texto(self) -> str:
        texto = ""
        for linea in self.lineas:
            linea = linea.strip()
            if texto.endswith("-") and linea[:1].islower():
                texto = texto[:-1] + linea
            else:
                texto = f"{texto} {linea}" if texto else linea
        if self.rico:
            # Los espacios, fuera de las marcas, y dos tramos en negrita seguidos, uno solo.
            texto = re.sub(r"\s+</b>", "</b> ", texto)
            texto = re.sub(r"<b>\s+", " <b>", texto)
            texto = re.sub(r"</b>(\s*)<b>", r"\1", texto)
            texto = texto.replace("<b></b>", "")
        return re.sub(r"\s+", " ", texto).strip()

    @property
    def rect(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.rects[0])
        for otro in self.rects[1:]:
            r |= otro
        return r


def _color(entero: int) -> tuple[float, float, float]:
    return ((entero >> 16 & 255) / 255, (entero >> 8 & 255) / 255, (entero & 255) / 255)


def _lineas_invisibles(pagina: pymupdf.Page) -> set[tuple[int, int]]:
    """Posiciones (x, y) redondeadas de los caracteres que no se ven."""
    ocultos = set()
    for traza in pagina.get_texttrace():
        if traza["type"] == 3 or traza.get("opacity", 1) == 0:
            for ch in traza["chars"]:
                ocultos.add((round(ch[2][0]), round(ch[2][1])))
    return ocultos


def _tramos(linea: dict) -> list[list[tuple[dict, str, pymupdf.Rect]]]:
    """Parte una línea donde hay un hueco en blanco de más de tres cuerpos de letra.

    En la rúbrica, «Nome:» y «Grupo:» van en la misma línea separadas por 51 espacios:
    son dos rótulos, y traducidos juntos se escribirían seguidos. Cada tramo es una lista
    de (span, carácter, caja de tinta); los espacios del hueco no entran en ningún tramo.
    Con un umbral de un cuerpo se partían también las líneas justificadas y las viñetas
    del resumen de Word. También se parte en tres espacios seguidos o más: la cabecera del
    resumen separa «1º ESO» de «BIOLOXÍA E XEOLOXÍA» con cuatro, y traducidas juntas se
    escribían con uno solo.
    """
    tramos: list[list] = [[]]
    ultimo_x1, blancos = None, []
    for span in linea["spans"]:
        for ch in span["chars"]:
            c, r = ch["c"], pymupdf.Rect(ch["bbox"])
            if not c.strip():
                blancos.append((span, c, r))
                continue
            if ultimo_x1 is not None and (r.x0 - ultimo_x1 > 3 * span["size"] or len(blancos) >= 3):
                tramos.append([])
            elif blancos and tramos[-1]:
                tramos[-1] += blancos
            blancos = []
            tramos[-1].append((span, c, r))
            ultimo_x1 = r.x1
    return [t for t in tramos if t]


def _familia(nombre: str) -> str:
    """«Aptos,Bold» → «Aptos»; «Arial-BoldMT» → «Arial»."""
    nombre = nombre.split("+", 1)[-1]
    return re.sub(r"[,-]?(Bold|Semibold|Black|Heavy)?(MT)?$", "", nombre, flags=re.I)


def _negrita(span: dict) -> bool:
    return "bold" in span["font"].lower() or bool(span["flags"] & 16)


def _sin_marca(tramo: list) -> tuple[list, bool]:
    """Quita la viñeta o el icono del principio de un tramo. Se deja en la página tal cual:
    lo que se traduce y se reescribe es el texto que la sigue."""
    i = 0
    while i < len(tramo) and (_SIMBOLICAS.search(tramo[i][0]["font"]) or tramo[i][1] in _VINETAS):
        i += 1
    # Las viñetas de segundo nivel de Word son una «o» en Courier New: un solo carácter, en
    # otra fuente que el texto y seguido de un espacio.
    if i == 0 and len(tramo) > 2 and not tramo[1][1].strip():
        siguiente = next((t for t in tramo[2:] if t[1].strip()), None)
        if siguiente and siguiente[0]["font"] != tramo[0][0]["font"] and tramo[0][1] in "o-–·*":
            i = 1
    if i == 0:
        return tramo, False
    while i < len(tramo) and not tramo[i][1].strip():
        i += 1
    return tramo[i:], True


def _estilos(tramo: list) -> list[tuple[str, bool, str]]:
    """El tramo en trozos de un mismo peso: (texto, negrita, fuente)."""
    trozos: list[list] = []
    for span, c, _ in tramo:
        b = _negrita(span)
        if trozos and (trozos[-1][1] == b or not c.strip()):
            trozos[-1][0] += c
        else:
            trozos.append([c, b, span["font"]])
    return [tuple(t) for t in trozos]


def nodos(pdf: Path | pymupdf.Document, lecturas: list[dict] | None = None) -> list[Nodo]:
    """Los bloques de texto de un PDF, en orden de lectura.

    Las líneas se agrupan en un nodo cuando van una debajo de otra, alineadas a la
    izquierda, con la misma letra y el interlineado de un párrafo.
    """
    doc = pdf if isinstance(pdf, pymupdf.Document) else pymupdf.open(pdf)
    resultado: list[Nodo] = []
    for pagina in doc:
        ocultos = _lineas_invisibles(pagina)
        lineas, vinetas = [], []
        for bloque in pagina.get_text("rawdict")["blocks"]:
            if bloque["type"] != 0:
                continue
            for linea in bloque["lines"]:
                if linea["dir"] != (1.0, 0.0):
                    continue
                for tramo in _tramos(linea):
                    antes = tramo
                    tramo, marca = _sin_marca(tramo)
                    texto = "".join(c for _, c, _ in tramo).strip()
                    if not texto:
                        # Una viñeta sola: Word la escribe a veces como una línea aparte, con
                        # el texto en otra a su derecha. Se apunta para asociársela.
                        if marca:
                            caja = pymupdf.Rect(antes[0][2])
                            for _, _, r in antes:
                                caja |= r
                            vinetas.append(caja)
                        continue
                    # La fuente de la línea es la que más caracteres tiene, no la del tramo más
                    # largo: una palabra en negrita no convierte la línea en negrita.
                    cuenta: dict[str, int] = {}
                    por_fuente = {}
                    for sp, c, _ in tramo:
                        if c.strip():
                            cuenta[sp["font"]] = cuenta.get(sp["font"], 0) + 1
                            por_fuente.setdefault(sp["font"], sp)
                    s = por_fuente[max(cuenta, key=cuenta.get)]
                    rect = pymupdf.Rect(tramo[0][2])
                    for _, _, r in tramo:
                        rect |= r
                    o = s["chars"][0]["origin"]
                    tramos_estilo = _estilos(tramo)
                    lineas.append({
                        "texto": texto, "rect": rect, "base": o[1],
                        "x0": rect.x0, "tamano": round(s["size"], 1), "fuente": s["font"],
                        "color": _color(s["color"]), "marca": marca, "estilos": tramos_estilo,
                        "fuentes": {b: f for _, b, f in reversed(tramos_estilo)},
                        "invisible": (round(o[0]), round(o[1])) in ocultos,
                    })
        # Una línea que tiene a su izquierda, a su misma altura, una viñeta suelta empieza un
        # punto de lista: no es la continuación del párrafo de arriba.
        for l in lineas:
            if not l["marca"]:
                centro = (l["rect"].y0 + l["rect"].y1) / 2
                l["marca"] = any(v.y0 <= centro <= v.y1 and v.x1 <= l["x0"] + 1
                                 and l["x0"] - v.x1 < 4 * l["tamano"] for v in vinetas)
        lineas.sort(key=lambda l: (round(l["x0"]), l["base"]))
        usadas = [False] * len(lineas)
        for i, l in enumerate(lineas):
            if usadas[i]:
                continue
            usadas[i] = True
            grupo = [l]
            for j in range(i + 1, len(lineas)):
                m, ultima = lineas[j], grupo[-1]
                if usadas[j] or abs(m["x0"] - ultima["x0"]) > 3 or m["marca"]:
                    continue
                paso = m["base"] - ultima["base"]
                # Una línea que cierra un paréntesis, o una frase sin llenar el ancho, seguida
                # de otra que abre algo nuevo, son dos textos: dos notas de la rúbrica.
                cierra = ultima["texto"].rstrip()[-1:] in ".!?)"
                abre = m["texto"].lstrip()[:1] == "(" or m["texto"].lstrip()[:1].isupper()
                corta = ultima["rect"].width < 0.85 * max(g["rect"].width for g in grupo + [m])
                if cierra and abre and (corta or ultima["texto"].rstrip().endswith(")")):
                    continue
                # La misma letra, o la misma familia si alguna de las dos líneas mezcla pesos:
                # una primera línea casi toda en negrita sigue siendo del mismo párrafo.
                mezcla = any(len({x for t, x, _ in g["estilos"] if t.strip()}) > 1 for g in (m, ultima))
                misma = m["fuente"] == ultima["fuente"] or (
                    mezcla and _familia(m["fuente"]) == _familia(ultima["fuente"]))
                if (m["tamano"] == ultima["tamano"] and misma
                        and 0.8 * ultima["tamano"] <= paso <= 1.45 * ultima["tamano"]):
                    grupo.append(m)
                    usadas[j] = True
            estilos = {b for g in grupo for t, b, _ in g["estilos"] if t.strip()}
            rico = len(estilos) > 1
            fuentes_nodo: dict[bool, str] = {}
            for g in grupo:
                for b, f in g["fuentes"].items():
                    fuentes_nodo.setdefault(b, f)
            resultado.append(Nodo(
                pagina=pagina.number,
                lineas=["".join(f"<b>{t}</b>" if b else t for t, b, _ in g["estilos"]) if rico
                        else g["texto"] for g in grupo],
                rects=[g["rect"] for g in grupo], bases=[g["base"] for g in grupo],
                tamano=grupo[0]["tamano"], fuente=grupo[0]["fuente"], color=grupo[0]["color"],
                invisible=grupo[0]["invisible"], fuentes=fuentes_nodo, rico=rico))
        # Los textos pintados sin capa que ya se han leído de la imagen entran como un nodo
        # más, con el cuerpo más frecuente de la página.
        propias = [l for l in lecturas or [] if l["pagina"] == pagina.number + 1]
        if propias:
            tamanos = [l["tamano"] for l in lineas] or [12.0]
            cuerpo = max(set(tamanos), key=tamanos.count)
            for lectura in propias:
                r = pymupdf.Rect(lectura["rect"])
                n = max(1, len(lectura["lineas"]))
                alto = r.height / n
                resultado.append(Nodo(
                    pagina=pagina.number, lineas=list(lectura["lineas"]), rects=[r],
                    bases=[r.y0 + alto * (i + 0.8) for i in range(n)], tamano=cuerpo, fuente="",
                    color=(0.0, 0.0, 0.0), invisible=True, en_imagen=True))
    resultado.sort(key=lambda n: (n.pagina, n.rect.y0, n.rect.x0))
    return resultado


def textos(pdf: Path, lecturas: list[dict] | None = None) -> list[str]:
    """Lo que hay que traducir de un PDF, sin repetir y en orden de lectura."""
    vistos, orden = set(), []
    for n in nodos(pdf, lecturas):
        if n.texto and n.texto not in vistos:
            vistos.add(n.texto)
            orden.append(n.texto)
    return orden


def es_imagen_con_capa(doc: pymupdf.Document, pagina: pymupdf.Page) -> bool:
    """La página es una imagen que la cubre entera con una capa de texto invisible."""
    ocultos = _lineas_invisibles(pagina)
    if not ocultos:
        return False
    area = abs(pagina.rect)
    return any(abs(r) >= 0.9 * area
               for img in pagina.get_images(full=True)
               for r in pagina.get_image_rects(img[0]))


# ------------------------------------------------------------------- la imagen

class _Imagen:
    """La imagen de fondo de una página, en píxeles, con conversión desde puntos."""

    def __init__(self, doc: pymupdf.Document, pagina: pymupdf.Page):
        img = max(pagina.get_images(full=True),
                  key=lambda i: max(abs(r) for r in pagina.get_image_rects(i[0])))
        self.xref = img[0]
        self.rect = pagina.get_image_rects(self.xref)[0]
        pix = pymupdf.Pixmap(doc, self.xref)
        if pix.n not in (3, 4) or pix.colorspace.n != 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        self.px = (np.frombuffer(pix.samples, dtype=np.uint8)
                   .reshape(pix.height, pix.width, pix.n)[:, :, :3].copy())
        self.ex = pix.width / self.rect.width
        self.ey = pix.height / self.rect.height
        self.lineas = self._paleta_lineas()

    def a_px(self, x: float, y: float) -> tuple[int, int]:
        return (int(round((x - self.rect.x0) * self.ex)), int(round((y - self.rect.y0) * self.ey)))

    def a_pt(self, c: float, f: float) -> tuple[float, float]:
        return (c / self.ex + self.rect.x0, f / self.ey + self.rect.y0)

    def _paleta_lineas(self) -> np.ndarray:
        """Los colores de las líneas del dibujo: los de los tramos horizontales largos.

        Un subrayado es una fila de píxeles del mismo color a lo largo de decenas de
        puntos; el texto nunca, porque entre letra y letra hay blanco.
        """
        minimo = int(40 * self.ex)
        colores = []
        filas = self.px[::2].astype(np.int16)
        tinta = (765 - filas.sum(axis=2)) > 150
        for f in range(filas.shape[0]):
            fila = tinta[f]
            if fila.sum() < minimo:
                continue
            cambios = np.diff(np.concatenate(([0], fila.view(np.int8), [0])))
            for a, b in zip(np.where(cambios == 1)[0], np.where(cambios == -1)[0]):
                if b - a >= minimo:
                    tramo = filas[f, a:b]
                    if tramo.std(axis=0).max() < 12:
                        colores.append(np.median(tramo, axis=0))
        if not colores:
            return np.zeros((0, 3))
        colores = np.array(colores)
        paleta: list[np.ndarray] = []
        for c in colores:
            if all(np.abs(c - p).max() > 30 for p in paleta):
                paleta.append(c)
        # Solo el color puro de cada línea: las filas de antialias dan mezclas con blanco
        # que, tomadas por colores de línea, protegían también el borde del texto.
        puros = []
        for c in paleta:
            u = c - 255
            mezcla = False
            for q in paleta:
                v = q - 255
                if q is c or (v @ v) <= (u @ u):
                    continue
                t = (u @ v) / (v @ v)
                if 0 < t < 0.95 and np.linalg.norm(u - t * v) < 20:
                    mezcla = True
            if not mezcla:
                puros.append(c)
        return np.array(puros)

    def es_linea(self, bloque: np.ndarray, fuerte: float = 0.15) -> np.ndarray:
        """Píxeles que son de una línea del dibujo: blanco mezclado con un color de línea."""
        u = bloque.astype(np.float32) - 255
        resultado = np.zeros(bloque.shape[:2], dtype=bool)
        for c in self.lineas:
            v = c.astype(np.float32) - 255
            t = np.clip((u @ v) / (v @ v), 0, 1)
            residuo = np.linalg.norm(u - t[..., None] * v, axis=-1)
            resultado |= (residuo < 22) & (t > fuerte)
        return resultado


def _bandas(filas: np.ndarray, minimo: int) -> list[tuple[int, int]]:
    """Tramos de filas consecutivas con tinta, uniendo los que son solo una tilde."""
    hay = filas > 0
    bandas, inicio = [], None
    for i, h in enumerate(hay):
        if h and inicio is None:
            inicio = i
        elif not h and inicio is not None:
            bandas.append([inicio, i])
            inicio = None
    if inicio is not None:
        bandas.append([inicio, len(hay)])
    # Una tilde o un punto sueltos encima de la línea forman su propia banda: se unen a
    # la siguiente.
    unidas: list[list[int]] = []
    for b in bandas:
        if unidas and unidas[-1][1] - unidas[-1][0] < minimo:
            unidas[-1][1] = b[1]
        else:
            unidas.append(b)
    if len(unidas) > 1 and unidas[-1][1] - unidas[-1][0] < minimo:
        ultima = unidas.pop()
        unidas[-1][1] = ultima[1]
    return [tuple(b) for b in unidas]


def _ajuste(imagen: _Imagen, nodos_pagina: list[Nodo]) -> tuple[tuple[float, float], tuple[float, float]]:
    """Cómo pasar de la capa invisible a la imagen: `x' = a·x + b` y `y' = c·y + d`.

    La capa no está solo desplazada: está a otra escala, un 2 % en las fichas de
    Biología. Arriba la imagen queda 7 pt por debajo de la capa y abajo 15 pt por encima,
    y con ventanas de medida fijas los últimos rótulos se medían cortados. Se ajusta con
    todos los rótulos y se descartan los que se desvían, que son los mal medidos.
    """
    capa_x, img_x, capa_y, img_y = [], [], [], []
    for n in nodos_pagina:
        if n.en_imagen:
            continue
        m = _medir(imagen, n)
        if m:
            capa_x.append(n.rects[0].x0)
            img_x.append(m["x0"])
            capa_y += n.bases
            img_y += m["bases"]
    if len(capa_y) < 4:
        return (1.0, 0.0), (1.0, 0.0)

    def recta(a: list[float], b: list[float]) -> tuple[float, float]:
        a, b = np.array(a), np.array(b)
        k, c = np.polyfit(a, b, 1)
        buenos = np.abs(b - (k * a + c)) < 2.5
        if buenos.sum() >= 4:
            k, c = np.polyfit(a[buenos], b[buenos], 1)
        return float(k), float(c)

    return recta(capa_x, img_x), recta(capa_y, img_y)


def _medir(imagen: _Imagen, nodo: Nodo, ajuste=None) -> dict | None:
    """Dónde está pintado el texto de un nodo en la imagen, y cómo."""
    t = nodo.tamano
    (kx, cx), (ky, cy) = ajuste or ((1.0, 0.0), (1.0, 0.0))
    # Sin ajuste la ventana tiene que absorber el desfase de la capa; con él, basta un margen.
    arriba, abajo = (1.6, 0.9) if ajuste is None else (1.25, 0.6)
    if nodo.en_imagen:
        # Ya está en coordenadas de la imagen: la zona donde se encontró, con un margen.
        x0, y0 = imagen.a_px(nodo.rect.x0 - 0.4 * t, nodo.rect.y0 - 0.4 * t)
        x1, y1 = imagen.a_px(nodo.rect.x1 + 0.4 * t, nodo.rect.y1 + 0.4 * t)
    else:
        x0, y0 = imagen.a_px(kx * nodo.rect.x0 + cx - 20, ky * nodo.bases[0] + cy - arriba * t)
        x1, y1 = imagen.a_px(kx * nodo.rect.x1 + cx + 30, ky * nodo.bases[-1] + cy + abajo * t)
    x0, y0 = max(x0, 0), max(y0, 0)
    bloque = imagen.px[y0:y1, x0:x1]
    tinta = (765 - bloque.astype(np.int16).sum(axis=2)) > 90
    texto = tinta & ~imagen.es_linea(bloque)
    if texto.sum() < 20:
        return None
    filas = texto.sum(axis=1)
    bandas = _bandas(filas, minimo=int(0.3 * t * imagen.ey))
    if len(bandas) != len(nodo.lineas):
        return None

    bases, anchos, izquierda, derecha = [], [], [], []
    for a, b in bandas:
        cuenta = filas[a:b]
        base = a + max(i for i, c in enumerate(cuenta) if c >= 0.4 * cuenta.max())
        bases.append(imagen.a_pt(0, y0 + base + 1)[1])
        cols = np.where(texto[a:b].any(axis=0))[0]
        izquierda.append(cols.min())
        derecha.append(cols.max())
        anchos.append((cols.max() - cols.min() + 1) / imagen.ex)

    # Alineación: en un bloque de varias líneas, qué borde se repite. El título raíz de un
    # mapa va centrado; los rótulos, a la izquierda.
    alineacion, eje = "izquierda", None
    if len(bandas) > 1:
        bordes = np.array(izquierda) / imagen.ex
        centros = (np.array(izquierda) + np.array(derecha)) / 2 / imagen.ex
        if np.ptp(bordes) > 3 and np.ptp(centros) < np.ptp(bordes) / 2:
            alineacion = "centro"
            eje = imagen.a_pt(x0 + float(np.mean((np.array(izquierda) + np.array(derecha)) / 2)), 0)[0]

    ys, xs = np.where(texto)
    caja = (x0 + xs.min(), y0 + ys.min(), x0 + xs.max() + 1, y0 + ys.max() + 1)
    oscuros = bloque[texto]
    oscuros = oscuros[oscuros.astype(int).sum(axis=1) <= np.percentile(
        oscuros.astype(int).sum(axis=1), 30)]
    color = tuple(float(c) / 255 for c in np.median(oscuros, axis=0))

    # El subrayado: la fila de color de línea más larga justo debajo del texto. Se guarda
    # en píxeles, con las filas de antialias, para poder alargarlo copiándolo.
    subrayado = None
    abajo0 = caja[3] + 1
    abajo1 = min(imagen.px.shape[0], caja[3] + int(1.1 * t * imagen.ey))
    desde = max(0, caja[0] - int(15 * imagen.ex))
    if abajo1 > abajo0:
        franja = imagen.px[abajo0:abajo1, desde:min(imagen.px.shape[1], caja[2] + int(40 * imagen.ex))]
        linea = imagen.es_linea(franja, fuerte=0.15)
        cuenta = linea.sum(axis=1)
        if cuenta.max() > 0.6 * (caja[2] - caja[0]):
            f = int(cuenta.argmax())
            filas_linea = [i for i in range(len(cuenta)) if cuenta[i] >= 0.3 * cuenta.max()]
            # Solo las filas contiguas a la principal: debajo puede asomar otra rama.
            r0 = r1 = f
            while r0 - 1 in filas_linea:
                r0 -= 1
            while r1 + 1 in filas_linea:
                r1 += 1
            cols = np.where(linea[f])[0]
            subrayado = {
                "px": (desde + int(cols.min()), abajo0 + r0, desde + int(cols.max()) + 1, abajo0 + r1 + 1),
                "x0": imagen.a_pt(desde + cols.min(), 0)[0],
                "x1": imagen.a_pt(desde + cols.max() + 1, 0)[0],
                "y0": imagen.a_pt(0, abajo0 + r0)[1],
                "y1": imagen.a_pt(0, abajo0 + r1 + 1)[1],
            }

    return {
        "caja_px": caja,
        "x0": imagen.a_pt(x0 + min(izquierda), 0)[0],
        "x1": imagen.a_pt(x0 + max(derecha) + 1, 0)[0],
        "bases": bases, "anchos": anchos, "color": color, "subrayado": subrayado,
        "alineacion": alineacion, "eje": eje,
    }


def _borrar(imagen: _Imagen, caja: tuple[int, int, int, int]) -> None:
    """Pinta de blanco el texto de la caja y deja las líneas del dibujo que la crucen."""
    x0, y0, x1, y1 = caja
    x0, y0 = max(0, x0 - 2), max(0, y0 - 2)
    x1, y1 = min(imagen.px.shape[1], x1 + 2), min(imagen.px.shape[0], y1 + 2)
    bloque = imagen.px[y0:y1, x0:x1]
    conservar = imagen.es_linea(bloque, fuerte=0.5)
    bloque[~conservar] = 255


def _alargar_subrayado(imagen: _Imagen, subrayado: dict, hasta_pt: float) -> None:
    """Alarga un subrayado de la imagen copiando su propia columna: sale idéntico."""
    x0, y0, x1, y1 = subrayado["px"]
    hasta = min(imagen.px.shape[1] - 1, imagen.a_px(hasta_pt, 0)[0])
    if hasta <= x1:
        return
    cola = imagen.px[y0:y1, x1 - 3:x1].copy()        # el remate antialias del final
    columna = imagen.px[y0:y1, x1 - 6:x1 - 5].copy()  # una columna llena
    imagen.px[y0:y1, x1 - 3:hasta - 3] = columna
    imagen.px[y0:y1, hasta - 3:hasta] = cola


def _alargar_subrayado_izq(imagen: _Imagen, subrayado: dict, desde_pt: float) -> None:
    """Lo mismo por el extremo izquierdo, para las ramas que salen a la izquierda de la raíz."""
    x0, y0, x1, y1 = subrayado["px"]
    desde = max(0, imagen.a_px(desde_pt, 0)[0])
    if desde >= x0:
        return
    cabeza = imagen.px[y0:y1, x0:x0 + 3].copy()
    columna = imagen.px[y0:y1, x0 + 5:x0 + 6].copy()
    imagen.px[y0:y1, desde + 3:x0 + 3] = columna
    imagen.px[y0:y1, desde:desde + 3] = cabeza


def _texto_sin_capa(imagen: _Imagen, cubiertas: list[tuple[int, int, int, int]],
                    tamano: float) -> list[pymupdf.Rect]:
    """Zonas de la imagen con texto pintado que ninguna capa de texto describe.

    Pasa: en la ficha de las rocas, dos rótulos del mapa no tienen capa. No se pueden
    traducir sin leerlos de la imagen, y el informe tiene que decirlo.
    """
    celda = max(2, int(0.25 * tamano * imagen.ex))
    alto, ancho = imagen.px.shape[:2]
    filas, cols = alto // celda, ancho // celda
    recorte = imagen.px[:filas * celda, :cols * celda]
    tinta = (765 - recorte.astype(np.int16).sum(axis=2)) > 150
    texto = tinta & ~imagen.es_linea(recorte, fuerte=0.15)
    for x0, y0, x1, y1 in cubiertas:
        texto[max(0, y0 - celda):y1 + celda, max(0, x0 - celda):x1 + celda] = False
    rejilla = texto.reshape(filas, celda, cols, celda).sum(axis=(1, 3)) >= 3
    # Grupos de celdas vecinas, uniendo las separadas por el espacio entre palabras.
    etiquetas = -np.ones(rejilla.shape, dtype=int)
    grupos: list[list[tuple[int, int]]] = []
    for f, c in zip(*np.nonzero(rejilla)):
        if etiquetas[f, c] >= 0:
            continue
        pila, grupo = [(f, c)], []
        etiquetas[f, c] = len(grupos)
        while pila:
            a, b = pila.pop()
            grupo.append((a, b))
            for da in (-1, 0, 1):
                for db in range(-4, 5):
                    na, nb = a + da, b + db
                    if 0 <= na < filas and 0 <= nb < cols and rejilla[na, nb] and etiquetas[na, nb] < 0:
                        etiquetas[na, nb] = len(grupos)
                        pila.append((na, nb))
        grupos.append(grupo)
    zonas = []
    for grupo in grupos:
        fs, cs = [g[0] for g in grupo], [g[1] for g in grupo]
        r = pymupdf.Rect(*imagen.a_pt(min(cs) * celda, min(fs) * celda),
                         *imagen.a_pt((max(cs) + 1) * celda, (max(fs) + 1) * celda))
        if r.width >= 2 * tamano and r.height >= 0.5 * tamano:
            zonas.append(r)
    return zonas


# ------------------------------------------------------------------- escritura

def _partir(texto: str, font: pymupdf.Font, tamano: float, ancho: float) -> list[str]:
    lineas, actual = [], ""
    for palabra in texto.split():
        prueba = f"{actual} {palabra}" if actual else palabra
        if actual and font.text_length(prueba, tamano) > ancho:
            lineas.append(actual)
            actual = palabra
        else:
            actual = prueba
    if actual:
        lineas.append(actual)
    return lineas


def _repartir(texto: str, font: pymupdf.Font, tamano: float, n: int) -> list[str]:
    """El texto en `n` líneas lo más parejas posible: el ancho mínimo que lo permite."""
    if n <= 1:
        return [texto]
    bajo, alto = font.text_length(texto, tamano) / n, font.text_length(texto, tamano)
    mejor = [texto]
    for _ in range(24):
        medio = (bajo + alto) / 2
        prueba = _partir(texto, font, tamano, medio)
        if len(prueba) <= n:
            mejor, alto = prueba, medio
        else:
            bajo = medio
    return mejor


class _Lienzo:
    """La página sin el texto que se sustituye, para comprobar dónde hay sitio.

    En una página de imagen es la propia imagen ya borrada; en una vectorial, la página
    renderizada después de quitar el texto.
    """

    def __init__(self, px: np.ndarray, a_px):
        self.px = px
        self.a_px = a_px
        self.ocupado: list[pymupdf.Rect] = []
        # El sitio del texto original de cada nodo que se va a reescribir: en una página
        # vectorial ya no se ve, pero otro nodo no debe crecer hasta él. Cada nodo puede
        # ocupar el suyo.
        self.reservado: list[tuple[pymupdf.Rect, int]] = []

    @classmethod
    def de_pagina(cls, pagina: pymupdf.Page) -> "_Lienzo":
        pix = pagina.get_pixmap(matrix=pymupdf.Matrix(_ZOOM, _ZOOM), alpha=False)
        px = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        return cls(px, lambda x, y: (int(round(x * _ZOOM)), int(round(y * _ZOOM))))

    def _zona(self, r: pymupdf.Rect) -> np.ndarray | None:
        x0, y0 = self.a_px(r.x0, r.y0)
        x1, y1 = self.a_px(r.x1, r.y1)
        if x0 < 0 or y0 < 0 or x1 > self.px.shape[1] or y1 > self.px.shape[0]:
            return None
        return self.px[y0:y1, x0:x1].astype(np.int16)

    def fondo(self, r: pymupdf.Rect) -> np.ndarray:
        zona = self._zona(r)
        if zona is None or not zona.size:
            return np.full(3, 255)
        return np.median(zona.reshape(-1, 3), axis=0)

    def libre_hasta(self, r: pymupdf.Rect, fondo: np.ndarray, maximo: float,
                    propio: int | None = None) -> float:
        """Hasta dónde sigue libre el fondo a la derecha de `r`: el borde de una celda o
        de una banda de color."""
        x0, y0 = self.a_px(r.x1, r.y0)
        x1, y1 = self.a_px(maximo, r.y1)
        zona = self._zona(pymupdf.Rect(r.x1, r.y0, maximo, r.y1))
        if zona is None or not zona.size:
            return r.x1
        ocupadas = (np.abs(zona - fondo).max(axis=2) > 40).sum(axis=0) > 1
        for o in self.ocupado + [o for o, d in self.reservado if d != propio]:
            if o.y1 > r.y0 and o.y0 < r.y1 and o.x0 > r.x1:
                maximo = min(maximo, o.x0)
        primera = int(np.argmax(ocupadas)) if ocupadas.any() else zona.shape[1]
        return min(maximo, r.x1 + primera * (maximo - r.x1) / max(1, zona.shape[1]))

    def libre(self, r: pymupdf.Rect, fondo: np.ndarray, propio: int | None = None) -> bool:
        if r.is_empty:
            return True
        if any(r.intersects(o) for o in self.ocupado):
            return False
        if any(r.intersects(o) for o, d in self.reservado if d != propio):
            return False
        zona = self._zona(r)
        if zona is None:
            return False
        return (np.abs(zona - fondo).max(axis=2) > 40).sum() <= 2


@dataclass
class _Plan:
    lineas: list[str]
    tamano: float
    bases: list[float]
    x0: float
    alargar_hasta: float | None = None
    eje: float | None = None        # centro de cada línea, si el bloque va centrado
    ancla_der: float | None = None  # borde derecho de la tinta, si crece hacia la izquierda
    alargar_desde: float | None = None
    ruta: str = ""                  # la fuente, para medir la tinta


# Alto que ocupa una línea respecto a su baseline, en fracción del cuerpo. Las mayúsculas
# acentuadas y la ele geminada llegan a 0,93 en Montserrat: con 0,8 un subrayado vecino
# alargado cruzaba la parte alta de las letras sin que la comprobación lo viera.
_SUBE, _BAJA = 0.95, 0.25


def _cajas(plan: _Plan, font: pymupdf.Font | None = None) -> list[pymupdf.Rect]:
    """La tinta de cada línea del plan: `x0` es donde empieza la tinta, no el origen."""
    t = plan.tamano
    cajas = []
    for linea, b in zip(plan.lineas, plan.bases):
        izq, der = _tinta(plan.ruta, linea)
        ancho = (der - izq) * t
        x = (plan.eje - ancho / 2 if plan.eje is not None
             else plan.ancla_der - ancho if plan.ancla_der is not None else plan.x0)
        cajas.append(pymupdf.Rect(x, b - _SUBE * t, x + ancho, b + _BAJA * t))
    return cajas


def _origen(plan: _Plan, linea: str, caja: pymupdf.Rect) -> float:
    """El origen tipográfico que pone la tinta de la línea donde dice su caja."""
    return caja.x0 - _tinta(plan.ruta, linea)[0] * plan.tamano


def _bases(bases: list[float], lineas: int, paso: float, abajo: bool = True) -> list[float]:
    """Baselines para `lineas` líneas.

    Con subrayado (`abajo`) la última línea queda donde estaba, sobre él, y se crece hacia
    arriba. Sin él —una celda de tabla— se conserva el centro del bloque: un texto de dos
    líneas que traducido cabe en una queda centrado en su celda, no pegado abajo.
    """
    if not abajo:
        centro = (bases[0] + bases[-1]) / 2
        return [centro + paso * (i - (lineas - 1) / 2) for i in range(lineas)]
    if lineas <= len(bases):
        return list(bases)[len(bases) - lineas:] if lineas < len(bases) else list(bases)
    extra = lineas - len(bases)
    return [bases[0] - paso * (extra - i) for i in range(extra)] + list(bases)


def _planificar(nuevo: str, font: pymupdf.Font, ruta: str, tamano: float, x0: float, bases: list[float],
                limite: float, lienzo: _Lienzo, fondo: np.ndarray, subrayado: dict | None,
                margen: float, borde: float, eje: float | None = None,
                propio: int | None = None, izquierda: dict | None = None) -> tuple[_Plan, str]:
    """Cómo escribir el texto nuevo en el sitio del viejo. Devuelve el plan y su estado.

    `izquierda` es para un rótulo de una rama que sale a la izquierda de la raíz: el
    extremo libre de su subrayado es el izquierdo, y si el texto no cabe se ancla a la
    derecha —junto a la curva— y crece hacia la izquierda.
    """
    n = len(bases)
    paso = (bases[1] - bases[0]) if n > 1 else 1.3 * tamano

    def probar_izq(escala: float, lineas_n: int, tope: float) -> _Plan | None:
        t = tamano * escala
        ancla = izquierda["ancla"]
        lineas = _partir(nuevo, font, t, ancla - tope + 0.2 * t)
        if len(lineas) > lineas_n:
            return None
        plan = _Plan(lineas, t, _bases(bases, len(lineas), paso, subrayado is not None),
                     x0, ancla_der=ancla, ruta=ruta)
        cajas = _cajas(plan, font)
        if min(c.x0 for c in cajas) < tope or not all(lienzo.libre(c, fondo, propio) for c in cajas):
            return None
        return plan

    def probar(escala: float, lineas_n: int, derecha: float) -> _Plan | None:
        t = tamano * escala
        # Cada línea se llena hasta el ancho disponible, como hacía el generador: el texto
        # corta donde cortaría el original, no en dos mitades iguales.
        # El reparto usa el avance, que excede a la tinta en los márgenes de los extremos.
        lineas = _partir(nuevo, font, t, derecha - x0 + 0.2 * t)
        if len(lineas) > lineas_n:
            return None
        plan = _Plan(lineas, t, _bases(bases, len(lineas), paso, subrayado is not None),
                     x0, eje=eje, ruta=ruta)
        cajas = _cajas(plan, font)
        if max(c.x1 for c in cajas) > derecha:
            return None
        if not all(lienzo.libre(c, fondo, propio) for c in cajas):
            return None
        return plan

    alargable = False
    if subrayado:
        ancho = (lambda iz, de: de - iz)(*_tinta(ruta, nuevo)) * tamano / n
        tira = pymupdf.Rect(subrayado["x1"], subrayado["y0"] - 1,
                            min(borde, x0 + ancho + margen + 2), subrayado["y1"] + 1)
        alargable = lienzo.libre(tira, fondo, propio)

    # Por orden de preferencia: tal cual; alargando el subrayado; un poco más pequeño; con
    # una línea más, que cambia más el aspecto; y, por último, bastante más pequeño.
    intentos = [(1.0, n, limite, "igual")]
    if alargable:
        intentos.append((1.0, n, borde, "subrayado alargado"))
    for escala in (0.96, 0.92):
        intentos.append((escala, n, limite, "reducido"))
        if alargable:
            intentos.append((escala, n, borde, "reducido"))
    for escala in (1.0, 0.96, 0.92):
        intentos.append((escala, n + 1, limite, "una línea más"))
    for escala in (0.88, 0.85):
        intentos += [(escala, n, limite, "reducido"), (escala, n + 1, limite, "reducido")]

    if izquierda and subrayado:
        # Rama izquierda: primero tal cual, alineado como estaba; si no cabe, anclado a la
        # derecha y alargando el subrayado por su extremo izquierdo.
        ancho = (lambda iz, de: de - iz)(*_tinta(ruta, nuevo)) * tamano / n
        tira = pymupdf.Rect(max(izquierda["borde"], izquierda["ancla"] - ancho - izquierda["margen"] - 2),
                            subrayado["y0"] - 1, subrayado["x0"], subrayado["y1"] + 1)
        alargable_izq = lienzo.libre(tira, fondo, propio)
        intentos_izq = [(1.0, n, "igual")]
        if alargable_izq:
            intentos_izq += [(1.0, n, "subrayado alargado"), (0.96, n, "reducido"),
                             (0.92, n, "reducido")]
        for escala, lineas_n, estado in intentos_izq:
            plan = (probar(escala, lineas_n, limite) if estado == "igual" and escala == 1.0
                    else probar_izq(escala, lineas_n, izquierda["borde"]))
            if plan:
                if plan.ancla_der is not None:
                    inicio = min(c.x0 for c in _cajas(plan, font)) - izquierda["margen"]
                    plan.alargar_desde = inicio if inicio < subrayado["x0"] else None
                return plan, estado

    for escala, lineas_n, derecha, estado in intentos:
        plan = probar(escala, lineas_n, derecha)
        if plan:
            if derecha == borde:
                fin = max(c.x1 for c in _cajas(plan, font)) + margen
                plan.alargar_hasta = fin if fin > subrayado["x1"] else None
            return plan, estado
    # No cabe de ninguna forma: se escribe lo más pequeño posible y se avisa.
    t = tamano * 0.85
    lineas = _repartir(nuevo, font, t, n + 1)
    return _Plan(lineas, t, _bases(bases, len(lineas), paso, subrayado is not None),
                     x0, eje=eje, ruta=ruta), "no cabe"


def _palabras(texto: str) -> list[list[tuple[str, bool]]]:
    """El texto con marcas <b>…</b> en palabras; cada palabra, en trozos de un peso.

    Solo se corta en los espacios: «<b>sólidos</b>,» es una palabra de dos trozos, y la
    coma no puede quedarse sola al principio de la línea siguiente.
    """
    palabras: list[list[tuple[str, bool]]] = [[]]
    negrita = False
    for trozo in re.split(r"(</?b>)", texto):
        if trozo in ("<b>", "</b>"):
            negrita = trozo == "<b>"
            continue
        for i, parte in enumerate(re.split(r"(\s+)", trozo)):
            if not parte:
                continue
            if parte.isspace():
                if palabras[-1]:
                    palabras.append([])
                continue
            palabras[-1].append((parte, negrita))
    return [p for p in palabras if p]


@dataclass
class _PlanRico:
    lineas: list[list[list[tuple[str, bool]]]]   # líneas → palabras → trozos
    tamano: float
    bases: list[float]
    x0s: list[float]


def _medida_rica(palabra: list[tuple[str, bool]], fonts: dict, t: float) -> float:
    return sum(fonts[b].text_length(texto, t) for texto, b in palabra)


def _planificar_rico(nuevo: str, fonts: dict, tamano: float, x0s: list[float],
                     bases: list[float], limite: float, paso: float, lienzo: _Lienzo,
                     fondo: np.ndarray, propio: int) -> tuple[_PlanRico, str]:
    """Como `_planificar`, para un párrafo de documento: con negritas dentro, anclado
    arriba —si necesita una línea más, crece hacia abajo— y con la sangría de cada línea."""
    palabras = _palabras(nuevo)
    n = len(bases)
    espacio = fonts[False if False in fonts else True]

    def lineas_para(t: float) -> list[list]:
        lineas, actual, ancho = [], [], 0.0
        blanco = espacio.text_length(" ", t)
        for p in palabras:
            x0 = x0s[min(len(lineas), len(x0s) - 1)]
            w = _medida_rica(p, fonts, t)
            if actual and x0 + ancho + blanco + w > limite:
                lineas.append(actual)
                actual, ancho = [p], w
            else:
                ancho += (blanco if actual else 0) + w
                actual.append(p)
        if actual:
            lineas.append(actual)
        return lineas

    def plan_de(t: float, lineas: list) -> _PlanRico:
        bs = list(bases[:len(lineas)]) + [bases[-1] + paso * (i + 1)
                                          for i in range(len(lineas) - n)]
        xs = [x0s[min(i, len(x0s) - 1)] for i in range(len(lineas))]
        return _PlanRico(lineas, t, bs, xs)

    def cajas(plan: _PlanRico) -> list[pymupdf.Rect]:
        blanco = espacio.text_length(" ", plan.tamano)
        return [pymupdf.Rect(x, b - _SUBE * plan.tamano,
                             x + sum(_medida_rica(p, fonts, plan.tamano) for p in linea)
                             + blanco * (len(linea) - 1), b + _BAJA * plan.tamano)
                for linea, b, x in zip(plan.lineas, plan.bases, plan.x0s)]

    intentos = [(1.0, n, "igual"), (0.96, n, "reducido"), (0.92, n, "reducido"),
                (1.0, n + 1, "una línea más"), (0.96, n + 1, "una línea más"),
                (0.88, n, "reducido"), (0.85, n, "reducido"), (0.88, n + 1, "reducido"),
                (0.85, n + 1, "reducido")]
    for escala, maximo, estado in intentos:
        t = tamano * escala
        lineas = lineas_para(t)
        if len(lineas) > maximo:
            continue
        plan = plan_de(t, lineas)
        if all(c.x1 <= limite + 0.5 and lienzo.libre(c, fondo, propio) for c in cajas(plan)):
            return plan, estado
    t = tamano * 0.85
    return plan_de(t, lineas_para(t)), "no cabe"


def _cajas_ricas(plan: _PlanRico, fonts: dict) -> list[pymupdf.Rect]:
    espacio = fonts[False if False in fonts else True]
    blanco = espacio.text_length(" ", plan.tamano)
    return [pymupdf.Rect(x, b - _SUBE * plan.tamano,
                         x + sum(_medida_rica(p, fonts, plan.tamano) for p in linea)
                         + blanco * (len(linea) - 1), b + _BAJA * plan.tamano)
            for linea, b, x in zip(plan.lineas, plan.bases, plan.x0s)]


def _calibrar(nodos_pagina: list[Nodo], perfil: dict) -> dict[float, tuple[str, float]]:
    """Fuente y tamaño reales de cada tamaño de la capa invisible, medidos en la imagen.

    El tamaño más frecuente es el cuerpo; los mayores, títulos. El tamaño se deduce del
    ancho que ocupa el texto pintado: la mediana de todas las líneas, para que todos los
    rótulos de un mismo nivel salgan iguales.
    """
    frecuencia: dict[float, int] = {}
    for n in nodos_pagina:
        frecuencia[n.tamano] = frecuencia.get(n.tamano, 0) + len(n.lineas)
    cuerpo = max(frecuencia, key=frecuencia.get)
    resultado = {}
    for tam in frecuencia:
        nombre = perfil["cuerpo"] if tam <= cuerpo else perfil["titulo"]
        ruta = fuente(nombre)
        if ruta is None:
            continue
        medidas = [ancho / (lambda iz, de: de - iz)(*_tinta(str(ruta), linea.strip()))
                   for n in nodos_pagina if n.tamano == tam and n.medida
                   for linea, ancho in zip(n.lineas, n.medida["anchos"])
                   if len(linea.strip()) >= 8]
        if medidas:
            resultado[tam] = (nombre, float(np.median(medidas)))
    return resultado


def _fila(pagina: pymupdf.Page, n: Nodo, estado: str, detalle: str) -> dict:
    return {"pagina": pagina.number + 1, "original": n.texto, "estado": estado, "detalle": detalle}


def imagenes(pdf: Path, maximo_px: int = 1400) -> list[dict]:
    """Las imágenes de un PDF de texto, para saber si llevan texto dentro.

    En el resumen de Biología, un recuadro entero («As augas do subsolo de Galicia…») es
    una imagen: no tiene capa de texto, así que ni se traducía ni se avisaba. Rehacer el
    texto de una foto o un diagrama es otro trabajo; lo que no puede pasar es que quede
    sin avisar. Cada imagen sale una vez, aunque se repita en todas las páginas, y se
    descartan las muy pequeñas. Las páginas que son una imagen con capa ya se tratan
    aparte.
    """
    from io import BytesIO

    from PIL import Image

    doc = pymupdf.open(pdf)
    vistas, resultado = set(), []
    for pagina in doc:
        if es_imagen_con_capa(doc, pagina):
            continue
        for img in pagina.get_images(full=True):
            xref = img[0]
            if xref in vistas:
                continue
            vistas.add(xref)
            rects = pagina.get_image_rects(xref)
            if not rects or max(abs(r) for r in rects) < 60 * 60:
                continue
            pix = pymupdf.Pixmap(doc, xref)
            if pix.colorspace is None or pix.colorspace.n != 3 or pix.alpha:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                if pix.alpha:
                    pix = pymupdf.Pixmap(pix, 0)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            r = max(rects, key=abs)
            # Solo lo que se ve: la banda de cabecera del resumen es una plantilla de otro
            # nivel, con «6 PRIMARIA», colocada de forma que esa parte queda fuera de la
            # página.
            visible = r & pagina.rect
            if visible.is_empty:
                continue
            ex, ey = im.width / r.width, im.height / r.height
            im = im.crop((int((visible.x0 - r.x0) * ex), int((visible.y0 - r.y0) * ey),
                          int((visible.x1 - r.x0) * ex), int((visible.y1 - r.y0) * ey)))
            im.thumbnail((maximo_px, maximo_px))
            salida = BytesIO()
            im.save(salida, format="PNG")
            r = visible
            resultado.append({"pagina": pagina.number + 1, "xref": xref,
                              "rect": [round(v, 1) for v in r], "png": salida.getvalue()})
    doc.close()
    return resultado


def extraer_imagen(pdf: Path, xref: int, carpeta: Path) -> Path:
    """La imagen `xref` del PDF, entera y a su resolución, en un fichero de `carpeta`.

    Un JPEG en RGB sale tal cual, sin recomprimir; lo demás, en PNG y con su transparencia
    si la tiene. Es lo que se rehace y después se pone en el sitio de la original.
    """
    doc = pymupdf.open(pdf)
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        info = doc.extract_image(xref) or {}
        if info.get("ext") in ("jpeg", "jpg") and info.get("colorspace") == 3 and not info.get("smask"):
            ruta = carpeta / f"{xref}.jpg"
            ruta.write_bytes(info["image"])
            return ruta
        pix = pymupdf.Pixmap(doc, xref)
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.colorspace is None or pix.colorspace.n != 3:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        if info.get("smask"):
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
        ruta = carpeta / f"{xref}.png"
        pix.save(str(ruta))
        return ruta
    finally:
        doc.close()


def _sustituir_imagen(doc: pymupdf.Document, xref: int, ruta: Path) -> None:
    """Pone la imagen de `ruta` en el objeto `xref`, que tiene sus mismas dimensiones.

    Se reescribe el propio objeto, no se inserta otro: `Page.replace_image` deja la imagen
    nueva también como recurso de la página y el PDF del resumen llevaba cada imagen dos
    veces. Un JPEG va tal cual; lo demás, comprimido, con la transparencia en su máscara.
    """
    from PIL import Image

    info = doc.extract_image(xref) or {}
    espacio = doc.xref_get_key(xref, "ColorSpace")[1]
    with Image.open(ruta) as im:
        if im.format == "JPEG":
            doc.update_stream(xref, ruta.read_bytes(), compress=False)
            doc.xref_set_key(xref, "Filter", "/DCTDecode")
        else:
            doc.update_stream(xref, im.convert("RGB").tobytes())
            if "A" in im.getbands() and info.get("smask"):
                doc.update_stream(info["smask"], im.getchannel("A").tobytes())
                doc.xref_set_key(info["smask"], "DecodeParms", "null")
        doc.xref_set_key(xref, "Width", str(im.width))
        doc.xref_set_key(xref, "Height", str(im.height))
    doc.xref_set_key(xref, "DecodeParms", "null")
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    # Un espacio de color de tres componentes se conserva (el ICC de la original); uno
    # indexado, gris o CMYK ya se ha convertido a RGB al extraer.
    if not (espacio == "/DeviceRGB" or (espacio.startswith("[") and "/ICCBased" in espacio
                                        and info.get("colorspace") == 3)):
        doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB")


def zonas_sin_capa(pdf: Path, lecturas: list[dict] | None = None) -> list[dict]:
    """Los textos pintados en una imagen que ninguna capa describe, recortados para leerlos.

    Devuelve la página, la zona en puntos y el recorte en PNG. Las zonas que ya tienen
    lectura no salen.
    """
    doc = pymupdf.open(pdf)
    # Productor y creador: la ficha de los invertebrados la hizo react-pdf y la reprocesó
    # después otra herramienta, que figura como productor.
    productor = " ".join(filter(None, ((doc.metadata or {}).get("producer"),
                                       (doc.metadata or {}).get("creator"))))
    perfil = next((p for clave, p in PERFILES_IMAGEN.items() if clave in productor), None)
    todos = nodos(doc, lecturas)
    zonas = []
    for pagina in doc:
        if perfil is None or not es_imagen_con_capa(doc, pagina):
            continue
        propios = [n for n in todos if n.pagina == pagina.number]
        imagen = _Imagen(doc, pagina)
        ajuste = _ajuste(imagen, propios)
        for n in propios:
            n.medida = _medir(imagen, n, ajuste) or {}
        calibre = _calibrar(propios, perfil)
        cuerpo = min((v[1] for v in calibre.values()), default=12.0)
        cubiertas = [n.medida["caja_px"] for n in propios if n.medida]
        for zona in _texto_sin_capa(imagen, cubiertas, cuerpo):
            x0, y0 = imagen.a_px(zona.x0 - 4, zona.y0 - 4)
            x1, y1 = imagen.a_px(zona.x1 + 4, zona.y1 + 4)
            recorte = np.ascontiguousarray(imagen.px[max(0, y0):y1, max(0, x0):x1])
            pix = pymupdf.Pixmap(pymupdf.csRGB, recorte.shape[1], recorte.shape[0],
                                 recorte.tobytes(), False)
            zonas.append({"pagina": pagina.number + 1, "rect": [round(v, 1) for v in zona],
                          "png": pix.tobytes("png")})
    doc.close()
    return zonas


def reescribir(pdf: Path, salida: Path, traducciones: dict[str, str],
               lecturas: list[dict] | None = None,
               imagenes: dict[int, Path] | None = None) -> list[dict]:
    """Sustituye en `pdf` cada texto que tenga traducción y guarda el resultado en `salida`.

    `imagenes` son las imágenes rehechas, por `xref`: se ponen en el sitio de la original,
    que puede aparecer en varias páginas. Devuelve una fila por nodo tocado o que no se
    pudo tocar, y una por cada zona de texto pintado sin capa, para el informe.
    """
    doc = pymupdf.open(pdf)
    for xref, ruta in (imagenes or {}).items():
        _sustituir_imagen(doc, xref, ruta)
    productor = " ".join(filter(None, ((doc.metadata or {}).get("producer"),
                                       (doc.metadata or {}).get("creator"))))
    # Un documento de Word es texto corrido: párrafos con negritas, viñetas y sangrías.
    documento = "word" in productor.lower()
    informe: list[dict] = []
    todos = nodos(doc, lecturas)

    for pagina in doc:
        propios = [n for n in todos if n.pagina == pagina.number]
        cambiar = [n for n in propios
                   if traducciones.get(n.texto) and traducciones[n.texto].strip() != n.texto]
        imagen, calibre = None, {}
        de_imagen = es_imagen_con_capa(doc, pagina)
        if not cambiar and not de_imagen:
            continue

        if de_imagen:
            perfil = next((p for clave, p in PERFILES_IMAGEN.items() if clave in productor), None)
            if perfil is None:
                informe += [_fila(pagina, n, "sin perfil",
                                  f"Texto pintado en una imagen por «{productor}», herramienta "
                                  "sin perfil de fuente.") for n in cambiar]
                continue
            imagen = _Imagen(doc, pagina)
            ajuste = _ajuste(imagen, propios)
            for n in propios:
                n.medida = _medir(imagen, n, ajuste) or {}
            calibre = _calibrar(propios, perfil)
            cuerpo = min((v[1] for v in calibre.values()), default=12.0)
            cubiertas = [n.medida["caja_px"] for n in propios if n.medida]
            for zona in _texto_sin_capa(imagen, cubiertas, cuerpo):
                informe.append({"pagina": pagina.number + 1, "original": "", "estado": "sin capa",
                                "rect": [round(v) for v in zona],
                                "detalle": "Texto pintado en la imagen sin capa de texto: no se "
                                           "puede traducir sin leerlo de la imagen."})

        # 1. Borrar: de la capa de texto siempre; de la imagen, si el texto está pintado.
        validos = []
        for n in cambiar:
            if imagen is not None:
                if not n.medida or n.tamano not in calibre:
                    informe.append(_fila(pagina, n, "no medido",
                                         "No se ha encontrado el texto pintado en la imagen."))
                    continue
                _borrar(imagen, n.medida["caja_px"])
            elif fuente(n.fuente) is None or (documento or n.rico) and any(
                    fuente(f) is None for f in n.fuentes.values()):
                falta = next((f for f in [n.fuente, *n.fuentes.values()] if fuente(f) is None), n.fuente)
                informe.append(_fila(pagina, n, "sin fuente",
                                     f"No hay una fuente libre para «{falta.split('+')[-1]}»."))
                continue
            for r in ([] if n.en_imagen else n.rects):
                pagina.add_redact_annot(pymupdf.Rect(r.x0, r.y0 + 0.3 * r.height,
                                                     r.x1, r.y1 - 0.3 * r.height), fill=False)
            validos.append(n)
        if not validos:
            continue
        pagina.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        # 2. Planificar sobre la página ya limpia.
        lienzo = _Lienzo(imagen.px, imagen.a_px) if imagen is not None else _Lienzo.de_pagina(pagina)
        raiz = None
        if imagen is not None:
            medidos = [x for x in propios if x.medida]
            if medidos:
                r = max(medidos, key=lambda x: x.tamano)
                raiz = (r.medida["x0"] + r.medida["x1"]) / 2
        if imagen is None:
            # La caja de un carácter ocupa todo el alto de la fuente y las de dos líneas
            # seguidas se solapan: se reserva la parte central, como al borrar.
            lienzo.reservado = [(pymupdf.Rect(r.x0, r.y0 + 0.2 * r.height, r.x1, r.y1 - 0.2 * r.height),
                                 id(n)) for n in validos for r in n.rects]
        # El margen derecho de la columna: donde acaban las líneas de los párrafos largos.
        columna = max((r.x1 for n in propios if len(n.rects) > 1 for r in n.rects),
                      default=pagina.rect.x1 - 40)
        pasos = [n.bases[1] - n.bases[0] for n in propios if len(n.bases) > 1]
        planes = []
        for n in validos:
            nuevo = re.sub(r"\s+", " ", traducciones[n.texto]).strip()
            if imagen is None and (documento or n.rico):
                # Un nodo de un solo peso —un título entero en negrita— no lleva marcas: todo
                # su texto va con su única fuente.
                unica = n.fuentes.get(False) or n.fuentes.get(True) or n.fuente
                n.fuentes = {False: n.fuentes.get(False, unica if not n.rico else unica),
                             True: n.fuentes.get(True, unica)}
                if not n.rico:
                    n.fuentes = {False: unica, True: unica}
                fonts = {b: pymupdf.Font(fontfile=str(fuente(f))) for b, f in n.fuentes.items()}
                escala = ESCALAS.get(n.fuente.split("+")[-1], 1.0)
                tamano = n.tamano * escala
                fondo = lienzo.fondo(n.rect)
                ancha = max(n.rects, key=lambda r: r.width)
                limite = min(max(columna, ancha.x1),
                             lienzo.libre_hasta(ancha, fondo, pagina.rect.x1 - 4, id(n)) - 3)
                paso = n.bases[1] - n.bases[0] if len(n.bases) > 1 else (
                    float(np.median(pasos)) if pasos else 1.4 * n.tamano)
                plan, estado = _planificar_rico(nuevo, fonts, tamano, [r.x0 for r in n.rects],
                                                n.bases, limite, paso, lienzo, fondo, id(n))
                lienzo.ocupado += _cajas_ricas(plan, fonts)
                planes.append((nuevo, n.fuentes, n.color, plan))
                informe.append({"pagina": pagina.number + 1, "original": n.texto, "nuevo": nuevo,
                                "estado": estado, "leido_de_la_imagen": False,
                                "tamano": round(plan.tamano, 1), "tamano_original": round(tamano, 1),
                                "lineas": len(plan.lineas), "lineas_original": len(n.bases)})
                continue
            if imagen is not None:
                nombre, tamano = calibre[n.tamano]
                m = n.medida
                x0, bases, color, subrayado = m["x0"], m["bases"], m["color"], m["subrayado"]
                eje = m.get("eje")
                # Lo que el subrayado sobresale del texto. En un nodo corto el generador lo
                # alarga hasta un ancho mínimo; más allá de 10 pt ese hueco es aprovechable.
                margen = min(10.0, max(2.0, subrayado["x1"] - m["x1"])) if subrayado else 2.0
                limite = subrayado["x1"] - margen if subrayado else m["x1"] + 2
                zona = pymupdf.Rect(*imagen.a_pt(*m["caja_px"][:2]), *imagen.a_pt(*m["caja_px"][2:]))
                # A la izquierda de la raíz del mapa, el subrayado crece hacia la izquierda.
                izquierda = None
                if subrayado and raiz is not None and m["x1"] < raiz:
                    izquierda = {"ancla": limite, "borde": 12.0,
                                 "margen": min(10.0, max(2.0, m["x0"] - subrayado["x0"]))}
            else:
                nombre, tamano = n.fuente.split("+")[-1], n.tamano
                x0 = n.rects[0].x0 + _tinta(str(fuente(nombre)), n.lineas[0].strip())[0] * tamano
                bases, color, subrayado, margen = n.bases, n.color, None, 2.0
                eje, izquierda = None, None
                limite, zona = n.rect.x1 + 2, n.rect
            font = pymupdf.Font(fontfile=str(fuente(nombre)))
            fondo = lienzo.fondo(zona)
            # Un texto vectorial puede crecer a la derecha mientras el fondo siga igual: hasta
            # el borde de su celda en una tabla, o de la banda de color en un título.
            if imagen is None:
                # Con el mismo aire que deja el original a la izquierda de una celda: 6 pt.
                limite = max(limite, lienzo.libre_hasta(n.rect, fondo, pagina.rect.x1 - 4, id(n)) - 6)
            plan, estado = _planificar(nuevo, font, str(fuente(nombre)), tamano, x0, bases, limite,
                                       lienzo, fondo, subrayado, margen, pagina.rect.x1 - 12, eje,
                                       id(n), izquierda)
            lienzo.ocupado += _cajas(plan, font)
            if plan.alargar_desde and subrayado and imagen is not None:
                _alargar_subrayado_izq(imagen, subrayado, plan.alargar_desde)
                lienzo.ocupado.append(pymupdf.Rect(plan.alargar_desde, subrayado["y0"] - 1,
                                                   subrayado["x0"], subrayado["y1"] + 1))
            if plan.alargar_hasta and subrayado and imagen is not None:
                _alargar_subrayado(imagen, subrayado, plan.alargar_hasta)
                lienzo.ocupado.append(pymupdf.Rect(subrayado["x1"], subrayado["y0"] - 1,
                                                   plan.alargar_hasta, subrayado["y1"] + 1))
            planes.append((nuevo, nombre, color, plan))
            informe.append({"pagina": pagina.number + 1, "original": n.texto, "nuevo": nuevo,
                            "estado": estado, "leido_de_la_imagen": n.en_imagen, "tamano": round(plan.tamano, 1),
                            "tamano_original": round(tamano, 1), "lineas": len(plan.lineas),
                            "lineas_original": len(bases)})

        # 3. Escribir.
        if imagen is not None:
            alto, ancho = imagen.px.shape[:2]
            pagina.replace_image(imagen.xref, pixmap=pymupdf.Pixmap(
                pymupdf.csRGB, ancho, alto, np.ascontiguousarray(imagen.px).tobytes(), False))
        insertadas: dict[str, str] = {}
        for nuevo, nombre, color, plan in planes:
            if isinstance(plan, _PlanRico):
                alias = {}
                for b, f in nombre.items():
                    ruta = str(fuente(f))
                    if ruta not in insertadas:
                        insertadas[ruta] = f"sarela{len(insertadas)}"
                        pagina.insert_font(fontname=insertadas[ruta], fontfile=ruta)
                    alias[b] = (insertadas[ruta], pymupdf.Font(fontfile=ruta))
                blanco = alias[False if False in alias else True][1].text_length(" ", plan.tamano)
                for linea, base, x in zip(plan.lineas, plan.bases, plan.x0s):
                    for palabra in linea:
                        for texto, b in palabra:
                            nombre_pdf, f = alias[b]
                            pagina.insert_text((x, base), texto, fontname=nombre_pdf,
                                               fontsize=plan.tamano, color=color)
                            x += f.text_length(texto, plan.tamano)
                        x += blanco
                continue
            if nombre not in insertadas:
                insertadas[nombre] = f"sarela{len(insertadas)}"
                pagina.insert_font(fontname=insertadas[nombre], fontfile=str(fuente(nombre)))
            for linea, base, caja in zip(plan.lineas, plan.bases, _cajas(plan)):
                pagina.insert_text((_origen(plan, linea, caja), base), linea, fontname=insertadas[nombre],
                                   fontsize=plan.tamano, color=color)

    try:
        doc.subset_fonts()
    except Exception:  # sin subconjunto el PDF pesa más, pero es igual de válido
        pass
    salida.parent.mkdir(parents=True, exist_ok=True)
    temporal = salida.with_suffix(".reescribiendo.pdf")
    doc.save(temporal, garbage=3, deflate=True)
    doc.close()
    temporal.replace(salida)
    return informe


