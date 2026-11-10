"""Servidor MCP del demostrador: las mismas operaciones, desde una conversación.

Es un envoltorio fino sobre `web.servicio`, igual que la interfaz web y la API REST. No
hay lógica aquí: lo que se haga desde Claude es exactamente lo que hace la pantalla, con
la misma base de datos y la misma memoria de adaptación.

En local, para Claude Desktop o Claude Code:

    {"mcpServers": {"sarela": {"command": "python",
      "args": ["-m", "mcp_servidor"], "cwd": "<ruta del proyecto>"}}}

Y sobre HTTP, cuando haya que compartirlo desde el VPS:

    python mcp_servidor.py --http --puerto 8801
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncio  # noqa: E402
from collections import OrderedDict  # noqa: E402

from fastmcp import FastMCP  # noqa: E402
from fastmcp.server.middleware import Middleware  # noqa: E402

from web import auditoria, servicio  # noqa: E402

servidor = FastMCP(
    name="sarela-contenidos",
    instructions=(
        "Adapta unidades de contenido educativo Maker: traducirlas a otro idioma o "
        "adaptarlas al currículo de otra comunidad autónoma. El flujo natural es: "
        "`opciones` para ver los destinos admitidos, `listar_paquetes` para ver qué hay "
        "subido, `encargar_trabajo` para procesarlo, `ver_trabajo` para leer el resultado "
        "y los términos que necesitan decisión editorial, `resolver_termino` para fijar "
        "cada uno y `guardar_paquete` para aplicarlos. "
        "Los términos pendientes son deliberados: el sistema no inventa equivalencias "
        "territoriales, atribuciones institucionales ni enlaces externos. Pregunta a la "
        "persona antes de resolverlos por tu cuenta. "
        "Cambiar algo en un trabajo es ponerse a trabajar en él: mientras tanto nadie más "
        "puede cambiarlo. Si otra persona lo tiene, la operación falla y lo dice; "
        "`ver_trabajo` enseña quién lo tiene en `en_uso`. Al terminar, `dejar_trabajo`."
    ),
)


class AuditoriaMCP(Middleware):
    """La auditoría del MCP. Lo que cambia algo ya lo registra `servicio`, igual que desde
    la pantalla; esto añade la entrada de cada sesión MCP (una vez, como el inicio de sesión
    en la web) y deja el nombre de la herramienta para que la auditoría común diga por dónde
    llegó cada acción. Las consultas no se registran, como en la web.

    Solo en la plataforma (`web/app.py`): por stdio en local no hay a quién atribuirlo."""

    MAXIMO = 5000

    def __init__(self) -> None:
        self._vistas: OrderedDict[str, None] = OrderedDict()

    async def on_request(self, context, call_next):
        sesion, cliente = None, ""
        try:
            ctx = context.fastmcp_context
            sesion = ctx.session_id if ctx is not None else None
            info = getattr(getattr(ctx.session, "client_params", None), "clientInfo", None)
            cliente = f"{info.name} {info.version}".strip() if info else ""
        except Exception:  # noqa: BLE001 — sin sesión (o sin datos del cliente), no se anota
            pass
        if sesion and sesion not in self._vistas:
            self._vistas[sesion] = None
            while len(self._vistas) > self.MAXIMO:
                self._vistas.popitem(last=False)
            usuario = auditoria.usuario_actual()
            if usuario:
                await asyncio.to_thread(auditoria.registrar, "entrar", usuario=usuario, canal="mcp",
                                        detalle=f"Cliente MCP: {cliente}" if cliente else None)
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        ficha = auditoria.HERRAMIENTA.set(getattr(context.message, "name", None))
        try:
            return await call_next(context)
        finally:
            auditoria.HERRAMIENTA.reset(ficha)


@servidor.tool
def opciones() -> dict[str, Any]:
    """Idiomas y comunidades autónomas que admite el sistema."""
    return servicio.opciones()


@servidor.tool
def listar_paquetes() -> list[dict[str, Any]]:
    """Unidades subidas, con su ficha: título, idioma, plantilla y número de componentes."""
    return servicio.listar_paquetes()


@servidor.tool
def subir_paquete(ruta: str) -> dict[str, Any]:
    """Sube un paquete Maker en zip desde una ruta local y devuelve su ficha.

    El zip original se guarda sin modificar: todo lo que se genera sale de una copia.
    """
    fichero = Path(ruta)
    if not fichero.is_file():
        raise ValueError(f"No encuentro el fichero: {ruta}")
    return servicio.subir_paquete(fichero.name, fichero.read_bytes())


@servidor.tool
def subir_paquete_base64(nombre: str, contenido_base64: str) -> dict[str, Any]:
    """Igual que `subir_paquete`, para cuando el zip no está en el disco del servidor."""
    return servicio.subir_paquete(nombre, base64.b64decode(contenido_base64))


@servidor.tool
def listar_trabajos(estado: str | None = None) -> list[dict[str, Any]]:
    """Trabajos encargados. `estado` admite: encolado, procesando, listo o error."""
    return servicio.listar_trabajos(estado)


@servidor.tool
def encargar_trabajo(paquete_id: int, tipo: str, idioma_destino: str | None = None,
                     comunidad: str | None = None) -> dict[str, Any]:
    """Procesa un paquete y devuelve el resultado.

    `tipo` es «traduccion» (con `idioma_destino`: es, ca, gl, eu, fr, en, pt) o
    «adaptacion» (con `comunidad`, una de las 17 autonomías). Procesa en el momento,
    así que puede tardar unos segundos con unidades grandes.
    """
    return servicio.crear_trabajo(paquete_id, tipo, idioma_destino, comunidad)


@servidor.tool
def ver_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Detalle de un trabajo: cifras, enlaces, términos pendientes y feedback recogido."""
    return servicio.obtener_trabajo(trabajo_id)


@servidor.tool
def resolver_termino(termino_id: int, valor_final: str) -> dict[str, Any]:
    """Fija el texto definitivo de un término pendiente y lo marca como revisado.

    Son decisiones editoriales con consecuencias —un ISBN, una atribución institucional,
    un equivalente territorial—, así que conviene confirmarlas con la persona antes de
    escribirlas.
    """
    return servicio.resolver_termino(termino_id, valor_final)


@servidor.tool
def otra_propuesta(termino_id: int, indicacion: str = "") -> dict[str, Any]:
    """Pide al asistente una propuesta nueva para un término de una adaptación.

    `indicacion` orienta la nueva («más corta», «con ejemplos de Málaga»…). La propuesta se
    guarda como propuesta, no como decisión: el término sigue como estaba.
    """
    return servicio.otra_propuesta(termino_id, indicacion)


@servidor.tool
def guardar_paquete(trabajo_id: int) -> dict[str, Any]:
    """Aplica al paquete los términos resueltos y lo deja listo para descargar."""
    return servicio.guardar_paquete(trabajo_id)


@servidor.tool
def anotar_feedback(trabajo_id: int, texto: str, autor: str | None = None) -> dict[str, Any]:
    """Registra una observación del revisor sobre el resultado.

    No es una nota suelta: el feedback acumulado es la entrada de la próxima versión del
    procesador.
    """
    return servicio.anotar_comentario(trabajo_id, texto, autor)


@servidor.tool
def listar_feedback(solo_sin_aplicar: bool = False) -> list[dict[str, Any]]:
    """Feedback recogido en todos los trabajos, para preparar la siguiente iteración."""
    return servicio.listar_comentarios(solo_sin_aplicar)


@servidor.tool
def pedir_lote(trabajo_id: int, tamano: int = 40) -> dict[str, Any]:
    """Pide un lote de frases que el sistema todavía no sabe traducir.

    Vienen sin repetir y en orden de lectura, cada una con su lección, el tipo de
    componente y cuántas veces aparece en la unidad. Traduce **el texto tal cual**,
    conservando los espacios duros (`&nbsp;`), los números y la puntuación. Si una frase
    es un rótulo de plantilla sin sentido («Block Title») o ya está en el idioma de
    destino, devuélvela igual: se descarta sola.

    Devuelve las traducciones con `entregar_lote` usando el mismo `id` de cada frase.
    """
    return servicio.pedir_lote(trabajo_id, tamano)


@servidor.tool
def entregar_lote(trabajo_id: int, traducciones: list[dict[str, str]],
                  autor: str | None = None) -> dict[str, Any]:
    """Entrega un lote traducido: `[{"id": "...", "destino": "..."}]`.

    Las traducciones se guardan en la memoria del sistema —así sirven para las próximas
    unidades, no solo para esta— y el trabajo se reprocesa al momento. Devuelve la nueva
    cobertura, de modo que se puede encadenar con otro `pedir_lote` hasta terminar.
    """
    return servicio.entregar_lote(trabajo_id, traducciones, autor)


@servidor.tool
def reprocesar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Vuelve a procesar un trabajo con la memoria actual, sin perder lo ya decidido."""
    return servicio.reprocesar_trabajo(trabajo_id)


@servidor.tool
def lanzar_asistente(trabajo_id: int, operacion: str,
                     huellas: list[str] | None = None) -> dict[str, Any]:
    """Pone a trabajar al asistente del servidor en segundo plano.

    `operacion` es «traducir» (todo lo que la memoria no conoce), «revisar» (lee la unidad
    entera y marca lo que depende del territorio aunque no esté en la lista de topónimos),
    «analizar» (plan de adaptación frente al currículo de destino), «proponer» (propuestas
    para los puntos territoriales de una adaptación), «enlaces» (candidatos comprobados
    para los enlaces pendientes), «rehacer_imagenes» (localiza con el asistente los rótulos
    que no se encuentran y rehace esas imágenes) o «proponer_imagenes» (fotos de Wikimedia
    Commons con licencia libre para las imágenes del territorio de origen). `huellas`
    limita las dos de imágenes a esas imágenes. Vuelve enseguida; consulta el avance con
    `estado_asistente`. Nada queda aprobado: las traducciones entran por revisar y lo
    demás son propuestas.
    """
    return servicio.lanzar_asistente(trabajo_id, operacion, huellas)


@servidor.tool
def estado_asistente(trabajo_id: int) -> dict[str, Any]:
    """Avance del asistente en un trabajo, su último resumen y el coste acumulado."""
    return servicio.estado_asistente(trabajo_id)


@servidor.tool
def aportar_documentacion(trabajo_id: int, texto: str, titulo: str = "") -> dict[str, Any]:
    """Aporta documentación de experto a un trabajo de adaptación: datos de la comunidad de
    destino, ejemplos, enlaces o actividades. El asistente la usa como fuente prioritaria al
    revisar, analizar y proponer. Para un Word o un PDF, súbelo desde la pantalla del trabajo.
    """
    return servicio.aportar_texto(trabajo_id, titulo, texto)


@servidor.tool
def dejar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Deja el trabajo libre para que otra persona pueda trabajar en él.

    Si no se deja, queda libre solo tras un rato sin actividad (lo fija Ajustes).
    """
    return servicio.dejar_trabajo(trabajo_id)


@servidor.tool
def borrar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Borra un trabajo que ya no sirve, con sus términos y sus ficheros.

    Solo quien lo encargó o un administrador. Antes deja una copia en el servidor, pero
    es irreversible desde la aplicación: confírmalo con la persona antes de llamarlo.
    """
    return servicio.borrar_trabajo(trabajo_id)


@servidor.tool
def traducciones_por_revisar(trabajo_id: int) -> dict[str, Any]:
    """Traducciones automáticas de la unidad que nadie ha confirmado todavía."""
    return servicio.traducciones_por_revisar(trabajo_id)


@servidor.tool
def aprobar_traduccion(frase_id: int, destino: str | None = None) -> dict[str, Any]:
    """Confirma una traducción automática; con `destino`, corregida.

    Solo lo aprobado sirve de terminología de referencia para las unidades siguientes,
    así que conviene confirmarlo con la persona antes de aprobar en bloque.
    """
    return servicio.aprobar_traduccion(frase_id, destino)


@servidor.tool
def estado_memoria() -> dict[str, Any]:
    """Qué sabe el sistema: cuántas frases traducidas y cuántas equivalencias territoriales.

    La memoria está indexada por contenido, así que lo aprendido en una unidad se aplica a
    las siguientes.
    """
    return servicio.estado_memoria()


@servidor.tool
def buscar_en_memoria(texto: str, limite: int = 25) -> dict[str, Any]:
    """Busca un texto en las memorias de traducción y de adaptación territorial."""
    return servicio.buscar_en_memoria(texto, limite)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--http", action="store_true", help="Servir por HTTP en vez de stdio.")
    p.add_argument("--puerto", type=int, default=8801)
    args = p.parse_args()
    if args.http:
        servidor.run(transport="http", host="127.0.0.1", port=args.puerto)
    else:
        servidor.run()


if __name__ == "__main__":
    main()
