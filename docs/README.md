# smart-adaptation-tool

Herramienta para realizar adaptaciones curriculares y traducciones sobre contenido smartED.
Sube una unidad de Maker (paquete publicado o fuente de Author), tradúcela a otra lengua o
adáptala al currículo de otra comunidad autónoma, revisa lo que el sistema no decide por su
cuenta y descarga el resultado o envíalo a learningCloud.

En producción vive en `https://adaptacion.sarela.tech`, dentro de la plataforma de demos
(`netexknowledge/smart-sarelatech-demos-platform`): acceso con Keycloak, MCP en `/mcp` y
auditoría común de la plataforma.

## Estructura del repositorio

| Carpeta | Qué contiene |
|---|---|
| `code/` | La aplicación: `web/` (interfaz, API y lógica), `src/` (lectura y escritura de paquetes de Maker y fuentes de Author), `mcp_servidor.py`, `scripts/` (cadena de adaptación y verificaciones), `despliegue/`, `Dockerfile` y `docker-compose.yml` |
| `docs/` | Esta documentación |
| `tests/` | Definición de pruebas del pipeline corporativo |

Las rutas que cita la documentación (`web/…`, `scripts/…`, `docker-compose.yml`) son
relativas a `code/`. Las unidades de clientes, los datos locales y las credenciales no se
versionan: el `.gitignore` es una lista de lo que sí entra.

## Documentación

- [LEEME](LEEME.md): cómo arrancarla, el servidor MCP, la base de datos y la arquitectura.
- [Despliegue](DESPLIEGUE.md): alta en la plataforma, actualizar, dar acceso y desarrollo en local.

## Verificaciones

Desde `code/`:

```bash
python scripts/verificar_hito0.py        # abrir y reconstruir un paquete sin cambios, byte a byte
python scripts/verificar_cirugia.py      # cambiar un componente sin tocar nada más
python scripts/verificar_auditoria.py    # auditoría común: contrato, envío y MCP
```
