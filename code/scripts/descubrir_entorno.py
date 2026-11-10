"""Diagnóstico de solo lectura del entorno de preproducción.

Comprueba que las tres piezas responden, que los tokens valen para lo que decimos que
valen y que el cliente de trabajo sigue teniendo la forma que damos por buena en
CLAUDE.md. Útil al empezar una sesión y después de cualquier release de Sarela.

No escribe nada: solo GET.

    python scripts/descubrir_entorno.py
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

import httpx

# Solo al ejecutarse desde la consola: importado desde otro proceso (el demostrador,
# por ejemplo) reemplazar la salida deja el flujo anterior cerrado y rompe al que importa.
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))

from scripts_comun import cargar_env  # noqa: E402
from smarted_cliente import ProveedorDeTokens  # noqa: E402


def caducidad_min(token: str) -> float:
    carga = token.split(".")[1]
    carga += "=" * (-len(carga) % 4)
    exp = json.loads(base64.urlsafe_b64decode(carga))["exp"]
    return (dt.datetime.fromtimestamp(exp, dt.UTC) - dt.datetime.now(dt.UTC)).total_seconds() / 60


def main() -> int:
    cargar_env(RAIZ.parent)   # el .env local vive en la raíz del repositorio, fuera de code/
    bo = os.environ.get("SMARTED_BACKOFFICE_URL", "https://pre-backoffice.smarted.cloud/mvc")
    cms = os.environ.get("SMARTED_CMS_URL", "https://pre-cms.smarted.cloud")
    cliente_id = os.environ.get("SMARTED_CUSTOMER_ID", "1")
    centro_id = os.environ.get("SMARTED_SCHOOL_ID", "1")
    avisos = 0

    with ProveedorDeTokens() as prov, httpx.Client(timeout=45.0) as http:
        print("=== Tokens")
        for servicio in ("backoffice", "classroom"):
            t = prov.token(servicio)
            print(f"  {servicio:11} vigente {caducidad_min(t):.0f} min")

        cab_bo, cab_cms = prov.cabecera("backoffice"), prov.cabecera("classroom")

        comprobaciones = [
            ("CMS operativo", "GET", f"{cms}/api/v1/status", {}, cab_cms),
            ("Actividades del cliente", "GET", f"{cms}/api/v1/customers/{cliente_id}/activities",
             {"page": 1, "size": 1, "searchMode": "ALL"}, cab_cms),
            ("Ficha del cliente", "GET", f"{bo}/rest/v1/customers/{cliente_id}", {}, cab_bo),
            ("Configuración del cliente", "GET", f"{bo}/rest/v1/customers/{cliente_id}/configuration",
             {}, cab_bo),
            ("Taxonomía de metadatos", "GET", f"{bo}/rest/v1/customers/{cliente_id}/metadatas",
             {}, cab_bo),
            ("Libros del centro", "GET",
             f"{bo}/rest/v2/customers/{cliente_id}/schools/{centro_id}/books",
             {"page": 0, "size": 1}, cab_bo),
        ]

        print(f"\n=== Lecturas (cliente {cliente_id}, centro {centro_id})")
        respuestas = {}
        for etiqueta, _, url, params, cab in comprobaciones:
            try:
                r = http.get(url, headers=cab, params=params or None)
                respuestas[etiqueta] = r
                marca = "ok " if r.status_code == 200 else "AVISO"
                if r.status_code != 200:
                    avisos += 1
                print(f"  [{marca}] {etiqueta:26} HTTP {r.status_code}")
            except Exception as e:
                avisos += 1
                print(f"  [AVISO] {etiqueta:26} {type(e).__name__}: {e}")

        r = respuestas.get("Configuración del cliente")
        if r is not None and r.status_code == 200:
            cfg = r.json()
            tam = cfg.get("maxUploadSize")
            print(f"\n  maxUploadSize: {tam} MB   authoring: {cfg.get('authoring')}")
            if isinstance(tam, (int, float)) and tam < 25:
                avisos += 1
                print("  [AVISO] el paquete de muestra ocupa 24 MB y no entraría")

        r = respuestas.get("Taxonomía de metadatos")
        if r is not None and r.status_code == 200:
            datos = r.json()
            datos = datos if isinstance(datos, list) else datos.get("content", [])
            print("\n=== Taxonomía")
            for m in datos:
                valores = m.get("metadataValues") or []
                print(f"  [{m.get('id')}] {m.get('name')} ({m.get('metadataFilterType')}): "
                      f"{len(valores)} valores")

    print("\n" + ("RESULTADO: entorno conforme a lo documentado en CLAUDE.md."
                  if not avisos else
                  f"RESULTADO: {avisos} aviso(s). Si es tras una release de Sarela, "
                  "actualizar CLAUDE.md."))
    return 0 if not avisos else 1


if __name__ == "__main__":
    raise SystemExit(main())
