# PLAN — smart-adaptation-tool (producto real)

> Documento vivo. Resume la decisión de base y todo lo que hay que tener en cuenta al
> pasar de la demo al producto real. Las rutas del código son relativas a `code/`.

## 1. Punto de partida

Esta carpeta es una **copia del repositorio demo** (`smart-adaptation-tool-original`),
copiada **sin su `.git`**, sobre un **repositorio nuevo y vacío**. Se trabaja aquí.

- **No es de cero**: se conserva el dominio difícil ya construido.
- **No es un fork** en su definición: es una copia sobre un repositorio nuevo, sin
  historial ni remoto heredados.
- **No es otro kit de monolito** ni un port a Java/React.

**Finalidad**: herramienta **interna para pocos editores**. Se prioriza la velocidad
sin generar deuda. Por eso:

> **Decisión: mantener Python/FastAPI y modernizar el front in situ.**

## 2. La aplicación hoy (qué es)

- Un **único proyecto Python** (monolito). Back: **FastAPI + Uvicorn**. Front: **HTML
  renderizado en servidor con Jinja2** (no SPA). Persistencia: **SQLAlchemy 2** (SQLite
  por defecto, PostgreSQL en producción). IA: **SDK de Anthropic**. Extra: **FastMCP**,
  PyMuPDF, Pillow, NumPy, OpenCV, Tesseract, Playwright/Chromium, openpyxl, python-docx.
- **Un solo puerto (8800)** sirve a la vez la interfaz (`/`), la API REST (`/api`,
  doc en `/api/docs`) y el MCP (`/mcp`). MCP suelto por HTTP: 8801. MCP por stdio:
  para Claude Desktop/Code.
- **Tres caras sobre la misma capa**: interfaz web, API REST y MCP son envoltorios finos
  sobre `web/servicio.py` (agnóstico de framework). **Esto se conserva**: es un acierto.
- Acceso con **Keycloak** vía el kit de la plataforma (`demos_kit`); en local `SARELA_LOCAL=1`.

## 3. Qué conservar, qué rehacer, qué descartar

| Activo | Veredicto | Motivo |
|---|---|---|
| `src/maker_paquete` (leer/reconstruir paquetes Maker y fuentes Author) | **Conservar tal cual** | El activo más caro de rehacer (byte a byte). |
| `src/smarted_cliente` (APIs smartED) | **Conservar y endurecer** | Acotado y útil. |
| Patrón de `web/servicio.py` (capa agnóstica) | **Conservar** | Una sola verdad para web/API/MCP. |
| `web/trabajador.py`, `ia.py`, `asistente.py` (motor de adaptación + IA) | **Conservar el conocimiento, refactorizar** | Reglas de negocio reales, pero en megaficheros y sin tests. |
| Plantillas Jinja2 + CSS heredado | **Conservar tecnología, reordenar** | Aquí está el grueso de la deuda (estilos en línea, sin macros). |
| `scripts/` por consola | **Absorber** | Duplica el pipeline de `web/`; debe quedar una sola implementación. |
| Estructura corporativa (`code/`, `docs/`, `tests/`, `catalog-info.yaml`) | **Conservar** | Ya cumple la norma de `netexknowledge`. |

## 4. Inventario de deuda técnica (medido)

- **~15.800 líneas de Python** en `code/`; `web/` ~11.500. Ficheros más grandes:
  `trabajador.py` 1.690, `pdf_texto.py` 1.415, `imagenes.py` 1.317, `servicio.py` 1.259,
  `asistente.py` 1.200, `app.py` 1.075, `ia.py` 913.
- **11 plantillas Jinja2**. `base.html` mete el CSS global en un `<style>` en línea; otras
  plantillas añaden su propio `<style>` (`imagenes.html`, `trabajo.html`).
- **Sin macros Jinja**: el bloque `<div class="tarjeta"><h2>…<div class="cuerpo">` se
  repite **28 veces a mano**. (Ojo: `class="tarjeta"` en sí **no** es deuda; lo es el
  copia-pega del bloque completo y los `style="…"` en línea.)
- **Estilos en línea por doquier**: p. ej. `style="display:flex;gap:…;align-items:center;flex-wrap:wrap"`
  repetido en varias plantillas.
- **`scripts/` ↔ `web/` solapados**: `trabajador.py` carga scripts por `importlib`;
  `scripts/adaptar.py` reimplementa parte del pipeline.
- **0 tests**: `tests/tests.yml` es una plantilla corporativa con todo comentado.
- Acoplamiento con la plataforma: **fino** (~9 referencias a `demos_kit`), fácil de aislar.

## 5. Plan por fases (ordenado por ROI)

- **Fase 0 — Red de seguridad (≈1 semana) ⚠️ PRIORIDAD 1**
  Convertir `scripts/verificar_*.py` en **tests de pytest**; añadir tests de contrato de
  `servicio.py`; rellenar `tests/tests.yml` y activar el pipeline. Sin esto, refactorizar
  es ruleta rusa.
- **Fase 1 — Front in situ (≈3-5 días)**
  **Macros Jinja** (`plantillas/_macros.html`): `{% call tarjeta('Título') %}`, `aviso`,
  `etiqueta_estado`, `campo`… y sustituir las 28 repeticiones. **Sacar el `<style>` de
  `base.html` a un `.css` estático** servido desde `estaticos/`. **Eliminar los
  `style="…"`** en favor de clases utilitarias. Mover los `<script>` de polling a estáticos.
- **Fase 2 — Un solo pipeline (≈2-3 días)**
  Quitar la duplicación `scripts/` ↔ `web/`; `scripts/` pasa a ser una **CLI fina**.
- **Fase 3 — Partir los megaficheros (≈1-2 semanas, incremental)**
  `trabajador.py` → orquestación + adaptación + informe; `pdf_texto.py`/`imagenes.py` →
  imagen/PDF; `ia.py`/`asistente.py` → proveedor IA + operaciones en segundo plano.
  Módulo a módulo, con los tests de la Fase 0 en verde.
- **Fase 4 — Frontera formal (cuando toque)**
  Extraer `servicio.py` + dominio a un paquete (`sarela_nucleo`); web/API/MCP quedan como
  **adaptadores**.

## 6. No-objetivos (evitar sobre-ingeniería)

- ❌ React / SPA / otro kit de monolito: para pocos editores internos es coste puro.
- ❌ Portar a Java "porque es la norma": el dominio real está en Python.
- ❌ Rehacer la concurrencia (proceso único + SQLite/Postgres + `BackgroundTasks`): sobra.
- ❌ i18n del interfaz ahora: herramienta interna en castellano; posponer.

## 7. Reglas de oro (anti-deuda)

1. **Ninguna funcionalidad nueva sin su test** (posible gracias a la Fase 0).
2. **Cero `style="…"` en línea**: todo a clases CSS.
3. **Una sola implementación del pipeline**: `scripts/` nunca reimplementa.

## 8. Notas del repositorio nuevo

- Contenido copiado del original **excluyendo `.git`**; el historial **no** viaja.
- Rama actual `main`, sin commits. **Falta crear el commit inicial y el remoto.**
- El `.gitignore` es una **lista blanca** (`/*` + `!`): para que algo entre hay que
  nombrarlo. Se ha añadido `!/PLAN.md` para que este documento sí se versione.
- **No se versionan** datos locales ni credenciales: `.env`, `.demostrador/`, `.trabajo/`,
  unidades de clientes (`courses/`, `fuente/`), derivados (`adaptacion/`), `api/`, informes.

## 9. Pendientes inmediatos

1. Revisar `catalog-info.yaml` (nombre/owner/annotations) para el repositorio nuevo.
2. Decidir dónde vive el nuevo remoto y hacer el **commit inicial**.
3. Empezar por la **Fase 0** (tests + CI) o por la **Fase 1** (mejora visual inmediata).
