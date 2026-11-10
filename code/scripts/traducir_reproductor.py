"""Traduce los textos de la interfaz del reproductor de la plantilla 573 a otro idioma.

Botones, avisos y mensajes de corrección («Comenzar», «¡Muy bien!») no son de la unidad:
van en una tabla dentro de `loader.js` (`ntxTranslates`, en `ntxTemplateBundleCompressed`)
que trae castellano, gallego, inglés y otros, pero no catalán ni euskera. Con
`locale = "ca"` el reproductor no la encuentra y cae al inglés.

Esto traduce una vez la tabla castellana y deja el resultado en
`src/maker_paquete/reproductor/<idioma>.json`, que `textos_reproductor` mete en el
`index.html` de cada paquete en ese idioma. Necesita la clave de la API: se ejecuta en el
servidor y el JSON resultante se guarda en el repositorio.

    python scripts/traducir_reproductor.py <loader.json de un paquete 573> ca [salida.json]
"""

from __future__ import annotations

import base64
import json
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

LOTE = 150
# Lo que no se puede tocar: etiquetas, marcadores de plantilla y entidades.
_FIJOS = re.compile(r"<[^>]+>|\{\{?[^{}]*\}?\}|%[sd\d]|\$\{[^}]*\}|&[a-z]+;|#[A-Za-z_]+#|\\n")

_SISTEMA = """Localizas la interfaz de un reproductor de libros de texto digitales para alumnado y profesorado de Primaria y Secundaria: botones, avisos, mensajes de corrección de actividades, rótulos de herramientas y textos de un editor de contenidos.

Traduce cada texto del castellano al {destino}. Recibes también la versión inglesa como contexto: si el castellano es ambiguo, la inglesa ayuda a entender qué es (un botón, un título, un mensaje).

Reglas:
- Registro de interfaz: breve, claro, el mismo tono y la misma persona (tú/usted) que el castellano.
- Conserva literalmente las etiquetas HTML, los marcadores ({{...}}, %s, ${...}, #...#), las entidades HTML y los saltos \\n, en el mismo número y orden.
- Respeta las mayúsculas del original: si va todo en mayúsculas, la traducción también.
- Nombres de producto, siglas y códigos se devuelven igual.
- Terminología escolar establecida en {destino}.
- Devuelve todos los textos, con el mismo «id»."""

_ESQUEMA = {
    "type": "object",
    "properties": {"textos": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "texto": {"type": "string"}},
        "required": ["id", "texto"], "additionalProperties": False}}},
    "required": ["textos"], "additionalProperties": False,
}


def tabla(loader_json: Path) -> dict:
    """La tabla de textos del reproductor, sacada de su `loader.json`."""
    recursos = json.loads(loader_json.read_text(encoding="utf-8"))["loaderAssets"]
    for recurso in recursos.values():
        m = re.search(r'ntxTemplateBundleCompressed="([^"]+)"', recurso.get("contents") or "")
        if m:
            return json.loads(zlib.decompress(base64.b64decode(m.group(1))))["ntxTranslates"]
    raise SystemExit("Este loader.json no lleva la tabla de textos del reproductor.")


def valida(origen: str, destino: str) -> bool:
    return bool(destino.strip()) and _FIJOS.findall(origen) == _FIJOS.findall(destino)


def traducir(textos: list[dict], idioma: str) -> tuple[dict[str, str], float]:
    from web import ia
    consumo = ia.Consumo()
    sistema = _SISTEMA.replace("{destino}", ia.LENGUAS.get(idioma, idioma))

    def lote(grupo: list[dict]) -> list[dict]:
        texto, _ = ia._llamar(sistema, json.dumps({"textos": grupo}, ensure_ascii=False),
                              consumo, esquema=_ESQUEMA, esfuerzo="medium")
        return ia._json(texto).get("textos", [])

    grupos = [textos[i:i + LOTE] for i in range(0, len(textos), LOTE)]
    resultado: dict[str, str] = {}
    with ThreadPoolExecutor(ia.CONCURRENCIA) as hilos:
        for respuesta in hilos.map(lote, grupos):
            resultado.update({r["id"]: r["texto"] for r in respuesta})
    por_id = {t["id"]: t for t in textos}
    malos = [t for i, t in por_id.items() if not valida(t["es"], resultado.get(i, ""))]
    # Lo que no pase la comprobación se repite una vez, en un lote propio.
    if malos:
        for r in lote(malos):
            if valida(por_id[r["id"]]["es"], r["texto"]):
                resultado[r["id"]] = r["texto"]
    return ({i: resultado[i] for i in por_id if valida(por_id[i]["es"], resultado.get(i, ""))},
            consumo.coste_usd())


def main() -> None:
    loader, idioma = Path(sys.argv[1]), sys.argv[2]
    salida = Path(sys.argv[3]) if len(sys.argv) > 3 else (
        RAIZ / "src" / "maker_paquete" / "reproductor" / f"{idioma}.json")
    t = tabla(loader)
    es, en = t["es"], t.get("en", {})
    unicos = sorted({v for v in es.values() if isinstance(v, str) and v.strip()})
    textos = [{"id": str(i), "es": v, "en": next((en[k] for k, x in es.items() if x == v and k in en), "")}
              for i, v in enumerate(unicos)]
    traducidos, coste = traducir(textos, idioma)
    por_texto = {t["es"]: traducidos[t["id"]] for t in textos if t["id"] in traducidos}
    resultado = {k: por_texto[v] for k, v in sorted(es.items()) if v in por_texto}
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=0, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(f"{len(resultado)} de {len(es)} claves ({len(traducidos)} de {len(unicos)} textos), "
          f"{coste:.2f} USD → {salida}", file=sys.stderr)


if __name__ == "__main__":
    main()
