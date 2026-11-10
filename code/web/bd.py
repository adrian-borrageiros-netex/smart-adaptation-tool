"""Modelo de datos del demostrador.

Escrito para PostgreSQL y probado también sobre SQLite, que es lo que usa el demostrador
si no se le indica otra cosa. No se usa ningún tipo propio de un motor concreto: cambiar
de uno a otro es cambiar `DATABASE_URL`.

    DATABASE_URL=postgresql+psycopg://usuario:clave@servidor/basedatos
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, func)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

RAIZ = Path(__file__).resolve().parent.parent
# Carpeta de datos: paquetes subidos, extraídos, resultados e informes. Configurable para
# que en el servidor viva en un volumen propio, fuera del código. En local, por defecto en
# la raíz del repositorio (fuera de code/), junto a las demás carpetas locales.
DATOS = Path(os.environ.get("SARELA_DATOS") or (RAIZ.parent / ".demostrador")).resolve()
DATOS.mkdir(parents=True, exist_ok=True)


def relativa(ruta: str | Path | None) -> str | None:
    """Cómo se guarda una ruta en la base de datos: relativa a `DATOS` si cae dentro.

    Guardarlas absolutas ataba la base de datos a la máquina donde se creó: las de
    Windows (`C:/Users/…`) no significan nada en el servidor Linux.
    """
    if ruta is None:
        return None
    p = Path(ruta)
    try:
        return p.resolve().relative_to(DATOS).as_posix()
    except ValueError:
        return str(p)


def ruta(valor: str | None) -> Path | None:
    """La ruta real de un valor guardado, sea relativo, absoluto o de otra máquina.

    Las rutas antiguas son absolutas y de Windows. Si no existen tal cual, se busca el
    tramo a partir de la carpeta de datos y se cuelga de la `DATOS` actual: así una base
    migrada al servidor sigue encontrando sus ficheros sin reescribirla.
    """
    if not valor:
        return None
    p = Path(valor)
    if not p.is_absolute() and "\\" not in valor and ":" not in valor[:3]:
        return DATOS / p
    if p.exists():
        return p
    partes = valor.replace("\\", "/").split("/")
    for marca in (DATOS.name, ".demostrador"):
        if marca in partes:
            return DATOS.joinpath(*partes[partes.index(marca) + 1:])
    return p


URL = os.environ.get("DATABASE_URL", f"sqlite:///{(DATOS / 'demostrador.db').as_posix()}")

# Estados, como constantes para que no se escriban a mano en cada sitio.
ENCOLADO, PROCESANDO, LISTO, ERROR = "encolado", "procesando", "listo", "error"
PENDIENTE, REVISADO = "pendiente", "revisado"
TRADUCCION, ADAPTACION = "traduccion", "adaptacion"

IDIOMAS = {
    "es": "Español", "ca": "Catalán", "gl": "Gallego", "eu": "Euskera",
    "fr": "Francés", "en": "Inglés", "pt": "Portugués",
}

COMUNIDADES = [
    "Andalucía", "Aragón", "Asturias", "Baleares", "Canarias", "Cantabria",
    "Castilla-La Mancha", "Castilla y León", "Cataluña", "Comunidad Valenciana",
    "Extremadura", "Galicia", "La Rioja", "Madrid", "Murcia", "Navarra", "País Vasco",
]

# Lengua vehicular habitual de cada comunidad. Es solo la propuesta inicial que ofrece la
# interfaz: una adaptación a Cataluña casi siempre lleva traducción al catalán, pero la
# decisión es del editor, que puede querer la versión castellana del mismo currículo.
IDIOMA_POR_COMUNIDAD = {
    "Cataluña": "ca", "Baleares": "ca", "Comunidad Valenciana": "ca",
    "Galicia": "gl", "País Vasco": "eu", "Navarra": "eu",
}


def idioma_sugerido(comunidad: str | None) -> str:
    return IDIOMA_POR_COMUNIDAD.get(comunidad or "", "es")


class Base(DeclarativeBase):
    pass


class Paquete(Base):
    """Una unidad subida por el editor, tal como llegó."""

    __tablename__ = "paquete"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(255))
    titulo: Mapped[str | None] = mapped_column(String(500))
    idioma_origen: Mapped[str | None] = mapped_column(String(8))     # el del contenido, leído
    idioma_declarado: Mapped[str | None] = mapped_column(String(8))  # la clave del paquete
    plantilla: Mapped[str | None] = mapped_column(String(120))
    # «maker»: paquete publicado (xAPI). «author»: fuente de Author, la unidad y sus
    # proyectos de actividades. Se descarga en el mismo formato en que se subió.
    formato: Mapped[str] = mapped_column(String(10), default="maker")
    notas: Mapped[str | None] = mapped_column(Text)   # lo que conviene saber al subirlo
    componentes: Mapped[int] = mapped_column(Integer, default=0)
    lecciones: Mapped[int] = mapped_column(Integer, default=0)
    # De dónde salió en learningCloud, en JSON: `proyecto` ({id, nombre}), `carpeta` (id) y
    # `ruta` (nombres de carpeta bajo el proyecto). Es lo que se replica al enviarlo.
    origen: Mapped[str | None] = mapped_column(Text)
    ruta_zip: Mapped[str] = mapped_column(String(1000))
    ruta_extraido: Mapped[str] = mapped_column(String(1000))
    subido_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    subido_por: Mapped[str | None] = mapped_column(String(120))

    trabajos: Mapped[list["Trabajo"]] = relationship(
        back_populates="paquete", cascade="all, delete-orphan", order_by="Trabajo.id.desc()")


class Trabajo(Base):
    """Un encargo sobre un paquete: traducirlo o adaptarlo a una comunidad."""

    __tablename__ = "trabajo"

    id: Mapped[int] = mapped_column(primary_key=True)
    paquete_id: Mapped[int] = mapped_column(ForeignKey("paquete.id", ondelete="CASCADE"))
    tipo: Mapped[str] = mapped_column(String(20))
    idioma_destino: Mapped[str | None] = mapped_column(String(8))
    comunidad: Mapped[str | None] = mapped_column(String(80))

    estado: Mapped[str] = mapped_column(String(20), default=ENCOLADO)
    mensaje: Mapped[str | None] = mapped_column(Text)

    ruta_resultado: Mapped[str | None] = mapped_column(String(1000))
    ruta_informe: Mapped[str | None] = mapped_column(String(1000))

    elementos_totales: Mapped[int] = mapped_column(Integer, default=0)
    elementos_adaptados: Mapped[int] = mapped_column(Integer, default=0)
    avisos_validacion: Mapped[int] = mapped_column(Integer, default=0)
    frases_totales: Mapped[int] = mapped_column(Integer, default=0)
    frases_conocidas: Mapped[int] = mapped_column(Integer, default=0)

    # Lo que está haciendo el asistente sobre este trabajo, para enseñar el progreso. Las
    # operaciones son largas —una unidad entera son decenas de lotes— y corren en segundo
    # plano: la pantalla las consulta aquí.
    ia_operacion: Mapped[str | None] = mapped_column(String(20))
    ia_estado: Mapped[str | None] = mapped_column(String(20))
    ia_mensaje: Mapped[str | None] = mapped_column(Text)
    ia_hechas: Mapped[int] = mapped_column(Integer, default=0)
    ia_total: Mapped[int] = mapped_column(Integer, default=0)
    # La operación en curso y las que esperan, en JSON: {"actual": {...}, "pendientes":
    # [...]}, cada una con `operacion`, `huellas` y `quien`. Lo que se pide mientras el
    # asistente trabaja se pone a la cola en vez de rechazarse.
    ia_cola: Mapped[str | None] = mapped_column(Text)

    # El último envío a learningCloud, en JSON: estado, proyecto, ruta, carpeta, enlace,
    # quién y cuándo. Lo escribe la página puente con lo que le devuelve el marcador.
    envio: Mapped[str | None] = mapped_column(Text)

    # Resumen de las imágenes con texto del último proceso, en JSON: cuántas se han rehecho
    # y cuáles van a diseño (`trabajador.resumen_imagenes`). La pantalla avisa con esto.
    imagenes: Mapped[str | None] = mapped_column(Text)
    # Si al procesar se rehacen las imágenes con texto (rótulos traducidos). Desactivarlo
    # deja las imágenes como en el original y acelera el proceso.
    regenerar_imagenes: Mapped[bool] = mapped_column(Boolean, default=True)

    # A qué etapa, curso y materia corresponde la unidad (id común de `curriculo.MATERIAS`).
    # Se deducen al encargar del rótulo de portada y se pueden corregir. Deciden qué trozo
    # del currículo de la comunidad recibe el asistente.
    etapa: Mapped[str | None] = mapped_column(String(20))
    curso: Mapped[int | None] = mapped_column(Integer)
    materia: Mapped[str | None] = mapped_column(String(40))
    # El plan de adaptación que deja «Analizar con el currículo», en JSON.
    plan: Mapped[str | None] = mapped_column(Text)

    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    creado_por: Mapped[str | None] = mapped_column(String(120))
    terminado_en: Mapped[dt.datetime | None] = mapped_column(DateTime)

    # Quién está trabajando en él. Mientras su última actividad sea reciente, nadie más
    # puede escribir en el trabajo; pasado el plazo de los ajustes queda libre. `en_uso_sub`
    # es el identificador de Keycloak, que no cambia; el nombre es para enseñarlo.
    en_uso_sub: Mapped[str | None] = mapped_column(String(64))
    en_uso_nombre: Mapped[str | None] = mapped_column(String(120))
    en_uso_desde: Mapped[dt.datetime | None] = mapped_column(DateTime)
    ultima_actividad: Mapped[dt.datetime | None] = mapped_column(DateTime)

    paquete: Mapped[Paquete] = relationship(back_populates="trabajos")
    pendientes: Mapped[list["Termino"]] = relationship(
        back_populates="trabajo", cascade="all, delete-orphan", order_by="Termino.id")
    comentarios: Mapped[list["Comentario"]] = relationship(
        back_populates="trabajo", cascade="all, delete-orphan", order_by="Comentario.id.desc()")
    documentos: Mapped[list["Documento"]] = relationship(
        back_populates="trabajo", cascade="all, delete-orphan", order_by="Documento.id")
    sustituciones: Mapped[list["Sustitucion"]] = relationship(
        back_populates="trabajo", cascade="all, delete-orphan", order_by="Sustitucion.id")

    @property
    def destino(self) -> str:
        idioma = IDIOMAS.get(self.idioma_destino or "", self.idioma_destino or "")
        if self.tipo == TRADUCCION:
            return idioma or "—"
        return f"{self.comunidad or '—'}" + (f" · {idioma}" if idioma else "")

    @property
    def sin_revisar(self) -> int:
        return sum(1 for p in self.pendientes if p.estado == PENDIENTE)

    @property
    def estado_revision(self) -> str:
        if not self.pendientes:
            return REVISADO
        return REVISADO if self.sin_revisar == 0 else PENDIENTE


class Termino(Base):
    """Un punto del contenido que el sistema no decide por su cuenta.

    Guarda el término tal como estaba, la propuesta del trabajador y lo que finalmente
    decide el editor. Las tres columnas juntas son lo que permite que la próxima
    ejecución ya sepa qué hacer.
    """

    __tablename__ = "termino"

    id: Mapped[int] = mapped_column(primary_key=True)
    trabajo_id: Mapped[int] = mapped_column(ForeignKey("trabajo.id", ondelete="CASCADE"))

    clave: Mapped[str] = mapped_column(String(120))
    leccion: Mapped[str | None] = mapped_column(String(255))
    motivo: Mapped[str | None] = mapped_column(Text)

    original: Mapped[str] = mapped_column(Text)
    propuesta: Mapped[str] = mapped_column(Text)
    valor_final: Mapped[str | None] = mapped_column(Text)
    # El valor del elemento, con su marcado, tal como estaba **antes** de decidir. El
    # guardado aplica cada decisión a partir de aquí y no del paquete ya guardado: si no,
    # guardar dos veces volvía a aplicar lo decidido sobre lo ya aplicado y duplicaba
    # párrafos enteros cuando la decisión contenía un marcador [PENDIENTE].
    valor_base: Mapped[str | None] = mapped_column(Text)
    # Enlaces que el asistente ha encontrado y comprobado para este término, en JSON. Son
    # candidatos: el término sigue pendiente hasta que el editor elige uno.
    candidatas: Mapped[str | None] = mapped_column(Text)
    # Quién lo detectó si no fue el procesador: «asistente» para los que encuentra la
    # revisión completa de la unidad. Reprocesar los conserva; si no, se perderían.
    detectado_por: Mapped[str | None] = mapped_column(String(40))

    estado: Mapped[str] = mapped_column(String(20), default=PENDIENTE)
    decidido_por: Mapped[str | None] = mapped_column(String(120))
    actualizado_en: Mapped[dt.datetime | None] = mapped_column(DateTime, onupdate=func.now())

    trabajo: Mapped[Trabajo] = relationship(back_populates="pendientes")

    @property
    def resuelto(self) -> str:
        return self.valor_final if self.valor_final is not None else self.propuesta


class Comentario(Base):
    """Observaciones del editor sobre el resultado.

    No son notas sueltas: son la materia prima de la siguiente versión del trabajador.
    `aplicado` marca las que ya se han incorporado al toolkit.
    """

    __tablename__ = "comentario"

    id: Mapped[int] = mapped_column(primary_key=True)
    trabajo_id: Mapped[int] = mapped_column(ForeignKey("trabajo.id", ondelete="CASCADE"))
    autor: Mapped[str | None] = mapped_column(String(120))
    texto: Mapped[str] = mapped_column(Text)
    aplicado: Mapped[bool] = mapped_column(Boolean, default=False)
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    trabajo: Mapped[Trabajo] = relationship(back_populates="comentarios")


class Sustitucion(Base):
    """Una imagen de la unidad sustituida por otra en un trabajo.

    Se identifica por la huella de la imagen original, no por su nombre: si la misma
    imagen aparece con dos nombres, se sustituyen las dos. La imagen subida se guarda tal
    cual en `ruta`; al aplicarla se ajusta a la proporción y al formato de la original.
    """

    __tablename__ = "sustitucion"
    __table_args__ = (UniqueConstraint("trabajo_id", "huella", name="sustitucion_unica"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    trabajo_id: Mapped[int] = mapped_column(ForeignKey("trabajo.id", ondelete="CASCADE"))
    huella: Mapped[str] = mapped_column(String(64))
    fichero: Mapped[str] = mapped_column(String(500))        # la original, para enseñarla
    ruta: Mapped[str] = mapped_column(String(1000))          # la subida, relativa a DATOS
    nombre_subido: Mapped[str | None] = mapped_column(String(255))
    # «recortar»: llena el hueco de la original y recorta lo que sobra. «encajar»: entra
    # entera, con márgenes del color del borde.
    ajuste: Mapped[str] = mapped_column(String(10), default="recortar")
    autor: Mapped[str | None] = mapped_column(String(120))
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    trabajo: Mapped[Trabajo] = relationship(back_populates="sustituciones")


class Documento(Base):
    """Documentación del experto para un trabajo: un Word, un PDF o un texto pegado.

    `texto` es lo que lee el asistente; `imagenes` y `enlaces`, en JSON, lo que se puede
    usar para sustituir imágenes y para los términos de enlace.
    """

    __tablename__ = "documento"

    id: Mapped[int] = mapped_column(primary_key=True)
    trabajo_id: Mapped[int] = mapped_column(ForeignKey("trabajo.id", ondelete="CASCADE"))
    nombre: Mapped[str] = mapped_column(String(255))
    tipo: Mapped[str] = mapped_column(String(10))           # docx, pdf, txt, pegado
    ruta: Mapped[str | None] = mapped_column(String(1000))   # el fichero subido, relativo a DATOS
    texto: Mapped[str] = mapped_column(Text, default="")
    imagenes: Mapped[str | None] = mapped_column(Text)       # [{"nombre", "ruta"}]
    enlaces: Mapped[str | None] = mapped_column(Text)        # ["https://…"]
    propuestas: Mapped[int] = mapped_column(Integer, default=0)   # de la ficha rellena
    autor: Mapped[str | None] = mapped_column(String(120))
    rol: Mapped[str | None] = mapped_column(String(20))
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    trabajo: Mapped[Trabajo] = relationship(back_populates="documentos")


class Frase(Base):
    """Memoria de traducción: una frase aprobada, reutilizable en cualquier paquete.

    Indexada **por contenido**, no por componente. Esa es la diferencia que hace que el
    trabajo de una unidad sirva para la siguiente: los identificadores de componente son
    propios de cada paquete, el texto no.

    La clave es el par de lenguas más el texto de origen normalizado. `huella` guarda ese
    texto reducido a una firma corta para poder indexarlo sin arrastrar párrafos enteros
    en el índice.
    """

    __tablename__ = "frase"
    __table_args__ = (UniqueConstraint("idioma_origen", "idioma_destino", "huella",
                                       name="frase_unica"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    idioma_origen: Mapped[str] = mapped_column(String(8), index=True)
    idioma_destino: Mapped[str] = mapped_column(String(8), index=True)
    huella: Mapped[str] = mapped_column(String(64), index=True)

    origen: Mapped[str] = mapped_column(Text)
    destino: Mapped[str] = mapped_column(Text)

    veces_usada: Mapped[int] = mapped_column(Integer, default=0)
    procedencia: Mapped[str | None] = mapped_column(String(255))
    aprobada_por: Mapped[str | None] = mapped_column(String(120))
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())

    # Frases que se revisaron y no hay que tocar: rótulos de plantilla («Footer title»),
    # signos sueltos, nombres propios, siglas. Se guardan con `destino` igual al origen.
    # Sin esto volverían a salir en cada lote, porque nada distingue «aún no traducida» de
    # «revisada y correcta tal cual», y el ciclo no terminaría nunca. Además viajan entre
    # paquetes: los rótulos de la plantilla Berlin son los mismos en todas las unidades.
    invariante: Mapped[bool] = mapped_column(Boolean, default=False)

    # Si una persona la ha dado por buena. Las que traduce el asistente entran sin revisar:
    # se aplican para poder ver el resultado, pero se distinguen de las aprobadas y no
    # pueden pisar a una que ya lo esté. Las anteriores a esta columna son todas humanas.
    revisada: Mapped[bool] = mapped_column(Boolean, default=True)


class Equivalencia(Base):
    """Memoria de adaptación territorial: un concepto y su equivalente en otro territorio.

    No es traducción: «gneis ollo de sapo» no se convierte en «caliza de montaña» por
    saber gallego, sino por saber geología. Por eso **nunca se aplica sola**: se propone
    al editor, que decide. `aprobada` marca las que ya pasaron por revisión humana.
    """

    __tablename__ = "equivalencia"
    __table_args__ = (UniqueConstraint("huella", "origen_territorio", "destino_territorio",
                                       name="equivalencia_unica"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    huella: Mapped[str] = mapped_column(String(64), index=True)

    origen_territorio: Mapped[str | None] = mapped_column(String(80), index=True)
    destino_territorio: Mapped[str | None] = mapped_column(String(80), index=True)

    concepto: Mapped[str] = mapped_column(Text)
    propuesta: Mapped[str] = mapped_column(Text)
    motivo: Mapped[str | None] = mapped_column(Text)

    aprobada: Mapped[bool] = mapped_column(Boolean, default=False)
    veces_usada: Mapped[int] = mapped_column(Integer, default=0)
    procedencia: Mapped[str | None] = mapped_column(String(255))
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class ConsumoIA(Base):
    """Lo que ha costado cada operación del asistente, para medir el coste por unidad."""

    __tablename__ = "consumo_ia"

    id: Mapped[int] = mapped_column(primary_key=True)
    trabajo_id: Mapped[int | None] = mapped_column(ForeignKey("trabajo.id", ondelete="SET NULL"))
    operacion: Mapped[str] = mapped_column(String(20))
    modelo: Mapped[str] = mapped_column(String(60))
    entrada: Mapped[int] = mapped_column(Integer, default=0)
    salida: Mapped[int] = mapped_column(Integer, default=0)
    cache_lectura: Mapped[int] = mapped_column(Integer, default=0)
    cache_escritura: Mapped[int] = mapped_column(Integer, default=0)
    busquedas: Mapped[int] = mapped_column(Integer, default=0)
    coste_usd: Mapped[float] = mapped_column(default=0.0)
    creado_en: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


class Ajuste(Base):
    """Configuración de la aplicación que se cambia desde la pantalla de ajustes."""

    __tablename__ = "ajuste"

    clave: Mapped[str] = mapped_column(String(80), primary_key=True)
    valor: Mapped[str] = mapped_column(Text)   # JSON
    actualizado_por: Mapped[str | None] = mapped_column(String(120))
    actualizado_en: Mapped[dt.datetime | None] = mapped_column(DateTime, server_default=func.now(),
                                                               onupdate=func.now())


class Auditoria(Base):
    """Quién ha hecho qué, desde dónde y cuándo: entradas, acciones y bloqueos.

    Sin claves ajenas: el registro tiene que sobrevivir a que se borre el trabajo o el
    paquete al que se refiere. Guarda datos personales (nombre, correo, IP), así que se
    conserva un plazo limitado: ver `auditoria.CONSERVACION`.
    """

    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[dt.datetime] = mapped_column(DateTime, index=True)   # UTC
    sub: Mapped[str | None] = mapped_column(String(64), index=True)
    usuario: Mapped[str | None] = mapped_column(String(120))
    nombre: Mapped[str | None] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200))
    canal: Mapped[str | None] = mapped_column(String(10))     # web, api, mcp
    ip: Mapped[str | None] = mapped_column(String(64))
    accion: Mapped[str] = mapped_column(String(40), index=True)
    trabajo_id: Mapped[int | None] = mapped_column(Integer, index=True)
    paquete_id: Mapped[int | None] = mapped_column(Integer)
    detalle: Mapped[str | None] = mapped_column(Text)
    resultado: Mapped[str] = mapped_column(String(12), default="ok")   # ok, denegado


def ahora() -> dt.datetime:
    """La hora en UTC y sin zona, que es como la guarda la base (`now()` en UTC)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


motor = create_engine(URL, echo=False,
                      connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {})
Sesion = sessionmaker(bind=motor, expire_on_commit=False)


def crear_esquema() -> None:
    """Crea lo que falte y añade las columnas nuevas a las tablas que ya existan.

    El esquema del demostrador todavía se mueve, y `create_all` no toca una tabla ya
    creada: al añadir una columna, la instalación anterior seguía sin ella y fallaba al
    escribir. Esto cubre el caso corriente —columnas nuevas, opcionales o con valor por
    defecto— que es el único que se ha dado. Para cambios de tipo o renombrados haría
    falta una herramienta de migraciones de verdad.
    """
    from sqlalchemy import inspect, text

    Base.metadata.create_all(motor)

    inspector = inspect(motor)
    existentes = set(inspector.get_table_names())
    with motor.begin() as conexion:
        for tabla in Base.metadata.sorted_tables:
            if tabla.name not in existentes:
                continue
            columnas = {c["name"] for c in inspector.get_columns(tabla.name)}
            for columna in tabla.columns:
                if columna.name in columnas:
                    continue
                tipo = columna.type.compile(motor.dialect)
                defecto = ""
                if columna.default is not None and columna.default.is_scalar:
                    valor = columna.default.arg
                    defecto = f" DEFAULT {valor!r}" if isinstance(valor, str) else f" DEFAULT {valor}"
                conexion.execute(text(
                    f"ALTER TABLE {tabla.name} ADD COLUMN {columna.name} {tipo}{defecto}"))
