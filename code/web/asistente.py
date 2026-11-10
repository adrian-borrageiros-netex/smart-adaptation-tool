"""Las tres operaciones del asistente sobre un trabajo.

Traducir, proponer adaptaciones y buscar enlaces. Son largas —una unidad son decenas de
llamadas— y corren en segundo plano: el progreso queda en el propio trabajo, en las
columnas `ia_*`, para que la pantalla lo enseñe. Las llamadas a la API van en paralelo,
pero todas las escrituras en la base de datos se hacen desde un solo hilo.

Nada de lo que hacen se da por bueno solo: ver `web/ia.py`.
"""

from __future__ import annotations

import json
import re
import threading
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import bd, fuentes, ia, memoria
from .trabajador import (MapaEditorial, Paquete, _es_traducible, abrir_unidad,
                         frases_pendientes, reprocesar)

OPERACIONES = ("traducir", "revisar", "proponer", "enlaces", "comprobar", "analizar",
               "rehacer_imagenes", "proponer_imagenes")


# ------------------------------------------------------------------- en segundo plano

_PRIMER_MENSAJE = {
    "traducir": "Buscando lo que la memoria no conoce.",
    "revisar": "Preparando la unidad para leerla.",
    "proponer": "Reuniendo los términos pendientes.",
    "enlaces": "Reuniendo los enlaces pendientes.",
    "comprobar": "Buscando imágenes rehechas que no se hayan releído.",
    "analizar": "Reuniendo el texto de la unidad y su currículo.",
    "rehacer_imagenes": "Buscando las imágenes con rótulos sin localizar.",
    "proponer_imagenes": "Preparando la búsqueda de imágenes.",
}
NOMBRES = {"traducir": "Traducir", "revisar": "Revisar la unidad entera",
           "proponer": "Proponer adaptaciones", "enlaces": "Buscar enlaces",
           "comprobar": "Releer imágenes", "analizar": "Analizar con el currículo",
           "rehacer_imagenes": "Rehacer imágenes", "proponer_imagenes": "Buscar fotos"}
_CERROJO = threading.Lock()   # comprobar y poner en marcha o a la cola, sin carreras


def _cola(t: bd.Trabajo) -> dict:
    datos = json.loads(t.ia_cola) if t.ia_cola else {}
    return {"actual": datos.get("actual"), "pendientes": datos.get("pendientes") or []}


def lanzar(trabajo_id: int, operacion: str, huellas: list[str] | None = None,
           quien: str | None = None) -> dict[str, Any]:
    """Arranca una operación en segundo plano y vuelve enseguida.

    Una sola a la vez por trabajo: dos traducciones simultáneas de la misma unidad
    pagarían dos veces lo mismo y se pisarían al guardar. Lo que se pide mientras hay otra
    en marcha se pone a la cola y arranca al acabar la anterior; lo que ya está pedido no
    se repite. `huellas` limita las operaciones de imágenes a esas imágenes.
    """
    if operacion not in OPERACIONES:
        raise ValueError(f"Operación desconocida: {operacion}")
    if not ia.disponible():
        raise ValueError("El asistente no está configurado: falta ANTHROPIC_API_KEY en el servidor.")
    clave = ia.estado_clave()
    if not clave["ok"]:
        raise ValueError(clave["motivo"])
    tarea = {"operacion": operacion, "huellas": sorted(huellas) if huellas else None, "quien": quien}
    with _CERROJO, bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        if trabajo is None:
            raise ValueError(f"No existe el trabajo {trabajo_id}")
        if operacion in ("revisar", "proponer", "enlaces", "analizar", "proponer_imagenes") and (
                trabajo.tipo != bd.ADAPTACION or not trabajo.comunidad):
            raise ValueError("Las propuestas y los enlaces son para adaptaciones curriculares: "
                             "en una traducción no se cambia de territorio.")
        if operacion == "analizar":
            from . import curriculo
            if curriculo.para_trabajo(trabajo.comunidad, trabajo.etapa, trabajo.materia,
                                      trabajo.curso)[0] is None:
                raise ValueError(f"No hay currículo de {trabajo.comunidad} para la materia y el "
                                 "curso de este trabajo. Revísalos en la configuración del trabajo.")
        cola = _cola(trabajo)
        if trabajo.ia_estado == "en_curso":
            mismas = lambda a: a and a["operacion"] == operacion and a.get("huellas") == tarea["huellas"]
            if mismas(cola["actual"]) or any(mismas(p) for p in cola["pendientes"]):
                raise ValueError(f"«{NOMBRES[operacion]}» ya está pedida para esto: espera a que termine.")
            cola["pendientes"].append(tarea)
            trabajo.ia_cola = json.dumps(cola, ensure_ascii=False)
            sesion.commit()
            return {**estado(trabajo_id), "en_cola": len(cola["pendientes"])}
        _empezar(trabajo, tarea, cola)
        sesion.commit()
    threading.Thread(target=_ejecutar, args=(trabajo_id, tarea), daemon=True,
                     name=f"asistente-{trabajo_id}").start()
    return estado(trabajo_id)


def _empezar(trabajo: bd.Trabajo, tarea: dict, cola: dict) -> None:
    trabajo.ia_operacion, trabajo.ia_estado = tarea["operacion"], "en_curso"
    trabajo.ia_hechas, trabajo.ia_total = 0, 0
    trabajo.ia_mensaje = _PRIMER_MENSAJE[tarea["operacion"]]
    cola["actual"] = tarea
    trabajo.ia_cola = json.dumps(cola, ensure_ascii=False)


def _ejecutar(trabajo_id: int, tarea: dict) -> None:
    """Hace la operación y, al acabar, la siguiente de la cola, hasta vaciarla."""
    funciones = {"traducir": traducir, "revisar": revisar, "proponer": proponer,
                 "enlaces": buscar_enlaces, "comprobar": comprobar_imagenes,
                 "analizar": analizar, "rehacer_imagenes": rehacer_imagenes,
                 "proponer_imagenes": proponer_imagenes}
    while tarea:
        opciones = ({"huellas": tarea.get("huellas")}
                    if tarea["operacion"] in ("rehacer_imagenes", "proponer_imagenes") else {})
        try:
            funciones[tarea["operacion"]](trabajo_id, **opciones)
        except ia.ClaveNoValida as e:
            _progreso(trabajo_id, estado="error", mensaje=str(e))
        except Exception as e:  # noqa: BLE001 — cualquier fallo tiene que llegar a la pantalla
            _progreso(trabajo_id, estado="error", mensaje=f"{type(e).__name__}: {e}")
        with _CERROJO, bd.Sesion() as sesion:
            trabajo = sesion.get(bd.Trabajo, trabajo_id)
            if trabajo is None:
                return
            cola = _cola(trabajo)
            cola["actual"] = None
            tarea = cola["pendientes"].pop(0) if cola["pendientes"] else None
            if tarea:
                # El resultado de la anterior se conserva en el mensaje de la siguiente.
                anterior = f"Terminado «{NOMBRES.get(trabajo.ia_operacion, trabajo.ia_operacion)}»: {trabajo.ia_mensaje}"
                _empezar(trabajo, tarea, cola)
                trabajo.ia_mensaje = f"{trabajo.ia_mensaje}\n{anterior}"[:4000]
            else:
                trabajo.ia_cola = json.dumps(cola, ensure_ascii=False)
            sesion.commit()


def recuperar_al_arrancar() -> int:
    """Lo que estaba en marcha cuando se paró el servidor no sigue: se dice, y se vacía la
    cola. Sin esto, el trabajo quedaba «trabajando» para siempre y no se podía relanzar."""
    with bd.Sesion() as sesion:
        colgados = sesion.query(bd.Trabajo).filter(bd.Trabajo.ia_estado == "en_curso").all()
        for t in colgados:
            t.ia_estado = "error"
            t.ia_mensaje = (f"«{NOMBRES.get(t.ia_operacion, t.ia_operacion)}» se interrumpió al "
                            "reiniciarse el servidor. Lo ya hecho está guardado: vuelve a lanzarla.")
            t.ia_cola = None
        sesion.commit()
        return len(colgados)


def estado(trabajo_id: int) -> dict[str, Any]:
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        if t is None:
            raise ValueError(f"No existe el trabajo {trabajo_id}")
        consumos = sesion.query(bd.ConsumoIA).filter_by(trabajo_id=trabajo_id).all()
        cola = _cola(t)
        if t.ia_estado != "en_curso":
            cola = {"actual": None, "pendientes": []}
        return {
            "disponible": ia.disponible(), "modelo": ia.MODELO, "clave": ia.estado_clave(),
            "operacion": t.ia_operacion, "estado": t.ia_estado, "mensaje": t.ia_mensaje,
            "hechas": t.ia_hechas or 0, "total": t.ia_total or 0,
            "cola": cola,
            # Las imágenes con algo en marcha o pedido, para marcarlas en su pantalla.
            "imagenes_en_curso": sorted(set((cola["actual"] or {}).get("huellas") or [])),
            "imagenes_en_cola": sorted({h for p in cola["pendientes"] for h in (p.get("huellas") or [])}),
            "coste_usd": round(sum(c.coste_usd for c in consumos), 4),
            "llamadas": [{"operacion": c.operacion, "modelo": c.modelo, "entrada": c.entrada,
                          "salida": c.salida, "cache_lectura": c.cache_lectura,
                          "busquedas": c.busquedas, "coste_usd": c.coste_usd,
                          "fecha": c.creado_en.isoformat() if c.creado_en else None}
                         for c in consumos],
        }


def _progreso(trabajo_id: int, **campos: Any) -> None:
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        for clave, valor in campos.items():
            setattr(trabajo, f"ia_{clave}", valor)
        sesion.commit()


def _registrar(trabajo_id: int, operacion: str, consumo: ia.Consumo) -> None:
    with bd.Sesion() as sesion:
        sesion.add(bd.ConsumoIA(
            trabajo_id=trabajo_id, operacion=operacion, modelo=consumo.modelo,
            entrada=consumo.entrada, salida=consumo.salida,
            cache_lectura=consumo.cache_lectura, cache_escritura=consumo.cache_escritura,
            busquedas=consumo.busquedas, coste_usd=round(consumo.coste_usd(), 4)))
        sesion.commit()


# ------------------------------------------------------------------- currículo

def _curriculo(trabajo_id: int, con_plan: bool = True) -> str | None:
    """El trozo del currículo de destino para el asistente, con el plan de adaptación si ya
    se ha analizado: así las propuestas siguen lo que se decidió al analizar."""
    from . import curriculo
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        texto, _ = curriculo.para_trabajo(t.comunidad, t.etapa, t.materia, t.curso)
        plan = json.loads(t.plan) if (t.plan and con_plan) else None
    if texto and plan and plan.get("sustituir"):
        texto += "\n\nPlan de adaptación acordado para esta unidad:\n" + "\n".join(
            f"- {p['leccion']}: {p['que']} → {p['propuesta']}" for p in plan["sustituir"])
    return texto


def _documentacion(trabajo_id: int, maximo: int = 80000) -> str | None:
    """Lo que ha aportado el experto, para el asistente. Cada documento con su autor."""
    from .documentacion import MAX_TEXTO
    with bd.Sesion() as sesion:
        docs = [(d.nombre, d.autor, d.rol, d.texto or "")
                for d in sesion.query(bd.Documento).filter_by(trabajo_id=trabajo_id).order_by(bd.Documento.id)]
    partes, total = [], 0
    for nombre, autor, rol, texto in docs:
        if not texto.strip() or total >= maximo:
            continue
        trozo = texto[:min(MAX_TEXTO, maximo - total)]
        partes.append(f"### {nombre} ({autor or 'sin autor'}, {rol or 'editor'})\n{trozo}")
        total += len(trozo)
    return "\n\n".join(partes) or None


def analizar(trabajo_id: int) -> dict[str, Any]:
    """Compara la unidad con el currículo de destino y deja el plan de adaptación.

    Va todo en una llamada, con el texto de la unidad agrupado por lecciones: el plan solo
    es coherente si el modelo ve la unidad entera junto al currículo.
    """
    from . import curriculo
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        comunidad, destino, unidad = t.comunidad, bd.ruta(t.ruta_resultado), t.paquete.titulo
        texto_curriculo, ficha = curriculo.para_trabajo(t.comunidad, t.etapa, t.materia, t.curso)
    mapa = MapaEditorial(abrir_unidad(destino))
    lecciones: dict[str, list[str]] = {}
    for e in mapa.redactables:
        if ".cmi5:" in e.clave or e.clave.endswith(".href") or not _es_traducible(e.plano):
            continue
        partes = lecciones.setdefault(e.titulo_leccion or "", [])
        if sum(len(x) for x in partes) < 12000 and e.plano not in partes:
            partes.append(e.plano)
    entrada = [{"leccion": l, "texto": " / ".join(p)} for l, p in lecciones.items() if p]
    origen = _territorio_de_origen(trabajo_id) or "el territorio de origen"
    _progreso(trabajo_id, hechas=0, total=1, mensaje=(
        f"Esperando al modelo: {len(entrada)} lecciones frente al currículo de {comunidad} "
        f"({ficha['materia']}). Suele tardar unos minutos."))
    consumo = ia.Consumo()
    respuesta = ia.analizar_curriculo(entrada, texto_curriculo, origen, comunidad, consumo, unidad,
                                      documentacion=_documentacion(trabajo_id))
    respuesta["curriculo"] = {k: ficha[k] for k in ("comunidad", "materia", "etapa", "cursos",
                                                    "exacto", "norma", "boletin", "url")}
    respuesta["fecha"] = bd.ahora().isoformat(timespec="seconds")
    with bd.Sesion() as sesion:
        sesion.get(bd.Trabajo, trabajo_id).plan = json.dumps(respuesta, ensure_ascii=False)
        sesion.commit()
    _registrar(trabajo_id, "analizar", consumo)
    _progreso(trabajo_id, estado="listo", hechas=1, total=1, mensaje=(
        f"Plan de adaptación listo: {len(respuesta.get('sustituir', []))} cosas que sustituir y "
        f"{len(respuesta.get('falta', []))} saberes que faltan. Coste estimado: "
        f"{consumo.coste_usd():.2f} USD."))
    return respuesta


# ------------------------------------------------------------------- traducir

def _en_lotes(frases: list[dict], tamano: int = 40,
              max_caracteres: int = 6000) -> list[list[dict]]:
    lotes, actual, caracteres = [], [], 0
    for f in frases:
        if actual and (len(actual) >= tamano or caracteres + len(f["texto"]) > max_caracteres):
            lotes.append(actual)
            actual, caracteres = [], 0
        actual.append(f)
        caracteres += len(f["texto"])
    if actual:
        lotes.append(actual)
    return lotes


def traducir(trabajo_id: int) -> dict[str, Any]:
    """Traduce todas las frases de la unidad que la memoria no conoce.

    Entran **sin revisar**: se aplican para poder ver el paquete y quedan en la lista de
    revisión hasta que un editor las confirma. Las que no pasan la validación no se
    guardan: esas frases siguen como estaban y se cuentan en el resumen.
    """
    lectura = leer_pdfs(trabajo_id)
    leer_imagenes(trabajo_id)
    datos = frases_pendientes(trabajo_id, tamano=10**9, max_caracteres=10**12)
    pendientes = datos.get("lote", [])
    if not pendientes:
        _progreso(trabajo_id, estado="listo", hechas=0, total=0,
                  mensaje=datos.get("mensaje") or "No queda nada por traducir.")
        return {"traducidas": 0}

    origen, destino = datos["idioma_origen"], datos["idioma_destino"]
    lotes = _en_lotes(pendientes)
    _progreso(trabajo_id, hechas=0, total=len(pendientes),
              mensaje=f"Traduciendo {len(pendientes)} frases en {len(lotes)} lotes.")
    glosario = memoria.glosario(origen, destino)
    autor = f"asistente ({ia.MODELO})"
    total = ia.Consumo()
    cuenta = {"traducidas": 0, "sin_cambio": 0, "rechazadas": 0, "lotes_fallidos": 0}
    avisos: list[str] = []
    hechas = 0

    def un_lote(lote: list[dict]):
        consumo = ia.Consumo()  # uno por hilo: se suman al terminar cada lote
        return ia.traducir_lote(lote, origen, destino, glosario, consumo), consumo

    def recoger(lote: list[dict], resultado: list[dict], consumo) -> None:
        nonlocal hechas
        if consumo is not None:
            _acumular(total, consumo)
        por_id = {r.get("id"): r for r in resultado}
        for frase in lote:
            r = por_id.get(frase["id"])
            if r is not None:
                _guardar_traduccion(frase, r, origen, destino, autor, trabajo_id, cuenta, avisos)
        hechas += len(lote)
        _progreso(trabajo_id, hechas=hechas)

    # Primero en paralelo. Lo que la API frene por ritmo se repite después, de uno en uno:
    # es más lento, pero termina, en vez de dejar la unidad a medias.
    aplazados: list[list[dict]] = []
    with ThreadPoolExecutor(max_workers=max(1, ia.CONCURRENCIA)) as grupo:
        futuros = {grupo.submit(un_lote, lote): lote for lote in lotes}
        for futuro in as_completed(futuros):
            lote = futuros[futuro]
            try:
                resultado, consumo = futuro.result()
            except ia.LimiteIA:
                aplazados.append(lote)
                continue
            except ia.ClaveNoValida:
                raise   # ningún lote va a pasar: se para aquí
            except ia.ErrorIA as e:
                cuenta["lotes_fallidos"] += 1
                avisos.append(f"Lote de {len(lote)} frases: {e}")
                resultado, consumo = [], None
            recoger(lote, resultado, consumo)

    if aplazados:
        _progreso(trabajo_id, mensaje=f"La API ha pedido ir más despacio: {len(aplazados)} "
                                      "lotes se repiten de uno en uno.")
    for lote in aplazados:
        try:
            resultado, consumo = un_lote(lote)
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            cuenta["lotes_fallidos"] += 1
            avisos.append(f"Lote de {len(lote)} frases: {e}")
            resultado, consumo = [], None
        recoger(lote, resultado, consumo)

    _registrar(trabajo_id, "traducir", total)
    reprocesar(trabajo_id)
    # Los rótulos que Tesseract no ha encontrado, localizados por el asistente; y las
    # imágenes que se acaban de rehacer, releídas.
    localizacion = localizar_imagenes(trabajo_id)
    comprobacion = comprobar_imagenes(trabajo_id, final=False)

    resumen = (f"{cuenta['traducidas']} frases traducidas y {cuenta['sin_cambio']} que no "
               "cambian, todas pendientes de revisión.")
    if cuenta["rechazadas"]:
        resumen += f" {cuenta['rechazadas']} no pasaron la validación y siguen sin traducir."
    if cuenta["lotes_fallidos"]:
        resumen += f" {cuenta['lotes_fallidos']} de {len(lotes)} lotes fallaron."
    resumen += f" Coste estimado: {total.coste_usd():.2f} USD."
    if localizacion.get("localizadas"):
        resumen += f"\n{localizacion['mensaje']}"
    if comprobacion.get("releidas"):
        resumen += f"\n{comprobacion['mensaje']}"
    if avisos:
        resumen += "\n" + "\n".join(avisos[:20])
    _progreso(trabajo_id, estado="error" if cuenta["lotes_fallidos"] == len(lotes) else "listo",
              mensaje=resumen)
    return {**cuenta, "coste_usd": round(total.coste_usd(), 4)}


def leer_pdfs(trabajo_id: int) -> dict[str, Any]:
    """Lee de la imagen los rótulos de los PDF que no tienen capa de texto.

    Sin esto no se pueden traducir: en la ficha de las rocas de Biología, dos rótulos del
    mapa solo existen pintados. Lo leído se guarda por paquete y no se vuelve a pedir.
    """
    from . import pdf_texto
    from .trabajador import (_pdfs_maquetados, guardar_lecturas, guardar_lecturas_imagenes,
                             lecturas_imagenes, lecturas_pdf)
    with bd.Sesion() as sesion:
        paquete = sesion.get(bd.Trabajo, trabajo_id).paquete
        origen, idioma_origen = bd.ruta(paquete.ruta_extraido), paquete.idioma_origen
    total, leidos = ia.Consumo(), 0
    for pdf in _pdfs_maquetados(origen):
        # Las imágenes, clasificadas como las de la unidad: las de rótulos sobre fondo liso
        # se rehacen solas y las demás van a diseño. Las leídas antes sin clasificar se
        # vuelven a leer.
        vistas = [v for v in lecturas_imagenes(origen, pdf) if "tipo" in v]
        try:
            nuevas_img = [i for i in pdf_texto.imagenes(pdf)
                          if i["xref"] not in {v["xref"] for v in vistas}]
        except Exception:
            nuevas_img = []
        for inicio in range(0, len(nuevas_img), 5):
            grupo = nuevas_img[inicio:inicio + 5]
            _progreso(trabajo_id, mensaje=f"Leyendo {len(nuevas_img)} imágenes de {pdf.name}.")
            try:
                leido = ia.clasificar_imagenes(
                    [{"id": str(i["xref"]), "png": i["png"]} for i in grupo], idioma_origen, total)
            except ia.ErrorIA:
                continue
            for i in grupo:
                l = leido.get(str(i["xref"]))
                if l is None:
                    continue
                vistas.append({"xref": i["xref"], "pagina": i["pagina"], "rect": i["rect"],
                               "tipo": l["tipo"], "territorial": bool(l.get("territorial")),
                               "motivo": l.get("motivo", ""), "rotulos": l.get("rotulos", []),
                               "lineas": [x for r in l.get("rotulos", []) for x in r.get("lineas", [])]})
            guardar_lecturas_imagenes(origen, pdf, vistas)

        previas = lecturas_pdf(origen, pdf)
        try:
            zonas = pdf_texto.zonas_sin_capa(pdf, previas)
        except Exception:  # un PDF que no se puede analizar se queda como está
            continue
        if not zonas:
            continue
        _progreso(trabajo_id, mensaje=f"Leyendo {len(zonas)} rótulos pintados en {pdf.name}.")
        recortes = [{"id": str(i), "png": z["png"]} for i, z in enumerate(zonas)]
        try:
            leido = ia.leer_textos(recortes, total)
        except ia.ErrorIA:
            continue
        nuevas = [{"pagina": z["pagina"], "rect": z["rect"], "lineas": leido[str(i)]}
                  for i, z in enumerate(zonas) if leido.get(str(i))]
        if nuevas:
            guardar_lecturas(origen, pdf, previas + nuevas)
            leidos += len(nuevas)
    if total.entrada:
        _registrar(trabajo_id, "leer", total)
    return {"leidos": leidos, "coste_usd": round(total.coste_usd(), 4)}


def comprobar_imagenes(trabajo_id: int, final: bool = True) -> dict[str, Any]:
    """Relee las imágenes rehechas del trabajo que no se han releído todavía.

    El asistente dice qué se lee en cada rótulo, si queda texto en el idioma de origen y
    si ve defectos; el veredicto lo calcula el código comparando con lo esperado. Si ha
    releído algo, se reprocesa para que el informe y los avisos lo reflejen.
    """
    from . import imagenes
    from .trabajador import carpeta_imagenes_pdf, imagenes_pdf, traducciones_imagen
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        unidad, destino = bd.ruta(t.paquete.ruta_extraido), bd.ruta(t.ruta_resultado)
        idioma_origen = t.paquete.idioma_origen
        idioma = t.idioma_destino or idioma_origen
    hechas = imagenes.comprobaciones(unidad)
    # (lectura, carpeta de la rehecha, carpeta de la original, fichero): las de la unidad y
    # las de dentro de los PDF, que se guardan extraídas junto al resultado.
    candidatas = [(l, destino, unidad, l["fichero"]) for l in imagenes.lecturas(unidad).values()]
    for pdf, l in imagenes_pdf(unidad):
        carpeta = carpeta_imagenes_pdf(destino, pdf.name) if destino else None
        fichero = next((f.name for f in carpeta.glob(f"{l['xref']}.*")), None) if carpeta else None
        if fichero:
            candidatas.append((l, carpeta, carpeta / "original", fichero))
    pendientes = []
    for lectura, dir_rehecha, dir_original, fichero in candidatas:
        if imagenes.destino(lectura) != "automático" or not destino:
            continue
        rehecha, original = dir_rehecha / fichero, dir_original / fichero
        if not rehecha.exists() or rehecha.read_bytes() == original.read_bytes():
            continue
        huella = imagenes.huella_fichero(rehecha)
        if huella in hechas:
            continue
        traducciones = traducciones_imagen(lectura, idioma_origen, idioma)
        esperados = [traducciones.get(imagenes.texto_rotulo(r)) or imagenes.texto_rotulo(r)
                     for r in imagenes.rotulos_validos(lectura)]
        pendientes.append({"id": huella, "png": imagenes.para_leer(dir_rehecha, {"fichero": fichero}),
                           "esperados": esperados})
    if not pendientes:
        mensaje = "No hay imágenes rehechas sin releer."
        if final:
            _progreso(trabajo_id, estado="listo", mensaje=mensaje)
        return {"releidas": 0, "mensaje": mensaje}

    total, errores = ia.Consumo(), []
    for inicio in range(0, len(pendientes), 4):
        grupo = pendientes[inicio:inicio + 4]
        _progreso(trabajo_id, hechas=inicio, total=len(pendientes),
                  mensaje=f"Releyendo imágenes rehechas: {inicio} de {len(pendientes)}.")
        try:
            leido = ia.comprobar_imagenes(grupo, idioma_origen, idioma, total)
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            errores.append(str(e))
            continue
        for i in grupo:
            if i["id"] in leido:
                hechas[i["id"]] = {k: v for k, v in leido[i["id"]].items() if k != "id"}
        imagenes.guardar_comprobaciones(unidad, hechas)
    _registrar(trabajo_id, "comprobar", total)
    reprocesar(trabajo_id)

    veredictos = [imagenes.veredicto(hechas[i["id"]])["estado"] for i in pendientes if i["id"] in hechas]
    mensaje = (f"{len(veredictos)} imágenes rehechas releídas: {veredictos.count('bien')} bien, "
               f"{veredictos.count('revisar')} con defectos visuales y {veredictos.count('mal')} que no "
               f"pasan y van a diseño. Coste estimado: {total.coste_usd():.2f} USD.")
    if errores:
        mensaje += "\n" + "\n".join(errores)
    if final:
        _progreso(trabajo_id, estado="listo" if veredictos else "error", mensaje=mensaje)
    return {"releidas": len(veredictos), "mensaje": mensaje, "coste_usd": round(total.coste_usd(), 4)}


def leer_imagenes(trabajo_id: int) -> dict[str, Any]:
    """Lee y clasifica las imágenes de la unidad que aún no se han leído (punto 6).

    Se guarda por paquete: la segunda vez no cuesta nada.
    """
    from . import imagenes
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        unidad, idioma_origen = bd.ruta(t.paquete.ruta_extraido), t.paquete.idioma_origen
    datos = imagenes.lecturas(unidad)
    pendientes = [i for i in imagenes.inventario(unidad) if i["huella"] not in datos]
    total, errores = ia.Consumo(), []
    for inicio in range(0, len(pendientes), 5):
        grupo = pendientes[inicio:inicio + 5]
        _progreso(trabajo_id, mensaje=f"Leyendo imágenes de la unidad: {inicio} de {len(pendientes)}.")
        try:
            leido = ia.clasificar_imagenes(
                [{"id": i["huella"], "png": imagenes.para_leer(unidad, i)} for i in grupo],
                idioma_origen, total)
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            errores.append(str(e))
            continue
        for i in grupo:
            if i["huella"] in leido:
                datos[i["huella"]] = {**i, **{k: v for k, v in leido[i["huella"]].items() if k != "id"}}
        imagenes.guardar(unidad, datos)
    if total.entrada:
        _registrar(trabajo_id, "leer_imagenes", total)
    return {"leidas": len(pendientes), "errores": errores, "coste_usd": round(total.coste_usd(), 4)}


def localizar_imagenes(trabajo_id: int, huellas: list[str] | None = None) -> dict[str, Any]:
    """Pide al asistente dónde están los rótulos que Tesseract no encuentra, y rehace.

    Sin `huellas`, las imágenes de rótulos sobre fondo liso que van a diseño: casi siempre
    porque algún rótulo «no se encuentra», que en gallego es lo normal. El asistente da la
    caja aproximada y Tesseract la afina leyendo solo ese recorte. Con `huellas`
    —el botón de cada imagen—, esas, aunque ya estén rehechas: sus cajas pasan a mandar
    sobre las de Tesseract. Las cajas se guardan en la lectura del paquete, así que
    reprocesar da siempre lo mismo y no se vuelven a pagar. Después se reprocesa y se
    releen las imágenes nuevas: lo que la relectura no dé por bueno vuelve a la original.
    """
    from PIL import Image

    from . import imagenes
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        unidad, idioma_origen = bd.ruta(t.paquete.ruta_extraido), t.paquete.idioma_origen
        resumen = json.loads(t.imagenes) if t.imagenes else {}
    datos = imagenes.lecturas(unidad)
    if huellas:
        elegidas = [datos[h] for h in huellas if h in datos]
    else:
        # Las de rótulos sobre fondo liso que se han quedado sin rehacer, por lo que sea:
        # casi siempre porque un rótulo no se encuentra o su caja sale mal medida.
        perdidas = {f for d in resumen.get("diseno", []) if not d.get("pdf") and not d.get("territorial")
                    for f in [d["fichero"], *d.get("copias", [])]}
        elegidas = [l for l in datos.values()
                    if l["fichero"] in perdidas and imagenes.destino(l) == "automático"]
    elegidas = [l for l in elegidas if imagenes.rotulos_validos(l)]
    # De las copias de una misma imagen solo se localiza la principal: las demás salen de
    # ella al reprocesar.
    copia_de = {h: lider for lider, hs in imagenes.grupos(unidad).items() for h in hs if h != lider}
    huellas_elegidas = {l["huella"] for l in elegidas}
    elegidas = [l for l in elegidas if copia_de.get(l["huella"]) not in huellas_elegidas]
    if not elegidas:
        return {"localizadas": 0, "mensaje": "No hay imágenes con rótulos sin localizar."}

    total, errores, hechas = ia.Consumo(), [], 0
    for n, lectura in enumerate(elegidas):
        _progreso(trabajo_id, hechas=n, total=len(elegidas),
                  mensaje=f"Localizando rótulos con el asistente: imagen {n + 1} de {len(elegidas)}.")
        ruta = unidad / lectura["fichero"]
        png = imagenes.para_leer(unidad, lectura)
        with Image.open(ruta) as im:
            ancho = im.width
        with Image.open(BytesIO(png)) as im:
            ancho_png, alto_png = im.size
        escala = ancho / ancho_png
        # Solo los rótulos que Tesseract no encuentra en la imagen entera: los que sí, los
        # sitúa mejor que el asistente.
        rotulos = [r for r, s in zip(imagenes.rotulos_validos(lectura),
                                     imagenes.localizar(ruta, imagenes.rotulos_validos(lectura), idioma_origen))
                   if not s["ok"]]
        ya = imagenes.cajas_vigentes(lectura)
        if not rotulos or (not huellas and all(imagenes.texto_rotulo(r) in ya for r in rotulos)):
            # Nada que localizar, o ya localizada: basta con rehacerla con lo que se sabe
            # ahora. Pedirla otra vez es el botón de la imagen.
            hechas += 1
            continue
        try:
            cajas = ia.localizar_rotulos(png, ancho_png, alto_png,
                                         [{"id": str(i), "lineas": r["lineas"]} for i, r in enumerate(rotulos)],
                                         total)
        except ia.ClaveNoValida:
            raise
        except ia.ErrorIA as e:
            errores.append(f"{Path(lectura['fichero']).name}: {e}")
            continue
        guardadas = {}
        for i, r in enumerate(rotulos):
            lineas = cajas.get(str(i)) or []
            ajustadas = [imagenes.afinar(ruta, [v * escala for v in c], texto, idioma_origen)
                         if len(c) == 4 else None for c, texto in zip(lineas, r["lineas"])]
            if len(ajustadas) == len(r["lineas"]) and imagenes.cajas_coherentes(ajustadas):
                guardadas[imagenes.texto_rotulo(r)] = [list(c) for c in ajustadas]
        # Pedida a mano, lo nuevo sustituye a lo anterior; si no, se suma.
        lectura["cajas"] = guardadas if huellas else {**ya, **guardadas}
        lectura["cajas_version"] = imagenes.VERSION_CAJAS
        lectura.pop("cajas_mandan", None)
        datos[lectura["huella"]] = lectura
        imagenes.guardar(unidad, datos)
        hechas += 1
    _registrar(trabajo_id, "localizar", total)
    reprocesar(trabajo_id)
    comprobacion = comprobar_imagenes(trabajo_id, final=False)
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        resumen = json.loads(t.imagenes) if t.imagenes else {}
    siguen = {d["fichero"] for d in resumen.get("diseno", [])}
    rehechas = sum(1 for l in elegidas if l["fichero"] not in siguen)
    mensaje = (f"{hechas} imágenes localizadas por el asistente: {rehechas} rehechas y "
               f"{len(elegidas) - rehechas} que siguen yendo a diseño. "
               f"Coste estimado: {total.coste_usd() + comprobacion.get('coste_usd', 0):.2f} USD.")
    if errores:
        mensaje += "\n" + "\n".join(errores)
    return {"localizadas": hechas, "rehechas": rehechas, "mensaje": mensaje}


def ruta_propuestas_imagenes(trabajo_id: int) -> Path:
    return bd.DATOS / "propuestas_imagenes" / f"trabajo-{trabajo_id}.json"


def propuestas_imagenes(trabajo_id: int) -> dict[str, dict]:
    """Las fotos de Commons propuestas para cada imagen del trabajo, por huella."""
    ruta = ruta_propuestas_imagenes(trabajo_id)
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def _uso_imagen(destino: Path, fichero: str) -> dict:
    """Cómo usa la unidad una imagen: su título, su texto alternativo y la lección."""
    nombre = Path(fichero).name
    paquete = abrir_unidad(destino)
    componentes = {cid for cid, c in paquete.componentes.items()
                   if c.get("name") == "image" and nombre in json.dumps(c.get("properties") or {},
                                                                        ensure_ascii=False)}
    uso: dict[str, Any] = {"fichero": nombre}
    for e in MapaEditorial(paquete).elementos:
        if e.componente in componentes and e.propiedad in ("alt", "title") and e.plano:
            uso.setdefault(e.propiedad, e.plano)
            uso.setdefault("leccion", e.titulo_leccion)
    return uso


def proponer_imagenes(trabajo_id: int, huellas: list[str] | None = None) -> dict[str, Any]:
    """Busca en Wikimedia Commons fotos reales con licencia libre para sustituir imágenes.

    Sin `huellas`, las del territorio de origen. Para cada una, el asistente mira la imagen
    y cómo la usa la unidad y dice qué buscar; se busca en Commons, se filtra por licencia
    y tamaño, y el asistente mira las candidatas y se queda con las que sirven. Nada se
    sustituye: las propuestas quedan en la pantalla de imágenes, para que el editor elija.
    """
    from concurrent.futures import ThreadPoolExecutor

    from . import commons, imagenes
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        unidad, destino, comunidad = bd.ruta(t.paquete.ruta_extraido), bd.ruta(t.ruta_resultado), t.comunidad
        resumen = json.loads(t.imagenes) if t.imagenes else {}
    datos = imagenes.lecturas(unidad)
    if huellas:
        elegidas = [i for i in imagenes.inventario(unidad) if i["huella"] in huellas]
    else:
        territoriales = {f for d in resumen.get("diseno", []) if d.get("territorial")
                         for f in [d["fichero"], *d.get("copias", [])]}
        elegidas = [i for i in imagenes.inventario(unidad)
                    if (datos.get(i["huella"]) or {}).get("territorial") or i["fichero"] in territoriales]
    # Una búsqueda por imagen, no por copia: la foto elegida sustituye a todas.
    principales = set(imagenes.grupos(unidad))
    elegidas = [i for i in elegidas if i["huella"] in principales] or elegidas
    if not elegidas:
        mensaje = "No hay imágenes del territorio de origen que sustituir."
        _progreso(trabajo_id, estado="listo", mensaje=mensaje)
        return {"propuestas": 0, "mensaje": mensaje}
    origen = _territorio_de_origen(trabajo_id) or "el territorio de origen"
    guardadas = propuestas_imagenes(trabajo_id)
    total, errores, con_candidatas = ia.Consumo(), [], 0
    for n, imagen in enumerate(elegidas):
        _progreso(trabajo_id, hechas=n, total=len(elegidas),
                  mensaje=f"Buscando fotos en Wikimedia Commons: imagen {n + 1} de {len(elegidas)}.")
        lectura = datos.get(imagen["huella"]) or {}
        contexto = {**_uso_imagen(destino, imagen["fichero"]),
                    "por_que": lectura.get("motivo") or "Imagen ligada al territorio de origen.",
                    "texto_de_la_imagen": [imagenes.texto_rotulo(r) for r in lectura.get("rotulos", [])]}
        try:
            pedido = ia.buscar_imagen(imagenes.para_leer(unidad, imagen), contexto, origen, comunidad, total)
        except ia.ClaveNoValida:
            raise
        except ia.ErrorIA as e:
            errores.append(f"{Path(imagen['fichero']).name}: {e}")
            continue
        vistas, fotos = set(), []
        for consulta in pedido.get("consultas", [])[:4]:
            for f in commons.buscar(consulta, ancho_minimo=int(imagen["ancho"] * 0.8)):
                if f["url"] not in vistas:
                    vistas.add(f["url"])
                    fotos.append(f)
        fotos = fotos[:16]

        def miniatura(f):
            try:
                return commons.miniatura_jpeg(commons.descargar(f["miniatura"], 3 * 1024 * 1024))
            except Exception:  # noqa: BLE001 — una miniatura que no baja es una candidata menos
                return None
        with ThreadPoolExecutor(max_workers=4) as hilos:
            minis = list(hilos.map(miniatura, fotos))
        candidatas = [{"id": str(k), "png": m, "titulo": f["titulo"], "descripcion": f["descripcion"]}
                      for k, (f, m) in enumerate(zip(fotos, minis)) if m]
        elegidas_ia = []
        if candidatas:
            try:
                elegidas_ia = ia.elegir_imagenes(pedido, candidatas, total)
            except ia.ClaveNoValida:
                raise
            except ia.ErrorIA as e:
                errores.append(f"{Path(imagen['fichero']).name}: {e}")
        propuestas = [{**fotos[int(e["id"])], "nota": e["nota"], "motivo": e["motivo"]}
                      for e in elegidas_ia[:4] if e["id"].isdigit() and int(e["id"]) < len(fotos)]
        guardadas[imagen["huella"]] = {"que_buscar": pedido.get("que_buscar", ""),
                                       "consultas": pedido.get("consultas", []),
                                       "candidatas": propuestas, "fecha": bd.ahora().isoformat()}
        con_candidatas += bool(propuestas)
        ruta = ruta_propuestas_imagenes(trabajo_id)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(guardadas, ensure_ascii=False, indent=1), encoding="utf-8")
    _registrar(trabajo_id, "proponer imágenes", total)
    mensaje = (f"Fotos de Wikimedia Commons propuestas para {con_candidatas} de {len(elegidas)} "
               "imágenes; elígelas en «Imágenes de la unidad». "
               f"Coste estimado: {total.coste_usd():.2f} USD.")
    if errores:
        mensaje += "\n" + "\n".join(errores)
    _progreso(trabajo_id, estado="listo", mensaje=mensaje)
    return {"propuestas": con_candidatas, "mensaje": mensaje}


def rehacer_imagenes(trabajo_id: int, huellas: list[str] | None = None) -> dict[str, Any]:
    """La operación «Rehacer imágenes» del asistente: `localizar_imagenes` con su progreso."""
    r = localizar_imagenes(trabajo_id, huellas)
    _progreso(trabajo_id, estado="listo", mensaje=r["mensaje"])
    return r


def _acumular(total: ia.Consumo, parcial: ia.Consumo) -> None:
    total.entrada += parcial.entrada
    total.salida += parcial.salida
    total.cache_lectura += parcial.cache_lectura
    total.cache_escritura += parcial.cache_escritura
    total.busquedas += parcial.busquedas
    total.modelo = parcial.modelo


def _guardar_traduccion(frase: dict, r: dict, origen: str, destino: str, autor: str,
                        trabajo_id: int, cuenta: dict, avisos: list[str]) -> None:
    traducida, nota = r.get("destino") or "", (r.get("nota") or "").strip()
    motivo = ia.validar_traduccion(frase["texto"], traducida)
    if motivo:
        cuenta["rechazadas"] += 1
        avisos.append(f"«{frase['texto'][:60]}»: {motivo}")
        return
    procedencia = (f"asistente, trabajo {trabajo_id}" + (f" · nota: {nota}" if nota else ""))[:250]
    if memoria.normalizar(traducida) == memoria.normalizar(frase["texto"]):
        memoria.marcar_invariante(origen, destino, frase["texto"], procedencia=procedencia,
                                  aprobada_por=autor, revisada=False)
        cuenta["sin_cambio"] += 1
    else:
        memoria.guardar_frase(origen, destino, frase["texto"], traducida, procedencia=procedencia,
                              aprobada_por=autor, revisada=False)
        cuenta["traducidas"] += 1


# ------------------------------------------------------------------- proponer

def _es_enlace(termino: bd.Termino) -> bool:
    from .trabajador import _ENLACES_VACIOS
    original = termino.original or ""
    if termino.clave.endswith(".href") and original.strip().lower() in _ENLACES_VACIOS:
        return False
    return (termino.clave.endswith(".href") or bool(re.match(r"https?://", original))
            or bool(re.search(r"\[PENDIENTE[^\]]*(enlace|enllaç|ligazón|URL|link)", original, re.I)))


def _territorio_de_origen(trabajo_id: int) -> str | None:
    with bd.Sesion() as sesion:
        origen = bd.ruta(sesion.get(bd.Trabajo, trabajo_id).paquete.ruta_extraido)
    return fuentes.territorio_de([e.plano for e in MapaEditorial(abrir_unidad(origen)).redactables])


def proponer(trabajo_id: int) -> dict[str, Any]:
    """Propuestas para los puntos territoriales pendientes de una adaptación.

    Rellenan la propuesta de cada término y lo dejan pendiente. Van todas juntas siempre
    que quepan: la coherencia entre la lección, el mapa y las actividades de relacionar
    —el error que encontraron los revisores en Asturias— solo se garantiza viéndolas a la
    vez. Si no caben se parten por lecciones, nunca a mitad de una.
    """
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        comunidad = trabajo.comunidad
        idioma = trabajo.idioma_destino or trabajo.paquete.idioma_origen
        unidad = trabajo.paquete.titulo
        terminos = [(t.id, t.leccion, t.original) for t in trabajo.pendientes
                    if not t.valor_final and not _es_enlace(t) and _es_traducible(t.original or "")]

    if not terminos:
        _progreso(trabajo_id, estado="listo", hechas=0, total=0,
                  mensaje="No hay puntos territoriales pendientes sin decidir.")
        return {"propuestas": 0}

    _progreso(trabajo_id, hechas=0, total=len(terminos),
              mensaje="Leyendo la unidad para saber de qué territorio parte.")
    origen = _territorio_de_origen(trabajo_id) or "el territorio de origen"

    grupos, actual, caracteres = [], [], 0
    for tid, leccion, texto in terminos:
        if actual and caracteres + len(texto) > 60000 and actual[-1]["leccion"] != leccion:
            grupos.append(actual)
            actual, caracteres = [], 0
        actual.append({"id": str(tid), "leccion": leccion, "texto": texto})
        caracteres += len(texto)
    grupos.append(actual)

    consumo, criterios, hechas, errores = ia.Consumo(), [], 0, []
    for n, grupo in enumerate(grupos, 1):
        # Es una sola llamada larga por grupo: el contador no se mueve hasta que vuelve.
        _progreso(trabajo_id, mensaje=(
            f"Esperando al modelo: {len(grupo)} términos de {origen} a {comunidad} en una sola "
            "llamada, para que las propuestas sean coherentes entre sí"
            + (f" (parte {n} de {len(grupos)})" if len(grupos) > 1 else "")
            + ". Suele tardar unos minutos; el contador avanza al terminar."))
        try:
            respuesta = ia.proponer_adaptaciones(grupo, idioma, origen, comunidad, consumo,
                                                 curriculo=_curriculo(trabajo_id), unidad=unidad,
                                                 documentacion=_documentacion(trabajo_id))
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            errores.append(str(e))
            continue
        criterios.append(respuesta.get("criterio", ""))
        with bd.Sesion() as sesion:
            for p in respuesta.get("propuestas", []):
                ident = str(p.get("id", ""))
                termino = sesion.get(bd.Termino, int(ident)) if ident.isdigit() else None
                if termino is None or termino.trabajo_id != trabajo_id or termino.valor_final:
                    continue
                termino.propuesta = p.get("propuesta") or termino.propuesta
                verificar = [v for v in p.get("verificar", []) if v]
                termino.motivo = ("Propuesta del asistente, sin verificar. "
                                  + (p.get("motivo") or "")
                                  + (" Comprobar: " + "; ".join(verificar) if verificar else "")).strip()
                hechas += 1
            sesion.commit()
        _progreso(trabajo_id, hechas=hechas)

    _registrar(trabajo_id, "proponer", consumo)
    mensaje = (f"{hechas} propuestas de {origen} a {comunidad}, todas pendientes de revisión. "
               f"Coste estimado: {consumo.coste_usd():.2f} USD.")
    if any(criterios):
        mensaje += "\nCriterio: " + " ".join(c for c in criterios if c)
    if errores:
        mensaje += "\n" + "\n".join(errores)
    _progreso(trabajo_id, estado="error" if errores and not hechas else "listo", mensaje=mensaje)
    return {"propuestas": hechas, "coste_usd": round(consumo.coste_usd(), 4)}


def otra_propuesta(termino_id: int, indicacion: str = "") -> dict[str, Any]:
    """Una propuesta nueva para un solo término, cuando la que hay no sirve o no hay ninguna.

    Es una llamada corta y se espera en el momento: el editor está delante del término. La
    propuesta sustituye a la anterior, pero el término no cambia de estado ni se toca lo
    que el editor haya decidido: la nueva se ofrece en la caja y la guarda él. Para no
    perder la coherencia de conjunto que da pedirlas todas juntas, la llamada lleva lo que
    el editor ya ha fijado en el resto de la unidad.
    """
    if not ia.disponible():
        raise ValueError("El asistente no está configurado: falta ANTHROPIC_API_KEY en el servidor.")
    clave = ia.estado_clave()
    if not clave["ok"]:
        raise ValueError(clave["motivo"])
    with bd.Sesion() as sesion:
        termino = sesion.get(bd.Termino, termino_id)
        if termino is None:
            raise ValueError(f"No existe el término {termino_id}")
        trabajo = termino.trabajo
        if trabajo.tipo != bd.ADAPTACION or not trabajo.comunidad:
            raise ValueError("Las propuestas son para adaptaciones curriculares.")
        if _es_enlace(termino):
            raise ValueError("Es un enlace: los candidatos los busca «Buscar enlaces», que "
                             "comprueba que cada dirección exista.")
        trabajo_id, comunidad, unidad = trabajo.id, trabajo.comunidad, trabajo.paquete.titulo
        idioma = trabajo.idioma_destino or trabajo.paquete.idioma_origen
        anterior = ia_texto(termino.valor_final or termino.propuesta or "")
        fragmento = {"id": str(termino.id), "leccion": termino.leccion, "texto": termino.original,
                     "indicacion": (indicacion or "").strip()[:500]}
        if anterior and memoria.normalizar(anterior) != memoria.normalizar(termino.original or ""):
            fragmento["propuesta_anterior"] = anterior
        fragmento["otras_decisiones"] = [
            f"{t.leccion or ''}: {ia_texto(t.valor_final)[:300]}" for t in trabajo.pendientes
            if t.id != termino.id and t.valor_final and not _es_enlace(t)][:40]

    origen = _territorio_de_origen(trabajo_id) or "el territorio de origen"
    consumo = ia.Consumo()
    try:
        respuesta = ia.proponer_adaptaciones([fragmento], idioma, origen, comunidad, consumo,
                                             curriculo=_curriculo(trabajo_id), unidad=unidad,
                                             documentacion=_documentacion(trabajo_id),
                                             esfuerzo="medium")
    finally:
        if consumo.entrada or consumo.salida:
            _registrar(trabajo_id, "otra propuesta", consumo)
    propuesta = next((p for p in respuesta.get("propuestas", [])
                      if str(p.get("id")) == str(termino_id)), None)
    if not propuesta or not propuesta.get("propuesta"):
        raise ia.ErrorIA("El asistente no ha devuelto ninguna propuesta para este término.")
    verificar = [v for v in propuesta.get("verificar", []) if v]
    motivo = ("Propuesta del asistente, sin verificar. " + (propuesta.get("motivo") or "")
              + (" Comprobar: " + "; ".join(verificar) if verificar else "")).strip()
    with bd.Sesion() as sesion:
        termino = sesion.get(bd.Termino, termino_id)
        termino.propuesta, termino.motivo = propuesta["propuesta"], motivo
        sesion.commit()
    return {"termino_id": termino_id, "propuesta": propuesta["propuesta"], "motivo": motivo,
            "coste_usd": round(consumo.coste_usd(), 4)}


def ia_texto(valor: str | None) -> str:
    """El texto que lee el modelo, sin el marcado del elemento."""
    from maker_paquete import texto_plano
    valor = valor or ""
    return texto_plano(valor) if valor.lstrip().startswith("<") else valor


# ------------------------------------------------------------------- revisar

def revisar(trabajo_id: int) -> dict[str, Any]:
    """Lee la unidad entera y marca lo que depende del territorio, esté o no en la lista.

    Solo añade términos pendientes: las propuestas se piden después con «proponer». Los
    textos idénticos se envían una vez, y el hallazgo se aplica a todos los sitios donde
    aparecen. Van por lecciones enteras, nunca partiendo una.
    """
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        comunidad = trabajo.comunidad
        idioma = trabajo.idioma_destino or trabajo.paquete.idioma_origen
        unidad = trabajo.paquete.titulo
        destino = bd.ruta(trabajo.ruta_resultado)
        ya_marcadas = {t.clave for t in trabajo.pendientes}

    mapa = MapaEditorial(abrir_unidad(destino))
    por_texto: dict[str, dict] = {}
    for e in mapa.redactables:
        plano = e.plano
        if (".cmi5:" in e.clave or e.clave.endswith(".href") or e.clave in ya_marcadas
                or len(plano) < 4 or not _es_traducible(plano)):
            continue
        clave_texto = memoria.normalizar(plano)
        if clave_texto not in por_texto:
            por_texto[clave_texto] = {"id": f"f{len(por_texto) + 1}", "texto": plano,
                                      "leccion": e.titulo_leccion, "claves": []}
        por_texto[clave_texto]["claves"].append((e.clave, e.titulo_leccion, e.valor))
    # Y el texto de los PDF: el resumen de Biología tiene un apartado entero sobre Galicia,
    # y la lista de topónimos no ve un ejemplo local que no nombre ningún sitio.
    from .trabajador import _pdfs_maquetados, decisiones_pdf, sin_negritas, textos_pdf
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        unidad_origen = bd.ruta(t.paquete.ruta_extraido)
        idioma_origen = t.paquete.idioma_origen
    decididas = decisiones_pdf(trabajo_id)
    for pdf in _pdfs_maquetados(unidad_origen):
        try:
            fichas, _, _ = textos_pdf(unidad_origen, pdf, idioma_origen, idioma, decididas)
        except Exception:
            continue
        for f in fichas:
            plano = sin_negritas(f["traducido"] or f["original"])
            if f["clave"] in ya_marcadas or f.get("decidido") or len(plano) < 4:
                continue
            clave_texto = memoria.normalizar(plano)
            leccion = f"PDF «{pdf.name}», página {f['pagina']}"
            if clave_texto not in por_texto:
                por_texto[clave_texto] = {"id": f"f{len(por_texto) + 1}", "texto": plano,
                                          "leccion": leccion, "claves": []}
            por_texto[clave_texto]["claves"].append(
                (f["clave"], leccion, f["traducido"] or f["original"]))
    fragmentos = list(por_texto.values())
    por_id = {f["id"]: f for f in fragmentos}

    origen = _territorio_de_origen(trabajo_id) or "el territorio de origen"
    grupos, actual, caracteres = [], [], 0
    for f in fragmentos:
        if actual and caracteres + len(f["texto"]) > 60000 and actual[-1]["leccion"] != f["leccion"]:
            grupos.append(actual)
            actual, caracteres = [], 0
        actual.append(f)
        caracteres += len(f["texto"])
    if actual:
        grupos.append(actual)
    _progreso(trabajo_id, hechas=0, total=len(fragmentos),
              mensaje=f"Leyendo {len(fragmentos)} fragmentos en {len(grupos)} partes.")

    consumo, resumenes, errores, nuevos, hechas = ia.Consumo(), [], [], 0, 0
    importancias: dict[str, int] = {}
    for grupo in grupos:
        try:
            respuesta = ia.revisar_territorio(grupo, idioma, origen, comunidad, consumo, unidad,
                                              curriculo=_curriculo(trabajo_id, con_plan=False),
                                              documentacion=_documentacion(trabajo_id))
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            errores.append(str(e))
            continue
        resumenes.append(respuesta.get("resumen", ""))
        with bd.Sesion() as sesion:
            for h in respuesta.get("hallazgos", []):
                f = por_id.get(str(h.get("id", "")))
                if f is None:
                    continue
                importancia = h.get("importancia", "media")
                importancias[importancia] = importancias.get(importancia, 0) + 1
                for clave, leccion, valor in f["claves"]:
                    if clave in ya_marcadas:
                        continue
                    sesion.add(bd.Termino(
                        trabajo_id=trabajo_id, clave=clave, leccion=leccion,
                        motivo=f"Detectado por el asistente (importancia {importancia}): {h.get('motivo', '')}",
                        original=f["texto"], propuesta=f["texto"], valor_base=valor,
                        estado=bd.PENDIENTE, detectado_por="asistente"))
                    ya_marcadas.add(clave)
                    nuevos += 1
            sesion.commit()
        hechas += len(grupo)
        _progreso(trabajo_id, hechas=hechas)

    _registrar(trabajo_id, "revisar", consumo)
    detalle = ", ".join(f"{v} de importancia {k}" for k, v in sorted(importancias.items()))
    mensaje = (f"{nuevos} puntos nuevos que dependen del territorio"
               + (f" ({detalle})" if detalle else "") + ", añadidos como pendientes. "
               "Pide propuestas para ellos con «Proponer adaptaciones». "
               f"Coste estimado: {consumo.coste_usd():.2f} USD.")
    if any(resumenes):
        mensaje += "\n" + " ".join(r for r in resumenes if r)
    if errores:
        mensaje += "\n" + "\n".join(errores)
    _progreso(trabajo_id, estado="error" if errores and not hechas else "listo", mensaje=mensaje)
    return {"nuevos": nuevos, "coste_usd": round(consumo.coste_usd(), 4)}


# ------------------------------------------------------------------- enlaces

def buscar_enlaces(trabajo_id: int) -> dict[str, Any]:
    """Candidatos comprobados para cada enlace pendiente. Siguen pendientes."""
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        comunidad = trabajo.comunidad
        idioma = trabajo.idioma_destino or trabajo.paquete.idioma_origen
        unidad = trabajo.paquete.titulo
        destino = bd.ruta(trabajo.ruta_resultado)
        terminos = [(t.id, t.clave, t.leccion, t.original) for t in trabajo.pendientes
                    if not t.valor_final and _es_enlace(t)]

    if not terminos:
        _progreso(trabajo_id, estado="listo", hechas=0, total=0, mensaje="No hay enlaces pendientes.")
        return {"enlaces": 0}

    mapa = MapaEditorial(abrir_unidad(destino))
    dominios = fuentes.dominios(comunidad)
    _progreso(trabajo_id, hechas=0, total=len(terminos),
              mensaje=f"Buscando en {len(dominios)} dominios oficiales y educativos.")

    consumo, hechas, con_candidatas, errores = ia.Consumo(), 0, 0, []
    for tid, clave, leccion, original in terminos:
        rotulo = _rotulo(mapa, clave, original)
        contexto = f"Unidad: {unidad}. Lección: {leccion or '—'}. Texto: {original[:500]}"
        try:
            candidatas = ia.buscar_enlaces(rotulo, contexto, comunidad, idioma, dominios, consumo)
        except ia.ClaveNoValida:
            raise   # ningún lote va a pasar: se para aquí
        except ia.ErrorIA as e:
            errores.append(f"{clave}: {e}")
            candidatas = []
        with bd.Sesion() as sesion:
            sesion.get(bd.Termino, tid).candidatas = json.dumps(candidatas, ensure_ascii=False)
            sesion.commit()
        hechas += 1
        con_candidatas += any(c.get("comprobada") for c in candidatas)
        _progreso(trabajo_id, hechas=hechas)

    _registrar(trabajo_id, "enlaces", consumo)
    mensaje = (f"{con_candidatas} de {hechas} enlaces con al menos una candidata comprobada. "
               "Siguen pendientes: elige una en cada término. "
               f"{consumo.busquedas} búsquedas; coste estimado de tokens "
               f"{consumo.coste_usd():.2f} USD, las búsquedas se facturan aparte.")
    if errores:
        mensaje += "\n" + "\n".join(errores)
    _progreso(trabajo_id, estado="listo" if hechas > len(errores) else "error", mensaje=mensaje)
    return {"enlaces": hechas, "con_candidatas": con_candidatas}


def _rotulo(mapa: MapaEditorial, clave: str, original: str) -> str:
    """Lo que anuncia un enlace. Para un `href`, el título del mismo componente."""
    if not clave.endswith(".href"):
        return original
    componente = clave.split(".")[0]
    for propiedad in ("title", "titleHtml", "textContent"):
        try:
            texto = mapa.elemento(f"{componente}.{propiedad}").plano
        except KeyError:
            continue
        if texto:
            return texto
    return original


# ------------------------------------------------------------------- revisión

def traducciones_por_revisar(trabajo_id: int, todas: bool = False) -> dict[str, Any]:
    """Las traducciones automáticas de esta unidad que nadie ha confirmado, o todas."""
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        if trabajo is None:
            raise ValueError(f"No existe el trabajo {trabajo_id}")
        origen, destino = trabajo.paquete.idioma_origen, trabajo.idioma_destino
        ruta = bd.ruta(trabajo.paquete.ruta_extraido)
    if not destino or destino == origen:
        return {"trabajo_id": trabajo_id, "frases": []}

    from .trabajador import extraer_frases  # noqa: PLC0415
    textos, lecciones = [], {}
    for elemento in MapaEditorial(abrir_unidad(ruta)).redactables:
        for f in extraer_frases(elemento.valor):
            n = memoria.normalizar(f)
            if n and n not in lecciones:
                textos.append(n)
                lecciones[n] = elemento.titulo_leccion
    # Y lo que no está en la unidad pero se traduce con ella: el texto de los PDF y los
    # rótulos de las imágenes. Sin esto, 124 traducciones de «Los Invertebrados» no se
    # podían revisar desde la pantalla.
    from . import imagenes
    from .trabajador import _textos_pdf, imagenes_pdf
    for fichero, texto in _textos_pdf(ruta):
        n = memoria.normalizar(texto)
        if n and n not in lecciones:
            textos.append(n)
            lecciones[n] = f"PDF «{fichero}»"
    con_lectura = [(f"Imagen «{Path(l['fichero']).name}»", l) for l in imagenes.lecturas(ruta).values()]
    con_lectura += [(f"Imagen del PDF «{pdf.name}», página {l['pagina']}", l)
                    for pdf, l in imagenes_pdf(ruta)]
    for leccion, lectura in con_lectura:
        if imagenes.destino(lectura) == "nada":
            continue
        for rotulo in imagenes.rotulos_validos(lectura):
            n = memoria.normalizar(imagenes.texto_rotulo(rotulo))
            if n and n not in lecciones:
                textos.append(n)
                lecciones[n] = leccion
    frases = memoria.frases_sin_revisar(origen, destino, textos, incluir_revisadas=todas)
    for f in frases:
        f["leccion"] = lecciones.get(f["origen"])
    return {"trabajo_id": trabajo_id, "idioma_origen": origen, "idioma_destino": destino,
            "frases": frases}
