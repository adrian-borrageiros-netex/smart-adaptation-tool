"""Prueba de la auditoría común de Sarela, sin servidor: base temporal, servicio simulado.

Uso: python scripts/verificar_auditoria.py [ruta de demos-plataforma/code]
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SARELA = Path(__file__).resolve().parent.parent   # code/
PLATAFORMA = Path(sys.argv[1]) if len(sys.argv) > 1 else SARELA.parent.parent / "demos-plataforma" / "code"
os.environ["SARELA_DATOS"] = tempfile.mkdtemp()
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(SARELA))
sys.path.insert(0, str(PLATAFORMA / "auditoria"))

import httpx  # noqa: E402
from modelo import Evento  # noqa: E402  — el contrato tal como lo valida el servicio

from demos_kit import Config  # noqa: E402
from demos_kit.auditoria import Auditor  # noqa: E402
from web import auditoria, bd  # noqa: E402

fallos = 0


def ok(c, m):
    global fallos
    print(("OK    " if c else "FALLA ") + m)
    fallos += 0 if c else 1


bd.crear_esquema()

# 1. Toda acción de la tabla, y las que se normalizan, pasan la validación del servicio.
import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

nombres = list(auditoria.ACCIONES) + ["asistente: traducir", "asistente: proponer imágenes", "salir", "rara acción"]
validos = True
for n in nombres:
    ev = auditoria.evento_comun(n, trabajo_id=3, paquete_id=2, detalle="texto del editor", canal="web")
    try:
        Evento.model_validate({"version_esquema": 1, "id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
                               "app": "adaptacion", "accion": ev["accion"], "resultado": ev["resultado"],
                               "objeto": {"tipo": ev["tipo"], "id": ev["objeto_id"]}, "detalle": ev["detalle"]})
    except Exception as e:  # noqa: BLE001
        validos = False
        print("   no válido:", n, ev["accion"], e)
ok(validos, f"las {len(nombres)} acciones cumplen el contrato del servicio")
ok(auditoria.accion_comun("resolver término") == "termino.resolver"
   and auditoria.accion_comun("asistente: proponer imágenes") == "asistente.proponer_imagenes",
   "acciones: tabla y normalización")
ev = auditoria.evento_comun("comentar", trabajo_id=3, detalle="Mi comentario privado", canal="mcp", herramienta="anotar_feedback")
ok("Mi comentario" not in json.dumps(ev) and ev["detalle"] == {"canal": "mcp", "herramienta": "anotar_feedback"},
   "el texto del editor no viaja; sí el canal y la herramienta")
ev = auditoria.evento_comun("guardar", trabajo_id=3, resultado="denegado", detalle="En uso por Ana")
ok(ev["resultado"] == "denegado" and ev["detalle"]["motivo"] == "En uso por Ana", "denegado: viaja el motivo")
ok(auditoria.evento_comun("enviar a learningCloud", trabajo_id=3, resultado="conflicto")["resultado"] == "error",
   "un resultado propio (conflicto) pasa a error")

# 2. De registrar() al servicio, por el hilo propio y con el kit de verdad.
recibidos = []


def manejar(peticion: httpx.Request) -> httpx.Response:
    if peticion.url.path.endswith("/token"):
        return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
    if peticion.url.path == "/eventos":
        lote = json.loads(peticion.content)
        for e in lote:
            Evento.model_validate(e)
        recibidos.extend(lote)
        return httpx.Response(200, json={"recibidos": len(lote), "nuevos": len(lote)})
    return httpx.Response(200)


cfg = Config(app="adaptacion", emisor="https://auth.sarela.tech/realms/demos", url_publica="x",
             cliente_secreto="s", secreto_sesion="x")
cola = Path(tempfile.mkdtemp()) / "cola.jsonl"
bucle = asyncio.new_event_loop()
import threading  # noqa: E402

threading.Thread(target=bucle.run_forever, daemon=True).start()
auditor = Auditor(cfg, url="http://auditoria:8000", cola=cola, keycloak_interno="http://keycloak:8080",
                  http=httpx.AsyncClient(transport=httpx.MockTransport(manejar)))
auditoria._comun, auditoria._comun_iniciado = (auditor, bucle), True

ana = {"sub": "s-ana", "usuario": "ana", "nombre": "Ana", "roles": ["editor"]}
inicio = time.time()
auditoria.registrar("resolver término", trabajo_id=7, detalle="T12: Junta de Andalucía", usuario=ana,
                    canal="mcp", ip="203.0.113.5")
ok(time.time() - inicio < 0.5, "registrar() no espera a la red")
ficha = auditoria.HERRAMIENTA.set("resolver_termino")
auditoria.registrar("guardar", trabajo_id=7, usuario=ana, canal="mcp")
auditoria.HERRAMIENTA.reset(ficha)
auditoria.registrar("entrar", usuario=auditoria.LOCAL, canal="web")
for _ in range(50):
    if len(recibidos) >= 2:
        break
    time.sleep(0.05)
time.sleep(0.2)
ok(len(recibidos) == 2, f"llegan al servicio los dos eventos de una persona, no el del usuario local ({len(recibidos)})")
if len(recibidos) >= 2:
    a, b = recibidos[0], recibidos[1]
    ok(a["accion"] == "termino.resolver" and a["actor"]["usuario"] == "ana" and a["ip"] == "203.0.113.5"
       and a["objeto"] == {"tipo": "trabajo", "id": "7", "nombre": ""} and "Junta" not in json.dumps(a),
       "evento: acción, persona, IP y objeto, sin el valor del término")
    ok(b["detalle"] == {"canal": "mcp", "herramienta": "resolver_termino"}, "evento: herramienta del MCP")
with bd.Sesion() as s:
    filas = s.query(bd.Auditoria).count()
ok(filas == 3, "la tabla propia sigue registrándolo todo, con su detalle")

# 3. Middleware del MCP: entrada una vez por sesión y herramienta en la acción.
import mcp_servidor  # noqa: E402
from fastmcp import Client  # noqa: E402

registros = []
original = auditoria.registrar
auditoria.registrar = lambda accion, **kw: registros.append((accion, kw, auditoria.HERRAMIENTA.get()))
auditoria.usuario_actual = lambda: ana
mcp_servidor.servidor.add_middleware(mcp_servidor.AuditoriaMCP())


@mcp_servidor.servidor.tool
def prueba_auditada() -> str:
    auditoria.registrar("guardar", trabajo_id=1)
    return "hecho"


async def sesion():
    async with Client(mcp_servidor.servidor) as c:
        await c.list_tools()
        await c.call_tool("prueba_auditada", {})
        await c.call_tool("opciones", {})

asyncio.run(sesion())
entradas = [r for r in registros if r[0] == "entrar"]
ok(len(entradas) == 1 and entradas[0][1].get("canal") == "mcp", f"MCP: una entrada por sesión ({len(entradas)})")
guardados = [r for r in registros if r[0] == "guardar"]
ok(len(guardados) == 1 and guardados[0][2] == "prueba_auditada", "MCP: la acción lleva el nombre de la herramienta")
ok(not [r for r in registros if r[0] not in ("entrar", "guardar")], "MCP: las consultas no se registran")
asyncio.run(sesion())
ok(len([r for r in registros if r[0] == "entrar"]) == 2, "MCP: otra sesión, otra entrada")
auditoria.registrar = original

print("\nTODO BIEN" if not fallos else f"\n{fallos} FALLOS")
sys.exit(1 if fallos else 0)
