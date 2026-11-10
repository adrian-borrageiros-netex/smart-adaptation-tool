"""Audita que las actividades sigan teniendo solución después de adaptarlas.

Una traducción puede dejar una actividad sin respuesta correcta posible sin romper
nada visible: el alumno la resuelve bien y el sistema se la da por mala. Ocurre cuando
la clave de respuesta es **el propio texto** y se traduce el enunciado pero no la clave,
o al revés. Este comprobador recorre cada tipo de actividad y verifica que la clave
sigue resolviendo.

Comprueba:

- `qTapTapTarget` con clave textual: su `title` tiene que coincidir, letra por letra, con
  el texto de un origen de la misma actividad. Es el caso que rompió la actividad de
  relacionar zonas de Asturias: los rótulos se tradujeron en su sitio, pero en el original
  los pares estaban cruzados respecto al orden visual.
- Orígenes cuyo `correct` es un identificador: ese destino tiene que existir y pertenecer
  a la misma actividad.
- Opciones de elección (`qSingleChoiceOption`, `qMultipleChoiceOption`, `qTrueFalseOption`,
  `qComboOption`, `qMatrixMultipleOption`): cada grupo necesita al menos una correcta.
- Huecos (`qFillInOption`): la clave no puede estar vacía.
- Enlaces: un `resourceLink` cuyo rótulo anuncia el territorio de destino pero cuya
  dirección sigue apuntando al de origen.

Comparando con el paquete original distingue lo que ya venía roto de lo que ha roto la
adaptación, que es lo único de lo que respondemos.

    python scripts/validar_actividades.py .trabajo/asturias --original courses/BIO003SP1ES
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import unicodedata
from pathlib import Path

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))

from maker_paquete import Paquete, texto_plano  # noqa: E402

TIPOS_OPCION = ("qSingleChoiceOption", "qMultipleChoiceOption", "qTrueFalseOption",
                "qMatrixMultipleOption")
TIPOS_ORIGEN = ("qTapTapOrigin", "qDragAndDropOrigin")

# Señales de que un enlace sigue apuntando al territorio de origen.
_DOMINIOS_ORIGEN = re.compile(r"\.gal\b|xunta\.es|galicia|galiza|xeoloxica", re.I)
_MARCADOR = re.compile(r"\[PENDIENTE", re.I)


def _sin_tildes(texto: str) -> str:
    """En catalán el rótulo dice «Galícia»: sin quitar la tilde, un enlace que sí anuncia
    Galicia se daba por contradictorio."""
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


class Auditoria:
    def __init__(self, paquete: Paquete):
        self.pk = paquete
        self.C = paquete.componentes

    def texto(self, cid: str) -> str:
        """Texto visible de un componente y sus descendientes."""
        partes, pila = [], [cid]
        while pila:
            comp = self.C.get(pila.pop(0)) or {}
            for prop in ("textContent", "title", "wording"):
                valor = (comp.get("properties") or {}).get(prop)
                if isinstance(valor, str):
                    plano = texto_plano(valor)
                    if plano and plano.lower() not in ("title", "fillin title"):
                        partes.append(plano)
            pila = (comp.get("componentChildren") or []) + pila
        return " ".join(partes).strip()

    def visible(self, cid: str) -> str:
        """Solo lo que se ve del componente: sin la clave de emparejamiento.

        El `title` de un destino es su clave de respuesta, no un rótulo. Mezclarlo con el
        texto visible hace ilegible el listado de parejas.
        """
        partes, pila = [], [cid]
        raiz = True
        while pila:
            actual = pila.pop(0)
            comp = self.C.get(actual) or {}
            for prop in ("textContent", "title", "wording"):
                if raiz and prop == "title" and comp.get("name") == "qTapTapTarget":
                    continue
                valor = (comp.get("properties") or {}).get(prop)
                if isinstance(valor, str):
                    plano = texto_plano(valor)
                    if plano and plano.lower() not in ("title", "fillin title"):
                        partes.append(plano)
            pila = (comp.get("componentChildren") or []) + pila
            raiz = False
        return " ".join(partes).strip()

    def actividad_de(self, cid: str) -> str | None:
        """Sube por el árbol hasta el contenedor de actividad."""
        actual = cid
        visto = set()
        while actual and actual not in visto:
            visto.add(actual)
            nombre = (self.C.get(actual) or {}).get("name", "")
            if nombre.startswith("q") and nombre.endswith("Activity"):
                return actual
            actual = (self.C.get(actual) or {}).get("parent")
        return None

    def hijos_por_tipo(self, raiz: str, tipos: tuple[str, ...]) -> list[str]:
        salida, pila = [], [raiz]
        while pila:
            actual = pila.pop(0)
            comp = self.C.get(actual) or {}
            if comp.get("name") in tipos and actual != raiz:
                salida.append(actual)
            pila = (comp.get("componentChildren") or []) + pila
        return salida

    def emparejamientos(self) -> dict[str, str]:
        """Qué origen empareja con qué destino, por identificador.

        Es la invariante que una traducción nunca debe alterar: cambia el texto de los
        rótulos, pero el par sigue siendo el mismo. Si cambia, es que los rótulos se
        tradujeron en su posición visual sin mirar a quién apuntaban.
        """
        pares: dict[str, str] = {}
        for cid, comp in self.C.items():
            if comp.get("name") != "qTapTapTarget":
                continue
            clave = ((comp.get("properties") or {}).get("title") or "").strip()
            if not clave or clave.isdigit():
                continue
            actividad = self.actividad_de(cid)
            if not actividad:
                continue
            for origen in self.hijos_por_tipo(actividad, TIPOS_ORIGEN):
                if self.texto(origen) == clave:
                    pares[cid] = origen
                    break
        for cid, comp in self.C.items():
            if comp.get("name") in TIPOS_ORIGEN:
                destino = (comp.get("properties") or {}).get("correct")
                if isinstance(destino, str) and destino in self.C:
                    pares[destino] = cid
        return pares

    def problemas(self) -> list[tuple[str, str, str]]:
        """Lista de (gravedad, clave, descripción)."""
        fallos: list[tuple[str, str, str]] = []

        # 1. Destinos cuya clave es un texto que debe coincidir con un origen.
        for cid, comp in self.C.items():
            if comp.get("name") != "qTapTapTarget":
                continue
            clave = ((comp.get("properties") or {}).get("title") or "").strip()
            if not clave or clave.isdigit():
                continue  # clave ordinal: el enlace va por identificador, no por texto
            actividad = self.actividad_de(cid)
            if not actividad:
                continue
            ids_origen = self.hijos_por_tipo(actividad, TIPOS_ORIGEN)
            # Si un origen apunta a este destino por identificador, el `title` es solo un
            # rótulo: «A Prehistoria» empareja así y daba 18 falsos GRAVE.
            if any((self.C[o].get("properties") or {}).get("correct") == cid for o in ids_origen):
                continue
            origenes = {self.texto(o): o for o in ids_origen}
            if clave not in origenes:
                fallos.append((
                    "GRAVE", f"{cid[:8]}.title",
                    f"La clave «{clave}» no coincide con ningún origen de la actividad. "
                    f"Muestra «{self.texto(cid)[:40]}». "
                    f"Orígenes: {', '.join(repr(t[:34]) for t in origenes)}"))

        # 2. Orígenes que apuntan a un destino por identificador.
        for cid, comp in self.C.items():
            if comp.get("name") not in TIPOS_ORIGEN:
                continue
            destino = (comp.get("properties") or {}).get("correct")
            if not isinstance(destino, str) or destino in ("", "true", "false"):
                continue
            if destino not in self.C:
                fallos.append(("GRAVE", f"{cid[:8]}.correct",
                               f"Apunta a un destino inexistente: {destino[:8]}"))

        # 3. Las actividades de emparejar necesitan destino designado en sus orígenes.
        #    Si todos valen "false", la actividad no tiene solución posible: el alumno la
        #    resuelve bien y el sistema se la da por mala, sin ningún indicio en pantalla.
        emparejar: dict[str, list[str]] = {}
        for cid, comp in self.C.items():
            if comp.get("name") in TIPOS_ORIGEN:
                actividad = self.actividad_de(cid)
                if actividad:
                    emparejar.setdefault(actividad, []).append(cid)
        for actividad, origenes in emparejar.items():
            designados = [o for o in origenes
                          if isinstance((self.C[o].get("properties") or {}).get("correct"), str)
                          and (self.C[o]["properties"]["correct"] not in ("", "true", "false"))]
            if not designados:
                fallos.append((
                    "GRAVE", f"{actividad[:8]}",
                    f"Actividad de emparejar sin respuesta definida: los {len(origenes)} "
                    f"orígenes tienen «correct» a false. No se puede acertar. "
                    f"«{self.visible(actividad)[:46]}»"))
            elif len(designados) < len(origenes):
                fallos.append((
                    "AVISO", f"{actividad[:8]}",
                    f"Solo {len(designados)} de {len(origenes)} orígenes tienen destino "
                    f"designado: «{self.visible(actividad)[:46]}»"))

        # 4. Cada grupo de opciones necesita al menos una correcta.
        grupos: dict[str, list[str]] = {}
        for cid, comp in self.C.items():
            if comp.get("name") in TIPOS_OPCION:
                actividad = self.actividad_de(cid)
                if actividad:
                    grupos.setdefault(actividad, []).append(cid)
        for actividad, opciones in grupos.items():
            marcadas = [o for o in opciones
                        if str((self.C[o].get("properties") or {}).get("correct", "")).lower()
                        in ("true", "1")]
            if not marcadas:
                fallos.append(("AVISO", f"{actividad[:8]}",
                               f"Ninguna de las {len(opciones)} opciones está marcada como "
                               f"correcta: «{self.texto(actividad)[:50]}»"))

        # 4 bis. Desplegables: `correct` no es «true» o «false» sino el identificador del
        #    ítem correcto, que tiene que ser uno de los suyos. Contarlos con las opciones
        #    de elección daba «ninguna opción correcta» en todas las actividades de
        #    desplegables.
        for cid, comp in self.C.items():
            if comp.get("name") != "qComboOption":
                continue
            items = comp.get("componentChildren") or []
            correcto = (comp.get("properties") or {}).get("correct") or ""
            if correcto not in items:
                fallos.append(("GRAVE", f"{cid[:8]}.correct",
                               f"Desplegable sin respuesta válida: «correct» no es ninguno de sus "
                               f"{len(items)} ítems. «{self.texto(self.actividad_de(cid) or cid)[:50]}»"))

        # 5. Huecos sin clave.
        for cid, comp in self.C.items():
            if comp.get("name") == "qFillInOption":
                clave = (comp.get("properties") or {}).get("correct")
                if not isinstance(clave, str) or not clave.strip():
                    fallos.append(("GRAVE", f"{cid[:8]}.correct", "Hueco sin respuesta esperada"))

        # 6. Enlaces incoherentes entre rótulo y dirección.
        for cid, comp in self.C.items():
            if comp.get("name") != "resourceLink":
                continue
            props = comp.get("properties") or {}
            rotulo, destino = props.get("title") or "", props.get("href") or ""
            if _MARCADOR.search(rotulo) or _MARCADOR.search(destino):
                continue
            if _DOMINIOS_ORIGEN.search(destino) and not _DOMINIOS_ORIGEN.search(_sin_tildes(rotulo)):
                fallos.append((
                    "GRAVE", f"{cid[:8]}.href",
                    f"El rótulo dice «{rotulo}» pero la dirección apunta al territorio de "
                    f"origen: {destino}"))

        return fallos


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paquete")
    p.add_argument("--original", default=None,
                   help="Paquete de partida, para distinguir lo heredado de lo introducido.")
    p.add_argument("--pares", action="store_true",
                   help="Lista los emparejamientos tal como los va a corregir el sistema.")
    args = p.parse_args()

    auditoria = Auditoria(Paquete.abrir(args.paquete))
    actual = auditoria.problemas()
    heredados = set()

    if args.original:
        previa = Auditoria(Paquete.abrir(args.original))
        heredados = {(g, k) for g, k, _ in previa.problemas()}

        # La comprobación decisiva: los pares no pueden haber cambiado de pareja.
        antes, despues = previa.emparejamientos(), auditoria.emparejamientos()
        for destino, origen_antes in antes.items():
            origen_ahora = despues.get(destino)
            if origen_ahora is None:
                actual.append(("GRAVE", f"{destino[:8]}.title",
                               "El destino ha perdido su pareja: la clave ya no coincide "
                               "con ningún origen de la actividad."))
            elif origen_ahora != origen_antes:
                # No es necesariamente un error: una adaptación que redistribuye el
                # contenido entre los elementos cambia las parejas a propósito. Lo que
                # no puede es pasar inadvertido, así que se marca para revisión y se
                # deja la decisión a quien conoce la materia.
                actual.append((
                    "REVISAR", f"{destino[:8]}.title",
                    f"La pareja ha cambiado. Antes «{previa.visible(destino)[:34]}» iba "
                    f"con «{previa.visible(origen_antes)[:34]}»; ahora "
                    f"«{auditoria.visible(destino)[:34]}» va con "
                    f"«{auditoria.visible(origen_ahora)[:34]}». Confirmar que es correcto."))

    nuevos = [f for f in actual if (f[0], f[1]) not in heredados]
    viejos = [f for f in actual if (f[0], f[1]) in heredados]

    print(f"Problemas detectados: {len(actual)}   introducidos por la adaptación: {len(nuevos)}")
    for etiqueta, lista in (("INTRODUCIDOS POR LA ADAPTACIÓN", nuevos),
                            ("YA PRESENTES EN EL ORIGINAL", viejos)):
        if not lista:
            continue
        print(f"\n=== {etiqueta}")
        for gravedad, clave, descripcion in sorted(lista):
            print(f"  [{gravedad}] {clave}: {descripcion}")

    if args.pares:
        # Que dos rótulos estén bien traducidos no garantiza que formen la pareja
        # correcta: eso depende del contenido, y ninguna comprobación automática puede
        # decidirlo. Lo que sí se puede es enseñar las parejas para que se revisen.
        print("\n=== Emparejamientos, tal como los dará por buenos el sistema")
        pares = auditoria.emparejamientos()
        por_actividad: dict[str, list[tuple[str, str]]] = {}
        for destino, origen in pares.items():
            actividad = auditoria.actividad_de(destino) or "?"
            por_actividad.setdefault(actividad, []).append((origen, destino))
        for actividad, lista in por_actividad.items():
            print(f"\n  «{auditoria.texto(actividad)[:60]}»")
            for origen, destino in lista:
                print(f"     {auditoria.visible(origen)[:38]:<40} ↔  "
                      f"{auditoria.visible(destino)[:38]}")

    graves = [f for f in nuevos if f[0] == "GRAVE"]
    print("\n" + ("RESULTADO: la adaptación no ha roto ninguna actividad."
                  if not graves else
                  f"RESULTADO: {len(graves)} actividad(es) rotas por la adaptación."))
    return 0 if not graves else 1


if __name__ == "__main__":
    raise SystemExit(main())
