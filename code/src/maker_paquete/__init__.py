"""Lectura, manipulación y reconstrucción de paquetes de unidad Maker y de fuentes de Author."""

from .mapa import (Elemento, Leccion, MapaEditorial, ajustar_relleno, claves_glosario, editar_texto,
                   enlaces_glosario, extraer_frases, reenlazar_glosario, reinsertar_frases,
                   texto_plano)
from .author import ACTIVIDADES, FuenteAuthor, es_fuente
from .paquete import Paquete
from .serializacion import volcar_cmi5, volcar_components, volcar_project

__all__ = [
    "Paquete", "FuenteAuthor", "es_fuente", "abrir_unidad", "ACTIVIDADES", "MapaEditorial", "Elemento", "Leccion",
    "texto_plano", "extraer_frases", "reinsertar_frases", "editar_texto",
    "claves_glosario", "enlaces_glosario", "reenlazar_glosario", "ajustar_relleno",
    "volcar_project", "volcar_components", "volcar_cmi5",
]


def abrir_unidad(ruta):
    """Abre lo que haya en `ruta`: un paquete publicado de Maker o un fuente de Author.

    Los dos ofrecen la interfaz que usa el mapa editorial, así que el resto del sistema
    no necesita saber cuál es.
    """
    from pathlib import Path
    ruta = Path(ruta)
    return FuenteAuthor.abrir(ruta) if es_fuente(ruta) else Paquete.abrir(ruta)
