"""Obtención y cacheo de tokens contra el servidor de autorización de smartED.

El circuito es el mismo para los dos servicios, y solo cambian la credencial Basic y
el nombre de usuario:

    POST https://pre-auth.smarted.cloud/api/v1/oauth/token
    Authorization: Basic <credencial del cliente>
    multipart/form-data:
        grant_type = trusted_client_username
        username   = backoffice-service | classroom-service

Cada servicio tiene su propia credencial y da acceso a una API distinta: `backoffice`
al SmartBackOffice y `classroom` a la API del CMS.

Las credenciales se leen del entorno y nunca del repositorio. Conviene tener presente
que `trusted_client_username` permite emitir un token para el usuario que se indique:
quien tenga la credencial Basic puede suplantar a cualquiera, así que se trata con el
mismo cuidado que una clave maestra. Ni las credenciales ni los tokens se registran
en ningún log.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass

import httpx

URL_AUTH_POR_DEFECTO = "https://pre-auth.smarted.cloud/api/v1/oauth/token"
GRANT = "trusted_client_username"

# Margen antes de la expiración real para no usar un token que caduca en vuelo.
MARGEN_RENOVACION_S = 60

USUARIOS = {
    "backoffice": "backoffice-service",
    "classroom": "classroom-service",
}


class ErrorAutenticacion(RuntimeError):
    pass


@dataclass
class _TokenCacheado:
    valor: str
    expira_en: float

    @property
    def vigente(self) -> bool:
        return time.time() < self.expira_en - MARGEN_RENOVACION_S


def _credencial_basic(servicio: str) -> str:
    """Credencial Basic del servicio, desde el entorno.

    Admite dos formas: la cabecera ya codificada en `SMARTED_<SERVICIO>_BASIC`, que es
    como la entrega Sarela, o el par id/secreto en `..._CLIENT_ID` y `..._CLIENT_SECRET`.
    """
    prefijo = f"SMARTED_{servicio.upper()}"

    if basic := os.environ.get(f"{prefijo}_BASIC"):
        return basic.removeprefix("Basic ").strip()

    cliente = os.environ.get(f"{prefijo}_CLIENT_ID")
    secreto = os.environ.get(f"{prefijo}_CLIENT_SECRET")
    if cliente and secreto:
        return base64.b64encode(f"{cliente}:{secreto}".encode()).decode()

    raise ErrorAutenticacion(
        f"Falta la credencial del servicio «{servicio}». Define {prefijo}_BASIC "
        f"o bien {prefijo}_CLIENT_ID y {prefijo}_CLIENT_SECRET en el entorno."
    )


def _exp_del_jwt(token: str) -> float | None:
    """Instante de expiración declarado dentro del propio token, si es legible."""
    try:
        carga = token.split(".")[1]
        carga += "=" * (-len(carga) % 4)
        exp = json.loads(base64.urlsafe_b64decode(carga)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _momento_de_expiracion(token: str, respuesta: dict) -> float:
    """El más temprano entre lo que dice `expires_in` y el `exp` del propio token.

    En preproducción se ha observado que el token vive del orden de minutos, así que
    fiarse solo de `expires_in` puede dejar en caché un token ya vencido. El `exp` del
    JWT es la fuente autorizada; `expires_in` se usa como respaldo si no es legible.
    """
    candidatos = []
    if (declarado := respuesta.get("expires_in")) is not None:
        try:
            candidatos.append(time.time() + float(declarado))
        except (TypeError, ValueError):
            pass
    if (exp := _exp_del_jwt(token)) is not None:
        candidatos.append(exp)
    return min(candidatos) if candidatos else time.time() + 300


class ProveedorDeTokens:
    """Emite tokens por servicio y los reutiliza mientras siguen vigentes."""

    def __init__(self, url_auth: str | None = None, cliente_http: httpx.Client | None = None):
        self.url_auth = url_auth or os.environ.get("SMARTED_AUTH_URL", URL_AUTH_POR_DEFECTO)
        self._http = cliente_http or httpx.Client(timeout=30.0)
        self._cache: dict[tuple[str, str], _TokenCacheado] = {}
        self._cerrojo = threading.Lock()

    def token(self, servicio: str, usuario: str | None = None,
              forzar_renovacion: bool = False) -> str:
        """Devuelve un token vigente para «backoffice» o «classroom».

        `usuario` emite el token en nombre de esa cuenta en lugar de la de servicio. El grant
        lo permite sin más comprobación, así que **solo debe usarse con autorización expresa
        del titular de la cuenta**: las acciones quedan registradas a su nombre.
        """
        if servicio not in USUARIOS:
            raise ValueError(f"Servicio desconocido: {servicio}. Use {' o '.join(USUARIOS)}.")

        clave = (servicio, usuario or USUARIOS[servicio])
        with self._cerrojo:
            cacheado = self._cache.get(clave)
            if cacheado and cacheado.vigente and not forzar_renovacion:
                return cacheado.valor

            nuevo = self._solicitar(servicio, clave[1])
            self._cache[clave] = nuevo
            return nuevo.valor

    def cabecera(self, servicio: str, usuario: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(servicio, usuario)}"}

    def _solicitar(self, servicio: str, usuario: str) -> _TokenCacheado:
        respuesta = self._http.post(
            self.url_auth,
            headers={"Authorization": f"Basic {_credencial_basic(servicio)}"},
            files={"grant_type": (None, GRANT), "username": (None, usuario)},
        )

        if respuesta.status_code != 200:
            # El cuerpo puede repetir la credencial enviada; no se incluye en el error.
            raise ErrorAutenticacion(
                f"El servidor de autorización rechazó la petición para «{servicio}»: "
                f"HTTP {respuesta.status_code}."
            )

        datos = respuesta.json()
        valor = datos.get("access_token")
        if not valor:
            raise ErrorAutenticacion(
                f"La respuesta para «{servicio}» no trae access_token. Claves: {sorted(datos)}"
            )

        return _TokenCacheado(valor=valor, expira_en=_momento_de_expiracion(valor, datos))

    def cerrar(self) -> None:
        self._http.close()

    def __enter__(self) -> "ProveedorDeTokens":
        return self

    def __exit__(self, *_) -> None:
        self.cerrar()
