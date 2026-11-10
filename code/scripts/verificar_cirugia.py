"""Hito 0, segunda mitad: comprobar que una modificación es quirúrgica.

Que reconstruir sin cambios no altere nada demuestra que el escritor es fiel. Falta la
otra mitad: que cambiar un elemento cambie ese elemento y nada más. Sin esta garantía,
una adaptación podría alterar en silencio partes de la unidad que nadie ha revisado.

La prueba modifica un único componente de texto en las dos representaciones y verifica
que solo project.json y components.json difieren, y que el número de bytes que cambian
se corresponde con la sustitución pedida.

    python scripts/verificar_cirugia.py
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from maker_paquete import Paquete  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
# Los trabajos locales viven en la raíz del repositorio, fuera de code/.
REPO = RAIZ.parent
EXTRAIDO = REPO / ".trabajo" / "hito0" / "extraido"

ORIGINAL = "Para comenzar"
SUSTITUTO = "Para empezar"


def main() -> int:
    if not EXTRAIDO.is_dir():
        print("Ejecuta antes scripts/verificar_hito0.py, que deja el paquete extraído.")
        return 2

    destino = REPO / ".trabajo" / "hito0" / "modificado"
    if destino.exists():
        shutil.rmtree(destino)

    paquete = Paquete.abrir(EXTRAIDO)

    # Localizar los componentes de texto que contienen la cadena, en ambas representaciones.
    objetivos = [
        cid for cid, c in paquete.componentes.items()
        if c.get("name") == "text" and ORIGINAL in (c.get("properties", {}) or {}).get("textContent", "")
    ]
    print(f"Componentes de texto que contienen «{ORIGINAL}»: {len(objetivos)}")
    if len(objetivos) != 1:
        print("La prueba espera exactamente uno para que el resultado sea inequívoco.")
        return 2

    cid = objetivos[0]
    for arbol in (paquete.componentes, paquete.components):
        props = arbol[cid]["properties"]
        props["textContent"] = props["textContent"].replace(ORIGINAL, SUSTITUTO)
    print(f"Modificado el componente {cid}: «{ORIGINAL}» -> «{SUSTITUTO}»")

    paquete.reconstruir(destino)

    distintos, iguales = [], 0
    for antes in sorted(EXTRAIDO.rglob("*")):
        if not antes.is_file():
            continue
        rel = antes.relative_to(EXTRAIDO)
        despues = destino / rel
        if antes.read_bytes() == despues.read_bytes():
            iguales += 1
        else:
            distintos.append(rel.as_posix())

    print(f"\nFicheros idénticos: {iguales}   modificados: {len(distintos)}")
    for nombre in distintos:
        a = (EXTRAIDO / nombre).stat().st_size
        b = (destino / nombre).stat().st_size
        print(f"  {nombre}: {a} -> {b} bytes ({b - a:+d})")

    esperados = {"project.json", "components.json"}
    # «comenzar» son 8 caracteres y «empezar» 7: cada fichero debe encoger exactamente 1 byte.
    ok = set(distintos) == esperados and all(
        (destino / n).stat().st_size - (EXTRAIDO / n).stat().st_size == -1 for n in esperados
    )

    print("\n" + ("RESULTADO: la modificación es quirúrgica. Solo cambian los dos ficheros "
                  "que contienen el texto, y solo en la medida esperada."
                  if ok else
                  "RESULTADO: el alcance del cambio no es el esperado. Revisar arriba."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
