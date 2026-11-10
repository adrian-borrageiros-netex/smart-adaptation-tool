"""Demostrador de adaptación de contenidos.

Dos caras sobre la misma capa de servicio:

- La interfaz que usa el editor, en HTML servido desde el propio servidor.
- Una API REST bajo `/api`, con las mismas operaciones y sin nada exclusivo de la web.

El servidor MCP (`mcp_servidor.py`) es un tercer envoltorio igual de fino. Lo que se
pruebe desde Claude es exactamente lo que hace la pantalla, no un camino paralelo.

    uvicorn web.app:aplicacion --port 8800
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from . import auditoria, bd, learningcloud, servicio
from .horas import fecha_hora, hora
from .trabajador import procesar, zip_vigente

RAIZ = Path(__file__).resolve().parent
plantillas = Jinja2Templates(directory=str(RAIZ / "plantillas"))
plantillas.env.filters["hora"] = hora
plantillas.env.filters["fecha_hora"] = fecha_hora

if os.environ.get("DEMOS_APP"):
    from demos_kit import Config
    from demos_kit.mcp import autenticacion
    from demos_kit.web import Acceso
    CFG = Config.del_entorno()
elif os.environ.get("SARELA_LOCAL") == "1":
    CFG = None
else:
    raise RuntimeError("Sin DEMOS_APP (plataforma) ni SARELA_LOCAL=1 (desarrollo en local): "
                       "no se arranca sin control de acceso.")

# A dónde lleva el logotipo: el portal de aplicaciones (DEMOS_URL_PORTAL). Sin él, a ningún sitio.
plantillas.env.globals["url_portal"] = (CFG.url_portal if CFG is not None
                                        else os.environ.get("DEMOS_URL_PORTAL", "").rstrip("/"))

# El MCP se sirve en /mcp, con la misma autenticación que la interfaz.
import mcp_servidor  # noqa: E402

if CFG is not None:
    mcp_servidor.servidor.auth = autenticacion(CFG)
    # Entrada de cada sesión MCP y herramienta de cada acción, en la auditoría.
    mcp_servidor.servidor.add_middleware(mcp_servidor.AuditoriaMCP())
    # Lee cualquier ruta del servidor: útil en local, inaceptable expuesto. Queda la
    # subida en base64.
    mcp_servidor.servidor.remove_tool("subir_paquete")
mcp_app = mcp_servidor.servidor.http_app(path="/mcp")

aplicacion = FastAPI(title="Sarela · Adaptación de contenidos",
                     description="Sube una unidad, tradúcela o adáptala a una comunidad, "
                                 "revisa los términos pendientes y guarda el resultado.",
                     version="0.2", lifespan=mcp_app.lifespan)
bd.crear_esquema()
auditoria.purgar()
auditoria.iniciar_comun()
# Lo que estaba haciendo el asistente cuando se paró el servidor no sigue: que se vea.
from . import asistente as _asistente  # noqa: E402
_asistente.recuperar_al_arrancar()

def _error(e: servicio.ErrorServicio) -> HTTPException:
    if isinstance(e, servicio.TrabajoOcupado):
        # Lo contestan los manejadores de abajo: 409 en la API y, en la pantalla, de vuelta
        # al trabajo, que explica quién lo tiene.
        raise e
    return HTTPException(status_code=400, detail=str(e))


# ------------------------------------------------------------------ acceso
# En la plataforma de demos (sarela.tech) el acceso es el de Keycloak, a través del kit
# común: inicio de sesión único, roles «admin» y «editor» del cliente de esta app, y la
# misma puerta para la interfaz, la API (Bearer) y el MCP. Los usuarios y sus permisos se
# gestionan en Keycloak, no aquí.
#
# Sin DEMOS_APP solo arranca en modo local, y hay que pedirlo (SARELA_LOCAL=1): así un
# despliegue al que le falte la configuración nunca queda abierto.


class _Contexto:
    """Deja el usuario de la petición donde lo lee el servicio, y apunta entradas y salidas.

    Va por dentro de la puerta del kit, que es quien pone `state.usuario`. Es ASGI puro,
    como la puerta: `BaseHTTPMiddleware` no propaga bien las variables de contexto. El MCP
    no pasa por aquí con usuario (se autentica aparte): ahí el servicio lo saca del token.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.concurrency import run_in_threadpool
        ruta = scope["path"]
        usuario = (scope.get("state") or {}).get("usuario")
        canal = "mcp" if ruta.startswith(("/mcp", "/.well-known")) else (
            "api" if ruta.startswith("/api") else "web")
        ip = (scope.get("client") or (None,))[0]
        sesion = scope.get("session")
        if sesion is not None:
            if usuario and sesion.get("usuario") and sesion.get("auditado") != usuario.get("caduca"):
                # Primera petición de una sesión nueva: es la entrada en la aplicación.
                sesion["auditado"] = usuario.get("caduca")
                await run_in_threadpool(auditoria.registrar, "entrar", usuario=usuario,
                                        canal="web", ip=ip)
            elif ruta == "/auth/salir" and sesion.get("usuario"):
                saliente = sesion["usuario"]
                n = await run_in_threadpool(servicio.dejar_todos, saliente.get("sub"))
                await run_in_threadpool(auditoria.registrar, "salir", usuario=saliente,
                                        canal="web", ip=ip,
                                        detalle=f"Deja {n} trabajos" if n else None)
        fichas = (auditoria.USUARIO.set(usuario), auditoria.CANAL.set(canal),
                  auditoria.IP.set(ip))
        try:
            await self.app(scope, receive, send)
        finally:
            for variable, ficha in zip((auditoria.USUARIO, auditoria.CANAL, auditoria.IP), fichas):
                variable.reset(ficha)


# El primero que se añade queda por dentro: tiene que ver ya lo que pone la puerta.
aplicacion.add_middleware(_Contexto)

if CFG is not None:
    Acceso(CFG).instalar(aplicacion)
else:
    try:
        from demos_kit.web import TEMA
        aplicacion.mount("/_tema", StaticFiles(directory=TEMA), name="tema")
    except ImportError:
        pass

    @aplicacion.middleware("http")
    async def _local(request: Request, siguiente):
        request.state.usuario = auditoria.LOCAL
        return await siguiente(request)


# ------------------------------------------------------------------ interfaz web

def _datos_inicio(trabajos: list[dict], paquetes: list[dict]) -> dict:
    """Lo que necesitan los buscadores de la portada: el curso y la materia en palabras, y
    los valores que se ofrecen en cada desplegable (solo los que aparecen en la lista)."""
    from . import curriculo
    for x in trabajos + paquetes:
        x["curso_texto"] = f"{x['curso']}.º {x['etapa']}" if x.get("curso") and x.get("etapa") else ""
        x["materia_texto"] = curriculo.NOMBRES.get(x.get("materia") or "", "")

    def valores(lista, clave):
        return sorted({x[clave] for x in lista if x.get(clave)})
    return {"filtros": {
        "destinos": valores(trabajos, "destino"), "cursos": valores(trabajos + paquetes, "curso_texto"),
        "materias": valores(trabajos + paquetes, "materia_texto"),
        "autores": valores(trabajos, "creado_por"), "idiomas": valores(paquetes, "idioma_origen")}}


@aplicacion.get("/", response_class=HTMLResponse)
def inicio(request: Request, mios: int = 0):
    trabajos = servicio.listar_trabajos()
    if mios:
        yo = auditoria.firma(_usuario(request))
        trabajos = [t for t in trabajos
                    if (t["en_uso"] and t["en_uso"]["mio"]) or (yo and t["creado_por"] == yo)]
    paquetes = servicio.listar_paquetes()
    return plantillas.TemplateResponse(request, "inicio.html", {
        "request": request, "paquetes": paquetes, "trabajos": trabajos, "mios": mios,
        "opciones": servicio.opciones(), **_datos_inicio(trabajos, paquetes),
    })


@aplicacion.post("/subir")
async def subir(request: Request, fichero: list[UploadFile]):
    try:
        servicio.subir_paquetes([(f.filename or "paquete.zip", await f.read()) for f in fichero])
    except servicio.ErrorServicio as e:
        trabajos, paquetes = servicio.listar_trabajos(), servicio.listar_paquetes()
        return plantillas.TemplateResponse(request, "inicio.html", {
            "request": request, "paquetes": paquetes, "trabajos": trabajos,
            "opciones": servicio.opciones(), **_datos_inicio(trabajos, paquetes),
            "error": str(e)}, status_code=400)
    return RedirectResponse("/#unidades", status_code=303)


# ------------------------------------------------------------------ learningCloud
# Un marcador del navegador, ejecutado en learningCloud con la sesión del usuario, abre la
# página /learningcloud y hace de puente: trae a Sarela el fuente de una carpeta o importa
# allí las unidades adaptadas. La credencial de learningCloud nunca llega aquí. Solo se
# aceptan mensajes de los orígenes de `SARELA_ORIGENES_IMPORTACION`.
ORIGENES_IMPORTACION = learningcloud.ENTORNOS
MARCADOR = RAIZ / "estaticos" / "marcador_learningcloud.js"


def _marcador(sarela: str) -> str:
    """El código del marcador, en una línea y con la dirección de esta Sarela dentro."""
    import re
    from urllib.parse import quote as citar
    codigo = MARCADOR.read_text(encoding="utf-8").replace("__SARELA__", sarela)
    codigo = re.sub(r"/\*.*?\*/", "", codigo, flags=re.S)
    codigo = re.sub(r"\s*\n\s*", " ", codigo).strip()
    return "javascript:" + citar(codigo, safe="(){}[];,=+-*/<>!&|?:.'\"_ ")


def _usuario(request: Request) -> dict:
    return getattr(request.state, "usuario", None) or {}


def _es_admin(request: Request) -> bool:
    return "admin" in _usuario(request).get("roles", [])


# `es_admin` ya lo usan como variable algunas plantillas.
plantillas.env.globals["rol_admin"] = _es_admin


@aplicacion.get("/learningcloud", response_class=HTMLResponse)
def learningcloud_web(request: Request):
    sarela = CFG.url_publica if CFG is not None else str(request.base_url).rstrip("/")
    return plantillas.TemplateResponse(request, "learningcloud.html", {
        "request": request, "marcador": _marcador(sarela), "origenes": ORIGENES_IMPORTACION,
        "ajustes": learningcloud.ajustes(), "enviables": learningcloud.enviables(),
        "es_admin": _es_admin(request)})


@aplicacion.get("/importar")
def importar_web():
    # La página de antes; el marcador antiguo la abría así.
    return RedirectResponse("/learningcloud", status_code=307)


@aplicacion.post("/importar")
async def importar(fichero: list[UploadFile], origen: str = Form("")):
    """Recibe los fuentes que entregó el marcador y los sube como cualquier subida."""
    import json
    try:
        datos_origen = json.loads(origen) if origen else None
        paquete = servicio.subir_paquetes([(f.filename or "fuente.zip", await f.read()) for f in fichero],
                                          origen=datos_origen if isinstance(datos_origen, dict) else None)
    except (servicio.ErrorServicio, ValueError) as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return paquete


@aplicacion.get("/learningcloud/actividad/{trabajo_id}/{fichero}")
def learningcloud_actividad(trabajo_id: int, fichero: str):
    try:
        datos = learningcloud.actividad(trabajo_id, fichero)
    except servicio.ErrorServicio as e:
        raise _error(e)
    return Response(datos, media_type="application/zip")


class Mapa(BaseModel):
    mapa: dict[str, str] = Field(description="Id de curso exportado de cada actividad → id que le "
                                             "ha dado la importación en learningCloud.")


@aplicacion.post("/learningcloud/unidad/{trabajo_id}")
def learningcloud_unidad(trabajo_id: int, cuerpo: Mapa):
    try:
        nombre, datos = learningcloud.unidad_reenlazada(trabajo_id, cuerpo.mapa)
    except servicio.ErrorServicio as e:
        raise _error(e)
    return Response(datos, media_type="application/zip",
                    headers={"X-Nombre": quote(nombre)})


@aplicacion.post("/learningcloud/resultado/{trabajo_id}")
def learningcloud_resultado(request: Request, trabajo_id: int, resultado: dict):
    try:
        return learningcloud.registrar_envio(trabajo_id, resultado,
                                             _usuario(request).get("nombre") or _usuario(request).get("usuario"))
    except servicio.ErrorServicio as e:
        raise _error(e)


@aplicacion.get("/ajustes", response_class=HTMLResponse)
def ajustes_web(request: Request, guardado: int = 0, comprobar: int = 0):
    from . import ia
    return plantillas.TemplateResponse(request, "ajustes.html", {
        "request": request, "ajustes": learningcloud.ajustes(), "comunidades": bd.COMUNIDADES,
        "es_admin": _es_admin(request), "guardado": guardado,
        "minutos_bloqueo": servicio.minutos_bloqueo(),
        "asistente": {"disponible": ia.disponible(), "modelo": ia.MODELO,
                      "clave": ia.estado_clave(forzar=bool(comprobar))}})


@aplicacion.post("/ajustes", response_class=HTMLResponse)
async def guardar_ajustes(request: Request):
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Hace falta el rol admin.")
    formulario = await request.form()
    destinos = {c: {"proyecto": formulario.get(f"proyecto_{i}", ""), "nombre": formulario.get(f"nombre_{i}", "")}
                for i, c in enumerate(bd.COMUNIDADES)}
    try:
        minutos = int(formulario.get("minutos_bloqueo") or servicio.minutos_bloqueo())
    except ValueError:
        minutos = -1
    try:
        servicio.guardar_minutos_bloqueo(minutos)
        learningcloud.guardar_destinos(destinos, _usuario(request).get("nombre") or _usuario(request).get("usuario"))
    except servicio.ErrorServicio as e:
        return plantillas.TemplateResponse(request, "ajustes.html", {
            "request": request, "ajustes": {**learningcloud.ajustes(), "destinos": {
                c: d for c, d in destinos.items() if d["proyecto"]}},
            "comunidades": bd.COMUNIDADES, "es_admin": True, "error": str(e),
            "minutos_bloqueo": minutos if minutos > 0 else servicio.minutos_bloqueo()},
            status_code=400)
    return RedirectResponse("/ajustes?guardado=1", status_code=303)


@aplicacion.post("/procesar")
def lanzar(tareas: BackgroundTasks, paquete_id: int = Form(...), tipo: str = Form(...),
           idioma_destino: str = Form(""), comunidad: str = Form(""),
           regenerar_imagenes: str = Form("")):
    try:
        trabajo = servicio.crear_trabajo(paquete_id, tipo, idioma_destino or None,
                                         comunidad or None, ejecutar=False,
                                         regenerar_imagenes=bool(regenerar_imagenes))
    except servicio.ErrorServicio as e:
        raise _error(e)
    tareas.add_task(procesar, trabajo["id"])
    return RedirectResponse(f"/trabajo/{trabajo['id']}", status_code=303)


def _puede_escribir(trabajo: dict) -> bool:
    """Libre (al escribir se toma) o suyo. Lo de otro, nadie: tampoco un administrador,
    que antes tiene que quitárselo de forma explícita."""
    return trabajo["en_uso"] is None or trabajo["en_uso"]["mio"]


@aplicacion.get("/trabajo/{trabajo_id}", response_class=HTMLResponse)
def detalle(request: Request, trabajo_id: int, auto: int = 0):
    # Abrir un trabajo libre es ponerse a trabajar en él. La recarga automática (`auto`)
    # no cuenta: es la página esperando al asistente, no una persona. Un administrador
    # no lo toma por mirarlo; lo toma al escribir o con el botón.
    if not auto and not _es_admin(request):
        try:
            servicio.tomar_trabajo(trabajo_id)
        except servicio.TrabajoOcupado:
            pass
        except servicio.ErrorServicio as e:
            raise HTTPException(status_code=404, detail=str(e))
    try:
        trabajo = servicio.obtener_trabajo(trabajo_id)
    except servicio.ErrorServicio as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not auto and f"/trabajo/{trabajo_id}" not in request.headers.get("referer", ""):
        auditoria.registrar("abrir", trabajo_id=trabajo_id,
                            detalle=None if _puede_escribir(trabajo) else
                            f"En uso por {trabajo['en_uso']['nombre']}: solo consulta")
    asistente = servicio.estado_asistente(trabajo_id)
    # Contar lo que queda por revisar abre el paquete: no se hace mientras el asistente
    # trabaja, que es cuando la página se recarga sola cada pocos segundos.
    por_revisar = (len(servicio.traducciones_por_revisar(trabajo_id)["frases"])
                   if asistente["estado"] != "en_curso" and trabajo["estado"] == "listo" else None)
    from . import curriculo
    return plantillas.TemplateResponse(request, "trabajo.html", {
        "request": request, "t": trabajo, "ia": asistente, "por_revisar": por_revisar,
        "puede_escribir": _puede_escribir(trabajo), "minutos_bloqueo": servicio.minutos_bloqueo(),
        "materias": curriculo.NOMBRES,
        "puede_borrar": _es_admin(request) or trabajo["creado_por"] == auditoria.firma(),
        "documentos": servicio.documentos_trabajo(trabajo_id) if trabajo["tipo"] == "adaptacion" else []})


@aplicacion.get("/trabajo/{trabajo_id}/imagenes", response_class=HTMLResponse)
def imagenes_web(request: Request, trabajo_id: int):
    trabajo = servicio.obtener_trabajo(trabajo_id)
    if not _puede_escribir(trabajo) and not _es_admin(request):
        return RedirectResponse(f"/trabajo/{trabajo_id}", status_code=303)
    del_experto = [{"documento_id": d["id"], "documento": d["nombre"], **i}
                   for d in servicio.documentos_trabajo(trabajo_id) for i in d["imagenes"]]
    return plantillas.TemplateResponse(request, "imagenes.html", {
        "request": request, "t": trabajo, "imagenes": servicio.imagenes_trabajo(trabajo_id),
        "puede_escribir": _puede_escribir(trabajo), "del_experto": del_experto,
        "ia": servicio.estado_asistente(trabajo_id)})


@aplicacion.post("/trabajo/{trabajo_id}/imagenes/{huella}")
async def sustituir_web(trabajo_id: int, huella: str, fichero: UploadFile,
                        ajuste: str = Form("recortar")):
    try:
        servicio.sustituir_imagen(trabajo_id, huella, fichero.filename or "", await fichero.read(),
                                  ajuste)
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?error={quote(str(e))}#i{huella}",
                                status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?hecho={huella}#i{huella}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/imagenes/{huella}/desde-documento")
def sustituir_desde_documento_web(trabajo_id: int, huella: str, imagen: str = Form(...),
                                  ajuste: str = Form("recortar")):
    documento_id, _, indice = imagen.partition(":")
    try:
        servicio.sustituir_desde_documento(trabajo_id, huella, int(documento_id), int(indice), ajuste)
    except servicio.TrabajoOcupado:
        raise
    except (servicio.ErrorServicio, ValueError) as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?error={quote(str(e))}#i{huella}",
                                status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?hecho={huella}#i{huella}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/documentos")
async def documento_web(trabajo_id: int, fichero: UploadFile):
    try:
        d = servicio.aportar_documento(trabajo_id, fichero.filename or "", await fichero.read())
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}?error_doc={quote(str(e))}#documentacion",
                                status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}?documento={d['id']}#documentacion", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/documentos/texto")
def documento_texto_web(trabajo_id: int, titulo: str = Form(""), texto: str = Form("")):
    try:
        d = servicio.aportar_texto(trabajo_id, titulo, texto)
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}?error_doc={quote(str(e))}#documentacion",
                                status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}?documento={d['id']}#documentacion", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/documentos/{documento_id}/borrar")
def documento_borrar_web(trabajo_id: int, documento_id: int):
    servicio.borrar_documento(trabajo_id, documento_id)
    return RedirectResponse(f"/trabajo/{trabajo_id}#documentacion", status_code=303)


@aplicacion.get("/trabajo/{trabajo_id}/documentos/{documento_id}/imagenes/{indice}")
def documento_imagen_web(trabajo_id: int, documento_id: int, indice: int):
    try:
        return FileResponse(servicio.imagen_documento(trabajo_id, documento_id, indice))
    except servicio.ErrorServicio as e:
        raise HTTPException(status_code=404, detail=str(e))


@aplicacion.get("/trabajo/{trabajo_id}/ficha-experto.docx")
def ficha_experto_web(trabajo_id: int):
    nombre, datos = servicio.ficha_experto(trabajo_id)
    return Response(datos, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename={nombre}"})


@aplicacion.post("/trabajo/{trabajo_id}/imagenes/{huella}/deshacer")
def deshacer_web(trabajo_id: int, huella: str):
    servicio.deshacer_sustitucion(trabajo_id, huella)
    return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes#i{huella}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/configuracion")
def configuracion_web(trabajo_id: int, regenerar_imagenes: str = Form(""),
                      reprocesar: str = Form(""), etapa: str = Form(""), curso: str = Form(""),
                      materia: str = Form("")):
    servicio.configurar_trabajo(trabajo_id, bool(regenerar_imagenes), etapa=etapa,
                                curso=int(curso) if curso.isdigit() else None, materia=materia)
    if reprocesar:
        servicio.reprocesar_trabajo(trabajo_id)
        return RedirectResponse(f"/trabajo/{trabajo_id}?reprocesado=1", status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}?configurado=1", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/tomar")
def tomar_web(request: Request, trabajo_id: int):
    servicio.tomar_trabajo(trabajo_id, forzar=_es_admin(request))
    return RedirectResponse(f"/trabajo/{trabajo_id}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/dejar")
def dejar_web(trabajo_id: int):
    servicio.dejar_trabajo(trabajo_id)
    return RedirectResponse("/", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/termino/{termino_id}/otra")
def otra_propuesta_web(trabajo_id: int, termino_id: int, indicacion: str = Form("")):
    """La página lo pide con fetch y pone la propuesta en la caja: contesta en JSON."""
    try:
        return servicio.otra_propuesta(termino_id, indicacion)
    except servicio.ErrorServicio as e:
        return JSONResponse({"detail": str(e)},
                            status_code=409 if isinstance(e, servicio.TrabajoOcupado) else 400)


@aplicacion.post("/trabajo/{trabajo_id}/borrar")
def borrar_web(trabajo_id: int):
    try:
        servicio.borrar_trabajo(trabajo_id)
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}?error_borrar={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/?borrado={trabajo_id}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/actividad")
def actividad_web(trabajo_id: int):
    """La página de quien lo tiene avisa de que sigue ahí, si la persona la está usando."""
    return servicio.seguir_en_trabajo(trabajo_id)


@aplicacion.post("/trabajo/{trabajo_id}/asistente/{operacion}")
def asistente_web(trabajo_id: int, operacion: str):
    servicio.lanzar_asistente(trabajo_id, operacion)
    return RedirectResponse(f"/trabajo/{trabajo_id}#asistente", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/imagenes/asistente/{operacion}")
def imagenes_asistente_web(trabajo_id: int, operacion: str, huella: str = Form("")):
    """Rehacer o buscar fotos para una imagen (con `huella`) o para todas las que lo piden."""
    if operacion not in ("rehacer_imagenes", "proponer_imagenes"):
        raise HTTPException(status_code=404)
    ancla = f"#i{huella}" if huella else ""
    try:
        datos = servicio.lanzar_asistente(trabajo_id, operacion, [huella] if huella else None)
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?error={quote(str(e))}{ancla}",
                                status_code=303)
    aviso = f"encolada={datos['en_cola']}" if datos.get("en_cola") else "lanzada=1"
    return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?{aviso}{ancla}", status_code=303)


@aplicacion.get("/trabajo/{trabajo_id}/asistente/estado")
def asistente_estado_web(trabajo_id: int):
    """Lo que consulta la página cada pocos segundos para saber si tiene que recargarse."""
    e = servicio.estado_asistente(trabajo_id)
    return {k: e[k] for k in ("estado", "operacion", "mensaje", "hechas", "total", "cola",
                              "imagenes_en_curso", "imagenes_en_cola")}


@aplicacion.post("/trabajo/{trabajo_id}/imagenes/{huella}/commons")
def usar_commons_web(trabajo_id: int, huella: str, indice: int = Form(...),
                     ajuste: str = Form("recortar")):
    try:
        servicio.usar_foto_commons(trabajo_id, huella, indice, ajuste)
    except servicio.TrabajoOcupado:
        raise
    except servicio.ErrorServicio as e:
        return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?error={quote(str(e))}#i{huella}",
                                status_code=303)
    return RedirectResponse(f"/trabajo/{trabajo_id}/imagenes?hecho={huella}#i{huella}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/reprocesar")
def reprocesar_web(trabajo_id: int):
    servicio.reprocesar_trabajo(trabajo_id)
    return RedirectResponse(f"/trabajo/{trabajo_id}?reprocesado=1", status_code=303)


@aplicacion.get("/trabajo/{trabajo_id}/traducciones", response_class=HTMLResponse)
def traducciones_web(request: Request, trabajo_id: int, todas: bool = False):
    trabajo = servicio.obtener_trabajo(trabajo_id)
    if not _puede_escribir(trabajo) and not _es_admin(request):
        return RedirectResponse(f"/trabajo/{trabajo_id}", status_code=303)
    datos = servicio.traducciones_por_revisar(trabajo_id, todas)
    return plantillas.TemplateResponse(request, "traducciones.html", {
        "request": request, "t": trabajo, "frases": datos["frases"], "todas": todas,
        "puede_escribir": _puede_escribir(trabajo)})


@aplicacion.post("/trabajo/{trabajo_id}/traducciones/devolver")
def devolver_web(trabajo_id: int):
    r = servicio.devolver_a_revision(trabajo_id)
    return RedirectResponse(f"/trabajo/{trabajo_id}/traducciones?devueltas={r['devueltas']}",
                            status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/traducciones/aprobar-todas")
def aprobar_todas_web(trabajo_id: int):
    servicio.aprobar_traducciones(trabajo_id)
    return RedirectResponse(f"/trabajo/{trabajo_id}/traducciones?aprobadas=1", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/traducciones/{frase_id:int}")
def aprobar_web(trabajo_id: int, frase_id: int, destino: str = Form(...), todas: bool = False):
    servicio.aprobar_traduccion(frase_id, destino, trabajo_id=trabajo_id)
    vista = "?todas=1" if todas else ""
    return RedirectResponse(f"/trabajo/{trabajo_id}/traducciones{vista}#f{frase_id}", status_code=303)


@aplicacion.get("/salud")
def salud():
    """Para el contenedor: responde si la aplicación y la base de datos están vivas."""
    servicio.listar_paquetes()
    return {"ok": True}


@aplicacion.post("/trabajo/{trabajo_id}/termino/{termino_id}")
def resolver(trabajo_id: int, termino_id: int, valor_final: str = Form(...)):
    try:
        servicio.resolver_termino(termino_id, valor_final)
    except servicio.ErrorServicio as e:
        raise _error(e)
    return RedirectResponse(f"/trabajo/{trabajo_id}#t{termino_id}", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/guardar")
def guardar(trabajo_id: int):
    try:
        servicio.guardar_paquete(trabajo_id)
    except servicio.ErrorServicio as e:
        raise _error(e)
    return RedirectResponse(f"/trabajo/{trabajo_id}?guardado=1", status_code=303)


@aplicacion.post("/trabajo/{trabajo_id}/comentario")
def comentar(trabajo_id: int, texto: str = Form(...), autor: str = Form("")):
    try:
        servicio.anotar_comentario(trabajo_id, texto, autor or None)
    except servicio.ErrorServicio as e:
        raise _error(e)
    return RedirectResponse(f"/trabajo/{trabajo_id}#comentarios", status_code=303)


@aplicacion.get("/auditoria")
def auditoria_web(request: Request, usuario: str = "", accion: str = "", trabajo: str = "",
                  desde: str = "", hasta: str = "", denegados: int = 0, formato: str = ""):
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Hace falta el rol admin.")
    import datetime as dt

    def dia(texto: str):
        try:
            return dt.date.fromisoformat(texto) if texto else None
        except ValueError:
            return None
    filtros = {"usuario": usuario, "accion": accion, "trabajo": trabajo, "desde": desde,
               "hasta": hasta, "denegados": denegados}
    filas = auditoria.consultar(usuario or None, accion or None,
                                int(trabajo) if trabajo.isdigit() else None,
                                dia(desde), dia(hasta), bool(denegados),
                                limite=20000 if formato == "csv" else 500)
    if formato == "csv":
        return Response(auditoria.csv_de(filas), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=auditoria.csv"})
    return plantillas.TemplateResponse(request, "auditoria.html", {
        "request": request, "filas": filas, "filtros": filtros,
        "opciones": auditoria.opciones_filtro(),
        "conservacion": auditoria.CONSERVACION.days,
        "query": request.url.query})


@aplicacion.get("/curriculos", response_class=HTMLResponse)
def curriculos_web(request: Request, comunidad: str = "", etapa: str = "", materia: str = "",
                   curso: int = 0):
    """Los currículos convertidos: qué hay de cada comunidad y el trozo que recibe el
    asistente para una materia y un curso."""
    from . import curriculo
    comunidades = [c for c in curriculo.FICHEROS if curriculo.disponible(c)]
    comunidad = comunidad if comunidad in comunidades else (comunidades[0] if comunidades else "")
    extracto = None
    if comunidad and etapa and materia and curso:
        texto, ficha = curriculo.para_trabajo(comunidad, etapa, materia, curso)
        extracto = {"texto": texto, "ficha": ficha}
    return plantillas.TemplateResponse(request, "curriculos.html", {
        "request": request, "comunidades": comunidades, "comunidad": comunidad,
        "materias": curriculo.materias(comunidad) if comunidad else [],
        "extracto": extracto, "elegido": {"etapa": etapa, "materia": materia, "curso": curso}})


@aplicacion.get("/curriculos/{comunidad}.xlsx")
def curriculo_excel(comunidad: str):
    from . import curriculo
    if not curriculo.disponible(comunidad):
        raise HTTPException(status_code=404, detail="No hay currículo de esa comunidad.")
    auditoria.registrar("descargar currículo", detalle=comunidad)
    return Response(curriculo.excel(comunidad),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('curriculo-' + comunidad + '.xlsx')}"})


@aplicacion.get("/memoria", response_class=HTMLResponse)
def memoria_web(request: Request, buscar: str = ""):
    return plantillas.TemplateResponse(request, "memoria.html", {
        "request": request, "resumen": servicio.estado_memoria(),
        "buscar": buscar,
        "resultados": servicio.buscar_en_memoria(buscar) if buscar else None})


@aplicacion.get("/comentarios", response_class=HTMLResponse)
def comentarios(request: Request):
    return plantillas.TemplateResponse(request, "comentarios.html", {
        "request": request, "comentarios": servicio.listar_comentarios()})


# --------------------------------------------------------- servir los paquetes

def _servir(base: Path, resto: str) -> FileResponse:
    destino = (base / resto).resolve()
    if not str(destino).startswith(str(base.resolve())) or not destino.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(destino)


def _servir_vista(base: Path, resto: str, docente: bool) -> Response:
    """Un fichero del paquete; con `docente`, el índice con la parte del profesorado visible."""
    if not docente or (resto or "index.html") != "index.html":
        return _servir(base, resto or "index.html")
    from maker_paquete.reproductor import modo_docente
    indice = base / "index.html"
    if not indice.is_file():
        raise HTTPException(status_code=404, detail="No encontrado")
    return HTMLResponse(modo_docente(indice.read_text(encoding="utf-8")))


@aplicacion.get("/contenido/{trabajo_id}/{resto:path}")
def contenido(trabajo_id: int, resto: str, docente: int = 0):
    with bd.Sesion() as s:
        trabajo = s.get(bd.Trabajo, trabajo_id)
        if trabajo is None or not trabajo.ruta_resultado:
            raise HTTPException(status_code=404, detail="Trabajo sin resultado")
        return _servir_vista(bd.ruta(trabajo.ruta_resultado), resto, bool(docente))


@aplicacion.get("/original/{paquete_id}/{resto:path}")
def original(paquete_id: int, resto: str, docente: int = 0):
    with bd.Sesion() as s:
        paquete = s.get(bd.Paquete, paquete_id)
        if paquete is None:
            raise HTTPException(status_code=404, detail="No existe el paquete")
        return _servir_vista(bd.ruta(paquete.ruta_extraido), resto, bool(docente))


@aplicacion.get("/informe/{trabajo_id}", response_class=HTMLResponse)
def informe(trabajo_id: int):
    with bd.Sesion() as s:
        trabajo = s.get(bd.Trabajo, trabajo_id)
        if trabajo is None or not trabajo.ruta_informe:
            raise HTTPException(status_code=404, detail="Todavía no hay informe")
        return HTMLResponse(bd.ruta(trabajo.ruta_informe).read_text(encoding="utf-8"))


@aplicacion.get("/descargar/{trabajo_id}")
def descargar(request: Request, trabajo_id: int):
    trabajo = servicio.obtener_trabajo(trabajo_id)
    if not _puede_escribir(trabajo) and not _es_admin(request):
        auditoria.registrar("descargar", trabajo_id=trabajo_id, resultado="denegado",
                            detalle=f"En uso por {trabajo['en_uso']['nombre']}")
        raise HTTPException(status_code=409, detail=str(servicio.TrabajoOcupado(
            trabajo_id, trabajo["en_uso"]["nombre"], trabajo["en_uso"]["desde"])))
    auditoria.registrar("descargar", trabajo_id=trabajo_id)
    with bd.Sesion() as s:
        trabajo = s.get(bd.Trabajo, trabajo_id)
        if trabajo is None or not trabajo.ruta_resultado:
            raise HTTPException(status_code=404, detail="Trabajo sin resultado")
        destino = bd.ruta(trabajo.ruta_resultado)
        nombre = f"{trabajo.paquete.nombre.removesuffix('.zip')}-{trabajo.id}.zip"
    zip_final = zip_vigente(destino)
    return FileResponse(zip_final, filename=nombre, media_type="application/zip")


# --------------------------------------------------------------------- API REST

api = FastAPI(title="API del demostrador", version="0.1")


# Los cuerpos de las operaciones que escriben van declarados, para que el MCP, la API y
# cualquier otro cliente manden la misma forma y quede descrita sola en /api/docs.

class Encargo(BaseModel):
    paquete_id: int
    tipo: str = Field(description="«traduccion» o «adaptacion».")
    idioma_destino: str | None = None
    comunidad: str | None = Field(default=None, description="Solo en una adaptación.")
    regenerar_imagenes: bool = Field(default=True, description="Rehacer las imágenes con texto.")


class Decision(BaseModel):
    valor_final: str
    marcar_revisado: bool = True


@api.get("/opciones", summary="Idiomas y comunidades admitidos")
def api_opciones():
    return servicio.opciones()


@api.get("/paquetes", summary="Paquetes subidos")
def api_paquetes():
    return servicio.listar_paquetes()


@api.post("/paquetes", summary="Subir un paquete Maker, o el fuente de Author con sus actividades")
async def api_subir(fichero: list[UploadFile]):
    try:
        return servicio.subir_paquetes([(f.filename or "paquete.zip", await f.read()) for f in fichero])
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.get("/trabajos", summary="Trabajos, opcionalmente por estado")
def api_trabajos(estado: str | None = None):
    return servicio.listar_trabajos(estado)


@api.post("/trabajos", summary="Encargar una traducción o una adaptación")
def api_crear(cuerpo: Encargo):
    try:
        return servicio.crear_trabajo(cuerpo.paquete_id, cuerpo.tipo,
                                      cuerpo.idioma_destino, cuerpo.comunidad,
                                      regenerar_imagenes=cuerpo.regenerar_imagenes)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.get("/trabajos/{trabajo_id}", summary="Detalle, con términos pendientes y comentarios")
def api_trabajo(trabajo_id: int):
    try:
        return servicio.obtener_trabajo(trabajo_id)
    except servicio.ErrorServicio as e:
        raise HTTPException(status_code=404, detail=str(e))


@api.post("/terminos/{termino_id}", summary="Resolver un término pendiente")
def api_resolver(termino_id: int, cuerpo: Decision):
    try:
        return servicio.resolver_termino(termino_id, cuerpo.valor_final,
                                         cuerpo.marcar_revisado)
    except servicio.ErrorServicio as e:
        raise _error(e)



# Los cuerpos de las operaciones que escriben van declarados: así el MCP, la API y cualquier
# cliente mandan la misma forma, y queda descrita sola en /api/docs.

class PeticionPropuesta(BaseModel):
    indicacion: str = Field(default="", description="Qué cambiar respecto a la anterior (opcional).")


@api.post("/terminos/{termino_id}/otra-propuesta",
          summary="Pedir al asistente otra propuesta para un término (no lo decide)")
def api_otra_propuesta(termino_id: int, cuerpo: PeticionPropuesta):
    return servicio.otra_propuesta(termino_id, cuerpo.indicacion)


class Traduccion(BaseModel):
    id: str = Field(description="Identificador de la frase, tal y como lo devuelve el lote.")
    destino: str = Field(description="El texto traducido.")


class LoteTraducido(BaseModel):
    traducciones: list[Traduccion]
    autor: str | None = Field(default=None, description="Quién lo ha traducido.")


class Feedback(BaseModel):
    texto: str
    autor: str | None = None


@api.post("/trabajos/{trabajo_id}/guardar", summary="Aplicar las decisiones y empaquetar")
def api_guardar(trabajo_id: int):
    try:
        return servicio.guardar_paquete(trabajo_id)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.post("/trabajos/{trabajo_id}/comentarios", summary="Registrar feedback del editor")
def api_comentar(trabajo_id: int, cuerpo: Feedback):
    try:
        return servicio.anotar_comentario(trabajo_id, cuerpo.texto, cuerpo.autor)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.get("/trabajos/{trabajo_id}/lote", summary="Frases pendientes de traducir")
def api_lote(trabajo_id: int, tamano: int = 40):
    try:
        return servicio.pedir_lote(trabajo_id, tamano)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.post("/trabajos/{trabajo_id}/lote", summary="Entregar un lote traducido")
def api_entregar(trabajo_id: int, cuerpo: LoteTraducido):
    try:
        return servicio.entregar_lote(
            trabajo_id, [t.model_dump() for t in cuerpo.traducciones], cuerpo.autor)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.post("/trabajos/{trabajo_id}/reprocesar", summary="Reprocesar con la memoria actual")
def api_reprocesar(trabajo_id: int):
    try:
        return servicio.reprocesar_trabajo(trabajo_id)
    except servicio.ErrorServicio as e:
        raise _error(e)


@api.get("/memoria", summary="Qué sabe el sistema: frases y equivalencias")
def api_memoria():
    return servicio.estado_memoria()


@api.get("/memoria/buscar", summary="Buscar en la memoria")
def api_memoria_buscar(texto: str, limite: int = 25):
    return servicio.buscar_en_memoria(texto, limite)


@api.get("/comentarios", summary="Feedback recogido, para la próxima iteración del toolkit")
def api_comentarios(solo_sin_aplicar: bool = False):
    return servicio.listar_comentarios(solo_sin_aplicar)


class Aprobacion(BaseModel):
    destino: str | None = Field(default=None, description="La traducción corregida; vacío para darla por buena tal cual.")
    autor: str | None = None


@api.get("/trabajos/{trabajo_id}/asistente", summary="Qué está haciendo el asistente y cuánto ha costado")
def api_asistente(trabajo_id: int):
    return servicio.estado_asistente(trabajo_id)


@api.post("/trabajos/{trabajo_id}/asistente/{operacion}",
          summary="Lanzar el asistente: traducir, revisar, analizar, proponer, enlaces, "
                  "rehacer_imagenes o proponer_imagenes")
def api_lanzar_asistente(trabajo_id: int, operacion: str, huella: list[str] | None = Query(None)):
    return servicio.lanzar_asistente(trabajo_id, operacion, huella)


class FotoCommons(BaseModel):
    indice: int = Field(description="Posición de la foto en las propuestas de esa imagen.")
    ajuste: str = Field(default="recortar", description="«recortar» o «encajar».")


@api.post("/trabajos/{trabajo_id}/imagenes/{huella}/commons",
          summary="Sustituir una imagen por una foto de Commons propuesta por el asistente")
def api_usar_commons(trabajo_id: int, huella: str, cuerpo: FotoCommons):
    return servicio.usar_foto_commons(trabajo_id, huella, cuerpo.indice, cuerpo.ajuste)


@api.get("/trabajos/{trabajo_id}/traducciones", summary="Traducciones automáticas sin revisar")
def api_traducciones(trabajo_id: int):
    return servicio.traducciones_por_revisar(trabajo_id)


@api.post("/traducciones/{frase_id}", summary="Aprobar una traducción automática")
def api_aprobar(frase_id: int, cuerpo: Aprobacion):
    return servicio.aprobar_traduccion(frase_id, cuerpo.destino, cuerpo.autor)


@api.post("/trabajos/{trabajo_id}/traducciones/aprobar", summary="Aprobar todas las de un trabajo")
def api_aprobar_todas(trabajo_id: int):
    return servicio.aprobar_traducciones(trabajo_id)


class TextoExperto(BaseModel):
    titulo: str = ""
    texto: str


@api.get("/trabajos/{trabajo_id}/documentos", summary="Documentación del experto del trabajo")
def api_documentos(trabajo_id: int):
    return servicio.documentos_trabajo(trabajo_id)


@api.post("/trabajos/{trabajo_id}/documentos", summary="Aportar un documento (Word, PDF o texto; también la ficha rellena)")
async def api_aportar_documento(trabajo_id: int, fichero: UploadFile):
    return servicio.aportar_documento(trabajo_id, fichero.filename or "", await fichero.read())


@api.post("/trabajos/{trabajo_id}/documentos/texto", summary="Aportar documentación pegada como texto")
def api_aportar_texto(trabajo_id: int, cuerpo: TextoExperto):
    return servicio.aportar_texto(trabajo_id, cuerpo.titulo, cuerpo.texto)


@api.delete("/trabajos/{trabajo_id}/documentos/{documento_id}", summary="Borrar un documento")
def api_borrar_documento(trabajo_id: int, documento_id: int):
    return servicio.borrar_documento(trabajo_id, documento_id)


@api.get("/trabajos/{trabajo_id}/ficha-experto", summary="La ficha en Word para el experto")
def api_ficha(trabajo_id: int):
    nombre, datos = servicio.ficha_experto(trabajo_id)
    return Response(datos, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename={nombre}"})


class Configuracion(BaseModel):
    regenerar_imagenes: bool | None = Field(default=None, description="Rehacer las imágenes con texto al procesar.")
    etapa: str | None = Field(default=None, description="«ESO» o «Primaria».")
    curso: int | None = None
    materia: str | None = Field(default=None, description="Id común de la materia (biologia_geologia, lengua…).")


@api.get("/trabajos/{trabajo_id}/imagenes", summary="Imágenes de la unidad y sus sustituciones")
def api_imagenes(trabajo_id: int):
    return servicio.imagenes_trabajo(trabajo_id)


@api.post("/trabajos/{trabajo_id}/imagenes/{huella}", summary="Sustituir una imagen de la unidad")
async def api_sustituir(trabajo_id: int, huella: str, fichero: UploadFile,
                        ajuste: str = Form("recortar")):
    return servicio.sustituir_imagen(trabajo_id, huella, fichero.filename or "",
                                     await fichero.read(), ajuste)


@api.delete("/trabajos/{trabajo_id}/imagenes/{huella}", summary="Volver a la imagen original")
def api_deshacer(trabajo_id: int, huella: str):
    return servicio.deshacer_sustitucion(trabajo_id, huella)


@api.post("/trabajos/{trabajo_id}/configuracion", summary="Opciones del trabajo (se aplican al reprocesar)")
def api_configurar(trabajo_id: int, cuerpo: Configuracion):
    return servicio.configurar_trabajo(trabajo_id, cuerpo.regenerar_imagenes, etapa=cuerpo.etapa,
                                       curso=cuerpo.curso, materia=cuerpo.materia)


@api.post("/trabajos/{trabajo_id}/tomar", summary="Ponerse a trabajar en un trabajo libre")
def api_tomar(trabajo_id: int):
    return servicio.tomar_trabajo(trabajo_id)


@api.post("/trabajos/{trabajo_id}/dejar", summary="Dejar un trabajo para que lo pueda tomar otra persona")
def api_dejar(trabajo_id: int):
    return servicio.dejar_trabajo(trabajo_id)


@api.delete("/trabajos/{trabajo_id}", summary="Borrar un trabajo (quien lo encargó o admin; deja copia)")
def api_borrar(trabajo_id: int):
    return servicio.borrar_trabajo(trabajo_id)


@api.get("/auditoria", summary="Registro de auditoría (rol admin)")
def api_auditoria(request: Request, usuario: str | None = None, accion: str | None = None,
                  trabajo_id: int | None = None, limite: int = 500):
    if not _es_admin(request):
        raise HTTPException(status_code=403, detail="Hace falta el rol admin.")
    return auditoria.consultar(usuario, accion, trabajo_id, limite=min(limite, 5000))


@api.exception_handler(servicio.ErrorServicio)
def _manejar_api(request: Request, e: servicio.ErrorServicio):
    return JSONResponse({"detail": str(e)},
                        status_code=409 if isinstance(e, servicio.TrabajoOcupado) else 400)


aplicacion.mount("/api", api)


@aplicacion.exception_handler(servicio.ErrorServicio)
def _manejar(request: Request, e: servicio.ErrorServicio):
    if isinstance(e, servicio.TrabajoOcupado):
        # Desde la pantalla, de vuelta al trabajo, que explica quién lo tiene.
        return RedirectResponse(f"/trabajo/{e.trabajo_id}?ocupado=1", status_code=303)
    return JSONResponse({"detail": str(e)}, status_code=400)


# El MCP se monta al final: un montaje en «/» delante de las rutas las taparía.
aplicacion.mount("/", mcp_app)
