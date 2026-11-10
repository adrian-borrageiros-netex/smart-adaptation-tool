"""Hito 0 del fuente de Author: ida y vuelta sin pérdida, mapa y cirugía.

Extrae los zips de una unidad y de sus proyectos de actividades en una carpeta de trabajo,
los abre como un único árbol y comprueba tres cosas:

1. Reescribir sin cambios deja todos los ficheros idénticos byte a byte.
2. El mapa editorial ve la unidad con sus actividades colgadas de cada conector.
3. Cambiar un texto de la teoría y uno de una actividad solo toca esos dos XML.

    python scripts/verificar_author.py <carpeta_de_trabajo> <zip_unidad> [<zip_actividad> ...]
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import zipfile
from collections import Counter
from pathlib import Path

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from maker_paquete import FuenteAuthor, MapaEditorial  # noqa: E402


def huellas(raiz: Path) -> dict[str, str]:
    return {f.relative_to(raiz).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in raiz.rglob("*") if f.is_file()}


def comparar(a: Path, b: Path) -> list[str]:
    ha, hb = huellas(a), huellas(b)
    return sorted(k for k in ha.keys() | hb.keys() if ha.get(k) != hb.get(k))


def main(trabajo: Path, zips: list[Path]) -> int:
    origen = trabajo / "origen"
    if trabajo.exists():
        shutil.rmtree(trabajo)
    for z in zips:
        with zipfile.ZipFile(z) as f:
            f.extractall(origen / z.stem)

    fuente = FuenteAuthor.abrir(origen)
    r = fuente.resumen()
    print(f"Unidad «{r['titulo']}», revisión {r['revision']}, idioma {r['idioma']} "
          f"(declara {r['idioma_declarado']}); {r['componentes']} componentes.")
    print(f"Actividades: {r['actividades']} proyectos para {r['conectores']} conectores; "
          f"sin resolver: {r['conectores_sin_actividad'] or 'ninguno'}.")

    # 1. Ida y vuelta
    fuente.reconstruir(trabajo / "igual")
    distintos = comparar(origen, trabajo / "igual")
    total = len(huellas(origen))
    print(f"\n1. Ida y vuelta: {total - len(distintos)} de {total} ficheros idénticos.")
    for d in distintos[:10]:
        print("   distinto:", d)

    # 2. Mapa
    mapa = MapaEditorial(fuente)
    tipos = Counter(e.tipo for e in mapa.redactables)
    en_actividades = [e for e in mapa.redactables
                      if fuente._elementos[e.componente][1] is not fuente.unidad]
    print(f"\n2. Mapa: {len(mapa.lecciones)} lecciones, {len(mapa.redactables)} elementos "
          f"redactables, {len(en_actividades)} de ellos en actividades.")
    print("   ", tipos.most_common(12))
    print("   ", [l.titulo[:40] for l in mapa.lecciones])

    # 3. Cirugía: un texto de la teoría y uno de una actividad
    teoria = next(e for e in mapa.redactables if e.tipo == "text" and e not in en_actividades)
    actividad = next((e for e in en_actividades if e.tipo == "text"), None)
    elegidos = [e for e in (teoria, actividad) if e is not None]
    fuente2 = FuenteAuthor.abrir(origen)
    mapa2 = MapaEditorial(fuente2)
    for e in elegidos:
        mapa2.aplicar_cambio(e.clave, e.valor.replace("<p>", "<p>[PRUEBA] ", 1) if "<p>" in e.valor
                             else "[PRUEBA] " + e.valor)
    fuente2.reconstruir(trabajo / "cirugia")
    cambiados = comparar(origen, trabajo / "cirugia")
    print(f"\n3. Cirugía: cambian {len(cambiados)} ficheros (se esperan {len(elegidos)}):")
    for c in cambiados:
        print("   ", c)
    if r["conectores_sin_actividad"]:
        print("\nFaltan los proyectos de actividades: sus textos no se adaptarían.")
    ok = not distintos and len(cambiados) == len(elegidos) and not r["conectores_sin_actividad"]
    print("\nResultado:", "BIEN" if ok else "REVISAR")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), [Path(a) for a in sys.argv[2:]]))
