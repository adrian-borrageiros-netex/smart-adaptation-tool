"""Retoques al reproductor de la plantilla 573 dentro del `index.html` de un paquete.

El reproductor trae su propia tabla de textos de interfaz (botones, avisos, mensajes de
corrección) en `loader.js`, con castellano, gallego, inglés y otros, pero sin catalán ni
euskera. Si el `locale` del paquete no está en la tabla cae al inglés: una unidad en
catalán decía «Start» y «That's right! Very good!». `textos_reproductor` añade al paquete
la tabla traducida (`reproductor/<idioma>.json`, generada con
`scripts/traducir_reproductor.py`) sin tocar la plantilla.

`modo_docente` es solo para la vista previa: enseña el contenido para el profesorado, que
el reproductor oculta fuera de smartED.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

CARPETA = Path(__file__).resolve().parent / "reproductor"

_TEXTOS = re.compile(r'<script id="sarela-textos-reproductor">.*?</script>\n?', re.S)
_BLOQUE = re.compile(r'var\s+ntxCafCompressed\s*=\s*"[^"]*";?\s*</script>\n?')

# La tabla se completa en cuanto el reproductor la crea: `ntxCaf` es una variable global
# que se asigna varias veces mientras carga, así que se vigila con un accesor. Lo que no
# esté traducido sale en castellano, nunca como la clave interna.
_PLANTILLA = """<script id="sarela-textos-reproductor">
(function () {
  var idioma = __IDIOMA__, textos = __TEXTOS__, valor, completada;
  function completar(v) {
    var t = v && v.ntxTranslates, tabla = {}, k;
    if (t && t !== completada) {
      for (k in (t.es || {})) tabla[k] = t.es[k];
      for (k in (t[idioma] || {})) tabla[k] = t[idioma][k];
      for (k in textos) tabla[k] = textos[k];
      t[idioma] = tabla;
      completada = t;
    }
    return v;
  }
  valor = window.ntxCaf;
  Object.defineProperty(window, "ntxCaf", {configurable: true,
    get: function () { return completar(valor); }, set: function (v) { valor = v; }});
})();
</script>
"""


def idiomas_disponibles() -> list[str]:
    return sorted(p.stem for p in CARPETA.glob("*.json"))


def textos_reproductor(html: str, idioma: str) -> str:
    """Deja en `html` la tabla de interfaz de `idioma`, si hay una; si no, la quita."""
    html = _TEXTOS.sub("", html)
    fichero = CARPETA / f"{idioma}.json"
    if not fichero.is_file():
        return html
    m = _BLOQUE.search(html)
    if not m:
        return html
    script = (_PLANTILLA.replace("__IDIOMA__", json.dumps(idioma))
              .replace("__TEXTOS__", json.dumps(json.loads(fichero.read_text(encoding="utf-8")),
                                                ensure_ascii=False, separators=(",", ":"))
                       .replace("</", "<\\/")))
    return html[:m.end()] + script + html[m.end():]


_DOCENTE = """<script id="sarela-modo-docente">
(function () {
  function activar() {
    var api = window.app && window.app.api;
    if (api && api.showTeacherVersion && document.body) { api.showTeacherVersion(); return true; }
    return false;
  }
  var intentos = 0, espera = setInterval(function () {
    if (activar() || ++intentos > 300) clearInterval(espera);
  }, 100);
  window.addEventListener("hashchange", function () { setTimeout(activar, 300); });
})();
</script>
"""


def modo_docente(html: str) -> str:
    """El `index.html` con el contenido del profesorado a la vista. Solo para previsualizar:
    fuera de smartED el reproductor se queda en modo alumno y su botón no responde."""
    i = html.rfind("</body>")
    return html[:i] + _DOCENTE + html[i:] if i >= 0 else html + _DOCENTE
