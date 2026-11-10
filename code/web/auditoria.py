"""Quién hace qué: el usuario de cada petición y el registro de auditoría.

La interfaz, la API y el MCP llaman a las mismas funciones de `servicio`, y es ahí donde
se registra y se comprueba el bloqueo. Para que el servicio sepa quién llama sin pasarle
el usuario por todas partes, cada petición lo deja en una variable de contexto:

- La interfaz y la API, desde el middleware de `app.py`, con lo que puso la puerta del
  kit de la plataforma (sesión o Bearer de Keycloak).
- El MCP, desde el token que presenta el cliente (`get_access_token` de FastMCP).

Qué se registra: la entrada en la aplicación (una vez por sesión, también por MCP), las
acciones que cambian algo y cada intento bloqueado. Las consultas no: la página de un
trabajo se recarga sola cada pocos segundos mientras trabaja el asistente. Los intentos
fallidos de inicio de sesión no llegan aquí; los guarda Keycloak (eventos del realm `demos`).

Cada evento va a dos sitios:

- La tabla `auditoria` de la app, con el detalle en texto: es la pantalla /auditoria.
- El servicio de auditoría común de la plataforma (Ajustes → Auditoría), con su contrato
  (`docs/AUDITORIA.md` de demos-plataforma): acción `objeto.verbo` y sin texto libre. Solo
  si hay `DEMOS_AUDITORIA_URL`. Sale desde un hilo propio, así que la petición no espera, y
  si el servicio no responde el kit lo deja en su cola en disco y lo reintenta.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import logging
import os
import re
import threading
import unicodedata
from contextvars import ContextVar
from typing import Any

from . import bd

# Guarda nombre, correo e IP de personas: se conserva un año y se borra lo anterior.
CONSERVACION = dt.timedelta(days=365)

USUARIO: ContextVar[dict | None] = ContextVar("usuario", default=None)
CANAL: ContextVar[str | None] = ContextVar("canal", default=None)
IP: ContextVar[str | None] = ContextVar("ip", default=None)
# La herramienta del MCP que se está ejecutando, para que la auditoría común diga por dónde
# llegó cada acción. La pone el middleware de `mcp_servidor`.
HERRAMIENTA: ContextVar[str | None] = ContextVar("herramienta", default=None)

registro = logging.getLogger(__name__)

# Fuera de la plataforma (desarrollo en local, MCP por stdio) no hay Keycloak: todo lo hace
# este usuario, administrador.
LOCAL = {"sub": "local", "usuario": "local", "nombre": "Local", "roles": ["admin"],
         "apps": [], "roles_apps": {}, "admin_plataforma": False, "local": True}


def usuario_actual() -> dict | None:
    """Quién hace la petición en curso, venga de la web, de la API o del MCP."""
    usuario = USUARIO.get()
    if usuario:
        return usuario
    acceso = _token_mcp()
    if acceso is None:
        return None if os.environ.get("DEMOS_APP") else LOCAL
    from demos_kit.web import usuario_de_claims
    return usuario_de_claims(acceso.claims, os.environ.get("DEMOS_APP", "adaptacion"))


def _token_mcp():
    """El token de quien llama a una herramienta del MCP.

    Primero el de la petición HTTP de ese mensaje, que FastMCP sigue por mensaje; si no,
    el del contexto de autenticación, que en una sesión con estado es el de la petición
    que la abrió (el mismo usuario: la sesión va ligada a su token).
    """
    try:
        from fastmcp.server.dependencies import get_access_token, get_http_request
    except ImportError:
        return None
    try:
        peticion = get_http_request()
        acceso = getattr(peticion.scope.get("user"), "access_token", None)
        if getattr(acceso, "claims", None):
            if peticion.client and not IP.get():
                IP.set(peticion.client.host)
            return acceso
    except Exception:
        pass
    try:
        return get_access_token()
    except Exception:
        return None


def canal_actual() -> str | None:
    if USUARIO.get():
        return CANAL.get()
    return "mcp" if usuario_actual() else CANAL.get()


def firma(usuario: dict | None = None) -> str | None:
    """Cómo se nombra a alguien en pantalla y en los campos de autoría."""
    usuario = usuario if usuario is not None else usuario_actual()
    if not usuario:
        return None
    return usuario.get("nombre") or usuario.get("usuario") or usuario.get("sub")


def es_admin(usuario: dict | None = None) -> bool:
    usuario = usuario if usuario is not None else usuario_actual()
    return bool(usuario) and "admin" in (usuario.get("roles") or [])


def registrar(accion: str, *, trabajo_id: int | None = None, paquete_id: int | None = None,
              detalle: str | None = None, resultado: str = "ok",
              usuario: dict | None = None, canal: str | None = None,
              ip: str | None = None) -> None:
    """Apunta una acción. Nunca falla hacia fuera: perder una línea de auditoría es malo,
    pero tumbar por ello lo que el usuario estaba haciendo es peor."""
    usuario = usuario if usuario is not None else usuario_actual()
    try:
        with bd.Sesion() as s:
            s.add(bd.Auditoria(
                fecha=bd.ahora(), accion=accion, trabajo_id=trabajo_id, paquete_id=paquete_id,
                detalle=(detalle or None) and detalle[:2000], resultado=resultado,
                sub=(usuario or {}).get("sub"), usuario=(usuario or {}).get("usuario"),
                nombre=(usuario or {}).get("nombre"), email=(usuario or {}).get("email") or None,
                canal=canal or canal_actual(), ip=ip or IP.get()))
            s.commit()
    except Exception:
        registro.exception("No se ha podido registrar «%s» en la auditoría", accion)
    _enviar_comun(accion, trabajo_id=trabajo_id, paquete_id=paquete_id, detalle=detalle,
                  resultado=resultado, usuario=usuario, canal=canal or canal_actual(),
                  ip=ip or IP.get())


# ---------------------------------------------------------------- auditoría común

# Las acciones de la app, en el formato del contrato común: `objeto.verbo`.
ACCIONES = {
    "entrar": "aplicacion.entrar",
    "salir": "aplicacion.salir",
    "abrir": "trabajo.abrir",
    "tomar": "trabajo.tomar",
    "dejar": "trabajo.dejar",
    "liberar": "trabajo.liberar",
    "encargar": "trabajo.crear",
    "configurar": "trabajo.configurar",
    "reprocesar": "trabajo.reprocesar",
    "guardar": "trabajo.guardar",
    "borrar": "trabajo.borrar",
    "descargar": "trabajo.exportar",
    "enviar a learningCloud": "trabajo.enviar_learningcloud",
    "subir": "paquete.subir",
    "resolver término": "termino.resolver",
    "otra propuesta": "termino.proponer",
    "comentar": "comentario.crear",
    "pedir lote": "traduccion.pedir_lote",
    "entregar lote": "traduccion.entregar_lote",
    "aprobar traducción": "traduccion.aprobar",
    "aprobar todas": "traduccion.aprobar_todas",
    "devolver a revisión": "traduccion.devolver",
    "sustituir imagen": "imagen.sustituir",
    "deshacer sustitución": "imagen.restaurar",
    "aportar documentación": "documentacion.aportar",
    "borrar documentación": "documentacion.borrar",
    "descargar ficha": "ficha.exportar",
    "descargar currículo": "curriculo.exportar",
    "ajustes": "ajustes.modificar",
}


def accion_comun(accion: str) -> str:
    """«resolver término» → «termino.resolver»; «asistente: traducir» → «asistente.traducir».
    Lo que no está en la tabla se normaliza igual, para que el servicio no lo rechace."""
    if accion in ACCIONES:
        return ACCIONES[accion]
    plano = unicodedata.normalize("NFKD", accion).encode("ascii", "ignore").decode().lower()
    plano = re.sub(r"\s*:\s*", ".", plano.strip())
    plano = re.sub(r"[^a-z0-9_.-]+", "_", plano).strip("_.-")
    return (plano or "accion")[:80]


def evento_comun(accion: str, *, trabajo_id: int | None = None, paquete_id: int | None = None,
                 detalle: str | None = None, resultado: str = "ok", canal: str | None = None,
                 herramienta: str | None = None) -> dict[str, Any]:
    """Lo que la auditoría común necesita de una acción: qué, sobre qué y por dónde.

    El texto de `detalle` no viaja, porque puede llevar lo que escribió el editor (un
    comentario, el valor de un término). La excepción es el motivo de un intento denegado
    o fallido, que lo escribe la propia app."""
    if trabajo_id:
        tipo, objeto_id = "trabajo", str(trabajo_id)
    elif paquete_id:
        tipo, objeto_id = "paquete", str(paquete_id)
    elif accion == "descargar currículo":
        tipo, objeto_id = "curriculo", (detalle or "")[:200]
    else:
        tipo, objeto_id = "", ""
    extra: dict[str, Any] = {}
    if canal:
        extra["canal"] = canal
    if herramienta:
        extra["herramienta"] = herramienta
    if trabajo_id and paquete_id:
        extra["paquete"] = paquete_id
    resultado = resultado if resultado in ("ok", "denegado", "error") else "error"
    if resultado != "ok" and detalle:
        extra["motivo"] = detalle[:300]
    return {"accion": accion_comun(accion), "tipo": tipo, "objeto_id": objeto_id,
            "resultado": resultado, "detalle": extra}


_comun: tuple[Any, asyncio.AbstractEventLoop] | None = None
_comun_iniciado = False
_comun_cerrojo = threading.Lock()


def iniciar_comun() -> bool:
    """Prepara el envío a la auditoría común si la app está en la plataforma y tiene
    `DEMOS_AUDITORIA_URL`: un bucle de asyncio en un hilo propio, porque las funciones del
    servicio son síncronas y no deben esperar a la red. Si quedó cola de antes, la reintenta."""
    global _comun, _comun_iniciado
    with _comun_cerrojo:
        if _comun_iniciado:
            return _comun is not None
        _comun_iniciado = True
        if not (os.environ.get("DEMOS_APP") and os.environ.get("DEMOS_AUDITORIA_URL")):
            return False
        try:
            from demos_kit import Config
            from demos_kit.auditoria import Auditor
            auditor = Auditor(Config.del_entorno())
        except Exception:
            registro.exception("No se puede preparar la auditoría común: solo queda la tabla propia")
            return False
        bucle = asyncio.new_event_loop()
        threading.Thread(target=bucle.run_forever, name="auditoria-comun", daemon=True).start()
        bucle.call_soon_threadsafe(auditor.al_arrancar)
        _comun = (auditor, bucle)
        return True


def _enviar_comun(accion: str, *, usuario: dict | None, ip: str | None, **datos) -> None:
    """Nunca falla hacia fuera ni espera: el kit escribe la línea, la envía o la encola."""
    if (usuario or {}).get("local") or not iniciar_comun() or _comun is None:
        return
    auditor, bucle = _comun
    try:
        ev = evento_comun(accion, herramienta=HERRAMIENTA.get(), **datos)
        actor = {"sub": (usuario or {}).get("sub", ""), "usuario": (usuario or {}).get("usuario", ""),
                 "roles": list((usuario or {}).get("roles") or [])}
        asyncio.run_coroutine_threadsafe(
            auditor.registrar(None, ev["accion"], ev["tipo"], ev["objeto_id"], "", ev["resultado"],
                              ev["detalle"], actor=actor, ip=ip or ""), bucle)
    except Exception:
        registro.exception("No se ha podido enviar «%s» a la auditoría común", accion)


def purgar() -> int:
    """Borra lo que supera el plazo de conservación."""
    with bd.Sesion() as s:
        n = (s.query(bd.Auditoria)
             .filter(bd.Auditoria.fecha < bd.ahora() - CONSERVACION)
             .delete(synchronize_session=False))
        s.commit()
    return n


def _fila(a: bd.Auditoria) -> dict[str, Any]:
    return {"id": a.id, "fecha": a.fecha.isoformat(timespec="seconds"), "sub": a.sub,
            "usuario": a.usuario, "nombre": a.nombre, "email": a.email, "canal": a.canal,
            "ip": a.ip, "accion": a.accion, "trabajo_id": a.trabajo_id,
            "paquete_id": a.paquete_id, "detalle": a.detalle, "resultado": a.resultado}


def consultar(usuario: str | None = None, accion: str | None = None,
              trabajo_id: int | None = None, desde: dt.date | None = None,
              hasta: dt.date | None = None, solo_denegados: bool = False,
              limite: int = 500) -> list[dict[str, Any]]:
    """Lo registrado, lo más reciente primero. Las fechas son días en hora de Madrid."""
    from .horas import inicio_del_dia
    with bd.Sesion() as s:
        q = s.query(bd.Auditoria).order_by(bd.Auditoria.id.desc())
        if usuario:
            q = q.filter(bd.Auditoria.usuario == usuario)
        if accion:
            q = q.filter(bd.Auditoria.accion == accion)
        if trabajo_id:
            q = q.filter(bd.Auditoria.trabajo_id == trabajo_id)
        if desde:
            q = q.filter(bd.Auditoria.fecha >= inicio_del_dia(desde))
        if hasta:
            q = q.filter(bd.Auditoria.fecha < inicio_del_dia(hasta + dt.timedelta(days=1)))
        if solo_denegados:
            q = q.filter(bd.Auditoria.resultado != "ok")
        return [_fila(a) for a in q.limit(limite).all()]


def opciones_filtro() -> dict[str, list[str]]:
    """Los usuarios y acciones que aparecen, para los desplegables de la pantalla."""
    with bd.Sesion() as s:
        usuarios = sorted({u for (u,) in s.query(bd.Auditoria.usuario).distinct() if u})
        acciones = sorted({a for (a,) in s.query(bd.Auditoria.accion).distinct() if a})
    return {"usuarios": usuarios, "acciones": acciones}


def csv_de(filas: list[dict[str, Any]]) -> str:
    from .horas import local
    salida = io.StringIO()
    campos = ["fecha", "usuario", "nombre", "email", "canal", "ip", "accion",
              "trabajo_id", "paquete_id", "resultado", "detalle"]
    escritor = csv.DictWriter(salida, fieldnames=campos, extrasaction="ignore", delimiter=";")
    escritor.writeheader()
    for f in filas:
        escritor.writerow({**f, "fecha": local(f["fecha"]).strftime("%Y-%m-%d %H:%M:%S")})
    # Con BOM, para que Excel lea las tildes.
    return "﻿" + salida.getvalue()
