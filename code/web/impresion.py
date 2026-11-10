"""La versión imprimible de una unidad: se regenera desde la propia unidad, no se traduce.

La plantilla Berlin trae un modo de impresión. Abierta como
`index.html?print=printVersionTemplate573`, pinta en una sola página las lecciones con
`printable`, quita las actividades no imprimibles, añade las ventanas emergentes
imprimibles —el glosario— y al terminar pone `window.status = "ready_to_print"`. Es lo que
Maker abría con wkhtmltopdf para generar el PDF que enlaza el componente `pdfLink`.

Así que regenerarla es imprimir la unidad ya traducida en ese mismo modo: sale del
contenido revisado, con el diseño de la plantilla, y si mañana se corrige una frase basta
con volver a imprimir. Se hace con Chromium, porque wkhtmltopdf ya no está en Debian.

Tres ajustes, medidos contra el PDF de Maker de «A orixe da vida»:

- **Estilos de pantalla**, no de impresión. wkhtmltopdf imprime por defecto con los de
  pantalla, y el CSS de la plantilla define para pantalla las columnas y los titulares.
  Con los de impresión los titulares salían a la mitad de tamaño.
- **Escala 1,28**, para que la letra de cuerpo salga a 11,5 pt como en Maker.
- **Liberar lo que recorta.** El glosario va en una ventana de altura fija (720 px para
  1.214 de contenido) y se imprimía cortado. Cualquier contenedor que recorte dentro de la
  vista de impresión se abre antes de imprimir.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import threading
import time
from pathlib import Path

CHROMIUM = os.environ.get("SARELA_CHROMIUM", "/usr/bin/chromium")
ESCALA = float(os.environ.get("SARELA_IMPRESION_ESCALA", "1.28"))
MODO = "printVersionTemplate573"

_PIE = ('<div style="width:100%;text-align:center;font-size:9px;color:#666;'
        "font-family:'Noto Sans',sans-serif\">— <span class=\"pageNumber\"></span> —</div>")

_LIBERAR = """() => { let n = 0;
  document.querySelectorAll('#print-view *').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.overflowY !== 'visible' && el.scrollHeight > el.clientHeight + 4) {
      for (const [p, v] of [['overflow', 'visible'], ['height', 'auto'], ['max-height', 'none']])
        el.style.setProperty(p, v, 'important');
      n++;
    }
  }); return n; }"""


class ErrorImpresion(RuntimeError):
    """No se ha podido imprimir la unidad. El mensaje es para el informe."""


def localizar(unidad: Path) -> Path | None:
    """El PDF de la versión imprimible de una unidad, si la tiene.

    Es el recurso del componente `pdfLink`. Una unidad sin ese componente no tiene versión
    imprimible y no hay nada que regenerar.
    """
    ruta = unidad / "components.json"
    if not ruta.exists():
        return None
    for comp in json.loads(ruta.read_text(encoding="utf-8")).values():
        if comp.get("componentName") != "pdfLink":
            continue
        # El fichero va en `properties.name`, sin ruta: «version_imprimible.pdf».
        nombre = Path(str((comp.get("properties") or {}).get("name") or "")).name
        if nombre.lower().endswith(".pdf") and (unidad / "resources" / nombre).exists():
            return unidad / "resources" / nombre
    return None


def imprimir(unidad: Path, salida: Path, escala: float = ESCALA) -> dict:
    """Imprime la unidad en el modo de impresión de la plantilla. Devuelve lo que ha hecho."""
    try:
        from playwright.sync_api import Error as ErrorPlaywright
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ErrorImpresion("Falta el motor de impresión (playwright) en este servidor.") from e

    class _Silencioso(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

    servidor = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Silencioso, directory=str(unidad)))
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{servidor.server_port}/index.html?print={MODO}"

    t0 = time.time()
    try:
        with sync_playwright() as p:
            opciones = {"args": ["--no-sandbox"]}
            if Path(CHROMIUM).exists():
                opciones["executable_path"] = CHROMIUM
            else:
                opciones["channel"] = "msedge"  # en un equipo Windows de desarrollo
            navegador = p.chromium.launch(**opciones)
            try:
                pagina = navegador.new_page()
                pagina.goto(url, wait_until="load", timeout=120_000)
                pagina.wait_for_function("window.status === 'ready_to_print'", timeout=120_000)
                pagina.wait_for_load_state("networkidle", timeout=60_000)
                pagina.emulate_media(media="screen")
                liberados = pagina.evaluate(_LIBERAR)
                salida.parent.mkdir(parents=True, exist_ok=True)
                temporal = salida.with_suffix(".imprimiendo.pdf")
                pagina.pdf(path=str(temporal), format="A4", print_background=True, scale=escala,
                           display_header_footer=True, header_template="<div></div>",
                           footer_template=_PIE,
                           margin={"top": "5mm", "bottom": "14mm", "left": "5mm", "right": "5mm"})
                temporal.replace(salida)
            finally:
                navegador.close()
    except ErrorPlaywright as e:
        raise ErrorImpresion(f"El navegador no ha podido imprimir la unidad: {e}") from e
    finally:
        servidor.shutdown()
    return {"segundos": round(time.time() - t0, 1), "contenedores_liberados": liberados}


# ------------------------------------------------------------------- comprobación

def _texto(ruta: Path) -> tuple[str, int, int]:
    import pymupdf
    d = pymupdf.open(ruta)
    texto = " ".join(p.get_text() for p in d)
    texto = re.sub(r"—\s*\d+\s*—", " ", texto)
    imagenes = sum(len(p.get_images()) for p in d)
    return re.sub(r"\s+", " ", texto), d.page_count, imagenes


def _frases(texto: str) -> list[str]:
    return [f.strip() for f in re.split(r"(?<=[.!?])\s+", texto) if len(f.split()) >= 6]


def comprobar(anterior: Path, nuevo: Path, cambia_idioma: bool) -> list[tuple[str, str, str]]:
    """Avisos sobre la versión regenerada, comparada con la que traía la unidad."""
    avisos = []
    t_ant, p_ant, i_ant = _texto(anterior)
    t_nue, p_nue, i_nue = _texto(nuevo)

    if cambia_idioma:
        # Frases largas del original que siguen igual en la nueva: texto sin traducir.
        restos = [f for f in _frases(t_ant) if f[:60] in t_nue]
        if len(restos) > 3:
            avisos.append(("GRAVE", "version_imprimible",
                           f"Quedan {len(restos)} frases en el idioma original en la versión "
                           f"imprimible, por ejemplo: «{restos[0][:120]}»."))
    if i_nue < i_ant:
        avisos.append(("AVISO", "version_imprimible",
                       f"La versión regenerada tiene menos imágenes que la original "
                       f"({i_nue} frente a {i_ant}): puede faltar contenido."))
    if not 0.7 <= p_nue / max(p_ant, 1) <= 1.5:
        avisos.append(("AVISO", "version_imprimible",
                       f"La versión regenerada tiene {p_nue} páginas y la original {p_ant}: "
                       "conviene mirarla."))
    return avisos


def regenerar(unidad: Path, original: Path, cambia_idioma: bool) -> tuple[list, dict | None]:
    """Regenera la versión imprimible de una unidad adaptada, si tiene.

    Devuelve los avisos para el informe y un resumen de lo hecho. Si no se puede
    regenerar, la unidad conserva la del original, y si además cambió de idioma eso es
    grave: el paquete llevaría la unidad traducida y su versión para imprimir sin traducir.
    """
    pdf = localizar(unidad)
    if pdf is None:
        return [], None
    pdf_original = localizar(original) or pdf
    try:
        hecho = imprimir(unidad, pdf)
    except ErrorImpresion as e:
        nivel = "GRAVE" if cambia_idioma else "AVISO"
        return [(nivel, "version_imprimible",
                 f"No se ha regenerado la versión imprimible ({e}). La unidad lleva la del "
                 "original" + (", en el idioma de origen." if cambia_idioma else "."))], None
    avisos = comprobar(pdf_original, pdf, cambia_idioma)
    _, paginas, imagenes = _texto(pdf)
    _, paginas_ant, _ = _texto(pdf_original)
    return avisos, {"fichero": pdf.name, "paginas": paginas, "paginas_original": paginas_ant,
                    "imagenes": imagenes, **hecho}
