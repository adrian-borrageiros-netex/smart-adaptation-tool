"""Convierte las decisiones ya tomadas en memoria reutilizable.

Los ficheros de `adaptacion/*.json` guardan el trabajo de la adaptación a Asturias
indexado **por componente**, con claves que solo valen para ese paquete. Este script los
lee junto al paquete de origen, recupera el texto que había antes de cada cambio y guarda
el par en la memoria por contenido, que sí sirve para cualquier unidad.

Al hacerlo separa las dos naturalezas:

- Si el número de frases coincide antes y después, es una **traducción**: se guarda frase a
  frase en la memoria de traducción y se aplicará sola en el futuro.
- Si no coincide, el texto se reescribió, no se tradujo: es una **adaptación territorial**.
  Va a la otra memoria, que se propone al editor pero nunca se aplica sin que alguien mire.

    python scripts/sembrar_memoria.py --origen courses/BIO003SP1ES --de gl --a es \\
        --territorio-origen Galicia --territorio-destino Asturias
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "src"))

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from maker_paquete import MapaEditorial, Paquete, extraer_frases, texto_plano  # noqa: E402

from web import bd, memoria  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--origen", default="courses/BIO003SP1ES",
                   help="Paquete sobre el que se tomaron las decisiones.")
    p.add_argument("--memoria", default="adaptacion",
                   help="Carpeta con los ficheros de decisiones.")
    p.add_argument("--de", default="gl")
    p.add_argument("--a", default="es")
    p.add_argument("--territorio-origen", default="Galicia")
    p.add_argument("--territorio-destino", default="Asturias")
    p.add_argument("--simular", action="store_true", help="No escribe nada; solo informa.")
    args = p.parse_args()

    bd.crear_esquema()

    paquete = Paquete.abrir(args.origen)
    mapa = MapaEditorial(paquete)
    print(f"Paquete de origen: {paquete.resumen()['titulo']}  "
          f"({len(mapa.redactables)} elementos)")

    nuevas_frases = repetidas = equivalencias = saltadas = sin_clave = 0

    for fichero in sorted(glob.glob(str(Path(args.memoria) / "*.json"))):
        datos = json.loads(Path(fichero).read_text(encoding="utf-8"))
        nombre = Path(fichero).name
        detalle = {"frases": 0, "equivalencias": 0, "saltadas": 0}

        # Los cambios por frases ya vienen alineados con el original: son traducción pura.
        for clave, traducciones in (datos.get("frases") or {}).items():
            try:
                original = mapa.elemento(clave).valor
            except KeyError:
                sin_clave += 1
                continue
            origenes = extraer_frases(original)
            if len(origenes) != len(traducciones):
                detalle["saltadas"] += 1
                saltadas += 1
                continue
            for antes, despues in zip(origenes, traducciones):
                if "[PENDIENTE" in despues:
                    saltadas += 1
                    continue
                if args.simular:
                    detalle["frases"] += 1
                    continue
                if memoria.guardar_frase(args.de, args.a, antes, despues,
                                         procedencia=nombre, aprobada_por="adaptación Asturias"):
                    nuevas_frases += 1
                else:
                    repetidas += 1
                detalle["frases"] += 1

        # Los cambios de valor completo pueden ser cualquiera de las dos cosas.
        for clave, cambio in (datos.get("cambios") or {}).items():
            nuevo = cambio["nuevo"] if isinstance(cambio, dict) else cambio
            motivo = cambio.get("motivo", "") if isinstance(cambio, dict) else ""
            try:
                original = mapa.elemento(clave).valor
            except KeyError:
                sin_clave += 1
                continue
            if "[PENDIENTE" in nuevo:
                # Un marcador no es una decisión: es la ausencia de una.
                detalle["saltadas"] += 1
                saltadas += 1
                continue

            antes, despues = extraer_frases(original), extraer_frases(nuevo)
            if antes and len(antes) == len(despues):
                for a, d in zip(antes, despues):
                    if args.simular:
                        continue
                    if memoria.guardar_frase(args.de, args.a, a, d, procedencia=nombre,
                                             aprobada_por="adaptación Asturias"):
                        nuevas_frases += 1
                    else:
                        repetidas += 1
                detalle["frases"] += len(antes)
            else:
                # El texto se reescribió: es criterio editorial, no traducción.
                concepto, propuesta = texto_plano(original), texto_plano(nuevo)
                if concepto and propuesta and concepto != propuesta:
                    if not args.simular:
                        memoria.guardar_equivalencia(
                            concepto, propuesta, args.territorio_origen,
                            args.territorio_destino, motivo=motivo or datos.get("descripcion"),
                            aprobada=True, procedencia=nombre)
                    equivalencias += 1
                    detalle["equivalencias"] += 1

        print(f"  {nombre:<44} frases {detalle['frases']:>4}  "
              f"equivalencias {detalle['equivalencias']:>3}  saltadas {detalle['saltadas']:>3}")

    print(f"\nFrases nuevas: {nuevas_frases}   ya conocidas: {repetidas}")
    print(f"Equivalencias territoriales: {equivalencias}")
    print(f"Saltadas (marcadores o desalineadas): {saltadas}   claves inexistentes: {sin_clave}")

    if not args.simular:
        inventario = memoria.resumen()
        print(f"\nMemoria: {inventario['frases']} frases {inventario['frases_por_par']}, "
              f"{inventario['equivalencias']} equivalencias "
              f"({inventario['equivalencias_aprobadas']} aprobadas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
