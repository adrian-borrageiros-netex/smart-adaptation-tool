"""Memoria del sistema: lo que ya se ha decidido, indexado por contenido.

Hasta ahora las decisiones se guardaban con la clave del componente (`b0d2c256.title`).
Ese identificador es un hash propio de cada paquete, así que el trabajo de una unidad no
servía para la siguiente: cobertura cero en cuanto llegaba contenido nuevo.

Aquí se indexa por el texto. Dos memorias, porque son dos cosas distintas:

- **Frases**: traducciones aprobadas. Se aplican solas. Una frase traducida en la unidad
  de Biología sirve en la de Lengua si el texto coincide.
- **Equivalencias**: adaptaciones territoriales. *No* se aplican solas. Que «gneis ollo de
  sapo» pase a «caliza de montaña» no se deduce de saber gallego, sino de saber geología,
  y equivocarse mete un dato falso en un libro de texto. Se proponen al editor.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable

from . import bd

_ESPACIOS = re.compile(r"\s+")
# El espacio duro y los caracteres invisibles que deja CKEditor no deben cambiar la clave:
# el mismo texto escrito dos veces tiene que encontrarse a sí mismo.
_INVISIBLES = dict.fromkeys(map(ord, " ​‌‍﻿"), " ")


def normalizar(texto: str) -> str:
    """Forma canónica de una frase, para que dos escrituras iguales encuentren la misma entrada."""
    limpio = unicodedata.normalize("NFC", texto or "").translate(_INVISIBLES)
    return _ESPACIOS.sub(" ", limpio).strip()


def huella(texto: str) -> str:
    """Firma corta del texto normalizado: indexable sin arrastrar párrafos enteros."""
    return hashlib.sha256(normalizar(texto).encode("utf-8")).hexdigest()[:40]


# ------------------------------------------------------------------ traducción

def _por_huella(textos: Iterable[str]) -> dict[str, list[str]]:
    """Cada huella con **todas** las formas exactas del texto que la producen.

    Una misma frase aparece a veces con un espacio al final y a veces sin él, según el
    nodo HTML donde caiga. Las dos comparten huella y entrada de memoria. Si el resultado
    se indexa por una sola forma, la otra se queda sin traducción, y el párrafo que la
    contiene sale entero en el idioma original porque la aplicación es todo o nada.
    """
    grupos: dict[str, list[str]] = {}
    for t in textos:
        if normalizar(t):
            formas = grupos.setdefault(huella(t), [])
            if t not in formas:
                formas.append(t)
    return grupos

def buscar_frases(idioma_origen: str, idioma_destino: str,
                  textos: Iterable[str]) -> dict[str, str]:
    """Traducciones conocidas para un conjunto de frases, por cada forma exacta recibida.

    Las invariantes no salen aquí: aplicarlas no cambiaría nada y contarlas como
    adaptadas inflaría la cobertura con texto que nadie ha traducido.
    """
    pendientes = _por_huella(textos)
    if not pendientes:
        return {}

    encontradas: dict[str, str] = {}
    with bd.Sesion() as s:
        # En lotes, para no construir una consulta con miles de parámetros.
        claves = list(pendientes)
        for i in range(0, len(claves), 400):
            filas = (s.query(bd.Frase)
                     .filter(bd.Frase.idioma_origen == idioma_origen,
                             bd.Frase.idioma_destino == idioma_destino,
                             bd.Frase.invariante.is_(False),
                             bd.Frase.huella.in_(claves[i:i + 400]))
                     .all())
            for fila in filas:
                for forma in pendientes[fila.huella]:
                    encontradas[forma] = fila.destino
    return encontradas


def frases_invariantes(idioma_origen: str, idioma_destino: str,
                       textos: Iterable[str]) -> set[str]:
    """Frases ya revisadas que se quedan como están. Para no volver a preguntarlas."""
    pendientes = _por_huella(textos)
    if not pendientes:
        return set()

    salida: set[str] = set()
    with bd.Sesion() as s:
        claves = list(pendientes)
        for i in range(0, len(claves), 400):
            filas = (s.query(bd.Frase.huella)
                     .filter(bd.Frase.idioma_origen == idioma_origen,
                             bd.Frase.idioma_destino == idioma_destino,
                             bd.Frase.invariante.is_(True),
                             bd.Frase.huella.in_(claves[i:i + 400]))
                     .all())
            for f in filas:
                salida.update(pendientes[f.huella])
    return salida


def marcar_invariante(idioma_origen: str, idioma_destino: str, texto: str,
                      procedencia: str | None = None,
                      aprobada_por: str | None = None, revisada: bool = True) -> bool:
    """Deja constancia de que una frase se revisó y no necesita traducción."""
    texto_n = normalizar(texto)
    if not texto_n:
        return False

    clave = huella(texto_n)
    with bd.Sesion() as s:
        fila = (s.query(bd.Frase)
                .filter_by(idioma_origen=idioma_origen, idioma_destino=idioma_destino,
                           huella=clave).one_or_none())
        if fila is None:
            s.add(bd.Frase(idioma_origen=idioma_origen, idioma_destino=idioma_destino,
                           huella=clave, origen=texto_n, destino=texto_n, invariante=True,
                           procedencia=procedencia, aprobada_por=aprobada_por,
                           revisada=revisada))
            s.commit()
            return True
        # Una traducción de verdad manda sobre la marca: si alguien la tradujo después,
        # es que sí había que tocarla.
        return False


def guardar_frase(idioma_origen: str, idioma_destino: str, origen: str, destino: str,
                  procedencia: str | None = None, aprobada_por: str | None = None,
                  revisada: bool = True) -> bool:
    """Registra una traducción. Devuelve si era nueva.

    Si ya existía con otro destino se actualiza: la última decisión del editor manda sobre
    la anterior, que es lo que espera quien acaba de corregir algo.
    """
    origen_n, destino_n = normalizar(origen), normalizar(destino)
    if not origen_n or not destino_n or origen_n == destino_n:
        return False

    clave = huella(origen_n)
    with bd.Sesion() as s:
        fila = (s.query(bd.Frase)
                .filter_by(idioma_origen=idioma_origen, idioma_destino=idioma_destino,
                           huella=clave).one_or_none())
        if fila is None:
            s.add(bd.Frase(idioma_origen=idioma_origen, idioma_destino=idioma_destino,
                           huella=clave, origen=origen_n, destino=destino_n,
                           procedencia=procedencia, aprobada_por=aprobada_por,
                           revisada=revisada))
            s.commit()
            return True
        if fila.revisada and not revisada:
            # Lo que ha aprobado una persona no lo cambia una traducción automática.
            return False
        if fila.destino != destino_n or fila.invariante or fila.revisada != revisada:
            fila.revisada = revisada
            fila.destino = destino_n
            fila.invariante = False
            fila.aprobada_por = aprobada_por or fila.aprobada_por
            s.commit()
        return False


def anotar_uso(idioma_origen: str, idioma_destino: str, textos: Iterable[str]) -> None:
    """Suma un uso a las frases aplicadas. Sirve para saber qué memoria está viva."""
    claves = [huella(t) for t in textos if normalizar(t)]
    if not claves:
        return
    with bd.Sesion() as s:
        for i in range(0, len(claves), 400):
            (s.query(bd.Frase)
             .filter(bd.Frase.idioma_origen == idioma_origen,
                     bd.Frase.idioma_destino == idioma_destino,
                     bd.Frase.huella.in_(claves[i:i + 400]))
             .update({bd.Frase.veces_usada: bd.Frase.veces_usada + 1},
                     synchronize_session=False))
        s.commit()


def frases_sin_revisar(idioma_origen: str, idioma_destino: str,
                       textos: Iterable[str], incluir_revisadas: bool = False) -> list[dict[str, Any]]:
    """Traducciones de estas frases: las que nadie ha confirmado, o todas.

    Con `incluir_revisadas` salen también las aprobadas, para poder corregirlas después:
    aprobar no puede ser un punto sin retorno.
    """
    orden = {huella(t): i for i, t in enumerate(textos) if normalizar(t)}
    if not orden:
        return []
    salida = []
    with bd.Sesion() as s:
        claves = list(orden)
        for i in range(0, len(claves), 400):
            for f in (s.query(bd.Frase)
                      .filter(bd.Frase.idioma_origen == idioma_origen,
                              bd.Frase.idioma_destino == idioma_destino,
                              bd.Frase.revisada.in_([False] if not incluir_revisadas else [False, True]),
                              bd.Frase.invariante.is_(False),
                              bd.Frase.huella.in_(claves[i:i + 400])).all()):
                salida.append({"id": f.id, "origen": f.origen, "destino": f.destino,
                               "revisada": f.revisada,
                               "aprobada_por": f.aprobada_por, "procedencia": f.procedencia,
                               "orden": orden[f.huella]})
    return sorted(salida, key=lambda x: x["orden"])


def aprobar_frase(frase_id: int, destino: str | None = None,
                  aprobada_por: str | None = None) -> dict[str, Any]:
    """Una persona da por buena una traducción, corregida o tal cual."""
    with bd.Sesion() as s:
        f = s.get(bd.Frase, frase_id)
        if f is None:
            raise KeyError(frase_id)
        if destino is not None and normalizar(destino):
            f.destino = normalizar(destino)
        f.revisada = True
        f.aprobada_por = aprobada_por or "editor"
        s.commit()
        return {"id": f.id, "origen": f.origen, "destino": f.destino}


def devolver_a_revision(procedencia: str) -> int:
    """Vuelve a marcar como por revisar lo que tradujo el asistente en un trabajo.

    Para deshacer un «aprobar todas» dado sin querer. Solo toca las traducciones cuyo
    origen es ese trabajo del asistente; las que corrigió o aprobó una persona en otras
    unidades no cambian.
    """
    with bd.Sesion() as s:
        filas = (s.query(bd.Frase)
                 .filter(bd.Frase.procedencia.like(f"{procedencia}%"),
                         bd.Frase.invariante.is_(False), bd.Frase.revisada.is_(True)).all())
        for f in filas:
            f.revisada = False
        s.commit()
        return len(filas)


def glosario(idioma_origen: str, idioma_destino: str, limite: int = 150,
             largo_max: int = 60) -> list[tuple[str, str]]:
    """Terminología ya aprobada, para que el asistente la respete en unidades nuevas.

    Solo frases cortas —términos y rótulos, no párrafos— y solo las que ha confirmado una
    persona: si entraran las automáticas, un error se propagaría de unidad en unidad.
    Ordenadas de forma estable para que la parte fija del prompt no cambie entre lotes y
    se pueda leer de la caché.
    """
    with bd.Sesion() as s:
        filas = (s.query(bd.Frase.origen, bd.Frase.destino)
                 .filter(bd.Frase.idioma_origen == idioma_origen,
                         bd.Frase.idioma_destino == idioma_destino,
                         bd.Frase.revisada.is_(True),
                         bd.Frase.invariante.is_(False),
                         bd.func.length(bd.Frase.origen) <= largo_max)
                 .order_by(bd.Frase.veces_usada.desc(), bd.Frase.id).limit(limite).all())
    return sorted((o, d) for o, d in filas)


# -------------------------------------------------------- adaptación territorial

def buscar_equivalencias(textos: Iterable[str], destino_territorio: str | None = None,
                         solo_aprobadas: bool = True) -> dict[str, dict[str, Any]]:
    """Equivalencias conocidas. Se devuelven para *proponer*, nunca para aplicar solas."""
    pendientes = _por_huella(textos)
    if not pendientes:
        return {}

    salida: dict[str, dict[str, Any]] = {}
    with bd.Sesion() as s:
        claves = list(pendientes)
        for i in range(0, len(claves), 400):
            consulta = s.query(bd.Equivalencia).filter(
                bd.Equivalencia.huella.in_(claves[i:i + 400]))
            if destino_territorio:
                consulta = consulta.filter(
                    bd.Equivalencia.destino_territorio == destino_territorio)
            if solo_aprobadas:
                consulta = consulta.filter(bd.Equivalencia.aprobada.is_(True))
            for fila in consulta.all():
                datos = {
                    "propuesta": fila.propuesta, "motivo": fila.motivo,
                    "destino_territorio": fila.destino_territorio,
                    "aprobada": fila.aprobada}
                for forma in pendientes[fila.huella]:
                    salida[forma] = datos
    return salida


def guardar_equivalencia(concepto: str, propuesta: str, origen_territorio: str | None,
                         destino_territorio: str | None, motivo: str | None = None,
                         aprobada: bool = False, procedencia: str | None = None) -> bool:
    concepto_n, propuesta_n = normalizar(concepto), normalizar(propuesta)
    if not concepto_n or not propuesta_n or concepto_n == propuesta_n:
        return False

    clave = huella(concepto_n)
    with bd.Sesion() as s:
        fila = (s.query(bd.Equivalencia)
                .filter_by(huella=clave, origen_territorio=origen_territorio,
                           destino_territorio=destino_territorio).one_or_none())
        if fila is None:
            s.add(bd.Equivalencia(
                huella=clave, concepto=concepto_n, propuesta=propuesta_n,
                origen_territorio=origen_territorio, destino_territorio=destino_territorio,
                motivo=motivo, aprobada=aprobada, procedencia=procedencia))
            s.commit()
            return True
        fila.propuesta = propuesta_n
        fila.aprobada = aprobada or fila.aprobada
        fila.motivo = motivo or fila.motivo
        s.commit()
        return False


# ------------------------------------------------------------------- inventario

def resumen() -> dict[str, Any]:
    """Qué sabe el sistema ahora mismo. Lo consulta la interfaz y también el MCP."""
    with bd.Sesion() as s:
        pares: dict[str, int] = {}
        for origen, destino, total in (
                s.query(bd.Frase.idioma_origen, bd.Frase.idioma_destino,
                        bd.func.count(bd.Frase.id))
                 .filter(bd.Frase.invariante.is_(False))
                 .group_by(bd.Frase.idioma_origen, bd.Frase.idioma_destino).all()):
            pares[f"{origen}→{destino}"] = total

        territorios: dict[str, int] = {}
        for destino, total in (
                s.query(bd.Equivalencia.destino_territorio,
                        bd.func.count(bd.Equivalencia.id))
                 .group_by(bd.Equivalencia.destino_territorio).all()):
            territorios[destino or "(sin territorio)"] = total

        return {
            "frases": s.query(bd.Frase).filter(bd.Frase.invariante.is_(False)).count(),
            "frases_por_revisar": s.query(bd.Frase).filter(bd.Frase.invariante.is_(False),
                                                           bd.Frase.revisada.is_(False)).count(),
            "invariantes": s.query(bd.Frase).filter(bd.Frase.invariante.is_(True)).count(),
            "frases_por_par": pares,
            "equivalencias": s.query(bd.Equivalencia).count(),
            "equivalencias_aprobadas": s.query(bd.Equivalencia)
                                        .filter(bd.Equivalencia.aprobada.is_(True)).count(),
            "equivalencias_por_territorio": territorios,
        }


def buscar(texto: str, limite: int = 25) -> dict[str, list[dict[str, Any]]]:
    """Búsqueda libre en las dos memorias, para revisar qué hay guardado."""
    patron = f"%{normalizar(texto)}%"
    with bd.Sesion() as s:
        frases = (s.query(bd.Frase)
                  .filter(bd.Frase.origen.ilike(patron) | bd.Frase.destino.ilike(patron))
                  .order_by(bd.Frase.veces_usada.desc()).limit(limite).all())
        equivalencias = (s.query(bd.Equivalencia)
                         .filter(bd.Equivalencia.concepto.ilike(patron)
                                 | bd.Equivalencia.propuesta.ilike(patron))
                         .limit(limite).all())
        return {
            "frases": [{"origen": f.origen, "destino": f.destino,
                        "par": f"{f.idioma_origen}→{f.idioma_destino}",
                        "veces_usada": f.veces_usada, "procedencia": f.procedencia}
                       for f in frases],
            "equivalencias": [{"concepto": e.concepto, "propuesta": e.propuesta,
                               "territorio": e.destino_territorio, "aprobada": e.aprobada,
                               "motivo": e.motivo} for e in equivalencias],
        }
