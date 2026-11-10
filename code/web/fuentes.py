"""Dónde se buscan los enlaces que se proponen al editor.

La búsqueda web se limita a estos dominios, y además se filtra en código cualquier URL que
caiga fuera. Son sitios institucionales, educativos y universitarios: lo que un libro de
texto puede citar sin que alguien tenga que investigar antes quién hay detrás.

Wikipedia no está a propósito. Es útil, pero no es una fuente oficial, y la decisión de
citarla en un libro de texto le corresponde al equipo editorial. Se añade aquí si la toman.

Cada dominio cubre también sus subdominios: `xunta.gal` incluye `edu.xunta.gal`.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# Organismos estatales que sirven en cualquier comunidad.
ESTATALES = [
    "gob.es",       # ministerios, incluido el de Educación
    "intef.es",     # recursos educativos abiertos
    "igme.es",      # Instituto Geológico y Minero
    "ign.es",       # Instituto Geográfico Nacional
    "csic.es",      # incluye el Museo Nacional de Ciencias Naturales
]

POR_COMUNIDAD: dict[str, list[str]] = {
    "Andalucía": ["juntadeandalucia.es", "us.es", "ugr.es"],
    "Aragón": ["aragon.es", "unizar.es"],
    "Asturias": ["asturias.es", "educastur.es", "uniovi.es"],
    "Baleares": ["caib.es", "uib.cat", "uib.es"],
    "Canarias": ["gobiernodecanarias.org", "ull.es", "ulpgc.es"],
    "Cantabria": ["cantabria.es", "educantabria.es", "unican.es"],
    "Castilla-La Mancha": ["castillalamancha.es", "jccm.es", "uclm.es"],
    "Castilla y León": ["jcyl.es", "usal.es", "uva.es"],
    "Cataluña": ["gencat.cat", "xtec.cat", "icgc.cat", "edu365.cat", "museuciencies.cat",
                 "ub.edu", "uab.cat"],
    "Comunidad Valenciana": ["gva.es", "uv.es", "ua.es"],
    "Extremadura": ["juntaex.es", "educarex.es", "unex.es"],
    "Galicia": ["xunta.gal", "usc.gal", "udc.es", "uvigo.gal"],
    "La Rioja": ["larioja.org", "unirioja.es"],
    "Madrid": ["comunidad.madrid", "madrid.org", "ucm.es", "uam.es"],
    "Murcia": ["carm.es", "um.es"],
    "Navarra": ["navarra.es", "unavarra.es"],
    "País Vasco": ["euskadi.eus", "ehu.eus"],
}

# Cómo aparece cada comunidad escrita en las lenguas del Estado, para reconocer de qué
# territorio habla una unidad sea cual sea su lengua.
NOMBRES: dict[str, list[str]] = {
    "Andalucía": ["andalucia", "andalusia"], "Aragón": ["aragon", "arago"],
    "Asturias": ["asturias", "asturies", "principado de asturias"],
    "Baleares": ["baleares", "balears", "illes balears"],
    "Canarias": ["canarias", "canaries"], "Cantabria": ["cantabria"],
    "Castilla-La Mancha": ["castilla-la mancha", "castella-la manxa"],
    "Castilla y León": ["castilla y leon", "castella i lleo"],
    "Cataluña": ["cataluna", "catalunya"],
    "Comunidad Valenciana": ["comunidad valenciana", "comunitat valenciana"],
    "Extremadura": ["extremadura"], "Galicia": ["galicia", "galiza", "xunta de galicia"],
    "La Rioja": ["la rioja"], "Madrid": ["comunidad de madrid"], "Murcia": ["region de murcia"],
    "Navarra": ["navarra", "nafarroa"], "País Vasco": ["pais vasco", "euskadi", "pais basc"],
}


def dominios(comunidad: str | None) -> list[str]:
    """Los dominios en los que buscar para una comunidad: los suyos primero."""
    return POR_COMUNIDAD.get(comunidad or "", []) + ESTATALES


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto or "")
                   if unicodedata.category(c) != "Mn").lower()


def territorio_de(textos: list[str]) -> str | None:
    """De qué comunidad habla una unidad, contando cuántas veces nombra a cada una.

    Hace falta para decirle al asistente qué hay que adaptar: «de Galicia a Cataluña» da
    mejores propuestas que «del territorio de origen a Cataluña». Si no nombra ninguna,
    devuelve `None` y el asistente trabaja sin esa pista.
    """
    cuenta: Counter[str] = Counter()
    todo = _sin_tildes(" ".join(textos))
    for comunidad, variantes in NOMBRES.items():
        for v in variantes:
            cuenta[comunidad] += len(re.findall(rf"\b{re.escape(_sin_tildes(v))}\b", todo))
    comunidad, veces = cuenta.most_common(1)[0] if cuenta else (None, 0)
    return comunidad if veces else None
