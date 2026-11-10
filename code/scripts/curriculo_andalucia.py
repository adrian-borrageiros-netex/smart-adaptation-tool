"""Convierte el currículo LOMLOE de Andalucía (BOJA) a datos estructurados.

Fuentes: las órdenes de 30 de mayo de 2023 (BOJA nº 104, de 2 de junio), que son las que
traen las materias; los decretos 101 y 102/2023 solo ordenan la etapa.

    ESO, PDF 1/2  https://www.juntadeandalucia.es/eboja/2023/104/BOJA23-104-00289-9727-01_00284752.pdf
    ESO, PDF 2/2  https://www.juntadeandalucia.es/eboja/2023/104/BOJA23-104-00246-9727-02_00284752.pdf
    Primaria      https://www.juntadeandalucia.es/eboja/2023/104/BOJA23-104-00208-9731-01_00284747.pdf

Se hace una vez y sin llamar a ningún modelo: cada saber lleva un código con la materia y
el curso (`BYG.1.A.1`; en Primaria el número es el ciclo), y los criterios van en tablas
«competencia · criterios · saberes». Salida: `web/curriculos/andalucia.json`, que es lo que
usa Sarela, y un Excel para revisarlo a mano.

    python scripts/curriculo_andalucia.py <eso_1.pdf> <eso_2.pdf> <primaria.pdf> [salida.json]
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pymupdf

RAIZ = Path(__file__).resolve().parent.parent

MATERIAS_ESO = {
    "BYG": "Biología y Geología", "DIG": "Digitalización", "EAR": "Expresión Artística",
    "ECE": "Economía y Emprendimiento", "EFI": "Educación Física",
    "EPV": "Educación Plástica, Visual y Audiovisual",
    "FOP": "Formación y Orientación Personal y Profesional", "FYQ": "Física y Química",
    "GEH": "Geografía e Historia", "LAT": "Latín", "LCL": "Lengua Castellana y Literatura",
    "LEX": "Lengua Extranjera", "MAT": "Matemáticas", "MAA": "Matemáticas A",
    "MAB": "Matemáticas B", "MUS": "Música", "VCE": "Educación en Valores Cívicos y Éticos",
    "TEC": "Tecnología", "TYD": "Tecnología y Digitalización",
}
MATERIAS_PRIMARIA = {
    "CMN": "Conocimiento del Medio Natural, Social y Cultural", "EAR": "Educación Artística",
    "EFI": "Educación Física", "LCL": "Lengua Castellana y Literatura",
    "LEX": "Lengua Extranjera", "MAT": "Matemáticas",
    "VCE": "Educación en Valores Cívicos y Éticos",
}

_CODIGO = r"[A-Z]{2,4}\.\d\.[A-Z]\.?\d+(?:\.\d+)?"
_SABER = re.compile(rf"^({_CODIGO})\.?\s*(.*)$")
_CODIGOS = re.compile(_CODIGO)
_BLOQUE = re.compile(r"^([A-Z])\.\s+(.{3,})$")
_COMPETENCIA = re.compile(r"^(\d{1,2})\.\s+(.+)$", re.S)
_CRITERIO = re.compile(r"^(\d{1,2}\.\d{1,2}(?:\.[a-z])?)\.?\s+(.+)$", re.S)
_CURSO = re.compile(r"(\d)\s*º")


def limpiar(texto: str) -> str:
    """Una línea de texto de PDF: sin guiones de partición ni espacios sueltos."""
    texto = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", texto)       # «ini-\nciándose»
    texto = re.sub(r"(\w)- (\w)", lambda m: m.group(1) + m.group(2)
                   if m.group(2).islower() else m.group(0), texto)  # «digita- les»
    return re.sub(r"\s+", " ", texto).strip()


def normalizar_codigo(codigo: str) -> str:
    """`CMN.3.C3` → `CMN.3.C.3`: alguna errata del BOJA pega la letra al número."""
    return re.sub(r"\.([A-Z])(\d)", r".\1.\2", codigo.rstrip("."))


# ----------------------------------------------------------------------- saberes

def saberes(doc: pymupdf.Document, paginas: list[int], fuera: dict[int, list]) -> dict:
    """Los saberes por código. `fuera` son las zonas de las tablas de criterios, que
    también citan códigos pero no son saberes."""
    entradas: dict[str, dict] = {}
    bloques_nombre: dict[tuple[str, str, str], str] = {}
    ultima_por_columna: dict[tuple[int, int], str] = {}
    pendiente_bloque: dict[int, tuple[str, str]] = {}
    for n in paginas:
        pagina = doc[n - 1]
        zonas = fuera.get(n, [])
        trozos = [b for b in pagina.get_text("blocks")
                  if 100 < b[1] < 760 and not any(z[0] - 2 <= b[0] and b[2] <= z[2] + 2 and
                                                  z[1] - 2 <= b[1] and b[3] <= z[3] + 2 for z in zonas)]
        for b in sorted(trozos, key=lambda b: (round(b[0] / 60), b[1])):
            columna = round(b[0] / 60)
            for linea in b[4].splitlines():
                linea = linea.strip()
                if not linea:
                    continue
                m = _SABER.match(linea)
                if m:
                    codigo = normalizar_codigo(m.group(1))
                    entradas[codigo] = {"codigo": codigo, "texto": m.group(2)}
                    ultima_por_columna[(n, columna)] = codigo
                    ultima_por_columna[(0, columna)] = codigo
                    if columna in pendiente_bloque:
                        letra, nombre = pendiente_bloque.pop(columna)
                        partes = codigo.split(".")
                        bloques_nombre[(partes[0], partes[1], letra)] = nombre
                    continue
                mb = _BLOQUE.match(linea)
                if mb and len(linea) < 120:
                    pendiente_bloque[columna] = (mb.group(1), limpiar(mb.group(2)).rstrip("."))
                    continue
                anterior = ultima_por_columna.get((0, columna))
                if anterior:
                    entradas[anterior]["texto"] += "\n" + linea
    salida = {}
    for codigo, e in entradas.items():
        partes = codigo.split(".")
        letra = partes[2][0]
        salida[codigo] = {"codigo": codigo, "texto": limpiar(e["texto"]),
                          "bloque": f"{letra}. {bloques_nombre.get((partes[0], partes[1], letra), '')}".strip()}
    return salida


# ---------------------------------------------------------------------- criterios

def _esquema(filas: list[list[str]]) -> list[tuple] | None:
    """Los papeles de las columnas, si estas filas son la cabecera de una tabla de criterios."""
    for i, fila in enumerate(filas[:2]):
        textos = [(c or "").replace("\n", " ") for c in fila]
        if not any("Criterios de evaluación" in t for t in textos):
            continue
        anterior = [(c or "").replace("\n", " ") for c in filas[i - 1]] if i > 0 else [""] * len(fila)
        esquema, cursos_vistos = [], []
        for j, t in enumerate(textos):
            if "Competencias" in t or (j == 0 and not t):
                esquema.append(("comp",))
            elif "Criterios" in t:
                m = _CURSO.search(t) or _CURSO.search(anterior[j] if j < len(anterior) else "") \
                    or next((_CURSO.search(anterior[k]) for k in range(j, -1, -1)
                             if k < len(anterior) and _CURSO.search(anterior[k])), None)
                curso = m.group(1) if m else "?"
                cursos_vistos.append(curso)
                esquema.append(("crit", curso))
            elif "Saberes" in t:
                # Tras el criterio de su curso (ESO) o compartido por los anteriores (Primaria).
                propios = [c for c in cursos_vistos if c not in
                           {x for e in esquema if e[0] == "sab" for x in e[1]}]
                esquema.append(("sab", tuple(propios or cursos_vistos)))
            else:
                esquema.append(("otro",))
        return esquema, i + 1
    return None


def criterios(doc: pymupdf.Document, paginas: list[int]) -> tuple[dict, dict[int, list]]:
    """Competencias y criterios por curso, con los saberes que cita cada criterio."""
    por_curso: dict[str, list] = defaultdict(list)
    esquema = None
    comp_actual: dict[str, dict] = {}
    ultimo_crit: dict[str, dict] = {}
    numero_comp, texto_comp = None, ""
    zonas: dict[int, list] = defaultdict(list)
    for n in paginas:
        for tabla in doc[n - 1].find_tables().tables:
            filas = tabla.extract()
            cabecera = _esquema(filas)
            inicio = 0
            if cabecera:
                esquema, inicio = cabecera
                numero_comp, texto_comp, comp_actual, ultimo_crit = None, "", {}, {}
            if not esquema or tabla.col_count != len(esquema):
                continue
            if filas and _SABER.match((filas[0][0] or "").strip()):
                continue   # una tabla de saberes de Primaria, no de criterios
            zonas[n].append(tabla.bbox)
            for fila in filas[inicio:]:
                celdas = [(c or "").strip() for c in fila]
                if any("Criterios de evaluación" in c or "Competencias específicas" == c for c in celdas):
                    continue
                nuevos: dict[str, dict] = {}
                for papel, celda in zip(esquema, celdas):
                    if not celda:
                        continue
                    if papel[0] == "comp":
                        m = _COMPETENCIA.match(celda)
                        if m:
                            numero_comp, texto_comp = m.group(1), m.group(2)
                            comp_actual = {}
                        else:
                            texto_comp += "\n" + celda
                            for c in comp_actual.values():
                                c["texto"] = texto_comp
                    elif papel[0] == "crit":
                        curso = papel[1]
                        m = _CRITERIO.match(celda)
                        if m:
                            if curso not in comp_actual or comp_actual[curso]["numero"] != numero_comp:
                                comp_actual[curso] = {"numero": numero_comp, "texto": texto_comp,
                                                      "criterios": []}
                                por_curso[curso].append(comp_actual[curso])
                            crit = {"codigo": m.group(1), "texto": m.group(2), "saberes": []}
                            comp_actual[curso]["criterios"].append(crit)
                            ultimo_crit[curso] = nuevos[curso] = crit
                        elif curso in ultimo_crit:
                            ultimo_crit[curso]["texto"] += "\n" + celda
                    elif papel[0] == "sab":
                        codigos = [normalizar_codigo(c) for c in _CODIGOS.findall(celda)]
                        for curso in papel[1]:
                            destino = nuevos.get(curso) or ultimo_crit.get(curso)
                            if destino is not None:
                                destino["saberes"] += [c for c in codigos if c not in destino["saberes"]]
    for comps in por_curso.values():
        for c in comps:
            c["texto"] = limpiar(c["texto"])
            for cr in c["criterios"]:
                cr["texto"] = limpiar(cr["texto"])
    return dict(por_curso), zonas


# --------------------------------------------------------------------------- todo

def paginas_por_prefijo(doc: pymupdf.Document) -> dict[str, list[int]]:
    paginas: dict[str, set[int]] = defaultdict(set)
    for i, pagina in enumerate(doc):
        for m in re.finditer(r"\b([A-Z]{2,4})\.\d\.[A-Z]", pagina.get_text()):
            paginas[m.group(1)].add(i + 1)
    return {k: sorted(v) for k, v in paginas.items()}


def materia(doc: pymupdf.Document, prefijo: str, nombre: str, etapa: str) -> dict | None:
    todas = paginas_por_prefijo(doc).get(prefijo)
    if not todas:
        return None
    # Las páginas con sus códigos, más las de la tabla que siguen sin citarlos.
    paginas = list(range(todas[0], min(todas[-1] + 3, len(doc)) + 1))
    crit, zonas = criterios(doc, paginas)
    sab = {c: s for c, s in saberes(doc, paginas, zonas).items() if c.startswith(prefijo + ".")}
    cursos = {}
    claves = sorted(set(crit) | ({s.split(".")[1] for s in sab} if etapa == "ESO" else set()))
    for curso in claves:
        if curso == "?":
            continue
        ciclo = str((int(curso) + 1) // 2) if etapa == "Primaria" else curso
        cursos[curso] = {
            "competencias": crit.get(curso, []),
            "saberes": [s for c, s in sorted(sab.items()) if c.split(".")[1] == ciclo],
        }
    return {"prefijo": prefijo, "nombre": nombre, "etapa": etapa, "cursos": cursos}


def main() -> None:
    eso1, eso2, primaria = (pymupdf.open(p) for p in sys.argv[1:4])
    salida = Path(sys.argv[4]) if len(sys.argv) > 4 else RAIZ / "web" / "curriculos" / "andalucia.json"
    datos = {
        "comunidad": "Andalucía", "idioma": "es",
        "fuentes": [
            {"etapa": "ESO", "norma": "Orden de 30 de mayo de 2023 (ESO)", "boletin": "BOJA nº 104, de 2/6/2023",
             "url": "https://www.juntadeandalucia.es/boja/2023/104/36"},
            {"etapa": "Primaria", "norma": "Orden de 30 de mayo de 2023 (Primaria)",
             "boletin": "BOJA nº 104, de 2/6/2023", "url": "https://www.juntadeandalucia.es/boja/2023/104/39"},
        ],
        "materias": [],
    }
    for prefijo, nombre in MATERIAS_ESO.items():
        m = materia(eso1, prefijo, nombre, "ESO") or materia(eso2, prefijo, nombre, "ESO")
        if m:
            datos["materias"].append(m)
    for prefijo, nombre in MATERIAS_PRIMARIA.items():
        m = materia(primaria, prefijo, nombre, "Primaria")
        if m:
            datos["materias"].append(m)
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for m in datos["materias"]:
        print(m["etapa"], m["prefijo"], m["nombre"], {c: (len(v["competencias"]),
              sum(len(x["criterios"]) for x in v["competencias"]), len(v["saberes"]))
              for c, v in m["cursos"].items()})


if __name__ == "__main__":
    main()
