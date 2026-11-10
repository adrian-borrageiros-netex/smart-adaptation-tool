"""El asistente: traducción, propuestas de adaptación y búsqueda de enlaces con Claude.

Hace el trabajo que hasta ahora se hacía conversando con Claude lote a lote, pero con una
regla que no cambia: **nada de lo que produce se da por bueno solo**.

- Las traducciones entran en la memoria marcadas «por revisar». Se aplican al paquete para
  que se pueda ver el resultado, pero no cuentan como aprobadas hasta que un editor las
  confirma.
- Las adaptaciones territoriales son **propuestas**: rellenan la caja del editor y el
  término sigue pendiente.
- Los enlaces son **candidatos**: el término sigue pendiente hasta que alguien elige uno.

Y tres cosas que no se delegan en el modelo, porque se pueden comprobar en código:

- Cada traducción se valida antes de guardarla: mismas entidades HTML, sin marcado nuevo y
  con la misma puntuación en los extremos, que es lo que la une a sus vecinas.
- Solo se aceptan URL que la búsqueda web haya devuelto de verdad, nunca una escrita por el
  modelo, y cada una se comprueba por HTTP antes de proponerla.
- Cada llamada registra su consumo, para saber cuánto cuesta de verdad una unidad.

Configuración, por variables de entorno:

    ANTHROPIC_API_KEY        la clave; solo en el servidor
    SARELA_IA_MODELO         claude-opus-5-5 por defecto
    SARELA_IA_CONCURRENCIA   lotes de traducción en paralelo (4)
    SARELA_IA_FALLBACKS      «default» para reintentar en otro modelo si uno declina; vacío
                             para no usarlo
    SARELA_IA_PROVEEDOR      «falso» para probar el circuito sin llamar a la API
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

MODELO = os.environ.get("SARELA_IA_MODELO", "claude-opus-5-5")
# Lotes en paralelo. Las cuentas nuevas de la API admiten pocas conexiones simultáneas: con
# 4 la primera unidad real perdió 24 de 28 lotes por error 429. Con 2 y los reintentos de
# `_llamar`, lo que no quepa espera su turno en vez de fallar.
CONCURRENCIA = int(os.environ.get("SARELA_IA_CONCURRENCIA", "2"))
REINTENTOS_LIMITE = 6
FALLBACKS = os.environ.get("SARELA_IA_FALLBACKS", "default").strip()
PROVEEDOR = os.environ.get("SARELA_IA_PROVEEDOR", "").strip().lower()

# Precios por millón de tokens (USD), para estimar el coste. Las búsquedas web se
# facturan aparte y se cuentan por separado.
PRECIOS = {
    "claude-opus-5-5": {"entrada": 4.0, "salida": 20.0, "cache_lectura": 0.20, "cache_escritura": 5.0},
    "claude-sonnet-5-5": {"entrada": 2.0, "salida": 10.0, "cache_lectura": 0.20, "cache_escritura": 2.5},
    "claude-haiku-4-5": {"entrada": 1.0, "salida": 5.0, "cache_lectura": 0.10, "cache_escritura": 1.25},
}

LENGUAS = {"es": "español", "ca": "catalán", "gl": "gallego", "eu": "euskera",
           "fr": "francés", "en": "inglés", "pt": "portugués"}


class ErrorIA(RuntimeError):
    """Algo que el asistente no ha podido hacer. El mensaje es para el editor."""


class ClaveNoValida(ErrorIA):
    """La API rechaza la clave: revocada, caducada o mal copiada. Ningún lote va a pasar."""


class LimiteIA(ErrorIA):
    """La API ha pedido esperar (429) más veces de las que se reintenta. Repetir más tarde."""


@dataclass
class Consumo:
    entrada: int = 0
    salida: int = 0
    cache_lectura: int = 0
    cache_escritura: int = 0
    busquedas: int = 0
    modelo: str = MODELO

    def sumar(self, uso: Any, modelo: str | None = None) -> None:
        if uso is None:
            return
        self.entrada += getattr(uso, "input_tokens", 0) or 0
        self.salida += getattr(uso, "output_tokens", 0) or 0
        self.cache_lectura += getattr(uso, "cache_read_input_tokens", 0) or 0
        self.cache_escritura += getattr(uso, "cache_creation_input_tokens", 0) or 0
        servidor = getattr(uso, "server_tool_use", None)
        self.busquedas += getattr(servidor, "web_search_requests", 0) or 0
        if modelo:
            self.modelo = modelo

    def coste_usd(self) -> float:
        p = PRECIOS.get(self.modelo, PRECIOS["claude-opus-5-5"])
        return (self.entrada * p["entrada"] + self.salida * p["salida"]
                + self.cache_lectura * p["cache_lectura"]
                + self.cache_escritura * p["cache_escritura"]) / 1_000_000


def disponible() -> bool:
    """Si hay con qué llamar a Claude. Sin clave, la pantalla lo dice en vez de fallar."""
    return PROVEEDOR == "falso" or bool(os.environ.get("ANTHROPIC_API_KEY"))


# El último resultado de comprobar la clave: (instante, estado). Bien se recuerda diez
# minutos; mal, uno, para que se note pronto cuando la arreglen.
_CLAVE: dict[str, Any] = {}
MENSAJE_CLAVE = ("El asistente no puede trabajar: la API de Anthropic rechaza la clave del "
                 "servidor (revocada, caducada o mal copiada). Avisa al administrador para que "
                 "ponga una nueva; mientras tanto no se puede traducir, revisar ni proponer.")


def estado_clave(forzar: bool = False) -> dict[str, Any]:
    """Si la clave de la API funciona. La comprobación lista los modelos: no gasta tokens."""
    if PROVEEDOR == "falso":
        return {"ok": True, "motivo": None}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "motivo": "Falta ANTHROPIC_API_KEY en el servidor."}
    anterior = _CLAVE.get("estado")
    if anterior and not forzar and time.time() - _CLAVE["cuando"] < (600 if anterior["ok"] else 60):
        return anterior
    import anthropic
    try:
        anthropic.Anthropic(max_retries=0, timeout=10).models.list(limit=1)
        estado = {"ok": True, "motivo": None}
    except anthropic.AuthenticationError:
        estado = {"ok": False, "motivo": MENSAJE_CLAVE}
    except Exception:
        # Sin red o la API caída: no se puede saber, y no se bloquea por ello.
        estado = anterior or {"ok": True, "motivo": None}
    _anotar_clave(estado)
    return estado


def _anotar_clave(estado: dict[str, Any]) -> None:
    _CLAVE.update(estado=estado, cuando=time.time())


# ------------------------------------------------------------------- la llamada

def _cliente():
    import anthropic
    # Los 429 los gestiona `_llamar` con esperas más largas que las del SDK.
    return anthropic.Anthropic(max_retries=2)


def _llamar(system: str, usuario: str | list[dict], consumo: Consumo, *, esquema: dict | None = None,
            esfuerzo: str = "medium", max_tokens: int = 32000,
            herramientas: list[dict] | None = None) -> tuple[str, list]:
    """Una petición a Claude. Devuelve el texto final y todos los bloques de contenido.

    Siempre en streaming: con salidas largas evita que la petición caduque. La parte fija
    del prompt va primero y con caché, para que los lotes siguientes de la misma unidad la
    lean a una fracción del precio.
    """
    import anthropic

    cliente = _cliente()
    mensajes: list[dict] = [{"role": "user", "content": usuario}]
    parametros: dict[str, Any] = {
        "model": MODELO, "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": esfuerzo},
    }
    if esquema is not None:
        parametros["output_config"]["format"] = {"type": "json_schema", "schema": esquema}
    if herramientas:
        parametros["tools"] = herramientas
    if FALLBACKS:
        parametros["betas"] = ["server-side-fallback-2026-07-01"]
        parametros["fallbacks"] = FALLBACKS

    bloques: list = []
    pausas = esperas = 0
    while True:
        try:
            with cliente.beta.messages.stream(messages=mensajes, **parametros) as flujo:
                mensaje = flujo.get_final_message()
        except anthropic.RateLimitError as e:
            # Demasiadas peticiones o conexiones a la vez. Se espera lo que diga la API, o
            # un tiempo creciente, y se repite: es un límite de ritmo, no un fallo.
            esperas += 1
            if esperas > REINTENTOS_LIMITE:
                raise LimiteIA(f"La API sigue pidiendo esperar tras {REINTENTOS_LIMITE} intentos.") from e
            time.sleep(_espera(e, esperas))
            continue
        except anthropic.BadRequestError as e:
            # Si la cuenta no tiene acceso al reintento en otro modelo, se sigue sin él.
            if "fallback" in str(e).lower() and "fallbacks" in parametros:
                parametros.pop("fallbacks")
                parametros.pop("betas", None)
                continue
            raise ErrorIA(f"La API ha rechazado la petición: {e}") from e
        except anthropic.AuthenticationError as e:
            _anotar_clave({"ok": False, "motivo": MENSAJE_CLAVE})
            raise ClaveNoValida(MENSAJE_CLAVE) from e
        except anthropic.APIError as e:
            raise ErrorIA(f"No se ha podido completar la llamada a la API: {e}") from e

        consumo.sumar(mensaje.usage, getattr(mensaje, "model", None))
        bloques.extend(mensaje.content)
        if mensaje.stop_reason == "refusal":
            detalle = getattr(mensaje, "stop_details", None)
            categoria = getattr(detalle, "category", None) if detalle else None
            raise ErrorIA(f"El modelo ha declinado la petición ({categoria or 'sin categoría'}).")
        if mensaje.stop_reason == "max_tokens":
            raise ErrorIA("La respuesta se ha cortado por longitud. Reduce el tamaño del lote.")
        if mensaje.stop_reason != "pause_turn":
            break
        pausas += 1  # una búsqueda larga puede pausar el turno; se reanuda
        if pausas > 5:
            raise ErrorIA("La búsqueda no ha terminado tras varios intentos.")
        mensajes.append({"role": "assistant", "content": mensaje.content})

    # El texto útil es el que viene después de la última herramienta.
    texto = []
    for bloque in reversed(bloques):
        tipo = getattr(bloque, "type", "")
        if tipo == "text":
            texto.append(bloque.text)
        elif tipo.endswith("tool_result") or tipo == "server_tool_use":
            break
    return "".join(reversed(texto)), bloques


def _espera(error: Any, intento: int) -> float:
    """Segundos antes de reintentar: lo que pida la API o, si no dice, 10, 20, 40… hasta 120."""
    try:
        return min(120.0, float(error.response.headers.get("retry-after")) + random.uniform(0, 2))
    except (AttributeError, TypeError, ValueError):
        return min(120.0, 10 * 2 ** (intento - 1)) + random.uniform(0, 3)


def _json(texto: str) -> Any:
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ErrorIA("La respuesta del modelo no es JSON válido.")


# ------------------------------------------------------------------- traducción

_ENTIDAD = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")
_NEGRITA = re.compile(r"</?b>")
_BORDE = set(",.;:)(]»«¿?¡!…·—–-\"'")


def validar_traduccion(origen: str, destino: str) -> str | None:
    """Motivo por el que una traducción no se puede aplicar, o `None` si vale.

    Los fragmentos son nodos de texto de un HTML: un párrafo con un enlace en medio llega
    partido en tres. Por eso importan cosas que en un texto suelto darían igual.
    """
    if not destino or not destino.strip():
        return "vacía"
    if sorted(_ENTIDAD.findall(origen)) != sorted(_ENTIDAD.findall(destino)):
        return "no conserva las entidades HTML (&nbsp; y similares)"
    # El texto de los PDF de Word lleva la negrita como <b>…</b>: tiene que volver igual.
    if _NEGRITA.findall(origen) != _NEGRITA.findall(destino):
        return "no conserva las marcas de negrita <b>…</b>"
    if ("<" in _NEGRITA.sub("", destino) or ">" in _NEGRITA.sub("", destino)) and not (
            "<" in _NEGRITA.sub("", origen) or ">" in _NEGRITA.sub("", origen)):
        return "introduce marcado HTML"
    o, d = origen.strip(), destino.strip()
    for a, b, lado in ((o[:1], d[:1], "inicio"), (o[-1:], d[-1:], "final")):
        if a in _BORDE and a != b:
            return f"cambia la puntuación del {lado}, que la une al texto vecino"
    return None


_SISTEMA_TRADUCCION = """Eres traductor editorial de libros de texto de Educación Secundaria. Traduces una unidad didáctica del {origen} al {destino}.

Recibes fragmentos en orden de lectura. Cada uno es un nodo de texto de un documento HTML, así que puede empezar o acabar a mitad de frase: un párrafo con un enlace en medio llega partido en tres fragmentos. Tradúcelos para que, vueltos a unir, lean como un texto natural en {destino}.

Reglas:
- Conserva exactamente la puntuación y los espacios del principio y del final de cada fragmento: es lo que lo une a sus vecinos.
- Conserva literalmente las entidades HTML como &nbsp; o &amp;, en el mismo número. No añadas etiquetas.
- Los fragmentos de tipo «pdf» son párrafos enteros de un documento. Si llevan marcas <b>…</b>, que indican negrita, devuélvelas en el mismo número y orden, rodeando las palabras equivalentes.
- Conserva cifras, unidades, fórmulas químicas, URL, siglas de competencias (CCL, STEM, CD, CPSAA, CC, CE, CCEC), nombres de personas y nombres de instituciones (por ejemplo, «Xunta de Galicia»).
- Topónimos: usa la forma establecida en {destino}, sin cambiar el lugar. Esto es una traducción, no una adaptación: si el texto habla de Galicia, sigue hablando de Galicia.
- Registro de libro de texto de 1.º de ESO y terminología científica correcta en {destino}.
- Los rótulos de plantilla en inglés («Block Title», «Button Text», «Footer title») y lo que no necesite traducción se devuelven idénticos.
- Si el original tiene una errata evidente e inequívoca, corrígela en la traducción y explícalo en «nota». No corrijas nada más.
- Devuelve todos los fragmentos, con el mismo «id».
{glosario}"""

_ESQUEMA_TRADUCCION = {
    "type": "object",
    "properties": {"traducciones": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "destino": {"type": "string"},
                       "nota": {"type": "string"}},
        "required": ["id", "destino", "nota"], "additionalProperties": False}}},
    "required": ["traducciones"], "additionalProperties": False,
}


def traducir_lote(frases: list[dict], idioma_origen: str, idioma_destino: str,
                  glosario: list[tuple[str, str]], consumo: Consumo) -> list[dict]:
    """Traduce un lote. `frases` lleva `id`, `texto`, `leccion` y `componente`.

    Devuelve `[{id, destino, nota}]`. El glosario —frases ya aprobadas para este par de
    lenguas— va en la parte fija del prompt: da coherencia terminológica entre unidades y
    se lee de la caché en todos los lotes salvo el primero.
    """
    if PROVEEDOR == "falso":
        return [{"id": f["id"], "destino": _traduccion_falsa(f["texto"]), "nota": ""}
                for f in frases]

    lista_glosario = ""
    if glosario:
        lista_glosario = ("\nTerminología ya aprobada en otras unidades; respétala:\n"
                          + "\n".join(f"- {o} → {d}" for o, d in glosario))
    system = _SISTEMA_TRADUCCION.format(origen=LENGUAS.get(idioma_origen, idioma_origen),
                                        destino=LENGUAS.get(idioma_destino, idioma_destino),
                                        glosario=lista_glosario)
    usuario = json.dumps({"fragmentos": [
        {"id": f["id"], "texto": f["texto"], "leccion": f.get("leccion") or "",
         "tipo": f.get("componente") or ""} for f in frases]}, ensure_ascii=False)
    texto, _ = _llamar(system, usuario, consumo, esquema=_ESQUEMA_TRADUCCION,
                       esfuerzo="medium")
    return _json(texto).get("traducciones", [])


_SISTEMA_LECTURA = """Transcribes texto de recortes de imagen de documentos escolares: mapas mentales, fichas y diagramas.

Cada recorte contiene un rótulo pintado en la imagen. Devuelve exactamente lo que se lee, línea a línea tal como se ve partido en el recorte:
- Sin traducir, sin corregir erratas y sin completar lo que no se ve.
- Con las tildes, la puntuación y las mayúsculas del original.
- Si un recorte no contiene texto legible, devuelve sus líneas vacías."""

_SISTEMA_LECTURA_IMAGEN = """Transcribes el texto que aparece dentro de imágenes de documentos escolares: fotografías, diagramas, esquemas y recuadros.

Para cada imagen, devuelve todo el texto que se lee en ella —rótulos, leyendas, títulos, párrafos—, una línea por cada línea visible y en orden de lectura:
- Sin traducir, sin corregir y sin describir la imagen.
- Solo lo que es texto escrito: los números sueltos que numeran fotos también cuentan.
- Si no hay ningún texto, devuelve sus líneas vacías."""

_ESQUEMA_LECTURA = {
    "type": "object",
    "properties": {"recortes": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "lineas": {"type": "array", "items": {"type": "string"}}},
        "required": ["id", "lineas"], "additionalProperties": False}}},
    "required": ["recortes"], "additionalProperties": False,
}


def leer_textos(recortes: list[dict], consumo: Consumo,
                imagenes: bool = False) -> dict[str, list[str]]:
    """Lee el texto pintado en cada recorte. `recortes` lleva `id` y `png` (bytes).

    Es para los rótulos de un PDF que están en la imagen sin capa de texto: sin leerlos no
    se pueden traducir. Lo leído entra en el circuito como cualquier otro texto del PDF, y
    el informe dice que se ha leído de la imagen.
    """
    if PROVEEDOR == "falso":
        return {r["id"]: ([] if imagenes else [f"Texto leído {r['id']}"]) for r in recortes}
    import base64
    contenido: list[dict] = []
    for r in recortes:
        contenido.append({"type": "text", "text": f"Recorte {r['id']}:"})
        contenido.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(r["png"]).decode("ascii")}})
    contenido.append({"type": "text", "text": "Transcribe cada recorte con su id."})
    texto, _ = _llamar(_SISTEMA_LECTURA_IMAGEN if imagenes else _SISTEMA_LECTURA, contenido,
                       consumo, esquema=_ESQUEMA_LECTURA, esfuerzo="low", max_tokens=16000)
    return {r["id"]: [l.strip() for l in r.get("lineas", []) if l.strip()]
            for r in _json(texto).get("recortes", [])}


_SISTEMA_IMAGENES = """Revisas las imágenes de una unidad didáctica de Educación Secundaria escrita en {origen}, para saber cuáles llevan texto que haya que traducir o adaptar.

Para cada imagen:
- «tipo», uno de estos:
  · «sin_texto»: no hay texto.
  · «no_traducible»: solo números, letras de referencia, fórmulas, cifras de escala o nombres que no cambian de una lengua a otra (nombres científicos, de personas).
  · «rotulos_fondo_liso»: rótulos, leyendas o títulos sobre un fondo liso o casi liso (blanco o de un color), separados del dibujo. Es lo típico de una infografía.
  · «rotulos_sobre_dibujo»: rótulos encima de una foto, un mapa, un degradado o una textura.
  · «texto_en_foto»: texto que forma parte de la escena (un cartel, un libro) o letras decoradas.
  Si mezcla varios, elige el que domine entre los textos que hay que traducir.
- «territorial»: verdadero si la imagen representa o nombra un territorio concreto de la comunidad de origen (un mapa de la comunidad, topónimos locales), de modo que en otra comunidad no basta con traducirla.
- «rotulos»: cada rótulo es una unidad de sentido, aunque ocupe varias líneas. Da sus líneas exactamente como se leen, en orden de lectura, sin traducir ni corregir. Omite los que sean solo números.
- «motivo»: una frase breve que justifique el tipo."""

_ESQUEMA_IMAGENES = {
    "type": "object",
    "properties": {"imagenes": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "tipo": {"type": "string", "enum": ["sin_texto", "no_traducible", "rotulos_fondo_liso",
                                                "rotulos_sobre_dibujo", "texto_en_foto"]},
            "territorial": {"type": "boolean"},
            "motivo": {"type": "string"},
            "rotulos": {"type": "array", "items": {
                "type": "object",
                "properties": {"lineas": {"type": "array", "items": {"type": "string"}}},
                "required": ["lineas"], "additionalProperties": False}},
        },
        "required": ["id", "tipo", "territorial", "motivo", "rotulos"],
        "additionalProperties": False}}},
    "required": ["imagenes"], "additionalProperties": False,
}


def clasificar_imagenes(imagenes: list[dict], idioma_origen: str, consumo: Consumo) -> dict[str, dict]:
    """Lee y clasifica imágenes. `imagenes` lleva `id` y `png` (bytes).

    Devuelve por id el tipo, si es territorial, el motivo y los rótulos con sus líneas.
    Es el inventario del punto 6: decide qué se puede rehacer solo y qué va a diseño.
    """
    if PROVEEDOR == "falso":
        return {i["id"]: {"tipo": "sin_texto", "territorial": False, "motivo": "prueba",
                          "rotulos": []} for i in imagenes}
    import base64
    contenido: list[dict] = []
    for i in imagenes:
        contenido.append({"type": "text", "text": f"Imagen {i['id']}:"})
        contenido.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(i["png"]).decode("ascii")}})
    contenido.append({"type": "text", "text": "Clasifica cada imagen con su id."})
    sistema = _SISTEMA_IMAGENES.format(origen=LENGUAS.get(idioma_origen, idioma_origen))
    texto, _ = _llamar(sistema, contenido, consumo, esquema=_ESQUEMA_IMAGENES,
                       esfuerzo="low", max_tokens=16000)
    return {str(r["id"]): r for r in _json(texto).get("imagenes", [])}


_SISTEMA_COMPROBACION = """Compruebas imágenes de una unidad didáctica en las que se han sustituido automáticamente los rótulos, del {origen} al {destino}.

Para cada imagen recibes los rótulos que debería decir. Devuelve:
- «leidos»: para cada rótulo esperado, el texto que se lee realmente en la imagen en ese rótulo, tal cual, aunque esté mal o cortado. Si no aparece, cadena vacía.
- «restos_origen»: cualquier texto que siga en {origen} y no debería (restos del rótulo original, letras sueltas mal borradas). Lista vacía si no hay.
- «defectos»: problemas visuales concretos del texto nuevo: cortado, superpuesto a un dibujo o a otro texto, manchas del borrado, ilegible. Lista vacía si no hay. No opines sobre la traducción ni sobre la fuente."""

_ESQUEMA_COMPROBACION = {
    "type": "object",
    "properties": {"imagenes": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "leidos": {"type": "array", "items": {
                "type": "object",
                "properties": {"esperado": {"type": "string"}, "leido": {"type": "string"}},
                "required": ["esperado", "leido"], "additionalProperties": False}},
            "restos_origen": {"type": "array", "items": {"type": "string"}},
            "defectos": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "leidos", "restos_origen", "defectos"], "additionalProperties": False}}},
    "required": ["imagenes"], "additionalProperties": False,
}


def comprobar_imagenes(imagenes: list[dict], idioma_origen: str, idioma_destino: str,
                       consumo: Consumo) -> dict[str, dict]:
    """Relee imágenes rehechas. `imagenes` lleva `id`, `png` y `esperados` (los rótulos).

    Devuelve por id lo que se lee en cada rótulo, los restos en el idioma de origen y los
    defectos visuales. El veredicto no lo da el modelo: lo calcula quien llama, comparando
    lo leído con lo esperado.
    """
    if PROVEEDOR == "falso":
        return {i["id"]: {"leidos": [{"esperado": e, "leido": e} for e in i["esperados"]],
                          "restos_origen": [], "defectos": []} for i in imagenes}
    import base64
    contenido: list[dict] = []
    for i in imagenes:
        contenido.append({"type": "text", "text": f"Imagen {i['id']}. Rótulos esperados: "
                                                  + json.dumps(i["esperados"], ensure_ascii=False)})
        contenido.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(i["png"]).decode("ascii")}})
    contenido.append({"type": "text", "text": "Comprueba cada imagen con su id."})
    sistema = _SISTEMA_COMPROBACION.format(origen=LENGUAS.get(idioma_origen, idioma_origen),
                                           destino=LENGUAS.get(idioma_destino, idioma_destino))
    texto, _ = _llamar(sistema, contenido, consumo, esquema=_ESQUEMA_COMPROBACION,
                       esfuerzo="low", max_tokens=16000)
    return {str(r["id"]): r for r in _json(texto).get("imagenes", [])}


_SISTEMA_LOCALIZAR = """Localizas rótulos pintados en una imagen de una unidad didáctica, para borrarlos y escribir encima su traducción.

Recibes la imagen, su tamaño en píxeles y los rótulos, cada uno con sus líneas tal como se leen. Para cada línea de cada rótulo devuelve la caja que ocupa su texto en la imagen, en píxeles de esa imagen: [x0, y0, x1, y1], con (0, 0) en la esquina superior izquierda.
- La caja abarca solo las letras de esa línea, ajustada, sin el dibujo de alrededor ni las líneas guía.
- Si un rótulo aparece varias veces, el que esté en el orden de lectura que le corresponde.
- Si una línea no se ve en la imagen, devuelve la caja vacía: []."""

_ESQUEMA_LOCALIZAR = {
    "type": "object",
    "properties": {"rotulos": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"},
                       "cajas": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}},
        "required": ["id", "cajas"], "additionalProperties": False}}},
    "required": ["rotulos"], "additionalProperties": False,
}


def localizar_rotulos(png: bytes, ancho: int, alto: int, rotulos: list[dict],
                      consumo: Consumo) -> dict[str, list[list[float]]]:
    """Dónde está cada línea de cada rótulo. `rotulos` lleva `id` y `lineas`.

    Es el respaldo de Tesseract, que en gallego y en imágenes pequeñas no encuentra muchos
    rótulos. Las cajas del modelo son aproximadas: quien llama las ajusta a la tinta, y la
    relectura comprueba el resultado.
    """
    if PROVEEDOR == "falso":
        return {r["id"]: [] for r in rotulos}
    import base64
    contenido = [
        {"type": "text", "text": f"Imagen de {ancho} × {alto} píxeles."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode("ascii")}},
        {"type": "text", "text": "Rótulos: " + json.dumps(
            [{"id": r["id"], "lineas": r["lineas"]} for r in rotulos], ensure_ascii=False)}]
    texto, _ = _llamar(_SISTEMA_LOCALIZAR, contenido, consumo, esquema=_ESQUEMA_LOCALIZAR,
                       esfuerzo="medium", max_tokens=16000)
    return {str(r["id"]): [c for c in r.get("cajas", [])] for r in _json(texto).get("rotulos", [])}


_SISTEMA_BUSCAR_IMAGEN = """Eres editor gráfico de libros de texto. Hay que sustituir una imagen de una unidad didáctica que se adapta de {origen} a {destino}, por una fotografía real con licencia libre de Wikimedia Commons.

Recibes la imagen original, cómo se usa en la unidad (título, texto alternativo, lección) y por qué hay que cambiarla. Devuelve:
- «que_buscar»: en una frase, qué tiene que mostrar la imagen nueva para cumplir la misma función didáctica en {destino}. Si la original muestra un lugar o una especie de {origen}, el equivalente real de {destino}; si es un concepto general, lo mismo, sin elementos de {origen}.
- «consultas»: de 2 a 4 búsquedas cortas para el buscador de Wikimedia Commons, de la más precisa a la más general, en la lengua en que suelen estar los títulos de esas fotos (inglés, castellano o el nombre científico).
- «evitar»: lo que descarta una foto (por ejemplo, que sea de {origen}, un dibujo, un mapa, texto superpuesto)."""

_ESQUEMA_BUSCAR_IMAGEN = {
    "type": "object",
    "properties": {"que_buscar": {"type": "string"},
                   "consultas": {"type": "array", "items": {"type": "string"}},
                   "evitar": {"type": "string"}},
    "required": ["que_buscar", "consultas", "evitar"], "additionalProperties": False,
}

_SISTEMA_ELEGIR_IMAGEN = """Eres editor gráfico de libros de texto. Eliges, entre fotografías de Wikimedia Commons, las que pueden sustituir a una imagen de una unidad didáctica.

Recibes lo que tiene que mostrar la imagen nueva, lo que hay que evitar y las candidatas, cada una con su id, su título y su descripción en Commons. Para cada candidata que sirva devuelve su id, una nota de 0 a 10 y el motivo en una frase. Descarta las que no muestren lo pedido, las que sean de otro lugar si el lugar importa, las de mala calidad, los dibujos y las que lleven texto o marcas encima. Si ninguna sirve, devuelve la lista vacía."""

_ESQUEMA_ELEGIR_IMAGEN = {
    "type": "object",
    "properties": {"elegidas": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "nota": {"type": "integer"},
                       "motivo": {"type": "string"}},
        "required": ["id", "nota", "motivo"], "additionalProperties": False}}},
    "required": ["elegidas"], "additionalProperties": False,
}


def buscar_imagen(png: bytes, contexto: dict, origen: str, destino: str, consumo: Consumo) -> dict:
    """Qué buscar en Commons para sustituir una imagen, y cómo."""
    if PROVEEDOR == "falso":
        return {"que_buscar": "prueba", "consultas": ["Quercus suber Andalucia"], "evitar": ""}
    import base64
    contenido = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode("ascii")}},
        {"type": "text", "text": json.dumps(contexto, ensure_ascii=False)}]
    sistema = _SISTEMA_BUSCAR_IMAGEN.format(origen=origen, destino=destino)
    texto, _ = _llamar(sistema, contenido, consumo, esquema=_ESQUEMA_BUSCAR_IMAGEN,
                       esfuerzo="low", max_tokens=4000)
    return _json(texto)


def elegir_imagenes(pedido: dict, candidatas: list[dict], consumo: Consumo) -> list[dict]:
    """Ordena las candidatas de Commons. `candidatas` lleva `id`, `png`, `titulo` y
    `descripcion`. Devuelve las que sirven, de mejor a peor."""
    if PROVEEDOR == "falso":
        return [{"id": c["id"], "nota": 5, "motivo": "prueba"} for c in candidatas]
    import base64
    contenido: list[dict] = [{"type": "text", "text": json.dumps(
        {"que_mostrar": pedido.get("que_buscar"), "evitar": pedido.get("evitar")}, ensure_ascii=False)}]
    for c in candidatas:
        contenido.append({"type": "text", "text": f"Candidata {c['id']}: {c['titulo']}. {c['descripcion'][:300]}"})
        contenido.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                      "data": base64.b64encode(c["png"]).decode("ascii")}})
    texto, _ = _llamar(_SISTEMA_ELEGIR_IMAGEN, contenido, consumo, esquema=_ESQUEMA_ELEGIR_IMAGEN,
                       esfuerzo="low", max_tokens=8000)
    elegidas = [e for e in _json(texto).get("elegidas", []) if e.get("nota", 0) >= 5]
    return sorted(elegidas, key=lambda e: -e["nota"])


def _traduccion_falsa(texto: str) -> str:
    """Para probar el circuito sin API: marca las palabras largas en minúscula.

    Respeta entidades y nombres propios, como haría el modelo: marcar «Galicia» impediría
    probar la detección de lo territorial, que es justo lo que viene después.
    """
    return re.sub(r"(&[#\w]+;)|(\b[^\W\d_A-ZÁÉÍÓÚÑÇ][^\W\d_]{3,})",
                  lambda m: m.group(1) or f"{m.group(2)}ᵃ", texto)


# ------------------------------------------------------------------- adaptación

_SISTEMA_ADAPTACION = """Eres editor de libros de texto de Educación Secundaria y adaptas una unidad a otra comunidad autónoma. La unidad ya está traducida al {idioma}; ahora hay que adaptar lo que depende del territorio: de {origen} a {destino}.

Recibes los fragmentos que el sistema ha marcado como ligados al territorio de origen, con la lección en la que aparecen. Para cada uno propón el texto adaptado.

Reglas:
- Cambia solo lo territorial: lugares, ejemplos, recursos, datos del territorio. Mantén la intención didáctica, la estructura y una longitud parecida.
- Rigor científico: lo que afirmes de {destino} tiene que ser cierto. Traducir o reutilizar una frase del territorio de origen puede dejar contenido falso: un territorio de rocas sedimentarias no se describe como uno de rocas magmáticas.
- No inventes datos concretos de los que no estés seguro (cifras, fechas, nombres de minas o de lugares). Si hacen falta, escribe [PENDIENTE: dato a verificar: …] y explícalo en «verificar».
- No propongas nunca ISBN, licencias, créditos, autoría, atribuciones institucionales o logotipos, ni direcciones web: devuelve [PENDIENTE: …] diciendo qué decisión hace falta y quién la toma.
- Si un fragmento no depende del territorio y vale tal cual, devuélvelo idéntico y dilo en «motivo».
- Coherencia: el mismo criterio en toda la unidad. Si reparte el territorio en zonas, las mismas zonas en la lección, en los pies de mapa y en las actividades de relacionar. Resume ese criterio en «criterio».
- Escribe en {idioma}, salvo el marcador: escribe siempre literalmente «[PENDIENTE: …]», en castellano y sin traducir, porque es la etiqueta que el sistema reconoce. Lo que va dentro, en {idioma}.
- Si un fragmento trae «propuesta_anterior», el editor ha pedido otra: propón una distinta de verdad, no una paráfrasis, y sigue «indicacion» si la hay. Si trae «otras_decisiones», son textos que el editor ya ha fijado en la unidad: sé coherente con ellos.
{curriculo}"""

_ESQUEMA_ADAPTACION = {
    "type": "object",
    "properties": {
        "criterio": {"type": "string"},
        "propuestas": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "propuesta": {"type": "string"},
                           "motivo": {"type": "string"},
                           "verificar": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "propuesta", "motivo", "verificar"],
            "additionalProperties": False}}},
    "required": ["criterio", "propuestas"], "additionalProperties": False,
}


def bloque_documentacion(documentacion: str | None) -> str:
    """La documentación del experto, como fuente que manda sobre lo que sepa el modelo."""
    if not documentacion:
        return ""
    return ("\nDocumentación aportada por el experto en la materia para esta adaptación. Es la "
            "fuente prioritaria: usa sus datos, ejemplos y enlaces, no los contradigas y, si cubre "
            "un punto, sigue lo que propone. Lo que no cubra, trátalo como siempre:\n" + documentacion)


def proponer_adaptaciones(fragmentos: list[dict], idioma: str, origen: str, destino: str,
                          consumo: Consumo, curriculo: str | None = None,
                          unidad: str | None = None, documentacion: str | None = None,
                          esfuerzo: str = "high") -> dict:
    """Propuestas para los puntos territoriales de una unidad, todas en una sola llamada.

    Van juntas a propósito: la coherencia entre la lección, el mapa y la actividad de
    relacionar fue el error que encontraron los revisores en la adaptación a Asturias, y
    solo se garantiza si el modelo ve todos los fragmentos a la vez. Para pedir otra
    propuesta de un solo punto, el fragmento lleva `propuesta_anterior`, `indicacion` y
    `otras_decisiones`, que suplen esa vista de conjunto.
    """
    if PROVEEDOR == "falso":
        return {"criterio": "Propuesta de prueba, sin llamar a la API.",
                "propuestas": [{"id": f["id"], "propuesta": "[PROPUESTA DE PRUEBA"
                                + (" OTRA" if f.get("propuesta_anterior") else "") + f"] {f['texto']}",
                                "motivo": "prueba", "verificar": []} for f in fragmentos]}

    bloque_curriculo = ""
    if curriculo:
        bloque_curriculo = ("\nCurrículo de destino (extracto del decreto). Las propuestas "
                            "tienen que servir para lo que pide:\n" + curriculo)
    system = _SISTEMA_ADAPTACION.format(idioma=LENGUAS.get(idioma, idioma), origen=origen,
                                        destino=destino, curriculo=bloque_curriculo
                                        + bloque_documentacion(documentacion))
    extras = ("propuesta_anterior", "indicacion", "otras_decisiones")
    usuario = json.dumps({"unidad": unidad or "", "fragmentos": [
        {"id": f["id"], "leccion": f.get("leccion") or "", "texto": f["texto"],
         **{k: f[k] for k in extras if f.get(k)}}
        for f in fragmentos]}, ensure_ascii=False)
    texto, _ = _llamar(system, usuario, consumo, esquema=_ESQUEMA_ADAPTACION,
                       esfuerzo=esfuerzo, max_tokens=64000)
    respuesta = _json(texto)
    for p in respuesta.get("propuestas", []):
        p["propuesta"] = normalizar_marcador(p.get("propuesta") or "")
    return respuesta


# El sistema reconoce «[PENDIENTE: …]». Escribiendo en otra lengua, el modelo tiende a
# traducir la etiqueta —«[PENDENT:» en catalán, «[PENDENTE:» en gallego o portugués—, y un
# marcador que no se reconoce deja el término contado como resuelto.
_MARCADOR_VARIANTE = re.compile(r"\[\s*(PENDENT|PENDENTE|PENDIENT|PENDING|ZAIN|ZAINTZEN)\w*\s*:", re.I)


def normalizar_marcador(texto: str) -> str:
    return _MARCADOR_VARIANTE.sub("[PENDIENTE:", texto)


# ------------------------------------------------------------------- revisión completa

_SISTEMA_REVISION = """Revisas una unidad didáctica de Educación Secundaria que se está adaptando de {origen} a {destino}. Ya está traducida al {idioma}. Recibes todos sus fragmentos de texto, con la lección en la que aparecen.

Localiza los que dependen del territorio de origen y habría que cambiar para {destino}, aunque no nombren ningún lugar:
- lugares, comarcas, ríos, montañas, yacimientos, museos, parques, espacios protegidos;
- especies, rocas, paisajes, fenómenos o recursos presentados como propios o característicos del territorio;
- datos, cifras, estadísticas, fechas o hechos históricos del territorio;
- instituciones, organismos, legislación, programas o proyectos del territorio;
- personas y referencias culturales locales usadas como ejemplo;
- actividades que piden trabajar sobre «tu comunidad», «tu entorno» o un recurso del territorio;
- créditos, licencias o atribuciones ligadas al territorio.

No marques el contenido científico general, válido en cualquier sitio, aunque aparezca en una lección territorial. Tampoco los rótulos de plantilla. Si no encuentras nada en un fragmento, no lo incluyas.

Para cada hallazgo indica el «id», qué es lo que depende del territorio y qué habría que cambiar, y la «importancia»: «alta» si deja contenido falso o inservible en {destino}, «media» si es un ejemplo local que convendría sustituir, «baja» si es opcional.
{curriculo}"""

_ESQUEMA_REVISION = {
    "type": "object",
    "properties": {
        "resumen": {"type": "string"},
        "hallazgos": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "motivo": {"type": "string"},
                           "importancia": {"type": "string", "enum": ["alta", "media", "baja"]}},
            "required": ["id", "motivo", "importancia"], "additionalProperties": False}}},
    "required": ["resumen", "hallazgos"], "additionalProperties": False,
}


def revisar_territorio(fragmentos: list[dict], idioma: str, origen: str, destino: str,
                       consumo: Consumo, unidad: str | None = None,
                       curriculo: str | None = None, documentacion: str | None = None) -> dict:
    """Lo que depende del territorio en una unidad entera, más allá de la lista de topónimos.

    La detección del procesador busca nombres de lugar conocidos. Un yacimiento, un museo o
    una especie local presentados como ejemplo pasan sin marcar si no aparecen en la lista:
    esto los busca leyendo el contenido, y solo señala. Las propuestas vienen después.
    """
    if PROVEEDOR == "falso":
        return {"resumen": "Revisión de prueba, sin llamar a la API.", "hallazgos": [
            {"id": f["id"], "motivo": "prueba", "importancia": "media"} for f in fragmentos[:2]]}
    bloque_curriculo = ""
    if curriculo:
        bloque_curriculo = ("\nCurrículo de destino (extracto). Marca también lo que, en un "
                            "contenido ligado al territorio, no sirva para los saberes que pide:\n"
                            + curriculo)
    system = _SISTEMA_REVISION.format(idioma=LENGUAS.get(idioma, idioma), origen=origen,
                                      destino=destino, curriculo=bloque_curriculo
                                      + bloque_documentacion(documentacion))
    usuario = json.dumps({"unidad": unidad or "", "fragmentos": [
        {"id": f["id"], "leccion": f.get("leccion") or "", "texto": f["texto"]}
        for f in fragmentos]}, ensure_ascii=False)
    texto, _ = _llamar(system, usuario, consumo, esquema=_ESQUEMA_REVISION,
                       esfuerzo="high", max_tokens=64000)
    return _json(texto)


# ------------------------------------------------------------------- currículo

_SISTEMA_ANALISIS = """Eres asesor curricular de una editorial de libros de texto. Una unidad didáctica escrita para {origen} se va a adaptar a {destino}. Recibes su texto, lección a lección, y el extracto del currículo oficial de {destino} para su materia y curso: competencias específicas, criterios de evaluación y saberes básicos.

Analiza qué hay que hacer para que la unidad sirva en {destino}:
- «cubre»: los saberes del currículo de destino que la unidad ya trabaja, y dónde (lección).
- «sustituir»: lo que está pensado para {origen} y hay que cambiar: ejemplos, lugares, datos o enfoques ligados a {origen}, y lo que el currículo de {destino} pide de otra manera. Para cada uno, la lección, qué es, por qué y una propuesta breve de por qué sustituirlo, ligada al currículo de {destino} cuando se pueda (cita el código del saber o el número del criterio).
- «falta»: saberes o criterios del currículo de {destino} que corresponden a esta unidad por su tema y no aparecen, con una sugerencia breve de cómo incorporarlos. Solo los relacionados con el tema de la unidad: no enumeres todo el currículo del curso.
- «resumen»: tres o cuatro frases para el editor.

Rigor: no inventes datos de {destino} (yacimientos, especies, cifras) de los que no estés seguro; si una propuesta los necesita, dilo como «a documentar». Escribe en castellano, aunque la unidad esté en otra lengua: lo lee el equipo editorial.

Currículo de {destino}:
{curriculo}"""

_ESQUEMA_ANALISIS = {
    "type": "object",
    "properties": {
        "resumen": {"type": "string"},
        "cubre": {"type": "array", "items": {"type": "object", "properties": {
            "saber": {"type": "string"}, "donde": {"type": "string"}},
            "required": ["saber", "donde"], "additionalProperties": False}},
        "sustituir": {"type": "array", "items": {"type": "object", "properties": {
            "leccion": {"type": "string"}, "que": {"type": "string"}, "por_que": {"type": "string"},
            "propuesta": {"type": "string"}},
            "required": ["leccion", "que", "por_que", "propuesta"], "additionalProperties": False}},
        "falta": {"type": "array", "items": {"type": "object", "properties": {
            "saber": {"type": "string"}, "sugerencia": {"type": "string"}},
            "required": ["saber", "sugerencia"], "additionalProperties": False}},
    },
    "required": ["resumen", "cubre", "sustituir", "falta"], "additionalProperties": False,
}


def analizar_curriculo(lecciones: list[dict], curriculo: str, origen: str, destino: str,
                       consumo: Consumo, unidad: str | None = None,
                       documentacion: str | None = None) -> dict:
    """El plan de adaptación de una unidad frente al currículo de destino, en una llamada."""
    if PROVEEDOR == "falso":
        return {"resumen": "Análisis de prueba, sin llamar a la API.",
                "cubre": [{"saber": "Saber de prueba", "donde": lecciones[0]["leccion"] if lecciones else ""}],
                "sustituir": [{"leccion": "", "que": "Ejemplo de prueba", "por_que": "prueba",
                               "propuesta": "prueba"}],
                "falta": [{"saber": "Saber de prueba", "sugerencia": "prueba"}]}
    system = _SISTEMA_ANALISIS.format(origen=origen, destino=destino, curriculo=curriculo) \
        + bloque_documentacion(documentacion)
    usuario = json.dumps({"unidad": unidad or "", "lecciones": lecciones}, ensure_ascii=False)
    texto, _ = _llamar(system, usuario, consumo, esquema=_ESQUEMA_ANALISIS,
                       esfuerzo="high", max_tokens=32000)
    return _json(texto)


# ------------------------------------------------------------------- enlaces

_SISTEMA_ENLACES = """Buscas recursos web para una unidad didáctica de 1.º de ESO que se está adaptando a {destino}. Para cada enlace que te pidan, busca entre 1 y 3 páginas que sirvan de verdad para ese rótulo y ese contexto: fuentes oficiales, institucionales o educativas, en {idioma} si existen. Prefiere páginas concretas a portadas. Si una página existe en varias lenguas, busca y devuelve la dirección de su versión en {idioma}, no la inglesa ni la castellana.

Cuando termines, responde solo con un objeto JSON, sin texto antes ni después:
{{"candidatas": [{{"url": "...", "titulo": "...", "motivo": "por qué sirve para este rótulo"}}]}}
Usa solo direcciones que hayas obtenido en los resultados de búsqueda. Si no encuentras nada adecuado, devuelve la lista vacía."""


def buscar_enlaces(rotulo: str, contexto: str, destino: str, idioma: str,
                   dominios: list[str], consumo: Consumo) -> list[dict]:
    """Candidatas para un enlace, ya comprobadas. Nunca se da ninguna por buena.

    La lista de dominios limita la búsqueda a fuentes oficiales y educativas del
    territorio, y además se filtra en código: una URL que no venga de los resultados de
    búsqueda o que caiga fuera de esos dominios se descarta aunque el modelo la proponga.
    """
    if PROVEEDOR == "falso":
        return [{"url": f"https://{dominios[0]}/", "titulo": "Candidata de prueba",
                 "motivo": "prueba", "comprobada": False, "estado": "sin comprobar (modo prueba)"}]

    herramientas = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
                     "allowed_domains": dominios[:60]}]
    system = _SISTEMA_ENLACES.format(destino=destino, idioma=LENGUAS.get(idioma, idioma))
    usuario = f"Rótulo del enlace: «{rotulo}»\nContexto: {contexto}"
    texto, bloques = _llamar(system, usuario, consumo, esfuerzo="medium",
                             max_tokens=16000, herramientas=herramientas)

    encontradas: dict[str, str] = {}
    for bloque in bloques:
        if getattr(bloque, "type", "") != "web_search_tool_result":
            continue
        contenido = getattr(bloque, "content", None)
        if isinstance(contenido, list):  # un error de la herramienta llega como objeto
            for r in contenido:
                if getattr(r, "url", None):
                    encontradas[_normalizar_url(r.url)] = getattr(r, "title", "") or ""

    try:
        propuestas = _json(texto).get("candidatas", [])
    except ErrorIA:
        propuestas = []
    salida = []
    for p in propuestas:
        url = p.get("url") or ""
        if _normalizar_url(url) not in encontradas or not _en_dominios(url, dominios):
            continue
        salida.append({**comprobar_url(url, dominios), "motivo": p.get("motivo", ""),
                       "titulo_busqueda": p.get("titulo") or encontradas[_normalizar_url(url)]})
    return salida[:3]


def _normalizar_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _en_dominios(url: str, dominios: list[str]) -> bool:
    anfitrion = (urlparse(url).hostname or "").lower()
    return any(anfitrion == d or anfitrion.endswith("." + d) for d in dominios)


def comprobar_url(url: str, dominios: list[str]) -> dict:
    """Si la dirección responde y sigue siendo lo que parecía.

    No basta con un 200: muchos sitios oficiales redirigen a la portada cuando la página
    ya no existe, y eso es justo el enlace roto que hay que evitar.
    """
    resultado = {"url": url, "comprobada": False, "estado": "", "titulo": ""}
    if urlparse(url).scheme not in ("http", "https") or not _en_dominios(url, dominios):
        resultado["estado"] = "fuera de los dominios permitidos"
        return resultado
    try:
        r = httpx.get(url, follow_redirects=True, timeout=12,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; revision-enlaces)"})
    except httpx.HTTPError as e:
        resultado["estado"] = f"no responde ({type(e).__name__})"
        return resultado

    final = str(r.url)
    if not _en_dominios(final, dominios):
        resultado["estado"] = f"redirige fuera de los dominios permitidos: {final}"
        return resultado
    if r.status_code != 200:
        resultado["estado"] = f"responde {r.status_code}"
        return resultado
    if urlparse(url).path.strip("/") and not urlparse(final).path.strip("/"):
        resultado["estado"] = "redirige a la portada: la página ya no existe"
        return resultado
    tipo = r.headers.get("content-type", "")
    if "html" in tipo:
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        resultado["titulo"] = " ".join(m.group(1).split())[:200] if m else ""
    elif "pdf" not in tipo:
        resultado["estado"] = f"tipo de contenido inesperado: {tipo}"
        return resultado
    resultado.update(comprobada=True, estado="responde correctamente", url_final=final)
    return resultado


# ------------------------------------------------------------------- utilidades

def sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto or "")
                   if unicodedata.category(c) != "Mn")
