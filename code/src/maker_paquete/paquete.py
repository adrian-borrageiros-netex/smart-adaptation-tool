"""Apertura y reconstrucción de un paquete de unidad Maker.

Un paquete es un zip (o su carpeta ya extraída) con esta forma:

    project.json          autoría, por idioma. Rutas de recurso absolutas
    components.json       ejecución, árbol aplanado. Rutas de recurso relativas
    cmi5.json             estructura unidad / lecciones / actividades
    imsmanifest.xml       envoltorio SCORM
    index.html            punto de entrada
    trackingConfig.js     configuración de seguimiento
    <plantilla>/          plantilla compilada, p. ej. 573_215_0_532
    resources/            imágenes, PDF y audio

Los tres JSON se cargan en memoria; el resto permanece en disco y se copia tal cual
al reconstruir. Así el coste de abrir un paquete no depende de los 19 MB de recursos.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .serializacion import VOLCADORES, cargar

JSON_GESTIONADOS = ("project.json", "components.json", "cmi5.json")


@dataclass
class Paquete:
    """Una unidad Maker abierta para inspección o modificación."""

    raiz: Path
    project: dict[str, Any]
    components: dict[str, Any]
    cmi5: dict[str, Any]
    _temporal: Path | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- apertura

    @classmethod
    def abrir(cls, origen: str | Path, destino_extraccion: str | Path | None = None) -> "Paquete":
        """Abre un paquete desde un zip o desde una carpeta ya extraída."""
        origen = Path(origen)
        temporal = None

        if origen.is_file() and origen.suffix.lower() == ".zip":
            if destino_extraccion is None:
                raise ValueError(
                    "Al abrir un zip hay que indicar destino_extraccion: "
                    "el paquete ocupa decenas de megabytes y no debe ir a un temporal del sistema."
                )
            raiz = Path(destino_extraccion)
            raiz.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(origen) as z:
                z.extractall(raiz)
            temporal = raiz
        elif origen.is_dir():
            raiz = origen
        else:
            raise FileNotFoundError(f"No es un zip ni una carpeta: {origen}")

        faltan = [n for n in JSON_GESTIONADOS if not (raiz / n).is_file()]
        if faltan:
            raise ValueError(f"El paquete no tiene {', '.join(faltan)}: {raiz}")

        return cls(
            raiz=raiz,
            project=cargar((raiz / "project.json").read_bytes()),
            components=cargar((raiz / "components.json").read_bytes()),
            cmi5=cargar((raiz / "cmi5.json").read_bytes()),
            _temporal=temporal,
        )

    # ------------------------------------------------------------- descripción

    @property
    def idiomas(self) -> list[str]:
        return list(self.project)

    @property
    def idioma_principal(self) -> str:
        return self.idiomas[0]

    @property
    def componentes(self) -> dict[str, Any]:
        """Componentes de autoría del idioma principal, indexados por identificador."""
        return self.project[self.idioma_principal]["components"]

    @property
    def plantilla(self) -> dict[str, Any]:
        return self.project[self.idioma_principal]["project"]["template"]

    @property
    def carpeta_plantilla(self) -> Path | None:
        """Carpeta de la plantilla compilada, identificable por contener el cargador."""
        for hijo in sorted(self.raiz.iterdir()):
            if hijo.is_dir() and (hijo / "loader.json").is_file():
                return hijo
        return None

    def resumen(self) -> dict[str, Any]:
        """Composición de la unidad, para orientar al editor antes de tocar nada."""
        from collections import Counter

        tipos = Counter(c.get("name") for c in self.componentes.values())
        recursos = self.raiz / "resources"
        ficheros_recurso = [f for f in recursos.rglob("*") if f.is_file()] if recursos.is_dir() else []
        titulos = self.cmi5.get("unitMetadata", {}).get("title", {})

        return {
            "titulo": titulos.get(self.idioma_principal) or next(iter(titulos.values()), None),
            "idiomas": self.idiomas,
            "plantilla": f"{self.plantilla.get('name')} ({self.plantilla.get('id')})",
            "componentes": len(self.componentes),
            "tipos_de_componente": len(tipos),
            "lecciones": len(self.cmi5.get("unitMetadata", {}).get("auOrBlock", [])),
            "recursos": len(ficheros_recurso),
            "bytes_recursos": sum(f.stat().st_size for f in ficheros_recurso),
            "componentes_mas_frecuentes": tipos.most_common(8),
        }

    # ---------------------------------------------------------- reconstrucción

    def reconstruir(self, destino: str | Path, comprimir: bool = False) -> Path:
        """Escribe el paquete completo en destino, con los JSON regenerados.

        Todo lo que no sean los tres JSON gestionados se copia sin tocar. Reconstruir
        sin haber modificado nada debe producir ficheros idénticos byte a byte al
        original: es la garantía de que una adaptación solo cambia lo que pretendía.
        """
        destino = Path(destino)
        if destino.exists() and any(destino.iterdir()):
            raise FileExistsError(f"El destino existe y no está vacío: {destino}")
        destino.mkdir(parents=True, exist_ok=True)

        for elemento in self.raiz.iterdir():
            if elemento.name in JSON_GESTIONADOS:
                continue
            if elemento.is_dir():
                shutil.copytree(elemento, destino / elemento.name)
            else:
                shutil.copy2(elemento, destino / elemento.name)

        for nombre, obj in (
            ("project.json", self.project),
            ("components.json", self.components),
            ("cmi5.json", self.cmi5),
        ):
            (destino / nombre).write_bytes(VOLCADORES[nombre](obj))

        if comprimir:
            zip_destino = destino.with_suffix(".zip")
            with zipfile.ZipFile(zip_destino, "w", zipfile.ZIP_DEFLATED) as z:
                for fichero in sorted(destino.rglob("*")):
                    if fichero.is_file():
                        z.write(fichero, fichero.relative_to(destino).as_posix())
            return zip_destino

        return destino
