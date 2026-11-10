"""Aplica un conjunto de cambios editoriales a un paquete Maker y lo reconstruye.

Los cambios viven en ficheros JSON bajo `adaptacion/`, con la forma:

    {
      "descripcion": "...",
      "cambios": {
        "<clave de elemento>": {"nuevo": "<HTML o texto>", "motivo": "..."},
        ...
      }
    }

La clave de elemento es la que devuelve el mapa editorial (`<8 primeros del id>.<propiedad>`).
Cada cambio se escribe en las dos representaciones del paquete y queda registrado en el
informe de diferencias, para que el editor pueda revisar qué se tocó y por qué.

    python scripts/adaptar.py courses/BIO003SP1ES .trabajo/asturias adaptacion/*.json
"""

from __future__ import annotations

import glob
import io
import json
import shutil
import sys
from pathlib import Path

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from maker_paquete import (MapaEditorial, Paquete, reinsertar_frases,  # noqa: E402
                           texto_plano)


def cargar_cambios(patrones: list[str]) -> list[tuple[str, dict]]:
    ficheros: list[str] = []
    for patron in patrones:
        ficheros.extend(sorted(glob.glob(patron)))
    if not ficheros:
        raise SystemExit(f"No encuentro ficheros de cambios en: {patrones}")
    return [(f, json.loads(Path(f).read_text(encoding="utf-8"))) for f in ficheros]


def main() -> int:
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    origen, destino, patrones = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]

    paquete = Paquete.abrir(origen)
    mapa = MapaEditorial(paquete)
    print(f"Paquete: {paquete.resumen()['titulo']}  ({len(mapa.redactables)} elementos redactables)")

    aplicados, fallidos = [], []
    for fichero, datos in cargar_cambios(patrones):
        # Dos formas de expresar un cambio: «cambios» sustituye el valor completo y
        # «frases» sustituye solo los nodos de texto, conservando el marcado intacto.
        cambios = datos.get("cambios", {})
        frases = datos.get("frases", {})
        print(f"\n{Path(fichero).name}: {datos.get('descripcion', '')}"
              f"  ({len(cambios)} completos, {len(frases)} por frases)")

        for clave, cambio in cambios.items():
            nuevo = cambio["nuevo"] if isinstance(cambio, dict) else cambio
            motivo = cambio.get("motivo", "") if isinstance(cambio, dict) else ""
            try:
                anterior = mapa.elemento(clave).valor
                mapa.aplicar_cambio(clave, nuevo)
                aplicados.append((clave, anterior, nuevo, motivo))
            except KeyError:
                fallidos.append((clave, "no existe en el mapa editorial"))

        for clave, traducciones in frases.items():
            try:
                anterior = mapa.elemento(clave).valor
            except KeyError:
                fallidos.append((clave, "no existe en el mapa editorial"))
                continue
            try:
                nuevo = reinsertar_frases(anterior, traducciones)
            except ValueError as e:
                fallidos.append((clave, str(e)))
                continue
            mapa.aplicar_cambio(clave, nuevo)
            aplicados.append((clave, anterior, nuevo, datos.get("descripcion", "")))

    print(f"\nAplicados: {len(aplicados)}   fallidos: {len(fallidos)}")
    for clave, razon in fallidos:
        print(f"  [fallo] {clave}: {razon}")

    if destino.exists():
        shutil.rmtree(destino)
    paquete.reconstruir(destino)
    print(f"\nPaquete reconstruido en {destino}")

    # Informe de diferencias, legible por un editor.
    informe = destino.parent / f"{destino.name}-diferencias.md"
    lineas = ["# Informe de adaptación", "",
              f"Elementos modificados: **{len(aplicados)}**", ""]
    for clave, antes, despues, motivo in aplicados:
        lineas += [f"## `{clave}`",
                   f"*{motivo}*" if motivo else "",
                   "", "**Antes**", "", f"> {texto_plano(antes) or '(vacío)'}", "",
                   "**Después**", "", f"> {texto_plano(despues) or '(vacío)'}", ""]
    informe.write_text("\n".join(lineas), encoding="utf-8")
    print(f"Informe de diferencias en {informe}")

    return 0 if not fallidos else 1


if __name__ == "__main__":
    raise SystemExit(main())
