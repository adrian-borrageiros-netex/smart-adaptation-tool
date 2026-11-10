"""Genera las imágenes que la adaptación a Asturias necesita sustituir.

Solo se generan las que pueden producirse con honestidad:

- El **mapa geológico simplificado**, que es un esquema, igual que lo era el original
  gallego. El pie de figura ya dice «mapa simplificado».
- El **esquema de la composición de la geosfera**, que es un diagrama de cajas con
  texto incrustado en gallego.

La fotografía aérea del lago de As Pontes **no** se genera: sustituirla exige una
fotografía real de Asturias. Fabricar una imagen de un lugar real y presentarla como
documental en un libro de texto no es aceptable, así que en su lugar se escribe un
marcador visible para que el paquete no pueda confundirse con uno terminado.

    python scripts/generar_imagenes.py .trabajo/asturias
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ESCALA = 3  # se dibuja en grande y se reduce, para que los bordes queden suaves

NEGRO = (26, 26, 26)
GRIS = (90, 100, 116)
VERDE = (27, 91, 0)
VERDE_CLARO = (226, 239, 220)
AMARILLO = (247, 224, 90)


def _fuente(tam: int, negrita: bool = False):
    # Las de Windows primero; DejaVu es la que trae el contenedor Linux del servidor. Sin
    # ella caería en la fuente de mapa de bits por defecto, ilegible en una leyenda.
    for nombre in (("seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
                   if negrita else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            continue
    return ImageFont.load_default()


# Contorno esquemático de Asturias. No pretende precisión cartográfica: reproduce la
# proporción real (unos 220 km de oeste a este por 90 de norte a sur), la costa
# cantábrica al norte y el borde montañoso al sur.
CONTORNO = [
    (0.045, 0.300), (0.090, 0.250), (0.150, 0.225), (0.215, 0.205), (0.280, 0.195),
    (0.350, 0.180), (0.420, 0.170), (0.490, 0.165), (0.560, 0.160), (0.630, 0.168),
    (0.700, 0.180), (0.770, 0.200), (0.835, 0.215), (0.900, 0.240), (0.950, 0.275),
    (0.965, 0.340), (0.945, 0.420), (0.960, 0.500), (0.930, 0.575), (0.880, 0.620),
    (0.820, 0.650), (0.760, 0.630), (0.700, 0.660), (0.640, 0.700), (0.575, 0.720),
    (0.510, 0.700), (0.450, 0.720), (0.385, 0.745), (0.320, 0.730), (0.260, 0.700),
    (0.200, 0.715), (0.145, 0.690), (0.100, 0.630), (0.070, 0.560), (0.055, 0.480),
    (0.035, 0.400),
]

# Las tres franjas del mapa, de oeste a este, en fracción del ancho.
ZONAS = [(0.00, 0.345, "trama"), (0.345, 0.655, "lineas"), (0.655, 1.00, "puntos")]

LEYENDA = [
    ("trama", "Pizarras y cuarcitas", "(occidente)"),
    ("lineas", "Carbón y rocas sedimentarias", "(centro)"),
    ("puntos", "Calizas", "(oriente)"),
]


def _pintar_patron(capa: ImageDraw.ImageDraw, caja, patron: str, paso: int) -> None:
    x0, y0, x1, y1 = caja
    if patron == "trama":
        for i in range(int(x0 - (y1 - y0)), int(x1 + (y1 - y0)), paso):
            capa.line([(i, y0), (i + (y1 - y0), y1)], fill=NEGRO, width=max(1, paso // 7))
            capa.line([(i, y1), (i + (y1 - y0), y0)], fill=NEGRO, width=max(1, paso // 7))
    elif patron == "lineas":
        for y in range(int(y0), int(y1), paso):
            capa.line([(x0, y), (x1, y)], fill=VERDE, width=max(1, paso // 3))
    elif patron == "puntos":
        r = max(1, paso // 5)
        for y in range(int(y0), int(y1), paso):
            desfase = 0 if (y // paso) % 2 == 0 else paso // 2
            for x in range(int(x0) + desfase, int(x1), paso):
                capa.ellipse([x - r, y - r, x + r, y + r], fill=GRIS)


def _muestra(patron: str, ancho: int, alto: int) -> Image.Image:
    """Cuadrito de leyenda con el patrón recortado a su propio borde."""
    m = Image.new("RGB", (ancho, alto),
                  VERDE_CLARO if patron == "lineas" else (255, 255, 255))
    _pintar_patron(ImageDraw.Draw(m), (0, 0, ancho, alto), patron, 8 * ESCALA)
    ImageDraw.Draw(m).rectangle([0, 0, ancho - 1, alto - 1], outline=NEGRO, width=ESCALA)
    return m


def mapa_asturias(destino: Path, ancho: int = 640, alto: int = 565) -> None:
    W, H = ancho * ESCALA, alto * ESCALA
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)

    mx0, my0 = int(W * 0.04), int(H * 0.05)
    mx1, my1 = int(W * 0.96), int(H * 0.62)
    mw, mh = mx1 - mx0, my1 - my0
    poligono = [(mx0 + x * mw, my0 + y * mh) for x, y in CONTORNO]

    silueta = Image.new("L", (W, H), 0)
    ImageDraw.Draw(silueta).polygon(poligono, fill=255)

    # Cada franja se dibuja aparte y se recorta a la intersección de su rectángulo con
    # la silueta; si no, las diagonales de la trama invaden las zonas vecinas.
    for ini_f, fin_f, patron in ZONAS:
        zx0, zx1 = mx0 + mw * ini_f, mx0 + mw * fin_f
        capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dc = ImageDraw.Draw(capa)
        dc.rectangle([zx0, my0, zx1, my1],
                     fill=VERDE_CLARO if patron == "lineas" else (255, 255, 255, 255))
        _pintar_patron(dc, (zx0, my0, zx1, my1), patron, 13 * ESCALA)

        recorte = Image.new("L", (W, H), 0)
        ImageDraw.Draw(recorte).rectangle([zx0, my0, zx1, my1], fill=255)
        mascara = Image.composite(silueta, Image.new("L", (W, H), 0), recorte)
        img.paste(capa, (0, 0), mascara)

    d.line(poligono + [poligono[0]], fill=NEGRO, width=2 * ESCALA, joint="curve")

    f_zona = _fuente(13 * ESCALA, negrita=True)
    for texto, fx in (("OCCIDENTE", 0.17), ("CENTRO", 0.50), ("ORIENTE", 0.82)):
        px, py = mx0 + mw * fx, my0 + mh * 0.45
        caja = d.textbbox((px, py), texto, font=f_zona, anchor="mm")
        holgura = 5 * ESCALA
        d.rectangle([caja[0] - holgura, caja[1] - holgura,
                     caja[2] + holgura, caja[3] + holgura], fill=(255, 255, 255))
        d.text((px, py), texto, font=f_zona, fill=NEGRO, anchor="mm")

    f_ref = _fuente(11 * ESCALA)
    d.text((mx0 + mw * 0.50, my0 - 8 * ESCALA), "Mar Cantábrico",
           font=f_ref, fill=GRIS, anchor="ms")
    borde_sur = my0 + mh * max(y for _, y in CONTORNO)
    d.text((mx0 + mw * 0.50, borde_sur + 10 * ESCALA), "Cordillera Cantábrica",
           font=f_ref, fill=GRIS, anchor="mt")

    # Leyenda, con la misma disposición que la del mapa gallego original.
    f_ley = _fuente(12 * ESCALA)
    f_sub = _fuente(10 * ESCALA)
    sw, sh = 40 * ESCALA, 26 * ESCALA
    fila = 42 * ESCALA
    margen = 16 * ESCALA
    lx0, lx1 = int(W * 0.10), int(W * 0.90)
    ly0 = int(H * 0.60)
    ly1 = ly0 + margen * 2 + fila * len(LEYENDA)
    d.rectangle([lx0, ly0, lx1, ly1], outline=NEGRO, width=2 * ESCALA)

    for i, (patron, etiqueta, matiz) in enumerate(LEYENDA):
        y = ly0 + margen + i * fila
        img.paste(_muestra(patron, sw, sh), (lx0 + margen, y))
        tx = lx0 + margen + sw + 16 * ESCALA
        d.text((tx, y + 2 * ESCALA), etiqueta, font=f_ley, fill=NEGRO)
        d.text((tx, y + 16 * ESCALA), matiz, font=f_sub, fill=GRIS)

    img.resize((ancho, alto), Image.LANCZOS).save(destino)
    print(f"  generado {destino.name}  ({ancho}x{alto})")


def esquema_geosfera(destino: Path, ancho: int = 640, alto: int = 171) -> None:
    W, H = ancho * ESCALA, alto * ESCALA
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    verde_caja = (86, 168, 79)
    verde_borde = (61, 133, 55)
    verde_hijo = (124, 190, 110)
    naranja = (237, 139, 39)

    f_tit = _fuente(13 * ESCALA, negrita=True)
    f_hijo = _fuente(12 * ESCALA)

    cx, cy, cw, ch = W // 2, int(H * 0.13), int(W * 0.26), int(H * 0.20)
    d.rounded_rectangle([cx - cw // 2, cy, cx + cw // 2, cy + ch], radius=5 * ESCALA,
                        fill=verde_caja, outline=verde_borde, width=ESCALA)
    d.text((cx, cy + ch // 2), "Composición de la geosfera", font=f_tit,
           fill=(255, 255, 255), anchor="mm")

    hijos = ["Minerales", "Rocas", "Explotación minera"]
    hy, hh, hw = int(H * 0.48), int(H * 0.26), int(W * 0.21)
    centros = [int(W * 0.17), W // 2, int(W * 0.83)]
    barra = cy + ch + int(H * 0.10)

    d.line([(cx, cy + ch), (cx, barra)], fill=naranja, width=2 * ESCALA)
    d.line([(centros[0], barra), (centros[2], barra)], fill=naranja, width=2 * ESCALA)
    for hx in centros:
        d.line([(hx, barra), (hx, hy)], fill=naranja, width=2 * ESCALA)

    for hx, etiqueta in zip(centros, hijos):
        d.rounded_rectangle([hx - hw // 2, hy, hx + hw // 2, hy + hh], radius=5 * ESCALA,
                            fill=verde_hijo, outline=verde_borde, width=ESCALA)
        d.text((hx, hy + hh // 2), etiqueta, font=f_hijo, fill=(255, 255, 255), anchor="mm")

    img.resize((ancho, alto), Image.LANCZOS).save(destino)
    print(f"  generado {destino.name}  ({ancho}x{alto})")


def marcador_pendiente(destino: Path, motivo: str, ancho: int, alto: int) -> None:
    """Marca visible para una imagen que exige material real y no puede generarse."""
    img = Image.new("RGB", (ancho, alto), (250, 246, 227))
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, ancho - 3, alto - 3], outline=(190, 150, 40), width=3)
    f = _fuente(max(10, ancho // 26), negrita=True)
    fp = _fuente(max(9, ancho // 32))
    d.text((ancho // 2, alto // 2 - 16), "IMAGEN PENDIENTE", font=f, fill=(140, 100, 20),
           anchor="mm")
    y = alto // 2 + 6
    for linea in motivo.split("\n"):
        d.text((ancho // 2, y), linea, font=fp, fill=(120, 95, 40), anchor="mm")
        y += max(12, alto // 14)
    img.save(destino)
    print(f"  marcador  {destino.name}  ({ancho}x{alto})")


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    recursos = Path(sys.argv[1]) / "resources"
    if not recursos.is_dir():
        raise SystemExit(f"No encuentro {recursos}")

    print("Imágenes de la adaptación a Asturias:")
    mapa_asturias(recursos / "id_070103_003_gal.png")
    esquema_geosfera(recursos / "ld_070103_31_gl.png")
    marcador_pendiente(recursos / "ld_070103_29_gl.jpg",
                       "Se necesita una fotografía real\ndel Pozo Sotón (Asturias)",
                       320, 219)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
