"""Paso final de una adaptación: títulos de cmi5 y cambio de idioma del paquete.

Dos cosas que no son traducción elemento a elemento y conviene hacer al final, sobre el
paquete ya adaptado:

1. **Títulos de cmi5.** No son texto independiente: reproducen el contenido de la
   actividad a la que titulan. Traducirlos a mano duplicaría el trabajo y arriesgaría
   que el índice de la unidad dejase de coincidir con lo que el alumno ve. Se derivan
   del árbol de componentes ya adaptado, que es como se generaron en origen.

2. **Idioma del paquete.** `project.json` está indexado por la clave de idioma y
   `cmi5.json` titula por idioma. Una unidad traducida al castellano debe declarar `es`,
   o la plataforma seguirá tratándola como gallega.

    python scripts/finalizar.py .trabajo/asturias --de gl --a es
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import zlib
from pathlib import Path

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from maker_paquete import Paquete, texto_plano  # noqa: E402
from maker_paquete.reproductor import textos_reproductor  # noqa: E402
from maker_paquete.serializacion import VOLCADORES  # noqa: E402

# Rótulos que la plantilla deja puestos y que no forman parte del enunciado.
_PLANTILLA = {
    "title", "page title", "page title 2", "page title 3", "button text", "essay title",
    "fillin title", "block title", "footer title", "activity no interactive",
    "desconecta!", "¡desconecta!", "subtitle popup", "panel title", "panel title 2",
}
# Componentes cuyo texto es para el profesorado y no aparece en el enunciado del alumno.
_NO_ENUNCIADO = {"teacherContent", "activityNoInteractive"}


def _enunciado(paquete: Paquete, cid: str) -> str:
    """Texto visible de una actividad, en el orden en que se lee.

    Las lecciones son la excepción: su título en cmi5 es el rótulo de la lección, no el
    contenido entero. Solo las actividades repiten ahí su enunciado completo.
    """
    comp_raiz = paquete.componentes.get(cid) or {}
    if comp_raiz.get("name") == "pageContent":
        return ((comp_raiz.get("properties") or {}).get("title") or "").strip()

    partes: list[str] = []
    pila = [cid]
    while pila:
        actual = pila.pop(0)
        comp = paquete.componentes.get(actual) or {}
        if comp.get("name") in _NO_ENUNCIADO and actual != cid:
            continue
        props = comp.get("properties") or {}
        for prop in ("textContent", "title", "wording"):
            valor = props.get(prop)
            if not isinstance(valor, str) or not valor.strip():
                continue
            if comp.get("name") in _NO_ENUNCIADO and prop == "title":
                continue
            plano = texto_plano(valor)
            if plano and plano.lower() not in _PLANTILLA:
                partes.append(plano)
        pila = (comp.get("componentChildren") or []) + pila

    texto = " ".join(partes)
    texto = re.sub(r"\s+([.,;:!?])", r"\1", texto)   # el marcado inline deja espacios sueltos
    return re.sub(r"\s+", " ", texto).strip()


def derivar_titulos(paquete: Paquete, idioma: str) -> int:
    """Reescribe los títulos de cmi5 a partir de los componentes ya adaptados."""
    cambiados = 0

    def recorrer(nodo: dict, cid: str | None) -> None:
        nonlocal cambiados
        titulo = nodo.get("title")
        if isinstance(titulo, dict) and cid:
            derivado = _enunciado(paquete, cid)
            if derivado:
                nodo["title"] = {idioma: derivado}
                cambiados += 1
        for hijo in nodo.get("auOrBlock") or []:
            recorrer(hijo, hijo.get("componentId"))

    unidad = paquete.cmi5.get("unitMetadata") or {}
    for hijo in unidad.get("auOrBlock") or []:
        recorrer(hijo, hijo.get("componentId"))
    return cambiados


def cambiar_idioma(paquete: Paquete, de: str, a: str) -> None:
    """Reindexa project.json y el título de la unidad al nuevo idioma."""
    if de in paquete.project:
        paquete.project[a] = paquete.project.pop(de)
        paquete.project[a]["language"] = a

    unidad = paquete.cmi5.get("unitMetadata") or {}
    if isinstance(unidad.get("title"), dict) and de in unidad["title"]:
        unidad["title"] = {a: unidad["title"].pop(de)}


def sincronizar_respuestas_cmi5(paquete: Paquete, idioma: str) -> int:
    """Actualiza en cmi5.json las opciones y la clave de respuesta de cada actividad.

    `cmi5.json` no solo repite los títulos: para cada actividad guarda `choices` (el texto
    de cada opción, por idioma) y `correctResponsesPattern` (la respuesta correcta). Es la
    capa con la que la plataforma sigue y corrige la actividad vía xAPI, así que si el
    enunciado se traduce y esto no, el alumno ve «Pizarra» y el sistema espera «Lousa».

    Todo se reconstruye desde los componentes, que son la fuente única.
    """
    actualizados = 0

    def texto_de(cid: str) -> str | None:
        """Texto visible de una opción.

        Muchas opciones no llevan el texto en sus propias propiedades: el contenedor
        (`qTrueFalseOption`, `qSingleChoiceOption`…) cuelga de un hijo de tipo `text`.
        Por eso se recorre el subárbol y no solo el componente.
        """
        pila = [cid]
        partes: list[str] = []
        while pila:
            comp = paquete.componentes.get(pila.pop(0))
            if not comp:
                continue
            props = comp.get("properties") or {}
            for prop in ("textContent", "title", "titleHtml", "wording"):
                valor = props.get(prop)
                plano = texto_plano(valor) if isinstance(valor, str) else ""
                if plano and plano.lower() not in _PLANTILLA:
                    partes.append(plano)
            pila = (comp.get("componentChildren") or []) + pila
        return " ".join(partes) or None

    def recorrer(nodo: dict) -> None:
        nonlocal actualizados
        # «choices» puede anidar «items» (las actividades de clasificar agrupan las
        # opciones por categoría) y la descripción llega unas veces como objeto por
        # idioma y otras como cadena suelta.
        def opciones(lista):
            for o in lista or []:
                if not isinstance(o, dict):
                    continue
                yield o
                yield from opciones(o.get("items"))

        for opcion in opciones(nodo.get("choices")):
            descripcion = opcion.get("description")
            nuevo = texto_de(opcion.get("id", ""))
            if nuevo is None:
                continue
            if isinstance(descripcion, dict):
                if descripcion != {idioma: nuevo}:
                    opcion["description"] = {idioma: nuevo}
                    actualizados += 1
            elif isinstance(descripcion, str) and descripcion != nuevo:
                opcion["description"] = nuevo
                actualizados += 1
        for respuesta in nodo.get("correctResponsesPattern") or []:
            comp = paquete.componentes.get(respuesta.get("id", "")) or {}
            esperado = (comp.get("properties") or {}).get("correct")
            if isinstance(esperado, str) and esperado and respuesta.get("correct") != esperado:
                respuesta["correct"] = esperado
                actualizados += 1
        for hijo in nodo.get("auOrBlock") or []:
            recorrer(hijo)

    recorrer(paquete.cmi5.get("unitMetadata") or {})
    return actualizados


def sincronizar_anidados(paquete: Paquete) -> int:
    """Propaga las propiedades a las copias anidadas de cada componente.

    Los componentes `quizConnector574` embeben, en `projectDataLink`, **una copia completa
    del subárbol de la actividad**, con los mismos identificadores que el árbol principal.
    Están tanto en `project.json` (bajo `resourceProperties`) como en `components.json`
    (bajo `extendedProperties`). Editar solo el árbol principal deja esas copias en el
    idioma original, y son las que alimentan algunas actividades.

    La propiedad manda desde el árbol principal: se copia hacia las anidadas, nunca al
    revés.
    """
    sincronizados = 0

    def propagar(arbol: dict, contenedor: str) -> None:
        nonlocal sincronizados
        for comp in arbol.values():
            enlace = (comp.get(contenedor) or {}) if isinstance(comp.get(contenedor), dict) else {}
            anidados = (enlace.get("projectDataLink") or {}).get("components") or {}
            for cid, anidado in anidados.items():
                maestro = paquete.componentes.get(cid)
                if maestro and anidado.get("properties") != maestro.get("properties"):
                    anidado["properties"] = json.loads(json.dumps(maestro["properties"]))
                    sincronizados += 1

    propagar(paquete.componentes, "resourceProperties")
    propagar(paquete.components, "extendedProperties")
    return sincronizados


_BLOB = re.compile(r'(var\s+ntxCafCompressed\s*=\s*")([^"]+)(")')

# Campos de userLogged con datos personales de quien publicó el paquete.
_PERSONALES = ("name", "surname", "username", "email", "commonId", "su", "profile",
               "collaborator", "externalId", "srcPhoto")


def sincronizar_index(ruta: Path, paquete: Paquete, idioma: str,
                      titulo: str | None) -> bool:
    """Reescribe la copia del proyecto que index.html lleva embebida.

    `index.html` guarda, comprimida con zlib y en base64, una **tercera copia** del
    contenido además de project.json y components.json, y el reproductor renderiza desde
    ella. Si no se regenera, la unidad se sigue viendo en el idioma original por muy
    traducidos que estén los otros dos ficheros. De paso se vacían los datos personales
    de quien publicó, que viajan en claro dentro del paquete entregado.
    """
    index = ruta / "index.html"
    if not index.is_file():
        return False
    html = index.read_text(encoding="utf-8")
    m = _BLOB.search(html)
    if not m:
        return False

    datos = json.loads(zlib.decompress(base64.b64decode(m.group(2))).decode("utf-8"))

    ntxdat = datos.get("ntxdat") or {}
    ntxdat["projectDataCollection"] = {idioma: paquete.project[idioma]}
    ntxdat["revision"] = paquete.project[idioma].get("revision", ntxdat.get("revision"))
    ntxdat["project"] = paquete.project[idioma].get("project", ntxdat.get("project"))
    datos["ntxdat"] = ntxdat

    entorno = datos.get("ntxEnvironment") or {}
    if titulo:
        entorno["publicationTitle"] = titulo
    entorno["locale"] = idioma
    datos["ntxEnvironment"] = entorno

    usuario = datos.get("userLogged")
    borrados = 0
    if isinstance(usuario, dict):
        for campo in _PERSONALES:
            if usuario.get(campo):
                usuario[campo] = ""
                borrados += 1

    crudo = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nuevo = base64.b64encode(zlib.compress(crudo, 9)).decode("ascii")
    html = html[: m.start(2)] + nuevo + html[m.end(2):]

    if titulo:
        cabecera = (paquete.componentes.get(
            next((c for c, x in paquete.componentes.items()
                  if x.get("name") == "interface"), ""), {}) or {}).get("properties", {})
        rotulo = texto_plano(cabecera.get("title1", "")) or titulo
        html = re.sub(r"<title>.*?</title>", f"<title>{rotulo}</title>", html, count=1)

    # Los botones y avisos del reproductor, en el idioma del paquete si su tabla no lo trae.
    html = textos_reproductor(html, idioma)
    index.write_text(html, encoding="utf-8")
    print(f"index.html: copia embebida regenerada ({len(crudo)} bytes de contenido), "
          f"{borrados} campos personales vaciados")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paquete")
    p.add_argument("--de", default="gl")
    p.add_argument("--a", default="es")
    p.add_argument("--titulo-unidad", default=None,
                   help="Título de la unidad en el idioma de destino.")
    args = p.parse_args()

    ruta = Path(args.paquete)
    paquete = Paquete.abrir(ruta)
    print(f"Paquete: {ruta}   idiomas: {paquete.idiomas}")

    cambiar_idioma(paquete, args.de, args.a)
    if args.titulo_unidad:
        paquete.cmi5["unitMetadata"]["title"] = {args.a: args.titulo_unidad}

    n_anid = sincronizar_anidados(paquete)
    print(f"Copias anidadas en projectDataLink sincronizadas: {n_anid}")

    n_resp = sincronizar_respuestas_cmi5(paquete, args.a)
    print(f"Opciones y claves de respuesta de cmi5 actualizadas: {n_resp}")

    n = derivar_titulos(paquete, args.a)
    print(f"Títulos de cmi5 derivados del contenido adaptado: {n}")
    print(f"Idioma del paquete: {args.de} -> {args.a}   ahora: {list(paquete.project)}")

    for nombre, obj in (("project.json", paquete.project),
                        ("components.json", paquete.components),
                        ("cmi5.json", paquete.cmi5)):
        (ruta / nombre).write_bytes(VOLCADORES[nombre](obj))

    sincronizar_index(ruta, paquete, args.a, args.titulo_unidad)

    # El manifiesto SCORM repite el título de la unidad en dos sitios.
    manifiesto = ruta / "imsmanifest.xml"
    if manifiesto.is_file() and args.titulo_unidad:
        xml = manifiesto.read_text(encoding="utf-8")
        xml = re.sub(r"<title>.*?</title>", f"<title>{args.titulo_unidad}</title>", xml)
        manifiesto.write_text(xml, encoding="utf-8")
        print(f"Manifiesto actualizado con el título «{args.titulo_unidad}»")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
