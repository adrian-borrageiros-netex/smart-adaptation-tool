"""Fotografías con licencia libre de Wikimedia Commons, para sustituir imágenes.

Una foto de un lugar real tiene que ser real: no se genera. Commons tiene millones con
licencia libre y una API pública. Aquí solo se busca y se filtra por licencia y tamaño; qué
buscar y cuáles sirven lo decide el asistente, y cuál se usa, el editor.

Las licencias que se aceptan obligan, salvo el dominio público, a citar al autor: el
crédito viaja con la imagen y la pantalla lo recuerda.
"""

from __future__ import annotations

import html
import re
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import httpx

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia pide un User-Agent que identifique la aplicación y cómo contactar.
CABECERAS = {"User-Agent": "Sarela-adaptacion/1.0 (https://adaptacion.sarela.tech; demostrador educativo)"}
HOSTS_IMAGEN = {"upload.wikimedia.org"}
MAX_BYTES = 15 * 1024 * 1024
_LIBRES = re.compile(r"^(cc0|cc[ -]by(-sa)?([ -]\d(\.\d)?)?|public domain|pd\b|dominio público)", re.I)


def _texto(valor: str | None) -> str:
    """Los metadatos de Commons vienen en HTML."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", valor or "")).split())


def licencia_libre(nombre: str) -> bool:
    return bool(_LIBRES.match((nombre or "").strip()))


def buscar(consulta: str, ancho_minimo: int = 0, limite: int = 12) -> list[dict[str, Any]]:
    """Fotos de Commons para una búsqueda, solo con licencia libre y tamaño suficiente."""
    parametros = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{consulta} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": limite,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 640,
    }
    try:
        r = httpx.get(API, params=parametros, headers=CABECERAS, timeout=20)
        r.raise_for_status()
        paginas = (r.json().get("query") or {}).get("pages") or {}
    except (httpx.HTTPError, ValueError):
        return []
    fotos = []
    for p in sorted(paginas.values(), key=lambda p: p.get("index", 0)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        licencia = _texto((meta.get("LicenseShortName") or {}).get("value"))
        if info.get("mime") not in ("image/jpeg", "image/png") or not licencia_libre(licencia):
            continue
        if (info.get("width") or 0) < ancho_minimo:
            continue
        fotos.append({
            "titulo": p.get("title", "").removeprefix("File:").removeprefix("Archivo:"),
            "pagina": info.get("descriptionurl"), "url": info.get("url"),
            "miniatura": info.get("thumburl") or info.get("url"),
            "ancho": info.get("width"), "alto": info.get("height"),
            "autor": _texto((meta.get("Artist") or {}).get("value"))[:200] or "autor desconocido",
            "licencia": licencia, "licencia_url": (meta.get("LicenseUrl") or {}).get("value"),
            "descripcion": _texto((meta.get("ImageDescription") or {}).get("value"))[:500],
        })
    return fotos


def descargar(url: str, maximo: int = MAX_BYTES) -> bytes:
    """Una imagen de Commons. Solo de su servidor de ficheros: nada que venga de otro sitio."""
    if urlparse(url).hostname not in HOSTS_IMAGEN or not url.startswith("https://"):
        raise ValueError("Solo se descargan imágenes de upload.wikimedia.org.")
    with httpx.stream("GET", url, headers=CABECERAS, timeout=60, follow_redirects=False) as r:
        r.raise_for_status()
        datos = BytesIO()
        for trozo in r.iter_bytes():
            datos.write(trozo)
            if datos.tell() > maximo:
                raise ValueError("La imagen pesa más de 15 MB.")
    return datos.getvalue()


def miniatura_jpeg(datos: bytes, lado: int = 512) -> bytes:
    """La miniatura, en JPEG y pequeña, para que el asistente la mire."""
    from PIL import Image
    with Image.open(BytesIO(datos)) as im:
        im = im.convert("RGB")
        im.thumbnail((lado, lado))
        salida = BytesIO()
        im.save(salida, "JPEG", quality=80)
    return salida.getvalue()


def credito(foto: dict) -> str:
    """Cómo se cita: título, autor y licencia."""
    return f"«{foto['titulo']}», {foto['autor']}, {foto['licencia']} (Wikimedia Commons)"
