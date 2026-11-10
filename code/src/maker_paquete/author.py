"""Apertura y reescritura del fuente de Author: la unidad y sus proyectos de actividades.

Un fuente de Author es un zip por proyecto con esta forma:

    unitCourseData.json        catálogo y curso: `guion`, `revision_index`, plantilla
    courseInfo.xml             el curso, en XML
    data/<N>/rev.xml           una por revisión
    data/<N>/lang/<idioma>/<guion>.xml   la unidad entera en esa revisión
    resources/                 imágenes, PDF y audio

La revisión vigente es la de `revision_index`. Su XML es **el mismo árbol de componentes
que el `project.json` de un paquete publicado**: un elemento con atributo `id` es un
componente, y sus hijos sin `id` son sus propiedades.

Las actividades no van en el zip de la unidad: cada `quizConnector574` solo guarda en
`projectDataLink` el id de otro proyecto de Author (plantilla 574). Aquí se abren todos
juntos y el componente `quiz` de cada actividad se cuelga de su conector, que es como
quedan en el paquete publicado. Así el mapa editorial, la memoria y los validadores
trabajan igual sobre un fuente que sobre un publicado.

Reescribir sin cambios produce los XML idénticos byte a byte: solo se tocan las
propiedades cuyo valor ha cambiado, y solo se reescribe el XML de la revisión vigente.
"""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLANTILLA_ACTIVIDADES = "574"
# Dónde guarda la aplicación los proyectos de actividades: dentro de la carpeta de la
# unidad, para que la unidad quede en la raíz con sus `resources/`, como un paquete
# publicado, y todo lo que ya trabaja sobre `resources/` sirva sin cambios.
ACTIVIDADES = "_actividades"


def es_fuente(carpeta: Path) -> bool:
    """Si una carpeta extraída es un proyecto de Author y no un paquete publicado."""
    carpeta = Path(carpeta)
    return (carpeta / "unitCourseData.json").is_file() and (carpeta / "data").is_dir()


def plantilla_de(carpeta: Path) -> str:
    return str(json.loads((Path(carpeta) / "unitCourseData.json").read_text(encoding="utf-8"))
               ["course"]["id_template"])


# ------------------------------------------------------------------ serializador

def _escapar(texto: str, comillas: bool) -> str:
    texto = texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if comillas:
        texto = texto.replace('"', "&quot;")
    return texto.replace("\r", "&#13;")


def _serializar(e: ET.Element, estilo: dict) -> str:
    atributos = "".join(f' {k}="{_escapar(v, True)}"' for k, v in e.attrib.items())
    hijos, texto = list(e), e.text or ""
    if not hijos and not texto:
        return f"<{e.tag}{atributos}/>" if estilo["autocierre"] else f"<{e.tag}{atributos}></{e.tag}>"
    return (f"<{e.tag}{atributos}>" + _escapar(texto, estilo["comillas"])
            + "".join(_serializar(h, estilo) + _escapar(h.tail or "", estilo["comillas"]) for h in hijos)
            + f"</{e.tag}>")


def _estilo(crudo: bytes) -> dict:
    """Cómo escribió el XML la versión de Author que lo guardó.

    Las revisiones actuales cierran los vacíos como `<x></x>` y escapan las comillas del
    texto; la primera revisión de una unidad de 2024 usa `<x/>` y comillas sin escapar.
    """
    return {"autocierre": b"/>" in crudo, "comillas": b"&quot;" in crudo}


def volcar(raiz: ET.Element, estilo: dict, crudo: bytes) -> bytes:
    cabecera = crudo.split(b"?>", 1)[0] + b"?>" if crudo.startswith(b"<?xml") else b""
    salto = b"\n" if cabecera and crudo[len(cabecera):len(cabecera) + 1] == b"\n" else b""
    final = b"\n" if crudo.endswith(b"\n") else b""
    return cabecera + salto + _serializar(raiz, estilo).encode("utf-8") + final


# ------------------------------------------------------------------ proyectos

@dataclass
class ProyectoAuthor:
    """Un proyecto de Author: la unidad o una de sus actividades."""

    raiz: Path
    datos: dict[str, Any]
    ruta_xml: Path
    crudo: bytes
    arbol: ET.Element
    estilo: dict

    @classmethod
    def abrir(cls, raiz: Path) -> "ProyectoAuthor":
        datos = json.loads((raiz / "unitCourseData.json").read_text(encoding="utf-8"))
        curso = datos["course"]
        vigente = raiz / "data" / str(curso["revision_index"]) / "lang"
        candidatos = sorted(vigente.glob(f"*/{curso['guion']}.xml"))
        if len(candidatos) != 1:
            raise ValueError(f"{raiz.name}: se esperaba un guion en la revisión vigente "
                             f"{curso['revision_index']} y hay {len(candidatos)}")
        crudo = candidatos[0].read_bytes()
        return cls(raiz=raiz, datos=datos, ruta_xml=candidatos[0], crudo=crudo,
                   arbol=ET.fromstring(crudo), estilo=_estilo(crudo))

    @property
    def id(self) -> str:
        return str(self.datos["course"]["id_course"])

    @property
    def plantilla(self) -> str:
        return str(self.datos["course"]["id_template"])

    @property
    def nombre(self) -> str:
        return self.datos["course"].get("name") or self.raiz.name

    @property
    def idioma(self) -> str:
        """El de la carpeta `lang/` de la revisión vigente. El `language` del curso puede
        mentir: «6. A Prehistoria» dice `es` y está en gallego."""
        return self.ruta_xml.parent.name

    @property
    def revision(self) -> str:
        return str(self.datos["course"]["revision_index"])

    def volcar(self) -> bytes:
        return volcar(self.arbol, self.estilo, self.crudo)


@dataclass
class FuenteAuthor:
    """Una unidad de Author con sus actividades, abierta como un único árbol.

    Ofrece la misma interfaz que `Paquete` para el mapa editorial: `componentes`,
    `components` (aquí el mismo diccionario, porque solo hay una copia del contenido),
    `cmi5` (vacío) e `idiomas`.
    """

    carpeta: Path
    unidad: ProyectoAuthor
    actividades: dict[str, ProyectoAuthor]
    componentes: dict[str, dict[str, Any]] = field(default_factory=dict)
    sin_resolver: list[str] = field(default_factory=list)
    _elementos: dict[str, tuple[ET.Element, ProyectoAuthor]] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- apertura

    @classmethod
    def abrir(cls, carpeta: Path) -> "FuenteAuthor":
        """Abre los proyectos extraídos de una unidad.

        Dos disposiciones: la de la aplicación, con la unidad en `carpeta` y sus
        actividades en `carpeta/_actividades/<proyecto>/`, y la de los zips tal cual,
        un proyecto por subcarpeta. La unidad es el proyecto que no es de actividades.
        """
        carpeta = Path(carpeta)
        if es_fuente(carpeta):
            extra = carpeta / ACTIVIDADES
            raices = [carpeta] + (sorted(p for p in extra.iterdir() if p.is_dir() and es_fuente(p))
                                  if extra.is_dir() else [])
        else:
            raices = sorted(p for p in carpeta.iterdir() if p.is_dir() and es_fuente(p))
        proyectos = [ProyectoAuthor.abrir(r) for r in raices]
        unidades = [p for p in proyectos if p.plantilla != PLANTILLA_ACTIVIDADES]
        if len(unidades) != 1:
            raise ValueError(f"Se esperaba una unidad y hay {len(unidades)}: "
                             f"{', '.join(p.nombre for p in unidades) or 'ninguna'}")
        fuente = cls(carpeta=carpeta, unidad=unidades[0],
                     actividades={p.id: p for p in proyectos if p.plantilla == PLANTILLA_ACTIVIDADES})
        fuente._construir()
        return fuente

    def _registrar(self, e: ET.Element, padre: str | None, proyecto: ProyectoAuthor) -> str:
        cid = e.attrib["id"]
        hijos = [h for h in e if "id" in h.attrib]
        self.componentes[cid] = {
            "id": cid, "name": e.tag, "parent": padre,
            "properties": {h.tag: h.text or "" for h in e if "id" not in h.attrib},
            "componentChildren": [h.attrib["id"] for h in hijos],
        }
        self._elementos[cid] = (e, proyecto)
        for h in hijos:
            self._registrar(h, cid, proyecto)
        return cid

    def _construir(self) -> None:
        self._registrar(self.unidad.arbol, None, self.unidad)
        # Cada conector recibe el `quiz` de su proyecto de actividades, como en el
        # paquete publicado. El módulo y la página del proyecto son andamiaje de Author.
        for cid, comp in list(self.componentes.items()):
            if comp["name"] != "quizConnector574":
                continue
            enlace = (comp["properties"].get("projectDataLink") or "").strip()
            proyecto = self.actividades.get(enlace)
            if proyecto is None:
                self.sin_resolver.append(enlace)
                continue
            for quiz in proyecto.arbol.iter("quiz"):
                if "id" in quiz.attrib:
                    comp["componentChildren"].append(self._registrar(quiz, cid, proyecto))

    # ------------------------------------------------------------- descripción

    @property
    def components(self) -> dict[str, dict[str, Any]]:
        return self.componentes

    @property
    def cmi5(self) -> dict[str, Any]:
        return {}

    @property
    def idiomas(self) -> list[str]:
        return [self.unidad.idioma]

    @property
    def idioma_principal(self) -> str:
        return self.unidad.idioma

    @property
    def raiz(self) -> Path:
        return self.carpeta

    @property
    def proyectos(self) -> list[ProyectoAuthor]:
        return [self.unidad, *self.actividades.values()]

    def resumen(self) -> dict[str, Any]:
        from collections import Counter
        tipos = Counter(c["name"] for c in self.componentes.values())
        return {
            "titulo": self.unidad.nombre, "idioma": self.unidad.idioma,
            "plantilla": f"Author, plantilla {self.unidad.plantilla}",
            "lecciones": sum(1 for c in self.componentes.values() if c["name"] == "pageContent"),
            "idioma_declarado": self.unidad.datos["course"].get("language"),
            "revision": self.unidad.revision, "componentes": len(self.componentes),
            "actividades": len(self.actividades),
            "conectores": sum(1 for c in self.componentes.values() if c["name"] == "quizConnector574"),
            "conectores_sin_actividad": self.sin_resolver,
            "componentes_mas_frecuentes": tipos.most_common(8),
        }

    # ---------------------------------------------------------------- escritura

    def _volcar_cambios(self) -> set[str]:
        """Pasa a los XML las propiedades que han cambiado. Devuelve los proyectos tocados."""
        tocados = set()
        for cid, comp in self.componentes.items():
            e, proyecto = self._elementos[cid]
            for h in e:
                if "id" in h.attrib or h.tag not in comp["properties"]:
                    continue
                nuevo = comp["properties"][h.tag]
                if (h.text or "") != nuevo:
                    h.text = nuevo or None
                    tocados.add(proyecto.id)
        return tocados

    def texto_xml(self) -> str:
        """El XML vigente de todos los proyectos, para buscar qué recursos se usan."""
        return "\n".join(p.crudo.decode("utf-8", errors="replace") for p in self.proyectos)

    def cambiar_idioma(self, nuevo: str) -> list[str]:
        """Marca cada proyecto en el idioma `nuevo`, en su sitio. Devuelve los cambiados.

        El idioma está en cuatro sitios y hay que cambiarlos todos: la carpeta
        `data/<revisión>/lang/<idioma>` de la revisión vigente, `language` del catálogo y
        del curso en `unitCourseData.json`, y `<language>` e `<id_language>` en
        `courseInfo.xml`. Es la opción que eligió el equipo editorial (2026-10-06), para
        que nadie tenga que acordarse de cambiarlo al importar. El resto de cada fichero
        no se toca: se sustituye el valor, no se vuelve a serializar.
        """
        cambiados = []
        for p in self.proyectos:
            antes = (p.idioma, p.datos["course"].get("language"))
            carpeta = p.ruta_xml.parent
            if carpeta.name != nuevo:
                otra = carpeta.with_name(nuevo)
                if otra.exists():
                    raise ValueError(f"{p.nombre}: la revisión {p.revision} ya tiene una carpeta "
                                     f"lang/{nuevo}")
                carpeta.rename(otra)
                p.ruta_xml = otra / p.ruta_xml.name
            # En bytes: `write_text` cambia los saltos de línea en Windows.
            datos = p.raiz / "unitCourseData.json"
            texto = datos.read_bytes().decode("utf-8")
            datos.write_bytes(re.sub(r'("language"\s*:\s*)"[^"]*"', lambda m: f'{m.group(1)}"{nuevo}"',
                                     texto).encode("utf-8"))
            p.datos = json.loads(datos.read_bytes().decode("utf-8"))
            info = p.raiz / "courseInfo.xml"
            if info.is_file():
                texto = info.read_bytes().decode("utf-8")
                info.write_bytes(re.sub(r"<(language|id_language)>[^<]*</\1>",
                                        lambda m: f"<{m.group(1)}>{nuevo}</{m.group(1)}>",
                                        texto).encode("utf-8"))
            if antes != (nuevo, nuevo):
                cambiados.append(p.nombre)
        return cambiados

    def cambiar_nombre(self, nuevo: str) -> bool:
        """Renombra la unidad, en su sitio. Devuelve si ha cambiado algo.

        Es el nombre con el que learningCloud crea el proyecto al importar: sin esto, la
        unidad adaptada a Andalucía llegaba como «6. A Prehistoria». Está en tres sitios:
        `name` del catálogo y del curso en `unitCourseData.json` (ASCII con `\\uXXXX`) y
        `<name>` en `courseInfo.xml`. Solo la unidad: las actividades se llaman por su
        código (`Id_070406_ap2_bl1`). Como en `cambiar_idioma`, se sustituye el valor sin
        volver a serializar.
        """
        p = self.unidad
        viejo = p.nombre
        if not nuevo or nuevo == viejo:
            return False
        datos = p.raiz / "unitCourseData.json"
        texto = datos.read_bytes().decode("utf-8")
        escrito = json.dumps(nuevo)
        if "\\/" in texto:
            escrito = escrito.replace("/", "\\/")

        def nombre(m: re.Match) -> str:
            return m.group(1) + escrito if json.loads(m.group(2)) == viejo else m.group(0)

        datos.write_bytes(re.sub(r'("name"\s*:\s*)("(?:[^"\\]|\\.)*")', nombre, texto).encode("utf-8"))
        p.datos = json.loads(datos.read_bytes().decode("utf-8"))
        info = p.raiz / "courseInfo.xml"
        if info.is_file():
            from xml.sax.saxutils import escape
            texto = info.read_bytes().decode("utf-8")
            info.write_bytes(texto.replace(f"<name>{escape(viejo)}</name>",
                                           f"<name>{escape(nuevo)}</name>").encode("utf-8"))
        return True

    def guardar(self) -> set[str]:
        """Escribe en su sitio los XML que han cambiado. Devuelve los proyectos tocados."""
        tocados = self._volcar_cambios()
        for p in self.proyectos:
            if p.id in tocados:
                p.ruta_xml.write_bytes(p.volcar())
        return tocados

    def reconstruir(self, destino: Path) -> Path:
        """Copia la unidad entera a `destino` con los XML vigentes regenerados.

        Basta con la revisión vigente: al importar, Author crea proyectos nuevos con ids
        nuevos a partir de ella, sin el historial, y reasigna a la vez los de la unidad y
        sus actividades, así que los `projectDataLink` siguen casando (confirmado por el
        equipo de Author el 2026-10-06).
        """
        destino = Path(destino)
        if destino.exists() and any(destino.iterdir()):
            raise FileExistsError(f"El destino existe y no está vacío: {destino}")
        self._volcar_cambios()
        shutil.copytree(self.carpeta, destino, dirs_exist_ok=True)
        for p in self.proyectos:
            (destino / p.ruta_xml.relative_to(self.carpeta)).write_bytes(p.volcar())
        return destino
