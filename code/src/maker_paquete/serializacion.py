"""Serialización de los JSON de un paquete Maker.

El paquete no usa un único serializador: cada fichero se genera en una etapa distinta
de la cadena de publicación y con una configuración distinta. Verificado sobre la unidad
de muestra, los tres se reproducen byte a byte con estas reglas:

    project.json      ASCII con escapes \\uXXXX y barras escapadas como \\/
    components.json   UTF-8 crudo, sin escapes
    cmi5.json         ASCII con escapes \\uXXXX, barras sin escapar

Los tres son compactos: sin espacios tras los dos puntos ni tras las comas, sin saltos
de línea y sin BOM. Escribir uno de ellos con la configuración de otro produce un fichero
válido pero distinto del original, lo que impide comparar reconstrucciones y, sobre todo,
introduce ruido en cualquier control de versiones sobre el contenido.
"""

from __future__ import annotations

import json
from typing import Any

_COMPACTO = (",", ":")


def volcar_project(obj: Any) -> bytes:
    """Serializa project.json: ASCII, escapes unicode y barras escapadas.

    La sustitución global de «/» es segura porque en JSON la barra solo puede aparecer
    dentro de una cadena: la sintaxis no la utiliza en ningún otro sitio.
    """
    texto = json.dumps(obj, ensure_ascii=True, separators=_COMPACTO)
    return texto.replace("/", "\\/").encode("utf-8")


def volcar_components(obj: Any) -> bytes:
    """Serializa components.json: UTF-8 crudo, sin escapes."""
    return json.dumps(obj, ensure_ascii=False, separators=_COMPACTO).encode("utf-8")


def volcar_cmi5(obj: Any) -> bytes:
    """Serializa cmi5.json: ASCII con escapes unicode, barras sin escapar."""
    return json.dumps(obj, ensure_ascii=True, separators=_COMPACTO).encode("utf-8")


def cargar(datos: bytes) -> Any:
    """Lee cualquiera de los tres. El orden de claves se conserva."""
    return json.loads(datos.decode("utf-8"))


VOLCADORES = {
    "project.json": volcar_project,
    "components.json": volcar_components,
    "cmi5.json": volcar_cmi5,
}
