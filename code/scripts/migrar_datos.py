"""Copia la base de datos del demostrador de SQLite a PostgreSQL.

Lo que importa conservar es la memoria —frases aprobadas y equivalencias territoriales—,
que es el trabajo acumulado, pero se copia todo: paquetes, trabajos, términos, feedback y
consumo. Las rutas antiguas son absolutas y de Windows; se reescriben relativas a la
carpeta de datos, que es como se guardan ahora.

Se ejecuta en el servidor, dentro del contenedor, con la carpeta de datos ya copiada:

    docker compose exec app python scripts/migrar_datos.py --desde /datos/demostrador.db

Por defecto no escribe nada: enseña qué copiaría. Con --escribir, copia. Se niega a
escribir sobre una base que ya tenga datos, para no mezclar dos instalaciones.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from web import bd  # noqa: E402

# En orden de dependencias: cada tabla después de aquellas a las que apunta.
TABLAS = ["paquete", "trabajo", "termino", "comentario", "frase", "equivalencia", "consumo_ia"]
RUTAS = {"paquete": ["ruta_zip", "ruta_extraido"], "trabajo": ["ruta_resultado", "ruta_informe"]}


def _relativa(valor: str | None) -> str | None:
    """`C:\\...\\.demostrador\\resultados\\trabajo-18` → `resultados/trabajo-18`."""
    if not valor:
        return valor
    partes = valor.replace("\\", "/").split("/")
    if ".demostrador" in partes:
        return "/".join(partes[partes.index(".demostrador") + 1:])
    return valor


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--desde", required=True, help="Fichero SQLite de origen.")
    p.add_argument("--escribir", action="store_true", help="Copiar de verdad.")
    args = p.parse_args()

    origen = create_engine(f"sqlite:///{Path(args.desde).as_posix()}")
    destino = bd.motor
    if destino.url.get_backend_name() == "sqlite" and Path(args.desde).resolve() == Path(
            destino.url.database or "").resolve():
        print("El origen y el destino son la misma base.")
        return 2

    # Primero el esquema en el origen, por si es de una versión anterior sin las columnas
    # nuevas: así cada fila trae todos los campos que espera el destino.
    bd.Base.metadata.create_all(destino)
    tablas = bd.Base.metadata.tables

    with destino.connect() as c:
        ocupadas = [t for t in TABLAS if c.execute(select(func.count()).select_from(tablas[t])).scalar()]
    if ocupadas and args.escribir:
        print(f"El destino ya tiene datos en: {', '.join(ocupadas)}. No se mezclan instalaciones.")
        return 1

    with origen.connect() as leer:
        existentes = set(origen.dialect.get_table_names(leer))
        resumen = {}
        filas_por_tabla = {}
        for nombre in TABLAS:
            if nombre not in existentes:
                resumen[nombre] = 0
                continue
            columnas_origen = {r[1] for r in leer.execute(text(f"PRAGMA table_info({nombre})"))}
            columnas = [c.name for c in tablas[nombre].columns if c.name in columnas_origen]
            filas = [dict(r._mapping) for r in leer.execute(
                text(f"SELECT {', '.join(columnas)} FROM {nombre}"))]
            for fila in filas:
                for campo in RUTAS.get(nombre, []):
                    fila[campo] = _relativa(fila.get(campo))
                for campo, columna in tablas[nombre].columns.items():
                    # SQLite guarda los booleanos como 0/1 y las fechas como texto.
                    if campo not in fila or fila[campo] is None:
                        continue
                    try:
                        tipo = columna.type.python_type
                    except NotImplementedError:
                        continue
                    if tipo is bool:
                        fila[campo] = bool(fila[campo])
                    elif tipo is dt.datetime and isinstance(fila[campo], str):
                        fila[campo] = dt.datetime.fromisoformat(fila[campo])
            filas_por_tabla[nombre] = filas
            resumen[nombre] = len(filas)

    for nombre in TABLAS:
        print(f"  {nombre:<14} {resumen[nombre]:>6} filas")
    if not args.escribir:
        print("\nSimulación: no se ha escrito nada. Repite con --escribir para copiar.")
        return 0

    with destino.begin() as escribir:
        for nombre in TABLAS:
            if filas_por_tabla.get(nombre):
                escribir.execute(tablas[nombre].insert(), filas_por_tabla[nombre])
        if destino.url.get_backend_name().startswith("postgresql"):
            # Las filas llevan su id; hay que avanzar las secuencias o el próximo alta
            # chocaría con un id ya usado.
            for nombre in TABLAS:
                escribir.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{nombre}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {nombre}), 1))"))
    print("\nCopiado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
