"""La documentación que aporta el experto a un trabajo de adaptación.

El experto trabaja en Sarela con su rol («experto» en Keycloak). Puede descargar la ficha
del trabajo —un Word con lo que hay que adaptar y un hueco para su propuesta en cada
punto—, rellenarla y subirla, o subir cualquier documento suyo (Word, PDF, texto), o pegar
el contenido directamente. De cada documento se guarda:

- el texto, que el asistente recibe como fuente prioritaria al revisar, analizar y proponer;
- las imágenes, que se pueden usar para sustituir las de la unidad;
- los enlaces, para los términos de enlace;
- y, si es la ficha rellena, la propuesta de cada punto, que pasa al término como propuesta
  del experto (el editor la confirma).
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any

_URL = re.compile(r"https?://[^\s<>\"')\]]+")
# La columna de la ficha donde el experto escribe. Al leer una ficha rellena se busca por
# este rótulo, no por la posición.
COLUMNA_PROPUESTA = "Propuesta del experto"
MAX_TEXTO = 60000   # lo que se pasa al asistente por documento
# Los apartados del final de la ficha, donde escribe el experto, con su texto de ayuda.
APARTADOS = {
    "Contenidos y datos para la adaptación": "Datos de la comunidad (lugares, especies, yacimientos, cifras) con su fuente.",
    "Enlaces": "Direcciones de recursos oficiales o educativos de la comunidad.",
    "Imágenes": "Imágenes que propones y qué imagen de la unidad sustituye cada una.",
    "Actividades": "Actividades que propones: enunciado, opciones y solución.",
}


def _docx(contenido: bytes) -> dict[str, Any]:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    doc = Document(BytesIO(contenido))
    propuestas: dict[int, str] = {}
    lineas_propuestas = []
    es_ficha = False
    for tabla in doc.tables:
        filas = [[c.text.strip() for c in fila.cells] for fila in tabla.rows]
        if filas and COLUMNA_PROPUESTA in filas[0] and "Ref." in filas[0]:
            es_ficha = True
            cabecera = filas[0]
            i_ref, i_prop = cabecera.index("Ref."), cabecera.index(COLUMNA_PROPUESTA)
            i_actual = cabecera.index("Texto actual") if "Texto actual" in cabecera else None
            for fila in filas[1:]:
                ref = fila[i_ref].lstrip("T").strip()
                if ref.isdigit() and fila[i_prop].strip():
                    propuestas[int(ref)] = fila[i_prop].strip()
                    actual = fila[i_actual][:120] if i_actual is not None else ""
                    lineas_propuestas.append(f"«{actual}» → {fila[i_prop].strip()}")

    lineas = []
    # De una ficha solo cuenta lo que escribe el experto: sus propuestas y los apartados
    # del final. El resto lo generó Sarela (el texto actual de la unidad, con sus enlaces
    # de origen) y no es aportación suya.
    capturando = not es_ficha
    for p in doc.paragraphs:
        texto = p.text.strip()
        estilo = (p.style.name or "").lower() if p.style is not None else ""
        titulo = estilo.startswith(("heading", "título", "titulo"))
        if es_ficha and titulo:
            capturando = texto in APARTADOS
        if not texto or not capturando or (es_ficha and (texto in APARTADOS.values()
                                                          or texto.startswith("Generada por Sarela"))):
            continue
        lineas.append(("\n## " if titulo else "") + texto)
    if lineas_propuestas:
        lineas = ["## Propuestas del experto para los puntos de la ficha"] + lineas_propuestas + lineas
    for tabla in ([] if es_ficha else doc.tables):
        filas = [[c.text.strip() for c in fila.cells] for fila in tabla.rows]
        for fila in filas:
            # Sin repetir celdas fusionadas, que python-docx devuelve varias veces.
            vistas = []
            for c in fila:
                if c and c not in vistas:
                    vistas.append(c)
            if vistas:
                lineas.append(" | ".join(vistas))
    enlaces = []
    for rel in doc.part.rels.values():
        if rel.reltype == RT.HYPERLINK and rel.is_external and rel._target.startswith("http"):
            enlaces.append(rel._target)
    imagenes = []
    with zipfile.ZipFile(BytesIO(contenido)) as z:
        for nombre in sorted(z.namelist()):
            if nombre.startswith("word/media/") and nombre.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                imagenes.append((nombre.rsplit("/", 1)[-1], z.read(nombre)))
    return {"texto": "\n".join(lineas), "enlaces": enlaces, "imagenes": imagenes,
            "propuestas": propuestas}


def _pdf(contenido: bytes) -> dict[str, Any]:
    import pymupdf
    doc = pymupdf.open(stream=contenido, filetype="pdf")
    texto, enlaces, imagenes, vistas = [], [], [], set()
    for n, pagina in enumerate(doc, 1):
        texto.append(pagina.get_text())
        enlaces += [l["uri"] for l in pagina.get_links() if l.get("uri", "").startswith("http")]
        for img in pagina.get_images(full=True):
            xref = img[0]
            if xref in vistas:
                continue
            vistas.add(xref)
            datos = doc.extract_image(xref)
            if datos and datos.get("width", 0) >= 120 and datos.get("height", 0) >= 120:
                imagenes.append((f"pagina{n}-{xref}.{datos['ext']}", datos["image"]))
    return {"texto": "\n".join(texto), "enlaces": enlaces, "imagenes": imagenes, "propuestas": {}}


def extraer(nombre: str, contenido: bytes) -> dict[str, Any]:
    """Texto, enlaces, imágenes y propuestas de un documento subido."""
    extension = nombre.lower().rsplit(".", 1)[-1] if "." in nombre else ""
    if extension == "docx":
        datos = _docx(contenido)
    elif extension == "pdf":
        datos = _pdf(contenido)
    elif extension in ("txt", "md"):
        datos = {"texto": contenido.decode("utf-8", errors="replace"), "enlaces": [],
                 "imagenes": [], "propuestas": {}}
    elif extension == "doc":
        raise ValueError("Los .doc antiguos no se pueden leer: guárdalo como .docx desde Word.")
    else:
        raise ValueError("Sube un Word (.docx), un PDF o un texto (.txt).")
    datos["texto"] = re.sub(r"\n{3,}", "\n\n", datos["texto"]).strip()
    datos["enlaces"] = list(dict.fromkeys(datos["enlaces"] + _URL.findall(datos["texto"])))
    return datos


def de_texto(texto: str) -> dict[str, Any]:
    """Lo mismo para un contenido pegado en la aplicación."""
    texto = re.sub(r"\n{3,}", "\n\n", texto or "").strip()
    return {"texto": texto, "enlaces": list(dict.fromkeys(_URL.findall(texto))), "imagenes": [],
            "propuestas": {}}


def ficha(trabajo: dict[str, Any], imagenes_diseno: list[dict], plan: dict | None) -> bytes:
    """El Word que rellena el experto: qué hay que adaptar y dónde escribir su propuesta."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = Document()
    estilo = doc.styles["Normal"]
    estilo.font.name, estilo.font.size = "Calibri", Pt(10.5)
    doc.add_heading(f"Ficha para el experto · trabajo {trabajo['id']}", level=0)
    doc.add_paragraph(f"Unidad: {trabajo.get('titulo') or trabajo['paquete']}\n"
                      f"Adaptación a: {trabajo['destino']}")
    if trabajo.get("curriculo"):
        c = trabajo["curriculo"]
        doc.add_paragraph(f"Currículo de destino: {c['materia']}, cursos {c['cursos']} de {c['etapa']} "
                          f"({c['norma']}, {c['boletin']}).")
    instrucciones = doc.add_paragraph()
    instrucciones.add_run("Cómo rellenarla. ").bold = True
    instrucciones.add_run(
        f"Escribe tu propuesta en la columna «{COLUMNA_PROPUESTA}» de cada punto: el texto con el "
        "que quieres sustituir el actual, en el idioma de la unidad. Deja vacío lo que valga tal "
        "cual. En los apartados del final añade los datos, enlaces, imágenes y actividades que "
        "quieras aportar; puedes pegar imágenes en el documento. Después súbela en Sarela, en el "
        "trabajo, apartado «Documentación del experto». No cambies la columna «Ref.».")

    if plan:
        doc.add_heading("Plan de adaptación", level=1)
        doc.add_paragraph(plan.get("resumen", ""))
        for x in plan.get("sustituir", []):
            doc.add_paragraph(f"{x['leccion']}: {x['que']} → {x['propuesta']}", style="List Bullet")
        if plan.get("falta"):
            doc.add_paragraph("Lo que falta del currículo:").runs[0].bold = True
            for x in plan["falta"]:
                doc.add_paragraph(f"{x['saber']}: {x['sugerencia']}", style="List Bullet")

    terminos = [t for t in trabajo.get("terminos", []) if t["estado"] == "pendiente"]
    doc.add_heading(f"Puntos que adaptar ({len(terminos)})", level=1)
    if terminos:
        tabla = doc.add_table(rows=1, cols=5)
        tabla.style = "Table Grid"
        for celda, texto in zip(tabla.rows[0].cells, ("Ref.", "Lección", "Texto actual",
                                                       "Por qué", COLUMNA_PROPUESTA)):
            celda.text = texto
            celda.paragraphs[0].runs[0].bold = True
        for t in terminos:
            fila = tabla.add_row().cells
            fila[0].text = f"T{t['id']}"
            fila[1].text = t.get("leccion") or ""
            fila[2].text = t.get("texto_editable") or t["original"]
            fila[3].text = (t.get("motivo") or "")[:300]
            fila[4].text = ""
            for celda in fila:
                for p in celda.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)
    else:
        doc.add_paragraph("No hay puntos pendientes.")

    if imagenes_diseno:
        doc.add_heading(f"Imágenes que hay que rehacer o sustituir ({len(imagenes_diseno)})", level=1)
        for i in imagenes_diseno:
            doc.add_paragraph(f"{i['fichero'].split('/')[-1]}: {i.get('detalle') or ''}",
                              style="List Bullet")
        doc.add_paragraph("Pega aquí las imágenes que propones, cada una con el nombre de la que sustituye.")

    for apartado, ayuda in APARTADOS.items():
        doc.add_heading(apartado, level=1)
        p = doc.add_paragraph(ayuda)
        p.runs[0].italic = True
        p.runs[0].font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
        doc.add_paragraph("")
    pie = doc.add_paragraph(f"Generada por Sarela · trabajo {trabajo['id']}")
    pie.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    salida = BytesIO()
    doc.save(salida)
    return salida.getvalue()
