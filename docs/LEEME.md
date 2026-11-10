# Demostrador de adaptación de contenidos

> El código está en `code/`: las rutas de este documento son relativas a `code/` y los
> comandos se ejecutan desde ahí. Los datos locales (`.demostrador/`, `.env`, unidades) viven
> en la raíz del repositorio, fuera de `code/`, y no se versionan.

Sube una unidad Maker, tradúcela o adáptala al currículo de otra comunidad, revisa lo que
el sistema no decide por su cuenta y guarda el resultado.

## Arrancar

```bash
python -m uvicorn web.app:aplicacion --host 127.0.0.1 --port 8800
```

`http://127.0.0.1:8800` para la interfaz, `/api/docs` para la API.

## Servidor MCP

```bash
python mcp_servidor.py            # stdio, para Claude Desktop o Claude Code
python mcp_servidor.py --http     # HTTP en el 8801, para compartir desde el VPS
```

Para Claude Desktop:

```json
{"mcpServers": {"sarela": {"command": "python", "args": ["-m", "mcp_servidor"],
  "cwd": "C:/Users/joserra.mosteiro/Documents/Claude/Projects/Sarela/code"}}}
```

## Base de datos

SQLite por defecto, en `.demostrador/demostrador.db` de la raíz del repositorio. Para PostgreSQL basta con la
variable de entorno; el esquema no usa nada propio de un motor concreto:

```bash
set DATABASE_URL=postgresql+psycopg://usuario:clave@servidor/sarela
```

| Tabla | Qué guarda |
|---|---|
| `paquete` | La unidad subida, sin modificar, con su ficha |
| `trabajo` | Un encargo: tipo, destino, estado, cifras y rutas del resultado |
| `termino` | Cada punto pendiente: original, propuesta del sistema y decisión del editor |
| `comentario` | Feedback del revisor, con marca de si ya se incorporó al procesador |

## Las tres caras son la misma

`web/servicio.py` contiene todas las operaciones y no depende de ningún framework.
La interfaz web, la API REST y el servidor MCP la llaman igual. No hay una vía principal
y otra de segunda: lo que se prueba desde Claude es lo que hace la pantalla.

## Traducción y adaptación son dos ejes, no dos opciones

Una adaptación curricular **también decide en qué lengua queda la unidad**: a Cataluña
normalmente en catalán, a Madrid en castellano. Por eso el idioma se pide siempre, y al
elegir comunidad se propone el suyo —catalán para Cataluña, Baleares y Valencia; gallego
para Galicia; euskera para País Vasco y Navarra; castellano para el resto—. El editor
puede cambiarlo si lo que quiere es la versión castellana de ese currículo.

El cambio de idioma reescribe las cinco copias del paquete y el `locale` del reproductor,
de modo que hasta los botones de navegación cambian de lengua. Lo que **no** hace todavía
es traducir el texto que el sistema no tiene en memoria: cuando se pide un idioma sin
cobertura completa, el informe lo dice con todas las letras —«el paquete se ha marcado
como catalán pero 375 de 959 elementos siguen en gallego»— para que nadie lo entregue así.

## Qué decide el sistema y qué no

El trabajador aplica la **memoria de adaptación** —los ficheros de `adaptacion/*.json`,
que son decisiones editoriales ya tomadas— a los elementos que reconoce. Para todo lo
demás que exige criterio no inventa nada: lo deja como **término pendiente**.

Se marca como pendiente lo que lleva un marcador `[PENDIENTE]` puesto por el toolkit y lo
que contiene una referencia territorial sin decisión guardada. Son los casos donde
equivocarse sale caro: un ISBN, la licencia de una edición, la atribución institucional,
un enlace externo o el equivalente asturiano de un accidente geográfico gallego.

Cuando el editor los resuelve, sus decisiones quedan en la base de datos y el feedback
acumulado alimenta la siguiente versión del procesador.

## Estado actual

Lo que hace hoy el procesado es **aplicar decisiones guardadas y señalar lo que falta**.
La traducción automática de texto nuevo todavía no está conectada: para una unidad que el
sistema no conoce, adaptará lo que reconozca y dejará el resto pendiente. Es deliberado
para el demostrador —enseña el circuito completo sin fingir capacidades— y es el siguiente
enganche natural.
