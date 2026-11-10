"""Convierte el currículo LOMLOE de Cataluña (DOGC) a datos estructurados.

Fuente: Decret 175/2022, de 27 de setembre, d'ordenació dels ensenyaments de l'educació
bàsica (DOGC núm. 8762, de 29/09/2022). Trae él mismo las áreas de Primaria (anexo 2) y las
materias de ESO (anexo 3). Se lee la versión oficial en castellano, porque la catalana sale
con texto corrupto en algunas páginas; el asistente escribe en catalán de todas formas.

    https://portaldogc.gencat.cat/utilsEADOP/PDF/8762/1928586.pdf

A diferencia de Andalucía, va por bloques de cursos («1.º, 2.º y 3.er curso» / «4.º curso»,
o por ciclos en Primaria) y los saberes no llevan código: son viñetas bajo un encabezado de
bloque. Las tres lenguas (castellana, catalana y aranés) comparten sección.
La modificación del Decret 480/2024 no toca ninguna de las materias que se usan.

    python scripts/curriculo_cataluna.py <decret175_es.pdf> [salida.json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pymupdf

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))
from curriculo_andalucia import limpiar  # noqa: E402

# Primera y última página (del PDF castellano) de cada materia.
PRIMARIA = {
    "Lenguas (Castellana, Catalana y Aranés)": (49, 69),
    "Lengua Extranjera": (70, 92),
    "Conocimiento del Medio Natural, Social y Cultural": (105, 128),
    "Educación Artística": (129, 145),
    "Educación en Valores Cívicos y Éticos": (146, 154),
    "Educación Física": (155, 177),
    "Matemáticas": (178, 199),
}
ESO = {
    "Lenguas (Castellana, Catalana y Aranés)": (203, 221),
    "Lengua Extranjera": (222, 239),
    "Biología y Geología": (258, 273),
    "Ciencias Sociales: Geografía e Historia": (274, 297),
    "Digitalización": (306, 314),
    "Educación en Valores Cívicos y Éticos": (324, 334),
    "Educación Física": (335, 349),
    "Física y Química": (389, 404),
    "Latín: Lengua y Cultura": (414, 425),
    "Matemáticas": (426, 446),
    "Música": (447, 454),
    "Tecnología": (460, 468),
    "Tecnología y Digitalización": (469, 478),
}
CURSOS_ETAPA = {"Primaria": [1, 2, 3, 4, 5, 6], "ESO": [1, 2, 3, 4]}

_ORDINALES = {"primer": 1, "primero": 1, "segundo": 2, "tercer": 3, "tercero": 3, "cuarto": 4,
              "quinto": 5, "sexto": 6}
_COMPETENCIA = re.compile(r"^Competencia específica (\d+)")
_CRITERIO = re.compile(r"^(\d{1,2}\.\d{1,2})\.?\s+(.+)$", re.S)
_IZQUIERDA, _DERECHA = 100, 485


def cursos_de(texto: str) -> list[int]:
    """«1.er, 2.º y 3.er curso» → [1, 2, 3]; «Cuarto curso» → [4]."""
    numeros = [int(n) for n in re.findall(r"(\d)\s*\.?\s*(?:º|er|o)", texto)]
    if not numeros:
        numeros = [_ORDINALES[p] for p in re.findall(r"[a-záéíóú]+", texto.lower()) if p in _ORDINALES]
    return sorted(set(numeros))


def encabezado_cursos(texto: str) -> list[int]:
    """Los cursos de un encabezado de saberes: «Primero y segundo», «Cuarto curso», «Tercer
    ciclo» (en Primaria, el ciclo n son los cursos 2n-1 y 2n). Vacío si no lo es."""
    palabras = re.findall(r"[a-záéíóú]+", texto.lower())
    if not palabras or len(palabras) > 6:
        return []
    if palabras[-1] == "ciclo" and palabras[0] in _ORDINALES:
        n = _ORDINALES[palabras[0]]
        return [2 * n - 1, 2 * n]
    if all(p in _ORDINALES or p in ("y", "a", "de", "curso", "cursos", "er", "o") for p in palabras):
        numeros = sorted({_ORDINALES[p] for p in palabras if p in _ORDINALES})
        if "a" in palabras and len(numeros) == 2:   # «Primero a tercero»
            numeros = list(range(numeros[0], numeros[1] + 1))
        return numeros
    return []


def clave(cursos: list[int]) -> str:
    return str(cursos[0]) if len(cursos) == 1 else f"{cursos[0]}-{cursos[-1]}"


def _bloques(doc: pymupdf.Document, desde: int, hasta: int):
    for n in range(desde, hasta + 1):
        for b in sorted(doc[n - 1].get_text("blocks"), key=lambda b: (round(b[1]), b[0])):
            x0, y0, x1, y1, texto = b[:5]
            if y0 < 95 or y0 > 730 or not texto.strip():
                continue   # cabecera y pie del DOGC
            yield n, x0, x1, texto.strip()


def materia(doc: pymupdf.Document, nombre: str, etapa: str, desde: int, hasta: int) -> dict:
    competencias: list[dict] = []
    saberes: dict[str, list[dict]] = {}
    modo, columnas, comp, ultimo = None, [], None, {}
    cursos_saber, bloque, apartado = None, "", ""
    for n, x0, x1, texto in _bloques(doc, desde, hasta):
        linea = texto.split("\n")[0].strip()
        m = _COMPETENCIA.match(texto)
        if m:
            comp = {"numero": m.group(1), "texto": "", "criterios": {}}
            competencias.append(comp)
            modo, columnas, ultimo = "enunciado", [], {}
            continue
        if texto == "Saberes":
            modo, cursos_saber, bloque, apartado = "saberes", None, "", ""
            continue
        if modo == "enunciado":
            if texto.startswith("Criterios de evaluación"):
                modo = "criterios"
            else:
                comp["texto"] += " " + texto
            continue
        if modo == "criterios":
            if re.search(r"(curso|ciclo)\s*$", linea) and len(texto) < 120 and not _CRITERIO.match(texto):
                columnas = [cursos_de(l) for l in texto.split("\n") if l.strip()]
                continue
            k = max(1, len(columnas))
            ancho_col = (_DERECHA - _IZQUIERDA) / k
            col = min(k - 1, max(0, int((x0 - _IZQUIERDA) / ancho_col)))
            estrecho = (x1 - x0) < ancho_col * 1.2 or k == 1
            mc = _CRITERIO.match(texto)
            cursos = columnas[col] if columnas else CURSOS_ETAPA[etapa]
            if mc and estrecho:
                crit = {"codigo": mc.group(1), "texto": mc.group(2)}
                comp["criterios"].setdefault(clave(cursos), []).append(crit)
                ultimo[col] = crit
            elif estrecho and col in ultimo and (k > 1 or texto[:1].islower()):
                ultimo[col]["texto"] += " " + texto     # sigue en la página siguiente
            else:
                modo = "explicacion"                   # el texto que comenta la competencia
            continue
        if modo == "saberes":
            if texto.startswith("-"):
                if cursos_saber is None:
                    cursos_saber = CURSOS_ETAPA[etapa]
                for item in re.split(r"\n\s*-\s*\n?", "\n" + texto.lstrip("-").lstrip()):
                    if item.strip():
                        saberes.setdefault(clave(cursos_saber), []).append(
                            {"bloque": " · ".join(filter(None, [bloque, apartado])),
                             "texto": limpiar(item)})
            elif texto.startswith("●"):
                apartado = limpiar(texto.lstrip("●").strip()).rstrip(".")
            elif len(texto) < 110 and "\n" not in texto.strip():
                cursos = encabezado_cursos(texto)
                if cursos:
                    cursos_saber, bloque, apartado = cursos, "", ""
                else:
                    bloque, apartado = limpiar(texto), ""
            elif saberes and texto[:1].islower():
                ultimo_saber = saberes[clave(cursos_saber)][-1]
                ultimo_saber["texto"] = limpiar(ultimo_saber["texto"] + " " + texto)
    cursos: dict[str, dict] = {}
    for c in competencias:
        for k, crits in c["criterios"].items():
            cursos.setdefault(k, {"competencias": [], "saberes": []})["competencias"].append(
                {"numero": c["numero"], "texto": limpiar(c["texto"]),
                 "criterios": [{"codigo": x["codigo"], "texto": limpiar(x["texto"]), "saberes": []}
                               for x in crits]})
    for k, lista in saberes.items():
        cursos.setdefault(k, {"competencias": [], "saberes": []})["saberes"] = [
            {"codigo": f"{i + 1}", **s} for i, s in enumerate(lista)]
    return {"prefijo": None, "nombre": nombre, "etapa": etapa, "cursos": cursos}


def main() -> None:
    doc = pymupdf.open(sys.argv[1])
    salida = Path(sys.argv[2]) if len(sys.argv) > 2 else RAIZ / "web" / "curriculos" / "cataluna.json"
    datos = {
        "comunidad": "Cataluña", "idioma": "es",
        "fuentes": [{"etapa": "Primaria y ESO",
                     "norma": "Decret 175/2022, de 27 de setembre, d'ordenació dels ensenyaments de l'educació bàsica",
                     "boletin": "DOGC núm. 8762, de 29/9/2022 (versión en castellano)",
                     "url": "https://portaldogc.gencat.cat/utilsEADOP/PDF/8762/1928586.pdf"}],
        "materias": [],
    }
    for etapa, tabla in (("Primaria", PRIMARIA), ("ESO", ESO)):
        for nombre, (desde, hasta) in tabla.items():
            m = materia(doc, nombre, etapa, desde, hasta)
            datos["materias"].append(m)
            print(etapa, nombre, {c: (len(v["competencias"]),
                  sum(len(x["criterios"]) for x in v["competencias"]), len(v["saberes"]))
                  for c, v in sorted(m["cursos"].items())})
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
