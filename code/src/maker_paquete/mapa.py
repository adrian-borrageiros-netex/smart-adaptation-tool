"""Mapa editorial de una unidad Maker.

Traduce el árbol de componentes a la granularidad en la que piensa un editor —lección,
bloque, elemento— y expone los textos redactables con un identificador estable, de modo
que quien adapta la unidad no tenga que saber nada del formato.

Un detalle que condiciona todo: cada componente vive **por duplicado**, en la
representación de autoría (`project.json`) y en la de ejecución (`components.json`), con
las mismas propiedades. Escribir en una sola deja el paquete incoherente, así que toda
modificación pasa por `aplicar_cambio`, que toca las dos.
"""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# Propiedades que contienen texto redactado, frente a las que son configuración
# (vistas, clases CSS, alineaciones, identificadores de respuesta correcta…).
PROPIEDADES_TEXTO = (
    "textContent", "title", "title1", "title2", "titleHtml", "title2Html", "title2html",
    "title3Html", "subtitle", "subtitleHtml", "titleButton", "alt", "creditsHtml", "key",
    "wording", "feedback", "feedbackOk", "feedbackKo", "placeholder", "label",
    "tableContent", "description", "href",
    # «correct» es la clave de respuesta. En los huecos es texto que el alumno teclea y
    # debe traducirse con el enunciado; en otros tipos guarda el id del componente
    # correcto y no se toca. Aparece en el mapa para que el editor pueda revisarlo.
    "correct",
)

# Valores que ocupan una propiedad de texto pero son marcadores de plantilla sin traducir.
_MARCADORES = {
    "page title", "page title 2", "page title 3", "panel title", "panel title 2",
    "title button", "subtitle popup", "quizelement", "quizoption", "quizactivity",
    "key", "?", "x", "a", "menú", "menu",
}

_ETIQUETAS = re.compile(r"<[^>]+>")
_ESPACIOS = re.compile(r"\s+")


def texto_plano(valor: str) -> str:
    """Contenido visible de una propiedad, sin marcado HTML."""
    return _ESPACIOS.sub(" ", html.unescape(_ETIQUETAS.sub(" ", valor))).strip()


@dataclass
class Elemento:
    """Una propiedad de texto concreta de un componente concreto."""

    componente: str
    propiedad: str
    valor: str
    tipo: str
    leccion: str | None = None
    titulo_leccion: str | None = None

    @property
    def plano(self) -> str:
        return texto_plano(self.valor)

    @property
    def palabras(self) -> int:
        return len(self.plano.split())

    @property
    def clave(self) -> str:
        """Identificador estable del elemento, utilizable en una conversación."""
        return f"{self.componente[:8]}.{self.propiedad}"

    @property
    def redactable(self) -> bool:
        p = self.plano
        return bool(p) and p.lower() not in _MARCADORES and not p.isdigit()


@dataclass
class Leccion:
    componente: str
    titulo: str
    posicion: int
    elementos: list[Elemento] = field(default_factory=list)

    @property
    def palabras(self) -> int:
        return sum(e.palabras for e in self.elementos if e.redactable)


class MapaEditorial:
    """Vista de la unidad por lecciones, con acceso a sus textos."""

    def __init__(self, paquete):
        self.paquete = paquete
        self.lecciones: list[Leccion] = []
        self._por_clave: dict[str, Elemento] = {}
        self._construir()

    # ------------------------------------------------------------------ armado

    def _hijos(self, cid: str) -> list[str]:
        return (self.paquete.componentes.get(cid, {}) or {}).get("componentChildren") or []

    def _descendientes(self, cid: str) -> Iterator[str]:
        pila = [cid]
        while pila:
            actual = pila.pop()
            yield actual
            pila.extend(self._hijos(actual))

    def _elementos_de(self, cid: str) -> Iterator[Elemento]:
        comp = self.paquete.componentes.get(cid) or {}
        props = comp.get("properties") or {}
        for prop in PROPIEDADES_TEXTO:
            valor = props.get(prop)
            if isinstance(valor, str) and valor.strip():
                yield Elemento(componente=cid, propiedad=prop, valor=valor,
                               tipo=comp.get("name", "?"))

    def _construir(self) -> None:
        # Las lecciones son los pageContent colgando del bloque de páginas.
        raices = [cid for cid, c in self.paquete.componentes.items()
                  if c.get("name") == "pageContent" and
                  (self.paquete.componentes.get(c.get("parent") or "", {}) or {})
                  .get("name") == "pageBlock"]

        orden = {cid: i for i, cid in enumerate(
            self._hijos(next((c for c, x in self.paquete.componentes.items()
                              if x.get("name") == "pageBlock"), "")))}

        for cid in sorted(raices, key=lambda c: orden.get(c, 999)):
            comp = self.paquete.componentes[cid]
            leccion = Leccion(
                componente=cid,
                titulo=(comp.get("properties") or {}).get("title") or "(sin título)",
                posicion=orden.get(cid, 999),
            )
            for descendiente in self._descendientes(cid):
                for elemento in self._elementos_de(descendiente):
                    elemento.leccion = cid
                    elemento.titulo_leccion = leccion.titulo
                    leccion.elementos.append(elemento)
                    self._por_clave[elemento.clave] = elemento
            self.lecciones.append(leccion)

        self._recoger_cmi5()

        # Elementos fuera de las lecciones (interfaz, ventanas emergentes, pie).
        cubiertos = {e.componente for l in self.lecciones for e in l.elementos}
        sueltos = Leccion(componente="", titulo="(fuera de las lecciones)", posicion=999)
        for cid in self.paquete.componentes:
            if cid in cubiertos:
                continue
            for elemento in self._elementos_de(cid):
                elemento.titulo_leccion = sueltos.titulo
                sueltos.elementos.append(elemento)
                self._por_clave[elemento.clave] = elemento
        if sueltos.elementos:
            self.lecciones.append(sueltos)

    def _recoger_cmi5(self) -> None:
        """Los títulos de cmi5.json son contenido redactable, no solo estructura.

        La unidad, sus lecciones y sus actividades repiten ahí el título que ya está en
        los componentes. Si se adapta uno y no el otro, el índice de la unidad y el
        contenido dejan de coincidir.
        """
        cmi5 = Leccion(componente="", titulo="(estructura cmi5)", posicion=1000)

        def recorrer(nodo: dict, ruta: str) -> None:
            titulo = nodo.get("title")
            if isinstance(titulo, dict):
                for idioma, valor in titulo.items():
                    if isinstance(valor, str) and valor.strip():
                        el = Elemento(componente=ruta, propiedad=f"cmi5:{idioma}",
                                      valor=valor, tipo="cmi5",
                                      titulo_leccion=cmi5.titulo)
                        cmi5.elementos.append(el)
                        self._por_clave[el.clave] = el
            for hijo in nodo.get("auOrBlock") or []:
                recorrer(hijo, hijo.get("componentId") or hijo.get("id", "")[-8:])

        unidad = self.paquete.cmi5.get("unitMetadata")
        if unidad:
            recorrer(unidad, "unidad")
        if cmi5.elementos:
            self.lecciones.append(cmi5)

    # ----------------------------------------------------------------- consulta

    @property
    def elementos(self) -> list[Elemento]:
        return [e for l in self.lecciones for e in l.elementos]

    @property
    def redactables(self) -> list[Elemento]:
        return [e for e in self.elementos if e.redactable]

    def elemento(self, clave: str) -> Elemento:
        return self._por_clave[clave]

    def buscar(self, patron: str, solo_redactables: bool = True) -> list[Elemento]:
        """Elementos cuyo texto visible contiene el patrón, sin distinguir mayúsculas."""
        rx = re.compile(patron, re.IGNORECASE)
        fuente = self.redactables if solo_redactables else self.elementos
        return [e for e in fuente if rx.search(e.plano)]

    def resumen(self) -> list[dict[str, Any]]:
        return [{"posicion": l.posicion, "leccion": l.titulo,
                 "elementos": len([e for e in l.elementos if e.redactable]),
                 "palabras": l.palabras}
                for l in self.lecciones]

    # ---------------------------------------------------------------- escritura

    def aplicar_cambio(self, clave: str, valor_nuevo: str) -> None:
        """Escribe el nuevo valor en las dos representaciones del paquete.

        `components.json` guarda las rutas de recurso en relativo y `project.json` en
        absoluto, así que el valor se reescribe para cada una respetando su convención.
        """
        elemento = self._por_clave[clave]
        cid, prop = elemento.componente, elemento.propiedad

        if prop.startswith("cmi5:"):
            self._aplicar_cmi5(elemento, prop.split(":", 1)[1], valor_nuevo)
            elemento.valor = valor_nuevo
            return

        autoria = self.paquete.componentes.get(cid)
        ejecucion = self.paquete.components.get(cid)
        if autoria is None or ejecucion is None:
            raise KeyError(f"El componente {cid} no está en las dos representaciones.")

        valor_autoria = autoria["properties"].get(prop, "")
        valor_ejecucion = ejecucion["properties"].get(prop, "")

        autoria["properties"][prop] = _trasladar_rutas(valor_autoria, valor_nuevo)
        ejecucion["properties"][prop] = _trasladar_rutas(valor_ejecucion, valor_nuevo)
        elemento.valor = valor_nuevo


    def _aplicar_cmi5(self, elemento: Elemento, idioma: str, valor_nuevo: str) -> None:
        destino = "unidad" if elemento.componente == "unidad" else elemento.componente

        def recorrer(nodo: dict, ruta: str) -> bool:
            if ruta == destino and isinstance(nodo.get("title"), dict):
                nodo["title"][idioma] = valor_nuevo
                return True
            for hijo in nodo.get("auOrBlock") or []:
                if recorrer(hijo, hijo.get("componentId") or hijo.get("id", "")[-8:]):
                    return True
            return False

        if not recorrer(self.paquete.cmi5["unitMetadata"], "unidad"):
            raise KeyError(f"No encuentro {destino} en la estructura cmi5.")


_SRC = re.compile(r'src="([^"]+)"')


def _trasladar_rutas(valor_anterior: str, valor_nuevo: str) -> str:
    """Conserva la forma de las rutas de recurso que tuviera el valor anterior.

    El mismo texto se guarda con rutas absolutas en autoría y relativas en ejecución. Si
    el valor nuevo trae un nombre de fichero que ya existía en el anterior, se reescribe
    con el prefijo que usaba esa representación.
    """
    previas = {ruta.rsplit("/", 1)[-1]: ruta for ruta in _SRC.findall(valor_anterior)}
    if not previas:
        return valor_nuevo

    def sustituir(m: re.Match) -> str:
        return f'src="{previas.get(m.group(1).rsplit("/", 1)[-1], m.group(1))}"'

    return _SRC.sub(sustituir, valor_nuevo)


# --------------------------------------------------------------------- frases

_NODO_TEXTO = re.compile(r"(<[^>]+>)|([^<]+)")


def extraer_frases(valor: str) -> list[str]:
    """Fragmentos de texto visible de un valor HTML, en orden de aparición.

    Traducir el HTML entero obliga a reescribir el marcado y expone a romperlo. Traducir
    solo los nodos de texto conserva etiquetas, atributos, enlaces e identificadores de
    CKEditor intactos por construcción.
    """
    frases = []
    for etiqueta, texto in _NODO_TEXTO.findall(valor):
        if etiqueta or not texto.strip():
            continue
        frases.append(texto)
    return frases


_TOKEN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]", re.UNICODE)


def _tokens(texto: str) -> list[tuple[str, bool, int]]:
    """Palabras y signos sueltos, con si van precedidos de espacio y dónde empiezan.

    Palabra a palabra no basta: en «<b>central</b>, des» el HTML separa «central» de la
    coma, y en el texto del editor van juntos como «central,». No casaban, se reescribían
    los dos nodos y se perdía el espacio de después.
    """
    salida, fin = [], 0
    for m in _TOKEN.finditer(texto):
        salida.append((m.group(), m.start() > fin, m.start()))
        fin = m.end()
    return salida


def editar_texto(valor: str, nuevo: str) -> str:
    """Aplica al HTML un cambio que el editor ha hecho sobre su texto visible.

    El editor no ve el marcado: ve `texto_plano(valor)` y lo corrige. Sustituir el valor
    entero por lo que escribe dejaría el párrafo sin enlaces, negritas ni estilos. Aquí se
    compara unidad a unidad —palabras y signos— el texto anterior con el nuevo y solo se
    reescriben los nodos de texto donde hay diferencias; los demás se conservan **tal
    cual**, con sus entidades y espacios duros. Lo nuevo va al nodo donde empezaba el
    tramo que sustituye, de modo que queda dentro del mismo enlace o la misma negrita.

    Si el texto no ha cambiado devuelve el valor original, lo que hace la operación
    idempotente: aplicarla dos veces da lo mismo que una.
    """
    partes = _NODO_TEXTO.findall(valor)
    plano, nodo_de_caracter = [], []
    for i, (etiqueta, texto) in enumerate(partes):
        if etiqueta:
            continue
        visible = html.unescape(texto)
        plano.append(visible)
        nodo_de_caracter += [i] * len(visible)
    plano = "".join(plano)

    if " ".join(plano.split()) == " ".join(nuevo.split()):
        return valor
    viejos = _tokens(plano)
    if not viejos:
        # Nada que conservar: un valor sin texto visible recibe el texto tal cual.
        return valor + html.escape(nuevo, quote=False)
    nodo_de = [nodo_de_caracter[inicio] for _, _, inicio in viejos]
    nuevos = [(t, espacio) for t, espacio, _ in _tokens(nuevo)]

    asignadas: dict[int, list[tuple[str, bool]]] = {i: [] for i in set(nodo_de)}
    tocados: set[int] = set()
    comparador = difflib.SequenceMatcher(a=[t for t, _, _ in viejos], b=[t for t, _ in nuevos],
                                         autojunk=False)
    for operacion, a0, a1, b0, b1 in comparador.get_opcodes():
        if operacion == "equal":
            for k in range(a1 - a0):
                asignadas[nodo_de[a0 + k]].append(nuevos[b0 + k])
                # Un espacio que aparece o desaparece también es un cambio del nodo.
                if nuevos[b0 + k][1] != viejos[a0 + k][1]:
                    tocados.add(nodo_de[a0 + k])
            continue
        if a1 > a0:
            destino = nodo_de[a0]
            tocados.update(nodo_de[a0:a1])
        else:  # inserción pura: al nodo de la unidad anterior, o al primero
            destino = nodo_de[a0 - 1] if a0 > 0 else nodo_de[0]
        tocados.add(destino)
        asignadas[destino].extend(nuevos[b0:b1])

    salida: list[str] = []
    for i, (etiqueta, texto) in enumerate(partes):
        if etiqueta or i not in tocados:
            salida.append(etiqueta or texto)
            continue
        fichas = asignadas.get(i, [])
        cuerpo = ""
        for k, (t, espacio) in enumerate(fichas):
            cuerpo += (" " if k and espacio else "") + t
        visible = html.unescape(texto)
        izq = visible[: len(visible) - len(visible.lstrip())]
        der = visible[len(visible.rstrip()):]
        # La primera unidad del nodo lleva delante el espacio que le corresponda, si el
        # nodo no lo traía ya y lo anterior no acaba en blanco.
        previo = "".join(salida)
        if fichas and fichas[0][1] and not izq and previo and not re.sub(r"<[^>]+>", "", previo)[-1:].isspace():
            izq = " "
        if not fichas and not izq and not der:
            continue
        salida.append(html.escape(izq + cuerpo + der, quote=False) if cuerpo else html.escape(izq or der, quote=False))
    return "".join(salida)


# ------------------------------------------------------------------- glosario
#
# Una palabra del texto abre su definición con un enlace que lleva la clave del término:
# `<a data-ntx-glossary-link-key="concoidea" …>`. El reproductor la compara con la clave
# de cada `glossaryItem`, que es su propiedad `key` o, si vale «Key» (lo habitual), el
# texto de su `glossaryConcept`. Las dos se comparan en minúsculas y sin tildes.
#
# Al traducir el término («Concoidea» → «Concoide») la clave del enlace sigue en el idioma
# de origen, no casa con ninguno y la ventana del glosario se abre vacía. Pasaba en todas
# las unidades traducidas con glosario.

_ENLACE_GLOSARIO = re.compile(r'(data-ntx-glossary-link-key=")([^"]*)(")')
_TILDES = str.maketrans("áéíóúàèìòùäëïöüâêîôûÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÂÊÎÔÛ",
                        "aeiouaeiouaeiouaeiouAEIOUAEIOUAEIOUAEIOU")


def clave_glosario(texto: str) -> str:
    """Una clave como la compara el reproductor (`app.utils.sanitize`)."""
    return html.unescape(texto or "").translate(_TILDES).lower().strip()


def claves_glosario(mapa: MapaEditorial) -> dict[str, str]:
    """La clave con la que el reproductor encuentra cada término, por id de `glossaryItem`."""
    comps = mapa.paquete.componentes
    claves = {}
    for cid, comp in comps.items():
        if comp.get("name") != "glossaryItem":
            continue
        clave = (comp.get("properties") or {}).get("key")
        if not clave or clave == "Key":
            concepto = next((h for h in comp.get("componentChildren") or []
                             if (comps.get(h) or {}).get("name") == "glossaryConcept"), None)
            clave = _ETIQUETAS.sub("", ((comps.get(concepto) or {}).get("properties") or {})
                                   .get("textContent") or "")
        claves[cid] = clave.strip()
    return claves


def enlaces_glosario(mapa: MapaEditorial) -> set[str]:
    """Las claves de todos los enlaces al glosario, como las compara el reproductor."""
    return {clave_glosario(m.group(2)) for e in mapa.elementos
            for m in _ENLACE_GLOSARIO.finditer(e.valor)}


def reenlazar_glosario(mapa: MapaEditorial, antes: dict[str, str]) -> tuple[int, set[str]]:
    """Lleva la clave de cada enlace al glosario a la del término en su estado actual.

    `antes` son las claves de los términos antes de traducirlos o de aplicar decisiones
    (`claves_glosario`). Devuelve cuántos elementos se han reescrito y qué claves siguen
    sin término al que llevar.
    """
    ahora = claves_glosario(mapa)
    nuevas = {clave_glosario(antes[i]): ahora[i] for i in antes
              if ahora.get(i) and clave_glosario(antes[i]) != clave_glosario(ahora[i])}
    validas = {clave_glosario(c) for c in ahora.values()}

    def sustituir(m: re.Match) -> str:
        vieja = m.group(2)
        nueva = nuevas.get(clave_glosario(vieja))
        if nueva is None:
            return m.group(0)
        # Se conserva el estilo de la original: Maker las escribe casi siempre en minúsculas.
        nueva = nueva.lower() if vieja == vieja.lower() else nueva
        return m.group(1) + html.escape(nueva, quote=True) + m.group(3)

    cambiados = 0
    if nuevas:
        for elemento in list(mapa.elementos):
            if elemento.propiedad.startswith("cmi5:") or "data-ntx-glossary-link-key" not in elemento.valor:
                continue
            valor = _ENLACE_GLOSARIO.sub(sustituir, elemento.valor)
            if valor != elemento.valor:
                try:
                    mapa.aplicar_cambio(elemento.clave, valor)
                except KeyError:
                    continue
                cambiados += 1
    return cambiados, enlaces_glosario(mapa) - validas


_RELLENO = re.compile(r"\.{4,}")


def ajustar_relleno(origen: str, destino: str) -> str:
    """Iguala la longitud de una línea de puntos a la del original.

    Las claves dicotómicas y los índices se maquetan con puntos hasta el número o la
    respuesta: «Tiene concha externa…………… 2». Con un número fijo de puntos, una
    traducción más larga o más corta deja de alinear con las líneas vecinas. Se cambia
    solo el relleno, y solo si el original y la traducción tienen uno cada uno.
    """
    if len(_RELLENO.findall(origen)) != 1 or len(_RELLENO.findall(destino)) != 1:
        return destino
    m = _RELLENO.search(destino)
    puntos = max(3, len(m.group(0)) + len(origen) - len(destino))
    return destino[:m.start()] + "." * puntos + destino[m.end():]


def reinsertar_frases(valor: str, traducciones: list[str]) -> str:
    """Devuelve el valor con sus nodos de texto sustituidos, respetando el marcado.

    Conserva los espacios de los extremos de cada nodo: en HTML son significativos y
    perderlos pega palabras a las etiquetas vecinas.
    """
    pendientes = list(traducciones)
    partes = []
    for etiqueta, texto in _NODO_TEXTO.findall(valor):
        if etiqueta:
            partes.append(etiqueta)
        elif not texto.strip():
            partes.append(texto)
        else:
            nuevo = pendientes.pop(0) if pendientes else texto
            izq = texto[: len(texto) - len(texto.lstrip())]
            der = texto[len(texto.rstrip()):]
            partes.append(f"{izq}{nuevo.strip()}{der}")
    if pendientes:
        raise ValueError(f"Sobran {len(pendientes)} traducciones para este valor.")
    return "".join(partes)
