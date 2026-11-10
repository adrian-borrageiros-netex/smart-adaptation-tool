"""Genera las imágenes que la adaptación a Cataluña necesita sustituir.

Mismo criterio que en la adaptación a Asturias, y por la misma razón: **solo se genera lo
que puede producirse con honestidad**.

- El **mapa geológico simplificado** sí. Es un esquema, como lo era el original gallego, y
  su propio pie de figura dice «mapa simplificat». Aquí las franjas no son de oeste a este
  sino de norte a sur, que es como se ordenan los dominios geológicos catalanes.
- Las **dos fotografías** no. Sustituir la foto del gneis ojo de sapo por una de los
  conglomerados de Montserrat, o la del lago de As Pontes por una de Cardona, exige
  fotografías reales de esos lugares. Fabricar una imagen de un sitio que existe y
  presentarla como documental en un libro de texto no es aceptable, así que en su lugar va
  un marcador visible para que el paquete no pueda confundirse con uno terminado.

    python scripts/generar_imagenes_cataluna.py .demostrador/resultados/trabajo-18
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from PIL import Image, ImageDraw  # noqa: E402

from generar_imagenes import (ESCALA, NEGRO, VERDE_CLARO, _fuente,  # noqa: E402
                              _pintar_patron, marcador_pendiente)

# Contorno esquemático de Cataluña. No pretende precisión cartográfica: reproduce la
# proporción real (unos 160 km de oeste a este por 170 de norte a sur, casi cuadrada), el
# borde pirenaico al norte, la costa mediterránea al sureste y el delta del Ebro al sur.
CONTORNO = [
    # Frontera pirenaica, de la Val d'Aran (noroeste) al cap de Creus (noreste).
    (0.210, 0.120), (0.255, 0.075), (0.320, 0.060), (0.385, 0.080), (0.450, 0.065),
    (0.520, 0.075), (0.590, 0.060), (0.660, 0.080), (0.730, 0.070), (0.800, 0.095),
    (0.860, 0.135), (0.905, 0.170),
    # Costa mediterránea, en diagonal hacia el suroeste: Costa Brava, Barcelona, Tarragona.
    (0.880, 0.240), (0.820, 0.300), (0.760, 0.355), (0.700, 0.415), (0.640, 0.470),
    (0.580, 0.530), (0.520, 0.585), (0.455, 0.645), (0.395, 0.700), (0.340, 0.755),
    # El saliente del delta del Ebro.
    (0.300, 0.800), (0.355, 0.830), (0.300, 0.860), (0.265, 0.905), (0.225, 0.940),
    # Límite occidental con Aragón, subiendo hacia el Pirineo.
    (0.185, 0.885), (0.160, 0.800), (0.140, 0.710), (0.120, 0.620), (0.105, 0.530),
    (0.130, 0.440), (0.110, 0.350), (0.140, 0.260), (0.170, 0.185),
]

# Paso de las tramas. Con el del mapa asturiano, más apaisado, las franjas de Cataluña
# salían tan densas que se veían como manchas negras y no se distinguían entre sí.
PASO = 20

# Las tres franjas, de norte a sur. Es la diferencia de fondo con el mapa gallego y con el
# asturiano: en Cataluña los dominios se ordenan por latitud, no por longitud.
ZONAS = [(0.000, 0.330, "trama"), (0.330, 0.680, "lineas"), (0.680, 1.000, "puntos")]

LEYENDA = [
    ("trama", "Granits, esquists i calcàries", "(Pirineu)"),
    ("lineas", "Conglomerats, calcàries i sals", "(Depressió Central)"),
    ("puntos", "Granits i pissarres", "(Serralades Costaneres)"),
]


GRIS = (90, 100, 116)
VERDE = (27, 91, 0)


def _trama(capa: ImageDraw.ImageDraw, caja, patron: str, paso: int) -> None:
    """Igual que la del mapa asturiano, pero con trazos más finos.

    Sobre una superficie tan grande como la de este mapa, el grosor original llenaba la
    franja y las tres tramas se veían como tres manchas negras iguales.
    """
    x0, y0, x1, y1 = caja
    if patron == "trama":
        grosor = max(1, paso // 14)
        for i in range(int(x0 - (y1 - y0)), int(x1 + (y1 - y0)), paso):
            capa.line([(i, y0), (i + (y1 - y0), y1)], fill=NEGRO, width=grosor)
            capa.line([(i, y1), (i + (y1 - y0), y0)], fill=NEGRO, width=grosor)
    elif patron == "lineas":
        for y in range(int(y0), int(y1), paso):
            capa.line([(x0, y), (x1, y)], fill=VERDE, width=max(1, paso // 5))
    elif patron == "puntos":
        r = max(1, paso // 8)
        for y in range(int(y0), int(y1), paso):
            desfase = 0 if (y // paso) % 2 == 0 else paso // 2
            for x in range(int(x0) + desfase, int(x1), paso):
                capa.ellipse([x - r, y - r, x + r, y + r], fill=GRIS)


def _muestra(patron: str, lado: int) -> Image.Image:
    """Cuadrito de leyenda, con el patrón a la misma densidad relativa que el mapa."""
    m = Image.new("RGB", (lado, lado), VERDE_CLARO if patron == "lineas" else (255, 255, 255))
    _trama(ImageDraw.Draw(m), (0, 0, lado, lado), patron, max(5, lado // 4))
    ImageDraw.Draw(m).rectangle([0, 0, lado - 1, lado - 1], outline=NEGRO, width=ESCALA)
    return m


def mapa_cataluna(destino: Path, ancho: int = 640, alto: int = 565) -> None:
    W, H = ancho * ESCALA, alto * ESCALA
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))

    # Cataluña es casi cuadrada (unos 160 km por 170), así que el contorno se dibuja en una
    # caja cuadrada y centrada. Estirándolo al ancho completo del lienzo, como en el mapa
    # asturiano —que sí es apaisado—, salía aplastado y no se reconocía.
    margen = int(0.04 * W)
    lado_mapa = min(W - 2 * margen, int(H * 0.68))
    izq = (W - lado_mapa) // 2
    poligono = [(izq + x * lado_mapa, margen + y * lado_mapa) for x, y in CONTORNO]

    silueta = Image.new("L", (W, H), 0)
    ImageDraw.Draw(silueta).polygon(poligono, fill=255)

    xs = [p[0] for p in poligono]
    ys = [p[1] for p in poligono]
    mx0, mx1, my0, my1 = min(xs), max(xs), min(ys), max(ys)

    # Las franjas se recortan por latitud y se enmascaran con la silueta, para que el
    # patrón no se salga del contorno.
    for desde, hasta, patron in ZONAS:
        zy0 = my0 + desde * (my1 - my0)
        zy1 = my0 + hasta * (my1 - my0)
        capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dc = ImageDraw.Draw(capa)
        if patron == "lineas":
            dc.rectangle([mx0, zy0, mx1, zy1], fill=(226, 239, 220, 255))
        _trama(dc, (mx0, zy0, mx1, zy1), patron, PASO * ESCALA)

        recorte = Image.new("L", (W, H), 0)
        ImageDraw.Draw(recorte).rectangle([mx0, zy0, mx1, zy1], fill=255)
        mascara = Image.composite(silueta, Image.new("L", (W, H), 0), recorte)
        img.paste(capa, (0, 0), mascara)

    ImageDraw.Draw(img).line(poligono + [poligono[0]], fill=NEGRO, width=2 * ESCALA)

    # Leyenda bajo el mapa.
    d = ImageDraw.Draw(img)
    f = _fuente(int(13 * ESCALA))
    fp = _fuente(int(11 * ESCALA))
    lado = int(22 * ESCALA)
    y = int(my1) + int(26 * ESCALA)
    for patron, texto, zona in LEYENDA:
        img.paste(_muestra(patron, lado), (int(margen), int(y)))
        d.text((margen + lado + int(10 * ESCALA), y + lado // 2 - int(9 * ESCALA)),
               texto, font=f, fill=NEGRO)
        d.text((margen + lado + int(10 * ESCALA), y + lado // 2 + int(4 * ESCALA)),
               zona, font=fp, fill=(90, 100, 116))
        y += lado + int(14 * ESCALA)

    # Sobre blanco, no con `convert("RGB")`: eso volvía negro todo lo transparente y el
    # Pirineo salía como una mancha sólida en vez de una trama.
    fondo = Image.new("RGB", (W, H), (255, 255, 255))
    fondo.paste(img, (0, 0), img)
    fondo.resize((ancho, alto), Image.LANCZOS).save(destino)
    print(f"  mapa      {destino.name}  ({ancho}x{alto})")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    recursos = Path(sys.argv[1]) / "resources"
    if not recursos.is_dir():
        print(f"No encuentro {recursos}")
        return 1

    mapa_cataluna(recursos / "id_070103_003_gal.png")

    marcador_pendiente(
        recursos / "id_070103_004_gl.jpg",
        "Conglomerats de Montserrat\nRequiere fotografía real",
        640, 400)
    marcador_pendiente(
        recursos / "ld_070103_29_gl.jpg",
        "Muntanya de Sal de Cardona\nRequiere fotografía real",
        320, 219)

    print("\nLas dos fotografías quedan pendientes a propósito: exigen material real "
          "de Cataluña,\nno una imagen generada de un lugar que existe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
