"""El currículo de cada comunidad, para que el asistente adapte con lo que pide la norma.

Los currículos se convierten una vez a datos (`scripts/curriculo_andalucia.py`,
`scripts/curriculo_cataluna.py`) y viven en `web/curriculos/<comunidad>.json`. A cada
trabajo solo va el trozo de su materia y su curso, en un texto compacto: el decreto entero
son cientos de páginas; el trozo, unos pocos miles de tokens.

La materia se identifica por un nombre común a todas las comunidades («biologia_geologia»),
porque cada una la llama a su manera («Bioloxía e Xeoloxía», «Biologia i Geologia»,
«Biología y Geología») y Cataluña junta las tres lenguas en una sola sección.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

CARPETA = Path(__file__).resolve().parent / "curriculos"
FICHEROS = {"Andalucía": "andalucia.json", "Cataluña": "cataluna.json"}

# id, nombre para enseñar y pistas para reconocerla en castellano, gallego y catalán. El
# orden importa: «educación física» antes que «física».
MATERIAS = [
    ("educacion_fisica", "Educación Física", ["educacion fisica", "educacion fisica", "educacio fisica"]),
    ("biologia_geologia", "Biología y Geología", ["biolog", "biolox", "xeolox", "geolog"]),
    ("fisica_quimica", "Física y Química", ["fisica", "quimica"]),
    ("geografia_historia", "Geografía e Historia", ["geograf", "xeograf", "histori", "ciencias sociales", "ciencies socials"]),
    ("lengua_extranjera", "Lengua Extranjera", ["lengua extranjera", "lingua estranxeira", "llengua estrangera"]),
    ("lengua", "Lengua", ["lengua", "lingua", "llengua"]),
    ("matematicas", "Matemáticas", ["matem"]),
    ("tecnologia", "Tecnología y Digitalización", ["tecnolog", "tecnolox"]),
    ("digitalizacion", "Digitalización", ["digitaliz"]),
    ("conocimiento_medio", "Conocimiento del Medio", ["conocimiento del medio", "coneixement del medi",
                                                       "ciencias da natureza", "ciencias de la naturaleza",
                                                       "ciencies de la natura", "ciencias sociais", "medio natural"]),
    ("educacion_artistica", "Educación Artística", ["artistic", "plastica", "musica", "musical"]),
    ("valores", "Valores Cívicos y Éticos", ["valores civicos", "valors civics"]),
    ("latin", "Latín", ["latin"]),
]
NOMBRES = {i: n for i, n, _ in MATERIAS}


def _normal(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in texto if unicodedata.category(c) != "Mn").lower()


def materia_de(texto: str) -> str | None:
    """El id común de una materia a partir de cómo la llame cualquier documento."""
    t = _normal(texto)
    if "y digitaliz" in t or "e dixitaliz" in t or "i digitalitz" in t:
        return "tecnologia"
    for ident, _, pistas in MATERIAS:
        if any(p in t for p in pistas):
            return ident
    return None


def deducir(textos: list[str]) -> dict[str, Any]:
    """Etapa, curso y materia de una unidad, a partir de su rótulo de portada («1º ESO ·
    Bioloxía e Xeoloxía · Unidade 3»), su título o su ruta en learningCloud."""
    etapa = curso = materia = None
    for texto in textos:
        t = re.sub(r"<[^>]+>|&nbsp;", " ", texto or "")
        n = _normal(t)
        if etapa is None:
            m = re.search(r"(\d)\s*[ºª°o.]?\s*(?:curso\s+(?:de\s+)?)?(eso|ep\b|primaria|educacion primaria)", n)
            if m:
                etapa = "ESO" if m.group(2) == "eso" else "Primaria"
                curso = int(m.group(1))
            elif re.search(r"\b0([1-9])_?(primaria|eso)\b", n):
                # Ruta de EDIXGAL: «06_primaria» es 6.º de Primaria; «07_ESO», 1.º de ESO.
                m = re.search(r"\b0([1-9])_?(primaria|eso)\b", n)
                numero = int(m.group(1))
                etapa = "ESO" if m.group(2) == "eso" else "Primaria"
                curso = numero - 6 if etapa == "ESO" and numero > 6 else numero
        if materia is None:
            materia = materia_de(t)
    return {"etapa": etapa, "curso": curso, "materia": materia}


@lru_cache(maxsize=8)
def _datos(comunidad: str) -> dict | None:
    fichero = FICHEROS.get(comunidad)
    if not fichero or not (CARPETA / fichero).is_file():
        return None
    return json.loads((CARPETA / fichero).read_text(encoding="utf-8"))


def disponible(comunidad: str | None) -> bool:
    return bool(comunidad) and _datos(comunidad) is not None


def _cubre(clave: str, curso: int) -> bool:
    """«1-3» cubre 1, 2 y 3; «4» solo el 4."""
    a, _, b = clave.partition("-")
    return a.isdigit() and int(a) <= curso <= int(b or a)


def localizar(comunidad: str, etapa: str, materia: str, curso: int) -> dict | None:
    """El bloque del currículo de una materia y un curso. Si la materia no se imparte en ese
    curso en la comunidad (Biología y Geología no hay en 2.º en Andalucía), el más cercano,
    avisando."""
    datos = _datos(comunidad)
    if not datos or not etapa or not materia or not curso:
        return None
    candidatas = [m for m in datos["materias"] if m["etapa"] == etapa and materia_de(m["nombre"]) == materia]
    mejor = None
    for m in candidatas:
        for clave, bloque in m["cursos"].items():
            if not (bloque["competencias"] or bloque["saberes"]):
                continue
            a = int(clave.partition("-")[0])
            b = int(clave.partition("-")[2] or a)
            distancia = 0 if a <= curso <= b else min(abs(curso - a), abs(curso - b))
            # A igual distancia, la que trae criterios.
            puntos = (distancia, 0 if bloque["competencias"] else 1)
            if mejor is None or puntos < mejor[0]:
                mejor = (puntos, m, clave, bloque)
    if mejor is None:
        return None
    (distancia, _), m, clave, bloque = mejor
    fuente = next((f for f in datos["fuentes"] if etapa in f["etapa"]), datos["fuentes"][0])
    return {"comunidad": comunidad, "materia": m["nombre"], "etapa": etapa, "cursos": clave,
            "curso_pedido": curso, "exacto": distancia == 0, "norma": fuente["norma"],
            "boletin": fuente["boletin"], "url": fuente["url"], **bloque}


def _cursos_texto(clave: str, etapa: str) -> str:
    a, _, b = clave.partition("-")
    return f"{a}.º{f' a {b}.º' if b else ''} de {etapa}"


def extracto(bloque: dict) -> str:
    """El trozo del currículo en texto compacto, para el asistente."""
    lineas = [f"{bloque['materia']}, {_cursos_texto(bloque['cursos'], bloque['etapa'])} "
              f"({bloque['norma']}, {bloque['boletin']})."]
    if not bloque["exacto"]:
        lineas.append(f"Atención: en {bloque['comunidad']} esta materia no tiene currículo para "
                      f"{bloque['curso_pedido']}.º; se usa el de {_cursos_texto(bloque['cursos'], bloque['etapa'])}.")
    if bloque["competencias"]:
        lineas.append("\nCompetencias específicas y criterios de evaluación:")
        for c in bloque["competencias"]:
            lineas.append(f"{c['numero']}. {c['texto']}")
            for cr in c["criterios"]:
                vinculos = f" [{', '.join(cr['saberes'])}]" if cr.get("saberes") else ""
                lineas.append(f"  {cr['codigo']}. {cr['texto']}{vinculos}")
    if bloque["saberes"]:
        lineas.append("\nSaberes básicos:")
        anterior = None
        for s in bloque["saberes"]:
            if s.get("bloque") and s["bloque"] != anterior:
                lineas.append(s["bloque"])
                anterior = s["bloque"]
            codigo = s["codigo"] if not s["codigo"].isdigit() else "-"
            lineas.append(f"  {codigo} {s['texto']}")
    return "\n".join(lineas)


def para_trabajo(comunidad: str | None, etapa: str | None, materia: str | None,
                 curso: int | None) -> tuple[str | None, dict | None]:
    """El extracto para un trabajo y su ficha, o (None, None) si no hay currículo."""
    if not comunidad:
        return None, None
    bloque = localizar(comunidad, etapa or "", materia or "", curso or 0)
    return (extracto(bloque), bloque) if bloque else (None, None)


def materias(comunidad: str) -> list[dict]:
    """Lo que hay de una comunidad, para la pantalla del currículo."""
    datos = _datos(comunidad) or {"materias": []}
    return [{"nombre": m["nombre"], "etapa": m["etapa"], "id": materia_de(m["nombre"]),
             "cursos": {c: {"competencias": len(b["competencias"]),
                            "criterios": sum(len(x["criterios"]) for x in b["competencias"]),
                            "saberes": len(b["saberes"])} for c, b in m["cursos"].items()}}
            for m in datos["materias"]]


def excel(comunidad: str) -> bytes:
    """El currículo en una hoja de cálculo, una fila por competencia, criterio y saber, para
    revisarlo a mano."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    datos = _datos(comunidad) or {"materias": [], "fuentes": []}
    libro = Workbook()
    hoja = libro.active
    hoja.title = "Currículo"
    hoja.append(["Etapa", "Materia", "Cursos", "Tipo", "Código", "Bloque o competencia", "Texto",
                 "Saberes vinculados"])
    for celda in hoja[1]:
        celda.font = Font(bold=True)
    for m in datos["materias"]:
        for clave, b in m["cursos"].items():
            for c in b["competencias"]:
                hoja.append([m["etapa"], m["nombre"], clave, "Competencia", c["numero"], "", c["texto"], ""])
                for cr in c["criterios"]:
                    hoja.append([m["etapa"], m["nombre"], clave, "Criterio", cr["codigo"], c["numero"],
                                 cr["texto"], ", ".join(cr.get("saberes") or [])])
            for s in b["saberes"]:
                hoja.append([m["etapa"], m["nombre"], clave, "Saber",
                             "" if s["codigo"].isdigit() else s["codigo"], s.get("bloque", ""), s["texto"], ""])
    for columna, ancho in zip("ABCDEFGH", (9, 32, 8, 12, 14, 34, 100, 30)):
        hoja.column_dimensions[columna].width = ancho
    for fila in hoja.iter_rows(min_row=2):
        fila[6].alignment = Alignment(wrap_text=True, vertical="top")
    hoja.freeze_panes = "A2"
    fuentes = libro.create_sheet("Fuentes")
    fuentes.append(["Etapa", "Norma", "Boletín", "Dirección"])
    for f in datos["fuentes"]:
        fuentes.append([f["etapa"], f["norma"], f["boletin"], f["url"]])
    salida = BytesIO()
    libro.save(salida)
    return salida.getvalue()
