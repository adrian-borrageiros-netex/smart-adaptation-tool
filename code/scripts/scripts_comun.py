"""Utilidades compartidas por los scripts de verificación."""

from __future__ import annotations

import os
from pathlib import Path


def _sin_comentario(valor: str) -> str:
    """Quita un comentario al final de la línea, salvo que el valor vaya entrecomillado."""
    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
        return valor[1:-1]
    return valor.split(" #", 1)[0].split("	#", 1)[0].strip()


def cargar_env(raiz: Path) -> None:
    """Carga un .env sencillo, sin dependencias externas.

    Las variables ya presentes en el entorno tienen prioridad, para poder sobrescribir
    puntualmente sin tocar el fichero. Las claves con valor vacío se ignoran: una entrada
    sin rellenar en el .env no debe tapar el valor por defecto de quien la consulta.
    """
    ruta = raiz / ".env"
    if not ruta.is_file():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        valor = _sin_comentario(valor.strip())
        if valor:
            os.environ.setdefault(clave.strip(), valor)
