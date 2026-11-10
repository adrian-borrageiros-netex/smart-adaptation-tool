"""Ida y vuelta con learningCloud (Author): traer una unidad y enviar la adaptada.

learningCloud no tiene API para terceros. Todo lo hace un marcador del navegador que se
ejecuta en learningCloud con la sesión del usuario, y que habla por postMessage con la
página `/learningcloud` de Sarela. Este módulo es la parte de Sarela: qué trabajos se
pueden enviar, a qué proyecto y carpeta, y los zips que el marcador importa allí.

Lo que costó averiguar, medido el 2026-10-27 en el proyecto «Sarela Pruebas»:

- Importar (`POST /api/content_managers/import`) crea un proyecto nuevo con **otro id de
  curso** (36936 → 46057). La unidad enlaza sus actividades por ese id
  (`projectDataLink`), y la importación **no lo reenlaza**: una unidad importada tal cual
  sigue abriendo las actividades originales, en el proyecto de origen y en su idioma.
  Por eso se importan primero las actividades, el marcador devuelve el id nuevo de cada
  una y Sarela reescribe los enlaces de la unidad antes de que se importe.
- El id nuevo lo da `get_ntxdata` (`ntxdat.project.id`); la respuesta de la importación
  no lo trae.
- La importación es síncrona, y el nombre del proyecto importado es el del curso.
- La política de seguridad de learningCloud no deja que su página pida nada a Sarela:
  los zips viajan por postMessage desde la ventana de Sarela, nunca por fetch.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

from . import auditoria, bd
from .servicio import ErrorServicio
from .trabajador import ACTIVIDADES, comprimir_proyecto, nombre_unidad

# Orígenes de learningCloud con los que se habla, separados por comas. El primero es el
# entorno donde están los proyectos destino.
ENTORNOS = [o.strip().rstrip("/") for o in os.environ.get(
    "SARELA_ORIGENES_IMPORTACION", "https://ntxfactory-content.learningcloud.me").split(",") if o.strip()]

CLAVE = "learningcloud"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ENLACE = re.compile(r"(<projectDataLink>)\s*([^<\s]*)\s*(</projectDataLink>)")
# El guion de cada revisión, en cualquier idioma: `data/<revisión>/lang/<idioma>/<guion>.xml`.
_GUION = re.compile(r"^data/[^/]+/lang/[^/]+/[^/]+\.xml$")


# --------------------------------------------------------------------- ajustes

def ajustes() -> dict[str, Any]:
    """El entorno y el proyecto destino de cada comunidad."""
    with bd.Sesion() as s:
        fila = s.get(bd.Ajuste, CLAVE)
        datos = json.loads(fila.valor) if fila else {}
        datos["actualizado_por"] = fila.actualizado_por if fila else None
    datos.setdefault("destinos", {})
    datos["entorno"] = ENTORNOS[0] if ENTORNOS else None
    return datos


def id_proyecto(texto: str) -> str | None:
    """El id de un proyecto a partir de su dirección en learningCloud o del propio id."""
    texto = (texto or "").strip()
    m = re.search(rf"/project/({_UUID})", texto) or re.fullmatch(rf"({_UUID})", texto)
    return m.group(1) if m else None


def guardar_destinos(destinos: dict[str, dict[str, str]], usuario: str | None = None) -> dict[str, Any]:
    """Guarda el proyecto destino de cada comunidad. Uno vacío quita el destino."""
    limpio = {}
    for comunidad, d in destinos.items():
        if comunidad not in bd.COMUNIDADES:
            raise ErrorServicio(f"Comunidad desconocida: {comunidad}")
        texto = (d.get("proyecto") or "").strip()
        if not texto:
            continue
        pid = id_proyecto(texto)
        if pid is None:
            raise ErrorServicio(f"{comunidad}: «{texto}» no es la dirección de un proyecto de "
                                "learningCloud ni su identificador.")
        limpio[comunidad] = {"proyecto": pid, "nombre": (d.get("nombre") or "").strip() or None}
    with bd.Sesion() as s:
        fila = s.get(bd.Ajuste, CLAVE) or bd.Ajuste(clave=CLAVE, valor="{}")
        valor = json.loads(fila.valor)
        anterior = valor.get("destinos")
        valor["destinos"] = limpio
        fila.valor = json.dumps(valor, ensure_ascii=False)
        fila.actualizado_por = usuario
        s.add(fila)
        s.commit()
    if anterior != limpio:
        auditoria.registrar("ajustes", detalle="Destinos de learningCloud: " + (", ".join(
            f"{c} → {d['nombre'] or d['proyecto']}" for c, d in limpio.items()) or "ninguno"))
    return ajustes()


# --------------------------------------------------------------------- origen

def origen(paquete: bd.Paquete) -> dict[str, Any]:
    """De dónde salió la unidad en learningCloud.

    Si la trajo el marcador, viene la ruta con sus nombres. Si se subió a mano, el propio
    fuente dice su carpeta (`catalogue.parent_id`) y su proyecto (`catalogue.root`), y el
    marcador resuelve los nombres al enviar.
    """
    datos = json.loads(paquete.origen) if paquete.origen else {}
    if (paquete.formato or "maker") == "author" and not datos.get("carpeta"):
        ficha = bd.ruta(paquete.ruta_extraido) / "unitCourseData.json"
        if ficha.is_file():
            catalogo = json.loads(ficha.read_text(encoding="utf-8")).get("catalogue") or {}
            if catalogo.get("parent_id"):
                datos["carpeta"] = catalogo["parent_id"]
                datos.setdefault("proyecto", {"id": catalogo.get("root")})
    return datos


# --------------------------------------------------------------------- proyectos

def _curso(carpeta: Path) -> dict[str, Any]:
    return json.loads((carpeta / "unitCourseData.json").read_text(encoding="utf-8"))["course"]


def _enlaces(unidad: Path) -> list[str]:
    """Los proyectos de actividades que enlaza la revisión vigente de la unidad."""
    curso = _curso(unidad)
    enlaces: list[str] = []
    for xml in sorted((unidad / "data" / str(curso["revision_index"]) / "lang").glob(f"*/{curso['guion']}.xml")):
        for m in _ENLACE.finditer(xml.read_text(encoding="utf-8")):
            if m.group(2) and m.group(2) not in enlaces:
                enlaces.append(m.group(2))
    return enlaces


def _proyectos(t: bd.Trabajo) -> tuple[Path, list[dict[str, Any]], list[str]]:
    """La carpeta del resultado, sus actividades enlazadas y los enlaces sin actividad."""
    destino = bd.ruta(t.ruta_resultado)
    if destino is None or not (destino / "unitCourseData.json").is_file():
        raise ErrorServicio(f"El trabajo {t.id} no tiene un fuente de Author como resultado.")
    enlaces = _enlaces(destino)
    actividades = {}
    carpeta = destino / ACTIVIDADES
    for p in sorted(carpeta.iterdir()) if carpeta.is_dir() else []:
        if (p / "unitCourseData.json").is_file():
            actividades[str(_curso(p)["id_course"])] = p
    # Solo viajan las actividades que la unidad usa, en el orden en que aparecen.
    lista = [{"fichero": f"{actividades[e].name}.zip", "id": e} for e in enlaces if e in actividades]
    return destino, lista, [e for e in enlaces if e not in actividades]


def _ficha(t: bd.Trabajo, destinos: dict[str, Any]) -> dict[str, Any]:
    problemas = []
    destino = destinos.get(t.comunidad or "") if t.tipo == bd.ADAPTACION else None
    if t.tipo != bd.ADAPTACION:
        problemas.append("Es una traducción: solo se envían adaptaciones, porque el proyecto "
                         "destino depende de la comunidad.")
    elif destino is None:
        problemas.append(f"No hay proyecto destino para {t.comunidad}: se configura en Ajustes.")
    o = origen(t.paquete)
    proyecto_origen = (o.get("proyecto") or {}).get("id")
    if not o.get("ruta") and not o.get("carpeta"):
        problemas.append("No se sabe de qué carpeta de learningCloud salió la unidad.")
    elif o.get("ruta") == [] or (o.get("carpeta") and o["carpeta"] == proyecto_origen):
        problemas.append("La unidad estaba en la raíz de su proyecto: no hay carpeta que replicar.")
    if destino and proyecto_origen and destino["proyecto"] == proyecto_origen:
        problemas.append("El proyecto destino es el de origen: la copia acabaría junto a la "
                         "unidad original.")
    unidad, actividades, sueltos = None, [], []
    try:
        carpeta, actividades, sueltos = _proyectos(t)
        unidad = f"{nombre_unidad(carpeta)}.zip"
    except (ErrorServicio, OSError, ValueError, KeyError) as e:
        problemas.append(str(e))
    if sueltos:
        problemas.append(f"La unidad enlaza {len(sueltos)} actividades que no están en el paquete "
                         f"(ids {', '.join(sueltos)}): en el destino seguirían abriendo las originales.")
    return {
        "trabajo": t.id, "titulo": t.paquete.titulo or t.paquete.nombre,
        "comunidad": t.comunidad, "idioma": t.idioma_destino, "destino": destino,
        "origen": o, "unidad": unidad, "actividades": actividades,
        "problemas": problemas, "envio": json.loads(t.envio) if t.envio else None,
    }


def enviables() -> list[dict[str, Any]]:
    """Los trabajos terminados sobre un fuente de Author, con lo que impide enviarlos."""
    destinos = ajustes()["destinos"]
    with bd.Sesion() as s:
        trabajos = (s.query(bd.Trabajo).filter(bd.Trabajo.estado == bd.LISTO)
                    .order_by(bd.Trabajo.id.desc()).all())
        return [_ficha(t, destinos) for t in trabajos if (t.paquete.formato or "maker") == "author"]


def ficha(trabajo_id: int) -> dict[str, Any]:
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        return _ficha(t, ajustes()["destinos"])


# --------------------------------------------------------------------- ficheros

def _trabajo(s, trabajo_id: int) -> bd.Trabajo:
    t = s.get(bd.Trabajo, trabajo_id)
    if t is None or t.estado != bd.LISTO:
        raise ErrorServicio(f"El trabajo {trabajo_id} no existe o no está terminado.")
    return t


def actividad(trabajo_id: int, fichero: str) -> bytes:
    """El zip de una de las actividades del trabajo, tal como la importa learningCloud."""
    with bd.Sesion() as s:
        destino, actividades, _ = _proyectos(_trabajo(s, trabajo_id))
    if fichero not in {a["fichero"] for a in actividades}:
        raise ErrorServicio(f"{fichero} no es una actividad de este trabajo.")
    return comprimir_proyecto(destino / ACTIVIDADES / fichero.removesuffix(".zip"))


def unidad_reenlazada(trabajo_id: int, mapa: dict[str, str]) -> tuple[str, bytes]:
    """El zip de la unidad con cada actividad enlazada por su id nuevo en learningCloud.

    `mapa` va del id de curso con que se exportó cada actividad al que le ha dado la
    importación. Se reescriben todas las revisiones, no solo la vigente: restaurar una
    antigua en Author no debe devolver la unidad a las actividades originales.
    """
    with bd.Sesion() as s:
        destino, actividades, sueltos = _proyectos(_trabajo(s, trabajo_id))
    if sueltos:
        raise ErrorServicio(f"La unidad enlaza actividades que no están en el paquete: {', '.join(sueltos)}.")
    faltan = [a["id"] for a in actividades if not str(mapa.get(a["id"], "")).isdigit()]
    if faltan:
        raise ErrorServicio(f"Falta el id nuevo de las actividades {', '.join(faltan)}.")
    mapa = {a["id"]: str(mapa[a["id"]]) for a in actividades}

    def reenlazar(relativa: str, contenido: bytes) -> bytes:
        if not _GUION.match(relativa):
            return contenido
        texto = contenido.decode("utf-8")
        nuevo = _ENLACE.sub(lambda m: m.group(1) + mapa.get(m.group(2), m.group(2)) + m.group(3), texto)
        return contenido if nuevo == texto else nuevo.encode("utf-8")

    datos = comprimir_proyecto(destino, excluir=(ACTIVIDADES, "_fuente.json"), sustituir=reenlazar)
    return f"{nombre_unidad(destino)}.zip", datos


def registrar_envio(trabajo_id: int, resultado: dict[str, Any], usuario: str | None = None) -> dict[str, Any]:
    """Guarda en el trabajo lo que devolvió el marcador: enviado o con error."""
    campos = ("estado", "mensaje", "proyecto", "ruta", "carpeta", "url", "unidad", "actividades")
    envio = {k: resultado.get(k) for k in campos if resultado.get(k) is not None}
    if envio.get("estado") not in ("enviado", "error", "conflicto"):
        raise ErrorServicio("Estado de envío desconocido.")
    envio["usuario"] = usuario
    envio["fecha"] = dt.datetime.now().isoformat(timespec="seconds")
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        t.envio = json.dumps(envio, ensure_ascii=False)
        s.commit()
    auditoria.registrar("enviar a learningCloud", trabajo_id=trabajo_id,
                        resultado="ok" if envio["estado"] == "enviado" else envio["estado"],
                        detalle=" / ".join(filter(None, [
                            (envio.get("proyecto") or {}).get("nombre"),
                            *(envio.get("ruta") or [])])) or envio.get("mensaje"))
    return envio
