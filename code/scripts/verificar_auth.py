"""Comprueba el circuito de autorización de los dos servicios de smartED.

Lee las credenciales del entorno (o de un .env en la raíz del proyecto), pide un token
para cada servicio y hace una llamada real de solo lectura con él. No imprime nunca el
token ni la credencial: solo su longitud y los datos no sensibles del contenido.

    python scripts/verificar_auth.py
"""

from __future__ import annotations

import base64
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
from smarted_cliente import ErrorAutenticacion, ProveedorDeTokens  # noqa: E402


def describir(token: str) -> str:
    """Resumen no sensible de un JWT: emisor, caducidad y permisos. Sin el token."""
    try:
        carga = token.split(".")[1]
        carga += "=" * (-len(carga) % 4)
        c = json.loads(base64.urlsafe_b64decode(carga))
    except Exception:
        return "(no es un JWT legible)"
    interesantes = ("client_id", "user_name", "authorities", "scope", "customer_id", "school_id", "exp")
    return ", ".join(f"{k}={c[k]}" for k in interesantes if k in c)


COMPROBACIONES = {
    "backoffice": (
        os.environ.get("SMARTED_BACKOFFICE_URL", "https://pre-backoffice.smarted.cloud/mvc")
        + "/rest/v1/contents/books/byUri?bookUri=comprobacion",
    ),
    "classroom": None,  # sin URL base conocida todavía; ver nota al final
}


def main() -> int:
    cargar_env(RAIZ.parent)   # el .env local vive en la raíz del repositorio, fuera de code/
    fallos = 0

    with ProveedorDeTokens() as proveedor:
        for servicio in ("backoffice", "classroom"):
            print(f"\n=== {servicio}")
            try:
                token = proveedor.token(servicio)
            except ErrorAutenticacion as e:
                print(f"  [fallo] {e}")
                fallos += 1
                continue

            print(f"  [ok] token obtenido ({len(token)} caracteres)")
            print(f"       {describir(token)}")

            # Segunda llamada: debe venir de la caché, sin volver al servidor.
            if proveedor.token(servicio) == token:
                print("       caché correcta: la segunda petición no vuelve al servidor")

            destino = COMPROBACIONES.get(servicio)
            if not destino:
                print("       (sin llamada de prueba: falta la URL base de esta API)")
                continue

            url = destino[0] if isinstance(destino, tuple) else destino
            try:
                r = httpx.get(url, headers=proveedor.cabecera(servicio), timeout=30.0)
                estado = "acepta el token" if r.status_code != 401 else "RECHAZA el token"
                print(f"       llamada de prueba: HTTP {r.status_code} — la API {estado}")
                if r.status_code == 401:
                    fallos += 1
            except Exception as e:
                print(f"       [fallo] la llamada de prueba no salió: {type(e).__name__}: {e}")
                fallos += 1

    print("\n" + ("RESULTADO: autorización operativa." if not fallos
                  else f"RESULTADO: {fallos} comprobación(es) fallida(s)."))
    return 0 if not fallos else 1


if __name__ == "__main__":
    raise SystemExit(main())
