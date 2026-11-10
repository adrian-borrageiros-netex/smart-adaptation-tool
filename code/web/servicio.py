"""Operaciones del demostrador, sin depender de ningún framework.

Todo lo que se puede hacer con el sistema está aquí, y devuelve diccionarios corrientes.
La interfaz web, la API REST y el servidor MCP son tres envoltorios finos sobre estas
mismas funciones: así no hay una vía «buena» y otra de segunda, y lo que se pruebe desde
Claude es exactamente lo que hace la pantalla.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from . import auditoria, bd, memoria
from .trabajador import (TRABAJO, aplicar_traducciones, frases_pendientes,
                         guardar_decisiones, procesar, reprocesar)

from maker_paquete import texto_plano  # noqa: E402  (src entra en la ruta con trabajador)

SUBIDAS = TRABAJO / "subidas"
EXTRAIDOS = TRABAJO / "extraidos"


class ErrorServicio(Exception):
    """Fallo esperable, con un mensaje que se le puede enseñar a una persona."""


class TrabajoOcupado(ErrorServicio):
    """Otra persona está trabajando en ese trabajo."""

    def __init__(self, trabajo_id: int, nombre: str | None, desde: dt.datetime | None):
        from .horas import hora
        self.trabajo_id = trabajo_id
        super().__init__(f"El trabajo {trabajo_id} lo está trabajando {nombre or 'otra persona'} "
                         f"desde las {hora(desde)}. Puedes consultar el informe y el contenido "
                         "generado, pero no cambiar nada hasta que lo deje.")


# ------------------------------------------------------- quién trabaja en cada trabajo
# Dos personas guardando a la vez el mismo trabajo se pisan: guardar reescribe el paquete
# entero. Por eso quien abre un trabajo libre se lo queda, y todo lo que escribe en él
# —por la web, la API o el MCP— pasa por `exigir`. El bloqueo caduca solo tras un rato
# sin actividad de su titular (ajuste «bloqueo»): el navegador no avisa con fiabilidad
# cuando alguien cierra la pestaña. Un administrador puede consultarlo siempre y quitárselo
# a otro de forma explícita, pero no escribir a la vez: volvería el mismo problema.

BLOQUEO = "bloqueo"
MINUTOS_BLOQUEO = 30


def minutos_bloqueo() -> int:
    with bd.Sesion() as s:
        fila = s.get(bd.Ajuste, BLOQUEO)
    try:
        return int(json.loads(fila.valor)["minutos"]) if fila else MINUTOS_BLOQUEO
    except (ValueError, KeyError, TypeError):
        return MINUTOS_BLOQUEO


def guardar_minutos_bloqueo(minutos: int) -> int:
    if not 5 <= minutos <= 480:
        raise ErrorServicio("El plazo del bloqueo tiene que estar entre 5 y 480 minutos.")
    with bd.Sesion() as s:
        fila = s.get(bd.Ajuste, BLOQUEO) or bd.Ajuste(clave=BLOQUEO, valor="{}")
        anterior = json.loads(fila.valor).get("minutos", MINUTOS_BLOQUEO)
        fila.valor = json.dumps({"minutos": minutos})
        fila.actualizado_por = auditoria.firma()
        s.add(fila)
        s.commit()
    if anterior != minutos:
        auditoria.registrar("ajustes", detalle=f"Bloqueo de trabajos: {anterior} → {minutos} minutos")
    return minutos


def _limite() -> dt.datetime:
    return bd.ahora() - dt.timedelta(minutes=minutos_bloqueo())


def _en_uso(t: bd.Trabajo, limite: dt.datetime | None = None) -> dict[str, Any] | None:
    """Quién lo tiene, si el bloqueo sigue vigente. `mio` es relativo a quien pregunta."""
    limite = limite or _limite()
    if not t.en_uso_sub or not t.ultima_actividad or t.ultima_actividad < limite:
        return None
    yo = auditoria.usuario_actual() or {}
    plazo = dt.timedelta(minutes=minutos_bloqueo())
    return {"nombre": t.en_uso_nombre, "mio": t.en_uso_sub == yo.get("sub"),
            "desde": t.en_uso_desde.isoformat() if t.en_uso_desde else None,
            "ultima_actividad": t.ultima_actividad.isoformat(),
            "caduca": (t.ultima_actividad + plazo).isoformat()}


def _ocupado(trabajo_id: int) -> ErrorServicio:
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            return ErrorServicio(f"No existe el trabajo {trabajo_id}")
        return TrabajoOcupado(trabajo_id, t.en_uso_nombre, t.en_uso_desde)


def tomar_trabajo(trabajo_id: int, forzar: bool = False) -> dict[str, Any]:
    """Quien llama pasa a ser el titular, si está libre, caducado o ya era suyo.

    Es una sola sentencia condicionada: si dos personas lo abren a la vez, solo una lo
    consigue. `forzar` se lo quita a quien lo tenga; solo puede hacerlo un administrador.
    """
    from sqlalchemy import and_, case, or_, true, update
    yo = auditoria.usuario_actual()
    if not yo or not yo.get("sub"):
        raise ErrorServicio("No se sabe quién hace la petición.")
    if forzar and not auditoria.es_admin(yo):
        raise ErrorServicio("Solo un administrador puede quitarle un trabajo a otra persona.")
    T = bd.Trabajo
    ahora, limite = bd.ahora(), _limite()
    with bd.Sesion() as s:
        actual = s.get(T, trabajo_id)
        if actual is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        antes = _en_uso(actual, limite)
        sigue = and_(T.en_uso_sub == yo["sub"], T.ultima_actividad >= limite)
        condicion = true() if forzar else or_(
            T.en_uso_sub.is_(None), T.ultima_actividad.is_(None),
            T.ultima_actividad < limite, T.en_uso_sub == yo["sub"])
        r = s.execute(update(T).where(T.id == trabajo_id, condicion).values(
            en_uso_sub=yo["sub"], en_uso_nombre=auditoria.firma(yo),
            en_uso_desde=case((sigue, T.en_uso_desde), else_=ahora),
            ultima_actividad=ahora).execution_options(synchronize_session=False))
        s.commit()
    if r.rowcount != 1:
        raise _ocupado(trabajo_id)
    if antes is None or not antes["mio"]:
        auditoria.registrar("tomar", trabajo_id=trabajo_id,
                            detalle=f"Se lo quita a {antes['nombre']}" if antes else None)
    return {"trabajo_id": trabajo_id, "en_uso": True}


def exigir(trabajo_id: int, accion: str) -> None:
    """Antes de escribir en un trabajo: hay que tenerlo, o que esté libre para tomarlo."""
    try:
        tomar_trabajo(trabajo_id)
    except TrabajoOcupado as e:
        auditoria.registrar(accion, trabajo_id=trabajo_id, resultado="denegado", detalle=str(e))
        raise


def seguir_en_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Señal de vida de la página de quien lo tiene: aplaza la caducidad, no lo toma."""
    from sqlalchemy import update
    yo = auditoria.usuario_actual() or {}
    with bd.Sesion() as s:
        r = s.execute(update(bd.Trabajo).where(
            bd.Trabajo.id == trabajo_id, bd.Trabajo.en_uso_sub == yo.get("sub"),
            bd.Trabajo.ultima_actividad >= _limite()).values(ultima_actividad=bd.ahora())
            .execution_options(synchronize_session=False))
        s.commit()
    return {"trabajo_id": trabajo_id, "en_uso": r.rowcount == 1}


def dejar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Lo libera su titular, o un administrador sea de quien sea."""
    yo = auditoria.usuario_actual() or {}
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        en_uso = _en_uso(t)
        if en_uso and not en_uso["mio"] and not auditoria.es_admin(yo):
            raise TrabajoOcupado(trabajo_id, t.en_uso_nombre, t.en_uso_desde)
        titular = t.en_uso_nombre
        t.en_uso_sub = t.en_uso_nombre = t.en_uso_desde = None
        s.commit()
    if en_uso:
        auditoria.registrar("dejar" if en_uso["mio"] else "liberar", trabajo_id=trabajo_id,
                            detalle=None if en_uso["mio"] else f"Se lo quita a {titular}")
    return {"trabajo_id": trabajo_id, "en_uso": False}


def dejar_todos(sub: str) -> int:
    """Al cerrar sesión se liberan sus trabajos."""
    from sqlalchemy import update
    with bd.Sesion() as s:
        r = s.execute(update(bd.Trabajo).where(bd.Trabajo.en_uso_sub == sub).values(
            en_uso_sub=None, en_uso_nombre=None, en_uso_desde=None)
            .execution_options(synchronize_session=False))
        s.commit()
    return r.rowcount


def _trabajo_de_termino(termino_id: int) -> int:
    with bd.Sesion() as s:
        termino = s.get(bd.Termino, termino_id)
        if termino is None:
            raise ErrorServicio(f"No existe el término {termino_id}")
        return termino.trabajo_id


# --------------------------------------------------------------------- consultas

def _paquete_dict(p: bd.Paquete) -> dict[str, Any]:
    # Etapa, curso y materia, para buscar: los de un trabajo suyo si los tiene (se pueden
    # haber corregido a mano), y si no, lo que digan su título y su ruta en learningCloud.
    from . import curriculo
    de_trabajo = next((t for t in p.trabajos if t.materia or t.curso), None)
    if de_trabajo:
        ficha = {"etapa": de_trabajo.etapa, "curso": de_trabajo.curso, "materia": de_trabajo.materia}
    else:
        ruta = " ".join(json.loads(p.origen).get("ruta") or []) if p.origen else ""
        ficha = curriculo.deducir([p.titulo or "", p.nombre or "", ruta])
    return {**ficha,
        "id": p.id, "nombre": p.nombre, "titulo": p.titulo,
        "idioma_origen": p.idioma_origen, "plantilla": p.plantilla,
        "formato": p.formato or "maker", "notas": p.notas,
        "componentes": p.componentes, "lecciones": p.lecciones,
        "subido_en": p.subido_en.isoformat() if p.subido_en else None,
        "subido_por": p.subido_por,
        "trabajos": [t.id for t in p.trabajos],
    }


def _trabajo_dict(t: bd.Trabajo, con_terminos: bool = False,
                  limite: dt.datetime | None = None) -> dict[str, Any]:
    datos = {
        "id": t.id, "paquete_id": t.paquete_id, "paquete": t.paquete.nombre,
        "titulo": t.paquete.titulo,
        "tipo": t.tipo, "idioma_destino": t.idioma_destino, "comunidad": t.comunidad,
        "idioma_origen": t.paquete.idioma_origen,
        "destino": t.destino, "estado": t.estado, "mensaje": t.mensaje,
        "elementos_totales": t.elementos_totales,
        "elementos_adaptados": t.elementos_adaptados,
        "avisos_validacion": t.avisos_validacion,
        "frases_totales": t.frases_totales,
        "frases_conocidas": t.frases_conocidas,
        "terminos_pendientes": t.sin_revisar,
        "estado_revision": t.estado_revision,
        "creado_en": t.creado_en.isoformat() if t.creado_en else None,
        "creado_por": t.creado_por,
        "terminado_en": t.terminado_en.isoformat() if t.terminado_en else None,
        "ultima_actividad": t.ultima_actividad.isoformat() if t.ultima_actividad else None,
        "en_uso": _en_uso(t, limite),
        "formato": t.paquete.formato or "maker",
        # Un fuente de Author no tiene reproductor: no se puede ver en el navegador.
        "url_contenido": (f"/contenido/{t.id}/index.html"
                          if t.estado == bd.LISTO and (t.paquete.formato or "maker") == "maker"
                          else None),
        "url_informe": f"/informe/{t.id}" if t.ruta_informe else None,
        "url_descarga": f"/descargar/{t.id}" if t.estado == bd.LISTO else None,
        "envio": json.loads(t.envio) if t.envio else None,
        "imagenes": json.loads(t.imagenes) if t.imagenes else None,
        "regenerar_imagenes": t.regenerar_imagenes is not False,
        "etapa": t.etapa, "curso": t.curso, "materia": t.materia,
        "plan": json.loads(t.plan) if t.plan else None,
    }
    if con_terminos:
        datos["terminos"] = [_termino_dict(x) for x in t.pendientes]
        datos["comentarios"] = [
            {"id": c.id, "autor": c.autor, "texto": c.texto, "aplicado": c.aplicado,
             "creado_en": c.creado_en.isoformat() if c.creado_en else None}
            for c in t.comentarios]
    return datos


def _como_texto(valor: str | None) -> str:
    """Lo que ve el editor: texto, nunca marcado."""
    if not valor:
        return ""
    return texto_plano(valor) if valor.lstrip().startswith("<") else valor


def _termino_dict(t: bd.Termino) -> dict[str, Any]:
    propuesta = _como_texto(t.propuesta)
    return {"id": t.id, "clave": t.clave, "leccion": t.leccion, "motivo": t.motivo,
            "original": t.original, "propuesta": t.propuesta,
            "valor_final": t.valor_final, "estado": t.estado,
            "decidido_por": t.decidido_por,
            # La caja del editor viene rellena: con lo que ya decidió o, si no, con la
            # propuesta del sistema. Siempre como texto, aunque la decisión sea HTML.
            "texto_editable": _como_texto(t.valor_final) or propuesta,
            # Cuando la propuesta es el propio original, rellenar la caja invita a
            # aceptarla sin mirar. La pantalla lo avisa.
            "propuesta_sin_cambio": memoria.normalizar(propuesta)
                                    == memoria.normalizar(t.original),
            "candidatas": json.loads(t.candidatas) if t.candidatas else []}


def listar_paquetes() -> list[dict[str, Any]]:
    with bd.Sesion() as s:
        return [_paquete_dict(p) for p in
                s.query(bd.Paquete).order_by(bd.Paquete.id.desc()).all()]


def listar_trabajos(estado: str | None = None) -> list[dict[str, Any]]:
    with bd.Sesion() as s:
        consulta = s.query(bd.Trabajo).order_by(bd.Trabajo.id.desc())
        if estado:
            consulta = consulta.filter(bd.Trabajo.estado == estado)
        limite = _limite()
        return [_trabajo_dict(t, limite=limite) for t in consulta.all()]


def obtener_trabajo(trabajo_id: int) -> dict[str, Any]:
    from . import curriculo
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        datos = _trabajo_dict(t, con_terminos=True)
    # El trozo del currículo de destino que recibirá el asistente, si lo hay.
    _, ficha = curriculo.para_trabajo(datos["comunidad"], datos["etapa"], datos["materia"],
                                      datos["curso"])
    datos["curriculo"] = ({k: ficha[k] for k in ("comunidad", "materia", "etapa", "cursos",
                                                  "exacto", "norma", "boletin", "url")}
                          if ficha else None)
    datos["curriculo_disponible"] = curriculo.disponible(datos["comunidad"])
    return datos


def deducir_unidad(paquete_id: int) -> dict[str, Any]:
    """Etapa, curso y materia de una unidad, leídos de su rótulo de portada, su título y su
    ruta en learningCloud."""
    from maker_paquete import abrir_unidad

    from . import curriculo
    with bd.Sesion() as s:
        p = s.get(bd.Paquete, paquete_id)
        textos = [p.titulo or "", p.nombre or ""]
        if p.origen:
            textos.append(" ".join(json.loads(p.origen).get("ruta") or []))
        ruta = bd.ruta(p.ruta_extraido)
    try:
        unidad = abrir_unidad(ruta)
        textos = [c["properties"].get(k) or "" for c in unidad.componentes.values()
                  if c.get("name") == "interface" for k in ("title1", "title2")] + textos
    except Exception:
        pass
    return curriculo.deducir(textos)


def opciones() -> dict[str, Any]:
    """Qué destinos admite el sistema. Lo consulta la web y también el MCP."""
    return {"idiomas": bd.IDIOMAS, "comunidades": bd.COMUNIDADES,
            "tipos": [bd.TRADUCCION, bd.ADAPTACION],
            "idioma_por_comunidad": bd.IDIOMA_POR_COMUNIDAD}


# ---------------------------------------------------------------------- acciones

def subir_paquete(nombre: str, contenido: bytes) -> dict[str, Any]:
    """Guarda el zip, lo extrae y lee su ficha sin modificarlo."""
    return subir_paquetes([(nombre, contenido)])


def _es_maker(carpeta: Path) -> bool:
    return (carpeta / "project.json").is_file()


def _proyectos(carpeta: Path, profundidad: int = 0) -> list[Path]:
    """Las carpetas con un paquete o un proyecto dentro de lo extraído.

    Admite el zip tal cual, el zip con una carpeta raíz, un zip con varios zips dentro
    —los de una unidad de Author y sus actividades, descargados de uno en uno— y una
    carpeta con un proyecto por subcarpeta. Los zips de dentro de un proyecto (la sopa
    de letras de Prehistoria, por ejemplo) no se tocan: son recursos.
    """
    from maker_paquete import es_fuente
    if _es_maker(carpeta) or es_fuente(carpeta):
        return [carpeta]
    if profundidad > 2:
        return []
    encontrados = []
    for z in sorted(carpeta.glob("*.zip")):
        dentro = carpeta / z.stem
        try:
            with zipfile.ZipFile(z) as f:
                f.extractall(dentro)
        except zipfile.BadZipFile:
            continue
        z.unlink()
        encontrados += _proyectos(dentro, profundidad + 1)
    for hijo in sorted(p for p in carpeta.iterdir() if p.is_dir()):
        if not any(hijo == e or hijo in e.parents for e in encontrados):
            encontrados += _proyectos(hijo, profundidad + 1)
    return encontrados


def subir_paquetes(ficheros: list[tuple[str, bytes]],
                   origen: dict[str, Any] | None = None) -> dict[str, Any]:
    """Guarda lo subido, lo extrae y lee su ficha sin modificarlo.

    Un paquete publicado de Maker es un zip. Un fuente de Author son varios: el de la
    unidad y uno por cada proyecto de actividades, que se pueden subir a la vez o en un
    zip que los contenga. La unidad queda en la raíz de lo extraído y las actividades
    en `_actividades/`, de modo que todo lo que trabaja sobre `resources/` sirve igual.

    `origen` es la carpeta de learningCloud de la que lo trajo el marcador: proyecto,
    carpeta y ruta. Es la que se replica al enviar el resultado.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from maker_paquete import ACTIVIDADES, FuenteAuthor, MapaEditorial, abrir_unidad
    from maker_paquete.author import PLANTILLA_ACTIVIDADES, es_fuente, plantilla_de

    if not ficheros:
        raise ErrorServicio("No se ha subido ningún fichero.")
    SUBIDAS.mkdir(parents=True, exist_ok=True)
    EXTRAIDOS.mkdir(parents=True, exist_ok=True)

    nombre = ficheros[0][0] if len(ficheros) == 1 else f"{Path(ficheros[0][0]).stem}-y-{len(ficheros) - 1}.zip"
    seguro = "".join(c for c in Path(nombre).stem if c.isalnum() or c in "-_") or "paquete"
    with bd.Sesion() as s:
        siguiente = (s.query(bd.Paquete).count() or 0) + 1
    destino_zip = SUBIDAS / f"{siguiente:03d}-{seguro}.zip"
    if len(ficheros) == 1:
        destino_zip.write_bytes(ficheros[0][1])
    else:
        # Varios ficheros: se guardan tal cual, juntos en un zip.
        with zipfile.ZipFile(destino_zip, "w", zipfile.ZIP_STORED) as z:
            for n, contenido in ficheros:
                z.writestr(Path(n).name, contenido)

    extraido = EXTRAIDOS / f"{siguiente:03d}-{seguro}"
    temporal = extraido.with_name(extraido.name + ".subiendo")
    for carpeta in (extraido, temporal):
        if carpeta.exists():
            shutil.rmtree(carpeta)

    def fallar(mensaje: str, causa: Exception | None = None):
        shutil.rmtree(temporal, ignore_errors=True)
        shutil.rmtree(extraido, ignore_errors=True)
        destino_zip.unlink(missing_ok=True)
        raise ErrorServicio(mensaje) from causa

    try:
        for i, (n, contenido) in enumerate(ficheros):
            import io
            with zipfile.ZipFile(io.BytesIO(contenido)) as z:
                z.extractall(temporal / (Path(n).stem if len(ficheros) > 1 else "_"))
    except zipfile.BadZipFile as e:
        fallar("Algún fichero no es un zip válido.", e)

    proyectos = _proyectos(temporal)
    maker = [p for p in proyectos if _es_maker(p)]
    author = [p for p in proyectos if es_fuente(p)]
    notas = []
    if maker and not author:
        if len(maker) > 1:
            fallar(f"Hay {len(maker)} paquetes publicados: súbelos de uno en uno.")
        shutil.move(str(maker[0]), str(extraido))
        formato = "maker"
    elif author and not maker:
        unidades = [p for p in author if plantilla_de(p) != PLANTILLA_ACTIVIDADES]
        if len(unidades) != 1:
            fallar("En un fuente de Author tiene que haber una unidad y sus actividades; "
                   f"hay {len(unidades)} unidades." if unidades else
                   "Solo hay proyectos de actividades: falta el fuente de la unidad.")
        shutil.move(str(unidades[0]), str(extraido))
        (extraido / ACTIVIDADES).mkdir(exist_ok=True)
        for p in author:
            if p is not unidades[0]:
                shutil.move(str(p), str(extraido / ACTIVIDADES / p.name))
        (extraido / "_fuente.json").write_text(
            json.dumps({"unidad": unidades[0].name if unidades[0].name != "_" else Path(nombre).stem},
                       ensure_ascii=False), encoding="utf-8")
        formato = "author"
    elif maker and author:
        fallar("Se han mezclado un paquete publicado y un fuente de Author.")
    else:
        fallar("No parece un paquete de Maker ni un fuente de Author.")
    shutil.rmtree(temporal, ignore_errors=True)

    try:
        paquete = abrir_unidad(extraido)
        ficha = paquete.resumen()
    except Exception as e:
        fallar(f"No se ha podido abrir: {e}", e)

    if formato == "author":
        # Lo que conviene saber antes de encargar nada: si faltan actividades, sus textos
        # se quedarían sin adaptar.
        assert isinstance(paquete, FuenteAuthor)
        if paquete.sin_resolver:
            notas.append(f"Faltan {len(paquete.sin_resolver)} de {ficha['conectores']} proyectos "
                         f"de actividades (ids {', '.join(paquete.sin_resolver)}): sus textos no "
                         "se adaptarán. Súbelos junto a la unidad.")
        enlazados = {(c["properties"].get("projectDataLink") or "").strip()
                     for c in paquete.componentes.values() if c["name"] == "quizConnector574"}
        sueltos = [p.nombre for i, p in paquete.actividades.items() if i not in enlazados]
        if sueltos:
            notas.append(f"{len(sueltos)} proyectos de actividades no son de esta unidad y se "
                         f"ignoran: {', '.join(sueltos)}.")

    # El idioma del contenido se lee en el texto: la clave del paquete puede mentir. «5, Los
    # Invertebrados» declaraba `es` y estaba en gallego, y el fuente de «6. A Prehistoria»
    # declara `es` con el contenido en gallego.
    from . import idioma as deteccion
    declarado = (ficha.get("idioma_declarado") if formato == "author"
                 else paquete.idiomas[0] if paquete.idiomas else None)
    leido, confianza = deteccion.detectar([e.plano for e in MapaEditorial(paquete).redactables])
    with bd.Sesion() as s:
        fila = bd.Paquete(
            nombre=Path(nombre).name, titulo=ficha.get("titulo"),
            idioma_origen=leido if leido and confianza >= 0.6 else (
                paquete.idiomas[0] if paquete.idiomas else declarado),
            idioma_declarado=declarado, formato=formato, notas="\n".join(notas) or None,
            plantilla=ficha.get("plantilla"), componentes=ficha.get("componentes", 0),
            lecciones=ficha.get("lecciones", 0),
            origen=json.dumps(origen, ensure_ascii=False) if origen and formato == "author" else None,
            ruta_zip=bd.relativa(destino_zip), ruta_extraido=bd.relativa(extraido),
            subido_por=auditoria.firma())
        s.add(fila)
        s.commit()
        datos = _paquete_dict(fila)
    auditoria.registrar("subir", paquete_id=datos["id"],
                        detalle=f"{datos['nombre']} ({formato})"
                                + (" desde learningCloud" if origen else ""))
    return datos


def crear_trabajo(paquete_id: int, tipo: str, idioma_destino: str | None = None,
                  comunidad: str | None = None, ejecutar: bool = True,
                  regenerar_imagenes: bool = True) -> dict[str, Any]:
    if tipo not in (bd.TRADUCCION, bd.ADAPTACION):
        raise ErrorServicio(f"Tipo desconocido: {tipo}")
    if tipo == bd.TRADUCCION and idioma_destino not in bd.IDIOMAS:
        raise ErrorServicio("Hay que indicar un idioma de destino válido.")
    if tipo == bd.ADAPTACION and comunidad not in bd.COMUNIDADES:
        raise ErrorServicio("Hay que indicar una comunidad autónoma válida.")
    if tipo == bd.ADAPTACION and idioma_destino and idioma_destino not in bd.IDIOMAS:
        raise ErrorServicio("El idioma indicado no está admitido.")

    with bd.Sesion() as s:
        if s.get(bd.Paquete, paquete_id) is None:
            raise ErrorServicio(f"No existe el paquete {paquete_id}")
        # Una adaptación curricular puede llevar cambio de idioma o no: a Cataluña suele
        # ir en catalán, a Madrid en castellano. Si no se indica, se propone el habitual
        # de la comunidad.
        if tipo == bd.ADAPTACION and not idioma_destino:
            idioma_destino = bd.idioma_sugerido(comunidad)
    unidad = deducir_unidad(paquete_id)
    with bd.Sesion() as s:
        # Quien lo encarga es quien lo trabaja, de entrada.
        yo, ahora = auditoria.usuario_actual() or {}, bd.ahora()
        trabajo = bd.Trabajo(paquete_id=paquete_id, tipo=tipo,
                             idioma_destino=idioma_destino,
                             comunidad=comunidad if tipo == bd.ADAPTACION else None,
                             regenerar_imagenes=regenerar_imagenes,
                             etapa=unidad["etapa"], curso=unidad["curso"],
                             materia=unidad["materia"],
                             creado_por=auditoria.firma(yo),
                             en_uso_sub=yo.get("sub"), en_uso_nombre=auditoria.firma(yo),
                             en_uso_desde=ahora if yo.get("sub") else None,
                             ultima_actividad=ahora if yo.get("sub") else None)
        s.add(trabajo)
        s.commit()
        trabajo_id = trabajo.id
    auditoria.registrar("encargar", trabajo_id=trabajo_id, paquete_id=paquete_id,
                        detalle=f"{tipo} → " + " · ".join(filter(None, [comunidad, idioma_destino])))

    if ejecutar:
        procesar(trabajo_id)
    return obtener_trabajo(trabajo_id)


def resolver_termino(termino_id: int, valor_final: str,
                     marcar_revisado: bool = True) -> dict[str, Any]:
    """Fija el texto definitivo de un término y **devuelve la decisión a la memoria**.

    Este es el punto donde el sistema aprende: lo que decide un editor sobre una unidad
    deja de ser un dato de ese trabajo y pasa a estar disponible para el siguiente.
    """
    trabajo_id = _trabajo_de_termino(termino_id)
    exigir(trabajo_id, "resolver término")
    with bd.Sesion() as s:
        termino = s.get(bd.Termino, termino_id)
        termino.valor_final = valor_final
        termino.estado = bd.REVISADO if marcar_revisado else bd.PENDIENTE
        termino.decidido_por = auditoria.firma()
        trabajo = termino.trabajo
        contexto = (trabajo.tipo, trabajo.comunidad, trabajo.idioma_destino,
                    trabajo.paquete.idioma_origen, f"trabajo {trabajo.id}")
        original = termino.original
        s.commit()
        salida = _termino_dict(termino)
    auditoria.registrar("resolver término", trabajo_id=trabajo_id,
                        detalle=f"{salida['clave']}: {_como_texto(valor_final)[:300]}")

    # A la memoria va texto, no marcado: se propondrá en otros paquetes con otro HTML. Y
    # confirmar el texto tal cual no es una equivalencia: guardarlo como tal hacía que
    # luego se propusiera «el sistema conoce un equivalente» con el mismo texto.
    decidido = _como_texto(valor_final)
    sin_cambio = memoria.normalizar(decidido) == memoria.normalizar(original)
    if marcar_revisado and decidido.strip() and not sin_cambio:
        valor_final = decidido
        tipo, comunidad, idioma, idioma_origen, procedencia = contexto
        if tipo == bd.ADAPTACION and comunidad:
            # Una decisión territorial se guarda como equivalencia: volverá a proponerse,
            # no a aplicarse sola.
            memoria.guardar_equivalencia(original, valor_final, None, comunidad,
                                         motivo="Decidido por el editor en la revisión",
                                         aprobada=True, procedencia=procedencia)
        elif idioma and idioma_origen and idioma != idioma_origen:
            memoria.guardar_frase(idioma_origen, idioma, original, valor_final,
                                  procedencia=procedencia)
    return salida


def guardar_paquete(trabajo_id: int) -> dict[str, Any]:
    """Aplica las decisiones del editor al paquete y lo deja descargable."""
    with bd.Sesion() as s:
        if s.get(bd.Trabajo, trabajo_id) is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
    exigir(trabajo_id, "guardar")
    zip_final = guardar_decisiones(trabajo_id)
    auditoria.registrar("guardar", trabajo_id=trabajo_id)
    return {"trabajo_id": trabajo_id, "zip": str(zip_final),
            "url_descarga": f"/descargar/{trabajo_id}"}


def anotar_comentario(trabajo_id: int, texto: str, autor: str | None = None) -> dict[str, Any]:
    """Guarda una observación del editor. Es la entrada de la próxima versión del toolkit."""
    if not (texto or "").strip():
        raise ErrorServicio("El comentario está vacío.")
    with bd.Sesion() as s:
        if s.get(bd.Trabajo, trabajo_id) is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
    exigir(trabajo_id, "comentar")
    with bd.Sesion() as s:
        comentario = bd.Comentario(trabajo_id=trabajo_id, texto=texto.strip(),
                                   autor=autor or auditoria.firma())
        s.add(comentario)
        s.commit()
        datos = {"id": comentario.id, "trabajo_id": trabajo_id, "texto": comentario.texto}
    auditoria.registrar("comentar", trabajo_id=trabajo_id, detalle=datos["texto"][:300])
    return datos


def pedir_lote(trabajo_id: int, tamano: int = 40) -> dict[str, Any]:
    """Frases que el sistema todavía no sabe traducir, listas para que alguien las traduzca."""
    exigir(trabajo_id, "pedir lote")
    try:
        return frases_pendientes(trabajo_id, tamano)
    except ValueError as e:
        raise ErrorServicio(str(e)) from e


def entregar_lote(trabajo_id: int, traducciones: list[dict[str, str]],
                  autor: str | None = None) -> dict[str, Any]:
    """Guarda un lote traducido en memoria y vuelve a procesar el trabajo."""
    if not traducciones:
        raise ErrorServicio("El lote llega vacío.")
    exigir(trabajo_id, "entregar lote")
    try:
        datos = aplicar_traducciones(trabajo_id, traducciones, autor or auditoria.firma())
    except ValueError as e:
        raise ErrorServicio(str(e)) from e
    auditoria.registrar("entregar lote", trabajo_id=trabajo_id,
                        detalle=f"{len(traducciones)} traducciones")
    return datos


def reprocesar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Vuelve a ejecutar el trabajo con la memoria actual, conservando lo ya decidido."""
    with bd.Sesion() as s:
        if s.get(bd.Trabajo, trabajo_id) is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
    exigir(trabajo_id, "reprocesar")
    auditoria.registrar("reprocesar", trabajo_id=trabajo_id)
    reprocesar(trabajo_id)
    return obtener_trabajo(trabajo_id)


def borrar_trabajo(trabajo_id: int) -> dict[str, Any]:
    """Borra un trabajo que ya no sirve: sus filas y sus ficheros, con copia antes.

    Lo puede borrar quien lo encargó o un administrador, y no mientras se procesa ni con
    el asistente en marcha. La copia —filas en JSON y ficheros en un tar— queda en el
    volumen de datos, en `copias/`, para recuperarlo si hiciera falta. El paquete y la
    memoria no se tocan: lo que se decidió en el trabajo sigue sirviendo a los demás.
    """
    import tarfile
    from sqlalchemy import inspect
    yo = auditoria.usuario_actual() or {}
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        if not auditoria.es_admin(yo) and t.creado_por != auditoria.firma(yo):
            auditoria.registrar("borrar", trabajo_id=trabajo_id, resultado="denegado",
                                detalle=f"Lo encargó {t.creado_por or 'otra persona'}")
            raise ErrorServicio("Solo puede borrar el trabajo quien lo encargó o un administrador.")
        if t.estado in (bd.ENCOLADO, bd.PROCESANDO) or t.ia_estado == "en_curso":
            raise ErrorServicio("El trabajo se está procesando o el asistente está trabajando en "
                                "él. Espera a que termine para borrarlo.")
    exigir(trabajo_id, "borrar")

    def fila(o):
        return {c.key: (v.isoformat() if hasattr(v, "isoformat") else v)
                for c in inspect(o).mapper.column_attrs for v in [getattr(o, c.key)]}

    copia = bd.DATOS / "copias" / f"trabajo-{trabajo_id}-{bd.ahora():%Y-%m-%d-%H%M%S}"
    copia.mkdir(parents=True, exist_ok=True)
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        titulo = f"{t.paquete.titulo or t.paquete.nombre} → {t.destino}"
        filas = {"trabajo": fila(t), "terminos": [fila(x) for x in t.pendientes],
                 "comentarios": [fila(x) for x in t.comentarios],
                 "sustituciones": [fila(x) for x in t.sustituciones],
                 "documentos": [fila(x) for x in t.documentos],
                 "consumo": [fila(x) for x in s.query(bd.ConsumoIA).filter_by(trabajo_id=trabajo_id)]}
        resultado = bd.ruta(t.ruta_resultado) if t.ruta_resultado else None
        candidatos = [resultado, resultado and resultado.with_suffix(".zip"),
                      bd.ruta(t.ruta_informe) if t.ruta_informe else None,
                      bd.DATOS / "resultados" / f"trabajo-{trabajo_id}-imagenes-pdf",
                      bd.DATOS / "sustituciones" / f"trabajo-{trabajo_id}",
                      bd.DATOS / "documentos" / f"trabajo-{trabajo_id}"]
    ficheros = [p for p in candidatos if p and p.exists()]
    (copia / "filas.json").write_text(json.dumps(filas, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    with tarfile.open(copia / "ficheros.tar.gz", "w:gz") as tar:
        for p in ficheros:
            tar.add(p, arcname=str(p.relative_to(bd.DATOS)))
    with tarfile.open(copia / "ficheros.tar.gz") as tar:
        if len(tar.getnames()) < len(ficheros):
            raise ErrorServicio("No se pudo hacer la copia del trabajo; no se ha borrado nada.")

    with bd.Sesion() as s:
        s.query(bd.ConsumoIA).filter_by(trabajo_id=trabajo_id).update({"trabajo_id": None})
        s.delete(s.get(bd.Trabajo, trabajo_id))
        s.commit()
    for p in ficheros:
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    auditoria.registrar("borrar", trabajo_id=trabajo_id,
                        detalle=f"{titulo}. Copia en {copia.relative_to(bd.DATOS)}")
    return {"trabajo_id": trabajo_id, "borrado": True,
            "copia": str(copia.relative_to(bd.DATOS))}


def estado_memoria() -> dict[str, Any]:
    """Qué sabe el sistema: frases traducidas y equivalencias territoriales."""
    return memoria.resumen()


def buscar_en_memoria(texto: str, limite: int = 25) -> dict[str, Any]:
    """Busca en las dos memorias, para revisar o auditar lo guardado."""
    return memoria.buscar(texto, limite)


def listar_comentarios(solo_sin_aplicar: bool = False) -> list[dict[str, Any]]:
    """Todo el feedback recogido, para alimentar la siguiente iteración del trabajador."""
    with bd.Sesion() as s:
        consulta = s.query(bd.Comentario).order_by(bd.Comentario.id.desc())
        if solo_sin_aplicar:
            consulta = consulta.filter(bd.Comentario.aplicado.is_(False))
        return [{"id": c.id, "trabajo_id": c.trabajo_id, "autor": c.autor,
                 "texto": c.texto, "aplicado": c.aplicado,
                 "destino": c.trabajo.destino, "tipo": c.trabajo.tipo,
                 "creado_en": c.creado_en.isoformat() if c.creado_en else None}
                for c in consulta.all()]


# ------------------------------------------------------------------- imágenes

_ORDEN_IMAGEN = {"diseño": 0, "parcial": 0, "territorial": 1, "sustituida": 2, "rehecha": 3,
                 "con texto": 4, "sin texto": 5}
MAX_IMAGEN = 15 * 1024 * 1024


def _rutas_trabajo(trabajo_id: int) -> tuple[Path, Path | None, int]:
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ErrorServicio(f"No existe el trabajo {trabajo_id}")
        return bd.ruta(t.paquete.ruta_extraido), bd.ruta(t.ruta_resultado), t.paquete_id


def imagenes_trabajo(trabajo_id: int) -> list[dict[str, Any]]:
    """Las imágenes de la unidad, con lo que se sabe de cada una y si se ha sustituido.

    Primero las que van a diseño y las del territorio de origen: son las que hay que
    cambiar. Luego las sustituidas, las rehechas y el resto.
    """
    from . import imagenes as img
    from .asistente import propuestas_imagenes
    origen, destino, paquete_id = _rutas_trabajo(trabajo_id)
    propuestas = propuestas_imagenes(trabajo_id)
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        resumen = json.loads(t.imagenes) if t.imagenes else {}
        sustituciones = {x.huella: {"id": x.id, "nombre": x.nombre_subido, "autor": x.autor,
                                    "ajuste": x.ajuste,
                                    "creado_en": x.creado_en.isoformat() if x.creado_en else None}
                         for x in t.sustituciones}
    a_diseno = {f: d["detalle"] for d in resumen.get("diseno", [])
                for f in [d["fichero"], *d.get("copias", [])]}
    lecturas = img.lecturas(origen)
    inventario = img.inventario(origen)
    grupo_de = {h: lider for lider, hs in img.grupos(origen, inventario).items() for h in hs}
    salida = []
    for imagen in inventario:
        lectura = lecturas.get(imagen["huella"]) or {}
        fichero = imagen["fichero"]
        con_texto = lectura.get("tipo") not in (None, "sin_texto", "no_traducible")
        actual = destino / fichero if destino else None
        rehecha = bool(actual and actual.is_file() and con_texto
                       and actual.read_bytes() != (origen / fichero).read_bytes())
        estado = ("sustituida" if imagen["huella"] in sustituciones else
                  "diseño" if fichero in a_diseno else
                  "territorial" if lectura.get("territorial") else
                  "rehecha" if rehecha else
                  "con texto" if con_texto else "sin texto")
        version = int(actual.stat().st_mtime) if actual and actual.is_file() else 0
        salida.append({
            **imagen, "estado": estado, "motivo": a_diseno.get(fichero) or lectura.get("motivo"),
            "sustitucion": sustituciones.get(imagen["huella"]),
            "con_texto": con_texto, "territorial": bool(lectura.get("territorial")),
            "localizada_por_asistente": bool(img.cajas_vigentes(lectura)),
            "propuestas": propuestas.get(imagen["huella"]),
            "url_original": f"/original/{paquete_id}/{fichero}",
            "url_actual": f"/contenido/{trabajo_id}/{fichero}?v={version}" if destino else None})
    # Una tarjeta por imagen: las copias (otro tamaño, otra carpeta) van dentro de la de su
    # principal, y lo que se haga con ella se hace con todas.
    por_huella = {x["huella"]: x for x in salida}
    agrupadas = []
    for x in salida:
        if grupo_de.get(x["huella"], x["huella"]) != x["huella"]:
            continue
        copias = [por_huella[h] for h, l in grupo_de.items() if l == x["huella"] and h != x["huella"]]
        estados = {x["estado"]} | {c["estado"] for c in copias}
        agrupadas.append({**x, "copias": copias, "huellas": [x["huella"]] + [c["huella"] for c in copias],
                          "estado": "parcial" if {"rehecha", "diseño"} <= estados else x["estado"]})
    agrupadas.sort(key=lambda x: (_ORDEN_IMAGEN.get(x["estado"], 9), x["fichero"]))
    return agrupadas


def _grupo_de_imagen(origen: Path, huella: str) -> list[str]:
    """La huella y las de sus copias: lo que se sustituye junto."""
    from . import imagenes as img
    for lider, hs in img.grupos(origen).items():
        if huella in hs:
            return hs
    return [huella]


def _terminos_de_imagen(trabajo_id: int, destino: Path, fichero: str) -> int:
    """Al cambiar una imagen, su título y su texto alternativo pasan a revisión: si no,
    la unidad enseñaría una cosa y diría otra."""
    from maker_paquete import MapaEditorial, abrir_unidad
    nombre = Path(fichero).name
    paquete = abrir_unidad(destino)
    mapa = MapaEditorial(paquete)
    componentes = {cid for cid, c in paquete.componentes.items()
                   if c.get("name") == "image" and nombre in json.dumps(c.get("properties") or {},
                                                                        ensure_ascii=False)}
    creados = 0
    with bd.Sesion() as s:
        existentes = {x.clave for x in s.query(bd.Termino).filter(bd.Termino.trabajo_id == trabajo_id)}
        for e in mapa.elementos:
            if e.componente in componentes and e.propiedad in ("alt", "title") and e.redactable \
                    and e.clave not in existentes:
                s.add(bd.Termino(
                    trabajo_id=trabajo_id, clave=e.clave, leccion=e.titulo_leccion,
                    motivo=f"Se ha sustituido la imagen {nombre}: comprueba que su "
                           f"{'texto alternativo' if e.propiedad == 'alt' else 'título'} "
                           "describe la nueva.",
                    original=e.plano, propuesta=e.plano, valor_base=e.valor,
                    detectado_por="sustitución", estado=bd.PENDIENTE))
                creados += 1
        s.commit()
    return creados


def sustituir_imagen(trabajo_id: int, huella: str, nombre: str, contenido: bytes,
                     ajuste: str = "recortar") -> dict[str, Any]:
    """Cambia una imagen de la unidad por otra en este trabajo.

    La subida se guarda tal cual y se aplica ya al resultado; se vuelve a aplicar en cada
    proceso. La versión imprimible la recoge al guardar o reprocesar.
    """
    from io import BytesIO

    from PIL import Image

    from .trabajador import SUSTITUCIONES, aplicar_sustituciones, huellas_imagenes
    if ajuste not in ("recortar", "encajar"):
        raise ErrorServicio("Ajuste desconocido: «recortar» o «encajar».")
    if len(contenido) > MAX_IMAGEN:
        raise ErrorServicio("La imagen pesa más de 15 MB.")
    try:
        with Image.open(BytesIO(contenido)) as im:
            im.verify()
            formato = (im.format or "").upper()
    except Exception as e:
        raise ErrorServicio("El fichero no es una imagen que se pueda leer.") from e
    if formato not in ("JPEG", "PNG", "WEBP", "GIF"):
        raise ErrorServicio("Sube la imagen en JPG, PNG, WebP o GIF.")
    exigir(trabajo_id, "sustituir imagen")
    origen, destino, _ = _rutas_trabajo(trabajo_id)
    por_huella = huellas_imagenes(origen)
    if not por_huella.get(huella):
        raise ErrorServicio("Esa imagen no está en la unidad.")
    # Con sus copias: la misma imagen a otro tamaño en la galería o en la lección.
    grupo = [h for h in _grupo_de_imagen(origen, huella) if por_huella.get(h)]
    carpeta = SUSTITUCIONES / f"trabajo-{trabajo_id}"
    carpeta.mkdir(parents=True, exist_ok=True)
    guardada = carpeta / f"{huella}.{ {'JPEG': 'jpg'}.get(formato, formato.lower()) }"
    for anterior in carpeta.glob(f"{huella}.*"):
        anterior.unlink()
    guardada.write_bytes(contenido)
    with bd.Sesion() as s:
        for h in grupo:
            fila = (s.query(bd.Sustitucion).filter_by(trabajo_id=trabajo_id, huella=h).first()
                    or bd.Sustitucion(trabajo_id=trabajo_id, huella=h))
            fila.fichero, fila.ruta, fila.ajuste = por_huella[h][0], bd.relativa(guardada), ajuste
            fila.nombre_subido, fila.autor = Path(nombre or "").name[:255], auditoria.firma()
            s.add(fila)
        s.commit()
    ficheros = [f for h in grupo for f in por_huella[h]]
    terminos = 0
    if destino and destino.is_dir():
        aplicar_sustituciones(trabajo_id, origen, destino)
        terminos = sum(_terminos_de_imagen(trabajo_id, destino, f) for f in ficheros)
    auditoria.registrar("sustituir imagen", trabajo_id=trabajo_id,
                        detalle=f"{', '.join(ficheros)} ← {Path(nombre or '').name} ({ajuste})")
    return {"fichero": ficheros[0], "ficheros": ficheros, "terminos_nuevos": terminos}


def deshacer_sustitucion(trabajo_id: int, huella: str) -> dict[str, Any]:
    """Vuelve a la imagen de la unidad. Si se había rehecho con los rótulos traducidos,
    la versión rehecha vuelve al reprocesar."""
    import shutil

    from .trabajador import huellas_imagenes
    exigir(trabajo_id, "deshacer sustitución")
    origen, destino, _ = _rutas_trabajo(trabajo_id)
    grupo = _grupo_de_imagen(origen, huella)
    with bd.Sesion() as s:
        filas = s.query(bd.Sustitucion).filter(bd.Sustitucion.trabajo_id == trabajo_id,
                                               bd.Sustitucion.huella.in_(grupo)).all()
        if not filas:
            raise ErrorServicio("Esa imagen no está sustituida.")
        guardadas = {bd.ruta(f.ruta) for f in filas if f.ruta}
        ficheros = [f.fichero for f in filas]
        for f in filas:
            s.delete(f)
        # Los términos que abrió la sustitución y nadie ha resuelto, fuera.
        nombres = {Path(f).name for f in ficheros}
        for x in s.query(bd.Termino).filter_by(trabajo_id=trabajo_id, detectado_por="sustitución"):
            if not x.valor_final and any(n in (x.motivo or "") for n in nombres):
                s.delete(x)
        s.commit()
    for guardada in guardadas:
        if guardada and guardada.is_file():
            guardada.unlink()
    if destino:
        por_huella = huellas_imagenes(origen)
        for h in grupo:
            for relativa in por_huella.get(h, []):
                shutil.copyfile(origen / relativa, destino / relativa)
    auditoria.registrar("deshacer sustitución", trabajo_id=trabajo_id, detalle=", ".join(ficheros))
    return {"fichero": ficheros[0], "ficheros": ficheros}


def configurar_trabajo(trabajo_id: int, regenerar_imagenes: bool | None = None,
                       etapa: str | None = None, curso: int | None = None,
                       materia: str | None = None) -> dict[str, Any]:
    """Opciones del trabajo. Lo que no se indica no cambia. Las imágenes se rehacen al
    reprocesar; la etapa, el curso y la materia valen para la siguiente operación del
    asistente."""
    from . import curriculo
    if etapa is not None and etapa not in ("ESO", "Primaria", ""):
        raise ErrorServicio("La etapa es «ESO» o «Primaria».")
    if materia and materia not in curriculo.NOMBRES:
        raise ErrorServicio(f"Materia desconocida: {materia}")
    exigir(trabajo_id, "configurar")
    # None: no cambia. Cadena vacía en etapa o materia: se borra.
    valores = {"regenerar_imagenes": regenerar_imagenes, "curso": curso,
               "etapa": None if etapa is None else (etapa or ""),
               "materia": None if materia is None else (materia or "")}
    cambios = []
    with bd.Sesion() as s:
        t = s.get(bd.Trabajo, trabajo_id)
        for campo, valor in valores.items():
            if valor is None:
                continue
            valor = valor or None if campo in ("etapa", "materia") else valor
            if getattr(t, campo) != valor:
                cambios.append(f"{campo}: {getattr(t, campo)} → {valor}")
                setattr(t, campo, valor)
        s.commit()
    if cambios:
        auditoria.registrar("configurar", trabajo_id=trabajo_id, detalle="; ".join(cambios))
    return obtener_trabajo(trabajo_id)


# ------------------------------------------------------------- documentación del experto

MAX_DOCUMENTO = 30 * 1024 * 1024


def _rol(usuario: dict | None) -> str:
    roles = (usuario or {}).get("roles") or []
    return next((r for r in ("experto", "admin", "editor") if r in roles), "editor")


def _documento_dict(d: bd.Documento) -> dict[str, Any]:
    imagenes = json.loads(d.imagenes) if d.imagenes else []
    return {"id": d.id, "nombre": d.nombre, "tipo": d.tipo, "autor": d.autor, "rol": d.rol,
            "creado_en": d.creado_en.isoformat() if d.creado_en else None,
            "caracteres": len(d.texto or ""), "propuestas": d.propuestas,
            "enlaces": json.loads(d.enlaces) if d.enlaces else [],
            "imagenes": [{"indice": i, "nombre": x["nombre"],
                          "url": f"/trabajo/{d.trabajo_id}/documentos/{d.id}/imagenes/{i}"}
                         for i, x in enumerate(imagenes)],
            "inicio": (d.texto or "")[:400]}


def documentos_trabajo(trabajo_id: int) -> list[dict[str, Any]]:
    with bd.Sesion() as s:
        return [_documento_dict(d) for d in
                s.query(bd.Documento).filter_by(trabajo_id=trabajo_id).order_by(bd.Documento.id)]


def _guardar_documento(trabajo_id: int, nombre: str, tipo: str, datos: dict,
                       contenido: bytes | None) -> dict[str, Any]:
    from .trabajador import TRABAJO
    yo = auditoria.usuario_actual()
    with bd.Sesion() as s:
        doc = bd.Documento(trabajo_id=trabajo_id, nombre=nombre[:255], tipo=tipo,
                           texto=datos["texto"], enlaces=json.dumps(datos["enlaces"], ensure_ascii=False),
                           autor=auditoria.firma(yo), rol=_rol(yo))
        s.add(doc)
        s.flush()
        carpeta = TRABAJO / "documentos" / f"trabajo-{trabajo_id}" / str(doc.id)
        carpeta.mkdir(parents=True, exist_ok=True)
        if contenido is not None:
            fichero = carpeta / ("original." + tipo)
            fichero.write_bytes(contenido)
            doc.ruta = bd.relativa(fichero)
        imagenes = []
        for i, (nombre_img, datos_img) in enumerate(datos["imagenes"]):
            destino = carpeta / f"imagen-{i}.{nombre_img.rsplit('.', 1)[-1].lower()}"
            destino.write_bytes(datos_img)
            imagenes.append({"nombre": nombre_img, "ruta": bd.relativa(destino)})
        doc.imagenes = json.dumps(imagenes, ensure_ascii=False)
        # La ficha rellena: cada propuesta va a su término como propuesta del experto. No
        # se da por decidida: el editor la confirma, como cualquier otra.
        aplicadas = 0
        for termino_id, texto in datos.get("propuestas", {}).items():
            t = s.get(bd.Termino, termino_id)
            if t is None or t.trabajo_id != trabajo_id or t.valor_final:
                continue
            t.propuesta = texto
            t.motivo = f"Propuesta del experto ({auditoria.firma(yo) or 'sin nombre'}). " + \
                       re.sub(r"^Propuesta del experto \([^)]*\)\. ", "", t.motivo or "")
            aplicadas += 1
        doc.propuestas = aplicadas
        s.commit()
        salida = _documento_dict(doc)
    auditoria.registrar("aportar documentación", trabajo_id=trabajo_id,
                        detalle=f"{nombre} ({len(datos['texto'])} caracteres, "
                                f"{len(datos['imagenes'])} imágenes, {aplicadas} propuestas)")
    return salida


def aportar_documento(trabajo_id: int, nombre: str, contenido: bytes) -> dict[str, Any]:
    """Un documento del experto: Word (también la ficha rellena), PDF o texto."""
    from . import documentacion
    if len(contenido) > MAX_DOCUMENTO:
        raise ErrorServicio("El documento pesa más de 30 MB.")
    try:
        datos = documentacion.extraer(nombre or "", contenido)
    except ValueError as e:
        raise ErrorServicio(str(e)) from e
    except Exception as e:
        raise ErrorServicio(f"No se ha podido leer el documento: {e}") from e
    if not datos["texto"] and not datos["imagenes"]:
        raise ErrorServicio("El documento no tiene texto ni imágenes que se puedan leer.")
    exigir(trabajo_id, "aportar documentación")
    tipo = (nombre or "").lower().rsplit(".", 1)[-1]
    return _guardar_documento(trabajo_id, Path(nombre).name, tipo, datos, contenido)


def aportar_texto(trabajo_id: int, titulo: str, texto: str) -> dict[str, Any]:
    """Contenido pegado directamente en la aplicación."""
    from . import documentacion
    if not (texto or "").strip():
        raise ErrorServicio("El texto está vacío.")
    exigir(trabajo_id, "aportar documentación")
    return _guardar_documento(trabajo_id, (titulo or "").strip() or "Texto pegado", "pegado",
                              documentacion.de_texto(texto), None)


def borrar_documento(trabajo_id: int, documento_id: int) -> dict[str, Any]:
    import shutil
    exigir(trabajo_id, "borrar documentación")
    with bd.Sesion() as s:
        d = s.get(bd.Documento, documento_id)
        if d is None or d.trabajo_id != trabajo_id:
            raise ErrorServicio("Ese documento no es de este trabajo.")
        nombre, ruta = d.nombre, bd.ruta(d.ruta) if d.ruta else None
        s.delete(d)
        s.commit()
    from .trabajador import TRABAJO
    shutil.rmtree(TRABAJO / "documentos" / f"trabajo-{trabajo_id}" / str(documento_id),
                  ignore_errors=True)
    auditoria.registrar("borrar documentación", trabajo_id=trabajo_id, detalle=nombre)
    return {"borrado": documento_id}


def imagen_documento(trabajo_id: int, documento_id: int, indice: int) -> Path:
    with bd.Sesion() as s:
        d = s.get(bd.Documento, documento_id)
        if d is None or d.trabajo_id != trabajo_id:
            raise ErrorServicio("Ese documento no es de este trabajo.")
        imagenes = json.loads(d.imagenes) if d.imagenes else []
    if not 0 <= indice < len(imagenes):
        raise ErrorServicio("Esa imagen no está en el documento.")
    return bd.ruta(imagenes[indice]["ruta"])


def sustituir_desde_documento(trabajo_id: int, huella: str, documento_id: int, indice: int,
                              ajuste: str = "recortar") -> dict[str, Any]:
    """Sustituye una imagen de la unidad por una de la documentación del experto."""
    ruta = imagen_documento(trabajo_id, documento_id, indice)
    with bd.Sesion() as s:
        nombre = s.get(bd.Documento, documento_id).nombre
    return sustituir_imagen(trabajo_id, huella, f"{nombre} · {ruta.name}", ruta.read_bytes(), ajuste)


def ficha_experto(trabajo_id: int) -> tuple[str, bytes]:
    """El Word que rellena el experto para este trabajo."""
    from . import documentacion
    trabajo = obtener_trabajo(trabajo_id)
    diseno = (trabajo.get("imagenes") or {}).get("diseno", [])
    auditoria.registrar("descargar ficha", trabajo_id=trabajo_id)
    nombre = f"ficha-experto-trabajo-{trabajo_id}.docx"
    return nombre, documentacion.ficha(trabajo, diseno, trabajo.get("plan"))


# ------------------------------------------------------------------- el asistente

def estado_asistente(trabajo_id: int) -> dict[str, Any]:
    """Qué está haciendo el asistente en un trabajo y cuánto ha costado hasta ahora."""
    from . import asistente
    try:
        return asistente.estado(trabajo_id)
    except ValueError as e:
        raise ErrorServicio(str(e)) from e


def lanzar_asistente(trabajo_id: int, operacion: str,
                     huellas: list[str] | None = None) -> dict[str, Any]:
    """Arranca una operación del asistente en segundo plano. `huellas` limita las de
    imágenes (`rehacer_imagenes`, `proponer_imagenes`) a esas imágenes."""
    from . import asistente
    exigir(trabajo_id, f"asistente: {operacion}")
    try:
        datos = asistente.lanzar(trabajo_id, operacion, huellas=huellas or None,
                                 quien=auditoria.firma())
    except ValueError as e:
        raise ErrorServicio(str(e)) from e
    detalle = [f"Imágenes: {', '.join(huellas)}" if huellas else "",
               f"En cola, puesto {datos['en_cola']}" if datos.get("en_cola") else ""]
    auditoria.registrar(f"asistente: {operacion}", trabajo_id=trabajo_id,
                        detalle=". ".join(d for d in detalle if d) or None)
    return datos


def usar_foto_commons(trabajo_id: int, huella: str, indice: int,
                      ajuste: str = "recortar") -> dict[str, Any]:
    """Sustituye una imagen por una de las fotos de Commons que propuso el asistente.

    Se baja a un tamaño de hasta el doble de la original, no el fichero entero (las de
    Commons pasan a menudo de 20 MB). El crédito —título, autor y licencia— queda como
    nombre de la sustitución: las licencias CC BY obligan a citarlo en la unidad.
    """
    import re as _re

    from . import asistente, commons
    propuestas = asistente.propuestas_imagenes(trabajo_id).get(huella) or {}
    candidatas = propuestas.get("candidatas") or []
    if not 0 <= indice < len(candidatas):
        raise ErrorServicio("Esa propuesta ya no está: vuelve a pedirlas.")
    foto = candidatas[indice]
    ancho = next((i["ancho"] for i in imagenes_trabajo(trabajo_id) if i["huella"] == huella), 1024)
    objetivo = min(foto.get("ancho") or 0, 2 * ancho, 2560)
    url = foto["url"]
    if objetivo and objetivo < (foto.get("ancho") or 0) and "/640px-" in (foto.get("miniatura") or ""):
        url = _re.sub(r"/640px-", f"/{objetivo}px-", foto["miniatura"])
    try:
        contenido = commons.descargar(url)
    except Exception as e:  # noqa: BLE001
        raise ErrorServicio(f"No se ha podido descargar la foto de Commons: {e}") from e
    # El nombre de la sustitución se guarda como nombre de fichero: sin barras.
    datos = sustituir_imagen(trabajo_id, huella, commons.credito(foto).replace("/", "-"),
                             contenido, ajuste)
    return {**datos, "credito": commons.credito(foto), "licencia_url": foto.get("licencia_url"),
            "pagina": foto.get("pagina")}


def otra_propuesta(termino_id: int, indicacion: str = "") -> dict[str, Any]:
    """Pide al asistente una propuesta nueva para un término. No decide: la deja en la caja."""
    from . import asistente, ia
    trabajo_id = _trabajo_de_termino(termino_id)
    exigir(trabajo_id, "otra propuesta")
    try:
        datos = asistente.otra_propuesta(termino_id, indicacion)
    except (ValueError, ia.ErrorIA) as e:
        raise ErrorServicio(str(e)) from e
    auditoria.registrar("otra propuesta", trabajo_id=trabajo_id,
                        detalle=f"Término {termino_id}" + (f": {indicacion[:200]}" if indicacion else ""))
    return datos


def traducciones_por_revisar(trabajo_id: int, todas: bool = False) -> dict[str, Any]:
    from . import asistente
    try:
        return asistente.traducciones_por_revisar(trabajo_id, todas)
    except ValueError as e:
        raise ErrorServicio(str(e)) from e


def aprobar_traduccion(frase_id: int, destino: str | None = None,
                       autor: str | None = None, trabajo_id: int | None = None) -> dict[str, Any]:
    """Un editor confirma una traducción automática, corregida o tal cual.

    La frase es de la memoria, no de un trabajo: desde la API se aprueba sin más. Desde la
    pantalla de un trabajo llega `trabajo_id`, y entonces cuenta como trabajar en él.
    """
    if trabajo_id is not None:
        exigir(trabajo_id, "aprobar traducción")
    try:
        datos = memoria.aprobar_frase(frase_id, destino, aprobada_por=autor or auditoria.firma())
    except KeyError as e:
        raise ErrorServicio(f"No existe la frase {frase_id}") from e
    auditoria.registrar("aprobar traducción", trabajo_id=trabajo_id,
                        detalle=f"frase {frase_id}: {datos['destino'][:300]}")
    return datos


def aprobar_traducciones(trabajo_id: int, autor: str | None = None) -> dict[str, Any]:
    """Confirma de una vez todas las traducciones automáticas pendientes de un trabajo."""
    exigir(trabajo_id, "aprobar todas")
    frases = traducciones_por_revisar(trabajo_id)["frases"]
    for f in frases:
        memoria.aprobar_frase(f["id"], aprobada_por=autor or auditoria.firma())
    auditoria.registrar("aprobar todas", trabajo_id=trabajo_id, detalle=f"{len(frases)} frases")
    return {"aprobadas": len(frases)}


def devolver_a_revision(trabajo_id: int) -> dict[str, Any]:
    """Deshace la aprobación de lo que el asistente tradujo en este trabajo."""
    exigir(trabajo_id, "devolver a revisión")
    n = memoria.devolver_a_revision(f"asistente, trabajo {trabajo_id}")
    auditoria.registrar("devolver a revisión", trabajo_id=trabajo_id, detalle=f"{n} frases")
    return {"devueltas": n}
