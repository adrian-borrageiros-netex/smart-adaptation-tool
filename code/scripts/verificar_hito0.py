"""Hito 0, mitad local: ciclo de ida y vuelta sin pérdida sobre un paquete Maker.

Extrae el zip tal como llegaría de la plataforma, lo abre, lo reconstruye sin modificar
nada y compara fichero a fichero, byte a byte. Si algo no cuadra, imprime dónde.

    python scripts/verificar_hito0.py [ruta_al_zip] [carpeta_de_trabajo]
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import zipfile
from pathlib import Path

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from maker_paquete import Paquete  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
# Las unidades y los trabajos locales viven en la raíz del repositorio, fuera de code/.
REPO = RAIZ.parent
ZIP_POR_DEFECTO = REPO / "courses" / "4a2a2c55-1587-46fd-8291-71b7fd2a8026.zip"


def sha(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def inventario(raiz: Path) -> dict[str, tuple[int, str]]:
    return {
        f.relative_to(raiz).as_posix(): (f.stat().st_size, sha(f))
        for f in sorted(raiz.rglob("*"))
        if f.is_file()
    }


def primera_diferencia(a: Path, b: Path) -> str:
    da, db = a.read_bytes(), b.read_bytes()
    for i in range(min(len(da), len(db))):
        if da[i] != db[i]:
            ini = max(0, i - 50)
            return (
                f"byte {i}\n        original: ...{da[ini:i + 50]!r}\n"
                f"        generado: ...{db[ini:i + 50]!r}"
            )
    return f"longitudes distintas: {len(da)} frente a {len(db)}"


def main() -> int:
    zip_origen = Path(sys.argv[1]) if len(sys.argv) > 1 else ZIP_POR_DEFECTO
    trabajo = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / ".trabajo" / "hito0"

    if not zip_origen.is_file():
        print(f"No encuentro el paquete: {zip_origen}")
        return 2

    if trabajo.exists():
        shutil.rmtree(trabajo)
    extraido, reconstruido = trabajo / "extraido", trabajo / "reconstruido"

    print(f"Paquete: {zip_origen.name} ({zip_origen.stat().st_size / 1e6:.1f} MB)")

    with zipfile.ZipFile(zip_origen) as z:
        entradas = [n for n in z.namelist() if not n.endswith("/")]
    print(f"Entradas en el zip: {len(entradas)}")

    paquete = Paquete.abrir(zip_origen, destino_extraccion=extraido)

    print("\nComposición de la unidad")
    for clave, valor in paquete.resumen().items():
        if clave == "componentes_mas_frecuentes":
            print(f"  {clave}: " + ", ".join(f"{n} x{c}" for n, c in valor))
        elif clave == "bytes_recursos":
            print(f"  {clave}: {valor / 1e6:.1f} MB")
        else:
            print(f"  {clave}: {valor}")

    paquete.reconstruir(reconstruido)

    antes, despues = inventario(extraido), inventario(reconstruido)
    faltan = sorted(set(antes) - set(despues))
    sobran = sorted(set(despues) - set(antes))
    difieren = sorted(k for k in set(antes) & set(despues) if antes[k] != despues[k])

    print("\nComparación tras reconstruir sin cambios")
    print(f"  ficheros comparados: {len(antes)}")
    print(f"  ausentes: {len(faltan)}   añadidos: {len(sobran)}   distintos: {len(difieren)}")

    for etiqueta, lista in (("ausente", faltan), ("añadido", sobran)):
        for nombre in lista[:10]:
            print(f"    [{etiqueta}] {nombre}")

    for nombre in difieren[:10]:
        print(f"    [distinto] {nombre}")
        print(f"        {primera_diferencia(extraido / nombre, reconstruido / nombre)}")

    ok = not (faltan or sobran or difieren)
    print("\n" + ("RESULTADO: ida y vuelta sin pérdida, byte a byte." if ok
                  else "RESULTADO: hay diferencias. Revisar arriba."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
