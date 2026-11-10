"""Procesa un paquete: aplica lo que el sistema ya sabe y señala lo que no decide solo.

La pieza que hace esto sostenible es la **memoria de adaptación**: los ficheros de
`adaptacion/*.json` son decisiones editoriales tomadas y aprobadas, indexadas por
elemento. El trabajador aplica las que encajan con el paquete que le llega y, para todo
lo demás que requiere criterio —anclajes territoriales, enlaces externos, atribuciones
institucionales—, no se inventa nada: lo deja marcado como término pendiente para que lo
resuelva un editor.

Cuando el editor resuelve esos términos, sus decisiones vuelven a la memoria. La próxima
unidad parecida ya no preguntará.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
sys.path.insert(0, str(RAIZ / "scripts"))

from maker_paquete import (ACTIVIDADES, MapaEditorial, Paquete, abrir_unidad,  # noqa: E402
                           ajustar_relleno, claves_glosario, editar_texto, es_fuente, extraer_frases,
                           reenlazar_glosario, reinsertar_frases, texto_plano)

from . import bd, memoria  # noqa: E402

TRABAJO = bd.DATOS

_MARCA = re.compile(r"\[PENDIENTE[^\]]*\]")

# El valor que tiene la propiedad `href` de un componente cuando nadie ha puesto enlace.
_ENLACES_VACIOS = {"", "http://", "https://", "#", "http:", "https:"}


def _sin_tildes(texto: str) -> str:
    """Quita los diacríticos, para que «Galícia» y «Galicia» sean la misma palabra."""
    return "".join(c for c in unicodedata.normalize("NFD", texto or "")
                   if unicodedata.category(c) != "Mn")


# Rastros de que un texto está atado a un territorio concreto y no se puede traducir sin
# decidir. Vale más preguntar de más que colar un dato falso en un libro de texto.
#
# No basta con los nombres de las comunidades: un texto queda atado a Galicia nombrando
# «As Pontes», «Lugo» o el «gneis ollo de sapo», sin decir Galicia en ninguna parte.
# Recortar la comunidad a su primera palabra metía artículos en el patrón: «La Rioja»
# aportaba «La», que casa con cualquier frase en castellano. Se usan los nombres enteros y
# se descartan las palabras que por sí solas no señalan a ningún sitio.
_GENERICAS = {"la", "el", "les", "los", "las", "de", "del", "y", "comunidad", "region",
              "principado", "islas", "illes", "ciudad", "pais", "foral", "valenciana"}
_NOMBRES_CCAA = sorted(
    {p for c in bd.COMUNIDADES for p in _sin_tildes(c).replace("-", " ").split()
     if p.lower() not in _GENERICAS and len(p) > 3},
    key=len, reverse=True)

_TERRITORIO = re.compile(
    r"\b(" + "|".join(_NOMBRES_CCAA) +
    # Variantes en las otras lenguas del Estado de los territorios que cita esta unidad.
    r"|Asturies|Catalunya|Galiza|Euskadi|Andalusia|Arago|Balears|Canaries|Castella"
    r"|Galicia|Xunta|EDIXGAL|e-dixgal|gallego|galego|gallec|galaic"
    r"|As Pontes|Lugo|Ourense|Pontevedra|Coruna|Compostela|Finisterre"
    r"|Berbes|Caravia|Soton|Almeria|Sevilla"
    r"|ollo de sapo|ull de gripau|Armorica|volframio|tungste)\b"
    r"|\.gal\b|\bxunta\.es\b|\bminariasostible\b", re.I)


def _es_territorial(texto: str) -> bool:
    """Si el texto está atado a un territorio concreto.

    Se compara **sin tildes**. El corpus ya traducido escribe «Galícia», y un patrón con
    «Galicia» no casaba con él: en una adaptación primero se traduce y después se busca lo
    territorial, así que la detección no puede depender de en qué lengua esté el texto
    cuando se mira.
    """
    return bool(_TERRITORIO.search(_sin_tildes(texto)))


def _contexto(elemento) -> str:
    return texto_plano(elemento.valor)[:240]


def procesar(trabajo_id: int) -> None:
    """Ejecuta un encargo de principio a fin. Pensado para correr en segundo plano."""
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        if trabajo is None:
            return
        trabajo.estado = bd.PROCESANDO
        sesion.commit()
        paquete_bd = trabajo.paquete
        origen = bd.ruta(paquete_bd.ruta_extraido)
        destino = TRABAJO / "resultados" / f"trabajo-{trabajo.id}"

    try:
        resumen = _ejecutar(trabajo_id, origen, destino)
    except Exception as e:  # el editor tiene que ver el motivo, no un error en blanco
        with bd.Sesion() as sesion:
            trabajo = sesion.get(bd.Trabajo, trabajo_id)
            trabajo.estado = bd.ERROR
            trabajo.mensaje = f"{type(e).__name__}: {e}"
            trabajo.terminado_en = dt.datetime.now()
            sesion.commit()
        return

    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        trabajo.estado = bd.LISTO
        trabajo.terminado_en = dt.datetime.now()
        trabajo.ruta_resultado = bd.relativa(destino)
        trabajo.ruta_informe = bd.relativa(resumen["informe"])
        trabajo.elementos_totales = resumen["totales"]
        trabajo.elementos_adaptados = resumen["adaptados"]
        trabajo.avisos_validacion = resumen["avisos"]
        trabajo.frases_totales = resumen["frases_totales"]
        trabajo.frases_conocidas = resumen["frases_conocidas"]
        trabajo.imagenes = json.dumps(resumen["imagenes"], ensure_ascii=False)
        for termino in resumen["terminos"]:
            sesion.add(bd.Termino(trabajo_id=trabajo.id, **termino))
        sesion.commit()


def _ejecutar(trabajo_id: int, origen: Path, destino: Path) -> dict:
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        idioma, comunidad = trabajo.idioma_destino, trabajo.comunidad
        tipo = trabajo.tipo
        idioma_contenido = trabajo.paquete.idioma_origen
        regenerar = trabajo.regenerar_imagenes is not False

    paquete = abrir_unidad(origen)
    mapa = MapaEditorial(paquete)
    # Dos cosas distintas: la clave de idioma del paquete, con la que se reescriben las
    # cinco copias, y el idioma real del contenido, que decide qué se traduce.
    clave_idioma = paquete.idiomas[0] if paquete.idiomas else "es"
    idioma_origen = idioma_contenido or clave_idioma
    # Los términos del glosario antes de traducirlos: los enlaces del texto los buscan por
    # su nombre, y hay que llevarlos al nombre traducido (ver `reenlazar_glosario`).
    glosario = claves_glosario(mapa)
    _, glosario_roto_origen = reenlazar_glosario(mapa, glosario)

    # Todo el texto de la unidad, frase a frase. La memoria se consulta de una vez: una
    # unidad tiene miles de frases y preguntar por cada una sería inviable.
    por_elemento = {e.clave: extraer_frases(e.valor) for e in mapa.redactables}
    todas = {f for frases in por_elemento.values() for f in frases}

    conocidas = (memoria.buscar_frases(idioma_origen, idioma, todas)
                 if idioma and idioma != idioma_origen else {})

    # Frases que se quedan como están: las revisadas y devueltas iguales, y lo que no es
    # lenguaje (identificadores, números, puntuación suelta). Cuentan como resueltas, no
    # como pendientes: sin esto una coma suelta dejaba sin traducir el párrafo entero en
    # el que aparecía, porque la aplicación es todo o nada por elemento.
    intocables = set(memoria.frases_invariantes(idioma_origen, idioma, todas)) if idioma else set()
    intocables |= {f for f in todas if not _es_traducible(f)}
    aplicados: list[tuple[str, str, str, str]] = []
    terminos: list[dict] = []
    frases_conocidas = 0

    for elemento in mapa.redactables:
        frases = por_elemento[elemento.clave]
        traducciones = [conocidas.get(f) or (f if f in intocables else None) for f in frases]
        # Las líneas de puntos de una clave dicotómica conservan la longitud del original.
        traducciones = [ajustar_relleno(f, t) if t else t for f, t in zip(frases, traducciones)]
        frases_conocidas += sum(1 for f in frases if f in conocidas)

        # Todo o nada por elemento: aplicar solo las frases conocidas dejaría párrafos
        # mezclando dos lenguas a media frase, que es peor que no tocarlos.
        # Y al menos una traducción de verdad: un elemento hecho solo de intocables no se
        # ha adaptado, y contarlo como tal inflaría la cobertura con texto sin tocar.
        if frases and all(x is not None for x in traducciones) and any(f in conocidas
                                                                      for f in frases):
            anterior = elemento.valor
            try:
                mapa.aplicar_cambio(elemento.clave, reinsertar_frases(anterior, traducciones))
                aplicados.append((elemento.clave, anterior, elemento.valor,
                                  "Traducción conocida por el sistema"))
                continue
            except ValueError:
                pass

    _, glosario_roto = reenlazar_glosario(mapa, glosario)

    # Segunda pasada sobre el resultado ya aplicado: qué queda por decidir.
    #
    # Las equivalencias se consultan **aquí**, con el texto ya traducido, no antes. Una
    # adaptación traduce primero y decide lo territorial después, así que la memoria de
    # equivalencias se indexa por el texto en la lengua de destino; buscándolas sobre el
    # paquete de origen no casaba ninguna. Es el mismo orden que sigue `_es_territorial`.
    #
    # Solo en una adaptación curricular: en una traducción no se cambia de territorio, y
    # sin `comunidad` la consulta devolvía las equivalencias de cualquier otra, con lo que
    # un trabajo al catalán proponía sustituir el gneis gallego por caliza asturiana.
    aplicadas = {c for c, _, _, _ in aplicados}
    equivalentes = (memoria.buscar_equivalencias(
        {texto_plano(e.valor) for e in mapa.redactables}, comunidad)
        if comunidad else {})
    for elemento in mapa.redactables:
        plano = texto_plano(elemento.valor)
        marca = _MARCA.search(elemento.valor)
        equivalente = equivalentes.get(plano)
        # También si la lección entera lo es: la lección «Rocas y minerales de Galicia» hay
        # que reescribirla completa, no solo los párrafos que repiten el topónimo.
        territorial = _es_territorial(plano) or _es_territorial(elemento.titulo_leccion)
        sin_traducir = elemento.clave not in aplicadas and bool(por_elemento[elemento.clave])

        if not (marca or equivalente or territorial):
            continue
        # Los rótulos vacíos de la plantilla («Block Title», «Button Text») no son
        # contenido: arrastrados por estar en una lección territorial, solo hacen ruido.
        if not marca and not _es_traducible(plano):
            continue
        # Tampoco el enlace vacío que la plantilla pone por defecto en cada imagen.
        if elemento.clave.endswith(".href") and plano.strip().lower() in _ENLACES_VACIOS:
            continue

        # En una **adaptación**, todo lo territorial sale a revisión aunque ya esté
        # traducido: que «gneis ull de gripau» esté en catalán impecable no lo hace válido
        # para Cataluña. En una **traducción** no se cambia de territorio, así que basta
        # con avisar de lo que no se ha podido traducir.
        if not marca and not equivalente and not sin_traducir and not (comunidad and territorial):
            continue

        if marca:
            motivo = "Marcado como pendiente: exige una decisión que el sistema no toma"
            propuesta = plano
        elif equivalente:
            motivo = (f"El sistema conoce un equivalente para {comunidad or 'este territorio'}"
                      + (f": {equivalente['motivo'][:120]}" if equivalente.get("motivo") else "")
                      + ". Confírmalo antes de aplicarlo.")
            propuesta = equivalente["propuesta"]
        elif comunidad:
            motivo = (f"Contenido atado al territorio de origen. Reescríbelo para {comunidad} "
                      "o confirma que vale tal cual.")
            propuesta = plano
        else:
            motivo = "Referencia territorial sin decisión guardada"
            propuesta = plano

        terminos.append({
            "clave": elemento.clave, "leccion": elemento.titulo_leccion,
            "motivo": motivo, "original": plano, "propuesta": propuesta,
            "valor_base": elemento.valor,
            "estado": bd.PENDIENTE,
        })

    if conocidas:
        memoria.anotar_uso(idioma_origen, idioma, conocidas)

    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        shutil.rmtree(destino, ignore_errors=True)
    paquete.reconstruir(destino)

    cambio_idioma = bool(idioma and idioma != idioma_origen)
    fuente = es_fuente(destino)
    # Si el contenido cambia de idioma, o la clave no era la suya, se rehace la clave en
    # las cinco copias y el idioma del reproductor. En un fuente de Author no se toca
    # todavía: falta saber cómo espera Author el cambio de idioma (ver el informe).
    if idioma and (cambio_idioma or clave_idioma != idioma) and not fuente:
        _cambiar_idioma(destino, clave_idioma, idioma)
    idioma_fuente = []
    if fuente and idioma:
        # En el fuente, el idioma se marca en la carpeta de la revisión y en los datos del
        # curso de cada proyecto. También corrige el que declaraba mal: Prehistoria decía
        # `es` estando en gallego.
        idioma_fuente = abrir_unidad(destino).cambiar_idioma(idioma)

    # Las imágenes con texto, antes de la versión imprimible, para que salga con ellas.
    avisos_img, imagenes_hechas = [], []
    if (cambio_idioma or comunidad) and regenerar:
        avisos_img, imagenes_hechas = _reescribir_imagenes(origen, destino, idioma_origen,
                                                           idioma or idioma_origen, comunidad)
    elif cambio_idioma or comunidad:
        avisos_img.append(("AVISO", "imagenes", "No se han regenerado las imágenes con texto "
                           "(opción del trabajo): siguen como en el original."))
    # Las que ha sustituido una persona, encima de todo lo anterior.
    sustituidas = aplicar_sustituciones(trabajo_id, origen, destino)
    for h in imagenes_hechas:
        if h["fichero"] in sustituidas:
            h["destino"], h["estado"] = "sustituida", "sustituida"
    if sustituidas and imagenes_hechas:
        # Las sustituidas ya no van a diseño: los avisos se rehacen sin ellas.
        avisos_img = _avisos_imagenes(imagenes_hechas, "imagenes", "imágenes con texto",
                                      bd.IDIOMAS.get(idioma_origen, idioma_origen), comunidad,
                                      cambio_idioma)

    # La versión imprimible se regenera desde la unidad ya adaptada: es la sexta copia del
    # contenido, y sin esto el paquete llevaría la unidad traducida y su PDF sin traducir.
    avisos_pdf, impresion_hecha = list(avisos_img), None
    if aplicados and not fuente:
        from . import impresion
        avisos_impresion, impresion_hecha = impresion.regenerar(destino, origen, cambio_idioma)
        avisos_pdf += avisos_impresion

    # Los demás PDF —fichas, rúbricas, diagramas— no se regeneran: se reescribe su texto
    # en su sitio con lo que la memoria sabe traducir.
    pdfs_hechos: list[dict] = []
    decididas = decisiones_pdf(trabajo_id)
    if cambio_idioma or comunidad or decididas:
        avisos_maquetados, pdfs_hechos, terminos_pdf, imagenes_pdf = _reescribir_pdfs(
            origen, destino, idioma_origen, idioma or idioma_origen, comunidad, decididas,
            regenerar_imagenes=regenerar)
        avisos_pdf += avisos_maquetados
        terminos += terminos_pdf
        imagenes_hechas = list(imagenes_hechas) + imagenes_pdf

    avisos = _validar(destino, origen) + avisos_pdf
    avisos += _avisos_glosario(glosario_roto, glosario_roto_origen)
    if fuente:
        avisos += _nombrar_fuente(origen, destino, idioma_origen, idioma)
        avisos += _avisos_fuente(abrir_unidad(destino), idioma, idioma_fuente)
    if clave_idioma != idioma_origen:
        avisos.append(("AVISO", "idioma",
                       f"El paquete declara «{bd.IDIOMAS.get(clave_idioma, clave_idioma)}» pero su "
                       f"contenido está en «{bd.IDIOMAS.get(idioma_origen, idioma_origen)}»: se ha "
                       "tratado como lo que es. Conviene corregirlo en el origen."))
    total_frases = sum(len(f) for f in por_elemento.values())

    # La cobertura se mide sobre lo que hay que traducir. Un elemento sin frases, o hecho
    # solo de intocables —rótulos de plantilla, cifras, nombres que no cambian—, sale bien
    # tal cual: contarlo como pendiente anunciaba «484 elementos siguen en gallego» en una
    # unidad sin una sola frase en gallego.
    con_texto = [e.clave for e in mapa.redactables
                 if e.clave in aplicadas
                 or any(f not in intocables for f in por_elemento[e.clave])]
    sin_traducir = [c for c in con_texto if c not in aplicadas]

    if not aplicados and con_texto:
        avisos.insert(0, (
            "GRAVE", "cobertura",
            f"El sistema no reconoció ninguno de los {len(con_texto)} elementos con texto de "
            f"esta unidad: de sus {total_frases} frases, {frases_conocidas} estaban en "
            f"memoria. Se han señalado {len(terminos)} puntos que requieren decisión, pero "
            f"el contenido sigue como estaba."))
    elif cambio_idioma and sin_traducir:
        avisos.insert(0, (
            "GRAVE", "idioma",
            f"El paquete se ha marcado como «{bd.IDIOMAS.get(idioma, idioma)}» pero "
            f"{len(sin_traducir)} de {len(con_texto)} elementos con texto siguen en "
            f"«{bd.IDIOMAS.get(idioma_origen, idioma_origen)}»: no había traducción en "
            f"memoria para ellos. No se puede entregar así."))

    informe = _informe(destino, trabajo_id, tipo, idioma, comunidad, aplicados, terminos, avisos,
                       impresion_hecha, pdfs_hechos, imagenes_hechas)

    return {"informe": informe, "totales": len(con_texto),
            "adaptados": len(aplicados), "avisos": len(avisos), "terminos": terminos,
            "frases_totales": total_frases, "frases_conocidas": frases_conocidas,
            "imagenes": resumen_imagenes(imagenes_hechas)}


_SCRIPTS: dict[str, object] = {}


def _cargar_script(nombre: str):
    """Carga un script del toolkit una sola vez y sin que toque la salida del servidor.

    Los scripts de `scripts/` reemplazan `sys.stdout` al importarse para escribir con
    tildes en la consola de Windows. Cargarlos dos veces dentro del mismo proceso deja el
    flujo anterior cerrado y el siguiente `print` revienta, así que se cachean y se
    restaura la salida al terminar.
    """
    if nombre in _SCRIPTS:
        return _SCRIPTS[nombre]

    import importlib.util

    salida = sys.stdout
    try:
        spec = importlib.util.spec_from_file_location(nombre, RAIZ / "scripts" / f"{nombre}.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
    finally:
        sys.stdout = salida

    _SCRIPTS[nombre] = modulo
    return modulo


def _cambiar_idioma(destino: Path, de: str, a: str) -> None:
    """Reutiliza el paso de finalización del toolkit, que ya sabe tocar las cinco copias."""
    modulo = _cargar_script("finalizar")

    paquete = abrir_unidad(destino)
    modulo.cambiar_idioma(paquete, de, a)
    modulo.sincronizar_anidados(paquete)
    modulo.sincronizar_respuestas_cmi5(paquete, a)
    modulo.derivar_titulos(paquete, a)
    from maker_paquete.serializacion import VOLCADORES
    for nombre, obj in (("project.json", paquete.project),
                        ("components.json", paquete.components),
                        ("cmi5.json", paquete.cmi5)):
        (destino / nombre).write_bytes(VOLCADORES[nombre](obj))
    modulo.sincronizar_index(destino, paquete, a, None)


def _avisos_glosario(roto: set[str], roto_origen: set[str]) -> list[tuple[str, str, str]]:
    """Enlaces al glosario que no llevan a ningún término: la ventana se abre vacía."""
    avisos = []
    nuevos, heredados = sorted(roto - roto_origen), sorted(roto & roto_origen)
    if nuevos:
        avisos.append(("GRAVE", "glosario",
                       f"{len(nuevos)} enlaces al glosario no llevan a ningún término y abren la "
                       f"ventana vacía: {', '.join(nuevos)}. Revisa que el término exista con ese "
                       "nombre en el glosario."))
    if heredados:
        avisos.append(("AVISO", "glosario",
                       f"{len(heredados)} enlaces al glosario ya no llevaban a ningún término en el "
                       f"original: {', '.join(heredados)}."))
    return avisos


def _validar(destino: Path, origen: Path) -> list[tuple[str, str, str]]:
    modulo = _cargar_script("validar_actividades")

    heredados = {(g, k) for g, k, _ in modulo.Auditoria(abrir_unidad(origen)).problemas()}
    return [f for f in modulo.Auditoria(abrir_unidad(destino)).problemas()
            if (f[0], f[1]) not in heredados]


def _avisos_fuente(fuente, idioma: str | None, idioma_cambiado: list[str]) -> list[tuple[str, str, str]]:
    """Lo propio de un fuente de Author, para el informe."""
    avisos = []
    if fuente.sin_resolver:
        avisos.append(("GRAVE", "actividades",
                       f"Faltan {len(fuente.sin_resolver)} proyectos de actividades (ids "
                       f"{', '.join(fuente.sin_resolver)}): sus textos siguen como estaban. "
                       "Hay que subirlos junto a la unidad."))
    if idioma_cambiado:
        avisos.append(("AVISO", "idioma",
                       f"El fuente pasa a declarar «{bd.IDIOMAS.get(idioma, idioma)}» en "
                       f"{len(idioma_cambiado)} de {len(fuente.proyectos)} proyectos: carpeta "
                       f"`lang/{idioma}` de la revisión vigente, campo `language` y `courseInfo.xml`. "
                       "Comprueba al importar que Author lo toma así."))
    avisos.append(("AVISO", "fuente",
                   "Fuente de Author: no hay vista previa ni versión imprimible; se ven al "
                   "importarlo en Author, que genera los PDF de impresión (`contents*.pdf`) al "
                   f"publicar. El cambio va en la revisión vigente (la {fuente.unidad.revision} "
                   "de la unidad), que es la que Author toma al importar."))
    return avisos


def _nombrar_fuente(origen: Path, destino: Path, idioma_origen: str,
                    idioma: str | None) -> list[tuple[str, str, str]]:
    """Da a la unidad el nombre en la lengua de destino. Devuelve el aviso para el informe.

    El nombre no se traduce aparte: se toma del título de la unidad ya adaptado, como
    `finalizar.py` deriva los títulos de cmi5. En Prehistoria, `name` es exactamente el
    `title2` de la portada. Si no coincide con ningún título, se busca en la memoria; y si
    tampoco está, se deja y se avisa.
    """
    if not idioma or idioma == idioma_origen:
        return []
    antes = abrir_unidad(origen)
    nombre = antes.unidad.nombre.strip()
    sitios = [(cid, prop) for cid, comp in antes.componentes.items()
              for prop, valor in comp["properties"].items()
              if valor and texto_plano(valor).strip() == nombre]
    # La portada primero: un título de lección puede repetir el nombre de la unidad.
    sitios.sort(key=lambda s: antes.componentes[s[0]]["name"] != "interface")
    despues = abrir_unidad(destino)
    nuevo = None
    if sitios:
        cid, prop = sitios[0]
        nuevo = texto_plano(despues.componentes[cid]["properties"].get(prop) or "").strip()
        if nuevo == nombre:
            nuevo = None
    if not nuevo:
        nuevo = memoria.buscar_frases(idioma_origen, idioma, {nombre}).get(nombre)
    if not nuevo:
        return [("AVISO", "nombre",
                 f"El nombre del proyecto, «{nombre}», sigue en "
                 f"«{bd.IDIOMAS.get(idioma_origen, idioma_origen)}»: no coincide con ningún "
                 "título de la unidad ni está en la memoria. Es el nombre con el que se importa: "
                 "cámbialo en learningCloud después de enviarla.")]
    if despues.cambiar_nombre(nuevo):
        return [("AVISO", "nombre",
                 f"El proyecto pasa a llamarse «{nuevo}» (era «{nombre}»), tomado del título "
                 "de la unidad adaptada. Es el nombre con el que se importa en learningCloud.")]
    return []


def _pdfs_maquetados(unidad: Path) -> list[Path]:
    """Los PDF de la unidad que hay que reescribir: todos menos la versión imprimible,
    que se regenera entera desde la unidad.

    En un fuente de Author, solo los que la unidad usa: `resources/` arrastra los PDF que
    Author imprimió al publicar (`contents*.pdf`) y otros que ya no enlaza nadie, y
    traducirlos costaría sin servir para nada.
    """
    if es_fuente(unidad):
        texto = abrir_unidad(unidad).texto_xml()
        return [p for p in sorted((unidad / "resources").glob("*.pdf")) if p.name in texto]
    from . import impresion
    imprimible = impresion.localizar(unidad)
    return [p for p in sorted((unidad / "resources").glob("*.pdf"))
            if not (imprimible and p.name == imprimible.name)]


def _ruta_lecturas(unidad: Path, pdf: Path) -> Path:
    return TRABAJO / "lecturas" / unidad.name / f"{pdf.name}.json"


def lecturas_pdf(unidad: Path, pdf: Path) -> list[dict]:
    """Lo que el asistente ha leído de la imagen de un PDF: los rótulos pintados sin capa.

    Se guarda aparte del paquete, que no se toca, y por paquete: sirve para todos los
    trabajos que salgan de él.
    """
    import json
    ruta = _ruta_lecturas(unidad, pdf)
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else []


def lecturas_imagenes(unidad: Path, pdf: Path) -> list[dict]:
    """Lo que el asistente ha leído dentro de las imágenes de un PDF: `xref`, página,
    zona y líneas. Una imagen sin texto se guarda con las líneas vacías, para no volver a
    leerla."""
    import json
    ruta = TRABAJO / "lecturas" / unidad.name / f"{pdf.name}.imagenes.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else []


def guardar_lecturas_imagenes(unidad: Path, pdf: Path, lecturas: list[dict]) -> None:
    import json
    ruta = TRABAJO / "lecturas" / unidad.name / f"{pdf.name}.imagenes.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(lecturas, ensure_ascii=False, indent=1), encoding="utf-8")


def carpeta_imagenes_pdf(destino: Path, pdf_nombre: str) -> Path:
    """Dónde quedan las imágenes de un PDF del trabajo: las originales extraídas, en
    `original/`, y las rehechas. Fuera del paquete, que no las necesita."""
    return destino.parent / f"{destino.name}-imagenes-pdf" / Path(pdf_nombre).stem


def imagenes_pdf(unidad: Path) -> list[tuple[Path, dict]]:
    """Las imágenes de los PDF de la unidad ya clasificadas, con su PDF."""
    return [(pdf, l) for pdf in _pdfs_maquetados(unidad)
            for l in lecturas_imagenes(unidad, pdf) if "tipo" in l]


def _con_texto(lineas: list[str]) -> bool:
    """Si lo leído en una imagen es lenguaje: los números que numeran las fotos, no."""
    return any(len(re.findall(r"[^\W\d_]{3,}", l)) >= 1 and _es_traducible(l) for l in lineas)


def guardar_lecturas(unidad: Path, pdf: Path, lecturas: list[dict]) -> None:
    import json
    ruta = _ruta_lecturas(unidad, pdf)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(lecturas, ensure_ascii=False, indent=1), encoding="utf-8")


def _textos_pdf(unidad: Path) -> list[tuple[str, str]]:
    """(fichero, texto) de cada texto traducible de los PDF maquetados de la unidad."""
    from . import pdf_texto
    resultado = []
    for pdf in _pdfs_maquetados(unidad):
        try:
            textos = pdf_texto.textos(pdf, lecturas_pdf(unidad, pdf))
        except Exception:  # un PDF que no se puede leer no impide traducir el resto
            continue
        resultado += [(pdf.name, t) for t in textos if _es_traducible(t)]
    return resultado


# Lo que deja texto en el idioma de origen dentro del PDF es grave; lo demás, un aviso.
_PDF_GRAVE = {"sin capa": "está pintado en la imagen sin capa de texto",
              "sin fuente": "no hay una fuente libre equivalente",
              "sin perfil": "está pintado con una herramienta sin perfil de fuente",
              "no medido": "no se ha encontrado el texto en la imagen"}


def clave_pdf(fichero: str, texto: str) -> str:
    """La clave de un término de PDF: el fichero y la huella del texto original.

    No depende de la posición: el mismo texto, en cualquier página, es la misma decisión.
    """
    return f"pdf:{fichero}#{memoria.huella(texto)[:12]}"


def sin_negritas(texto: str) -> str:
    return re.sub(r"</?b>", "", texto or "")


def decisiones_pdf(trabajo_id: int) -> dict[str, tuple[str, str | None]]:
    """Lo que el editor ha decidido sobre textos de PDF: clave → (valor, base)."""
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        return {t.clave: (t.valor_final, t.valor_base) for t in trabajo.pendientes
                if t.clave.startswith("pdf:") and t.valor_final}


def textos_pdf(origen: Path, pdf: Path, idioma_origen: str, idioma: str,
               decisiones: dict | None = None) -> tuple[list[dict], dict[str, str], list[str]]:
    """Los textos de un PDF de la unidad, cada uno con su traducción y su página.

    Devuelve los textos —`original`, `traducido` (con sus marcas de negrita), `pagina` y
    `clave`—, el diccionario original → texto que hay que escribir, que ya lleva aplicadas
    las decisiones del editor, y los que siguen sin traducción.
    """
    from . import pdf_texto
    lecturas = lecturas_pdf(origen, pdf)
    todos = pdf_texto.nodos(pdf, lecturas)
    paginas_de: dict[str, set[int]] = {}
    for n in todos:
        paginas_de.setdefault(n.texto, set()).add(n.pagina)
    vistos, textos = set(), []
    for n in todos:
        if n.texto and n.texto not in vistos and _es_traducible(sin_negritas(n.texto)):
            vistos.add(n.texto)
            # Un título de apartado: entero en negrita y corto. Su nivel es la sangría, el
            # cuerpo y el color. La cabecera que se repite en cada página no es un título.
            titulo = None
            if ("bold" in n.fuente.lower() and not n.rico and len(n.texto) < 90
                    and len(paginas_de[n.texto]) < 3):
                titulo = (round(n.rect.x0), n.tamano, tuple(round(c, 2) for c in n.color))
            textos.append({"original": n.texto, "pagina": n.pagina + 1, "titulo": titulo,
                           "clave": clave_pdf(pdf.name, n.texto)})
    originales = [t["original"] for t in textos]
    cambia = idioma and idioma != idioma_origen
    conocidas = memoria.buscar_frases(idioma_origen, idioma, originales) if cambia else {}
    invariantes = set(memoria.frases_invariantes(idioma_origen, idioma, originales)) if cambia else set()
    faltan = [t for t in originales if t not in conocidas and t not in invariantes] if cambia else []
    escribir = dict(conocidas)
    for t in textos:
        t["traducido"] = conocidas.get(t["original"]) or (
            t["original"] if not cambia or t["original"] in invariantes else None)
        decidido = (decisiones or {}).get(t["clave"])
        if decidido:
            valor, base = decidido
            base = t["traducido"] or base or t["original"]
            escribir[t["original"]] = aplicar_decision(base, valor)
            t["decidido"] = True
            if t["original"] in faltan:
                faltan.remove(t["original"])
    return textos, escribir, faltan


def _reescribir_pdfs(origen: Path, destino: Path, idioma_origen: str, idioma: str,
                     comunidad: str | None = None, decisiones: dict | None = None,
                     regenerar_imagenes: bool = True) -> tuple[list, list[dict], list[dict], list[dict]]:
    """Reescribe el texto de los PDF maquetados de la unidad con lo que sabe la memoria.

    Se parte siempre del PDF original, así que reprocesar da lo mismo que procesar una vez.
    En una adaptación, además, lo que dependa del territorio sale como término pendiente,
    igual que en la unidad; lo que el editor decida se escribe en el PDF en lugar de la
    traducción de memoria.
    """
    from collections import Counter
    from . import imagenes, pdf_texto
    nombre_origen = bd.IDIOMAS.get(idioma_origen, idioma_origen)
    avisos, hechos, terminos, imagenes_hechas = [], [], [], []
    relecturas = imagenes.comprobaciones(origen)
    for pdf in _pdfs_maquetados(origen):
        clave = f"pdf:{pdf.name}"
        try:
            lecturas = lecturas_pdf(origen, pdf)
            fichas, conocidas, faltan = textos_pdf(origen, pdf, idioma_origen, idioma, decisiones)
            textos = [f["original"] for f in fichas]
        except Exception as e:
            avisos.append(("GRAVE", clave, f"No se ha podido leer el PDF ({e}): va el original, "
                                           f"en {nombre_origen}."))
            continue
        # Las imágenes con texto de dentro del PDF, con el mismo motor que las de la unidad:
        # se extraen enteras, se rehacen las de rótulos sobre fondo liso y se ponen en su
        # sitio al reescribir el PDF.
        carpeta = carpeta_imagenes_pdf(destino, pdf.name)
        filas_img, sustituir = [], {}
        leidas = lecturas_imagenes(origen, pdf) if regenerar_imagenes else []
        for lectura in leidas:
            if "tipo" not in lectura or imagenes.destino(lectura) == "nada":
                continue
            try:
                original = pdf_texto.extraer_imagen(pdf, lectura["xref"], carpeta / "original")
            except Exception:  # sin extraer no se rehace, pero se avisa abajo
                continue
            salida = carpeta / original.name
            shutil.copyfile(original, salida)
            fila = _tratar_imagen(lectura, original, salida,
                                  f"{pdf.name} · página {lectura['pagina']}", idioma_origen,
                                  idioma, comunidad, relecturas)
            if fila is None:
                continue
            fila.update({"pdf": pdf.name, "xref": lectura["xref"]})
            if fila["estado"] == "reescrita" and fila["destino"] != "diseño":
                sustituir[lectura["xref"]] = salida
            filas_img.append(fila)
        imagenes_hechas += filas_img
        if not textos and not sustituir:
            continue
        if comunidad:
            terminos += _terminos_pdf(pdf.name, fichas, comunidad)
        filas: list[dict] = []
        if conocidas or sustituir:
            try:
                filas = pdf_texto.reescribir(pdf, destino / "resources" / pdf.name, conocidas,
                                             lecturas, sustituir)
            except Exception as e:
                avisos.append(("GRAVE", clave, f"No se ha podido reescribir el PDF ({e}): va el "
                                               f"original, en {nombre_origen}."))
                continue
        # Solo cuenta lo que es lenguaje: cifras y rótulos sueltos no se traducen.
        traducibles = set(textos)
        filas = [f for f in filas if not f["original"] or f["original"] in traducibles]
        cuenta = Counter(f["estado"] for f in filas)
        if faltan:
            avisos.append(("GRAVE", clave,
                           f"{len(faltan)} de {len(textos)} textos siguen en {nombre_origen}: no hay "
                           f"traducción en memoria. Por ejemplo: «{faltan[0][:100]}»."))
        for estado, motivo in _PDF_GRAVE.items():
            casos = [f for f in filas if f["estado"] == estado]
            if casos:
                ejemplo = casos[0]["original"] or f"zona {casos[0].get('rect')}"
                # Textos distintos, no apariciones: la cabecera de un resumen se repite en
                # cada página.
                distintos = len({f["original"] or str(f.get("rect")) for f in casos})
                avisos.append(("GRAVE", clave,
                               f"{distintos} textos siguen en {nombre_origen} porque {motivo}. "
                               f"Por ejemplo: «{ejemplo[:100]}», página {casos[0]['pagina']}."))
        avisos += _avisos_imagenes(filas_img, clave, "imágenes con texto de este PDF",
                                   nombre_origen, comunidad, idioma != idioma_origen)
        # Las leídas antes de clasificarlas no se pueden rehacer, pero no pasan sin avisar:
        # el PDF saldría traducido con recuadros enteros en el idioma de origen.
        sin_clasificar = [l for l in leidas if "tipo" not in l and _con_texto(l["lineas"])]
        if sin_clasificar:
            ejemplos = "; ".join(f"página {l['pagina']}: «{' '.join(l['lineas'])[:70]}»"
                                 for l in sin_clasificar[:3])
            avisos.append(("GRAVE", clave,
                           f"{len(sin_clasificar)} imágenes llevan texto dentro, que sigue en "
                           f"{nombre_origen}, y están sin clasificar: hay que volver a leerlas "
                           f"(«Traducir»). {ejemplos}."))
        no_caben = [f for f in filas if f["estado"] == "no cabe"]
        if no_caben:
            avisos.append(("AVISO", clave,
                           f"{len(no_caben)} textos no caben en su sitio ni reduciendo la letra un "
                           f"15 %: se han escrito más pequeños y hay que mirarlos. Por ejemplo: "
                           f"«{no_caben[0]['nuevo'][:100]}», página {no_caben[0]['pagina']}."))
        hechos.append({
            "fichero": pdf.name, "textos": len(textos),
            "reescritos": sum(1 for f in filas if f.get("nuevo")),
            "a_su_tamano": cuenta["igual"] + cuenta["subrayado alargado"],
            "reducidos": cuenta["reducido"] + cuenta["una línea más"] + cuenta["no cabe"],
            "sin_traducir": len(set(faltan) | {f["original"] or str(f.get("rect")) for f in filas
                                               if f["estado"] in _PDF_GRAVE}),
            "leidos": len(lecturas),
            "imagenes_con_texto": len(filas_img) + len(sin_clasificar),
        })
    return avisos, hechos, terminos, imagenes_hechas


def _terminos_pdf(fichero: str, fichas: list[dict], comunidad: str) -> list[dict]:
    """Los textos de un PDF atados al territorio de origen, como términos pendientes.

    La misma detección que en la unidad, sobre el texto ya traducido: topónimos, marcas
    `[PENDIENTE]` y equivalencias conocidas para la comunidad. En el resumen de Biología,
    por ejemplo, «Na zona central, dende a costa central galega…».
    """
    planos = {f["clave"]: sin_negritas(f["traducido"] or f["original"]) for f in fichas}
    equivalentes = memoria.buscar_equivalencias(set(planos.values()), comunidad)
    terminos = []
    # Como en la unidad con la lección, un apartado cuyo título es territorial se revisa
    # entero: «Na zona este destacan as rochas metamórficas…», en el apartado «Rochas e
    # minerais en Galicia», no nombra ningún sitio y habla de Galicia.
    seccion, rotulo_seccion = None, ""
    for f in fichas:
        plano = planos[f["clave"]]
        titulo = f.get("titulo")
        if titulo and seccion and (titulo == seccion or titulo[0] < seccion[0] - 2):
            seccion = None
        if titulo and seccion is None and _es_territorial(plano):
            seccion, rotulo_seccion = titulo, plano
        if f.get("decidido"):
            continue
        marca, equivalente = _MARCA.search(plano), equivalentes.get(plano)
        territorial = _es_territorial(plano)
        if not (marca or equivalente or territorial or seccion):
            continue
        if seccion and not (marca or equivalente or territorial):
            terminos.append({
                "clave": f["clave"], "leccion": f"PDF «{fichero}», página {f['pagina']}",
                "motivo": (f"Está en el apartado «{rotulo_seccion}», que trata del territorio de "
                           f"origen. Reescríbelo para {comunidad} o confirma que vale tal cual."),
                "original": plano, "propuesta": plano,
                "valor_base": f["traducido"] or f["original"], "estado": bd.PENDIENTE,
            })
            continue
        if marca:
            motivo, propuesta = "Marcado como pendiente: exige una decisión que el sistema no toma", plano
        elif equivalente:
            motivo = (f"El sistema conoce un equivalente para {comunidad}"
                      + (f": {equivalente['motivo'][:120]}" if equivalente.get("motivo") else "")
                      + ". Confírmalo antes de aplicarlo.")
            propuesta = equivalente["propuesta"]
        else:
            motivo = (f"Contenido atado al territorio de origen. Reescríbelo para {comunidad} "
                      "o confirma que vale tal cual.")
            propuesta = plano
        terminos.append({
            "clave": f["clave"], "leccion": f"PDF «{fichero}», página {f['pagina']}",
            "motivo": motivo, "original": plano, "propuesta": propuesta,
            "valor_base": f["traducido"] or f["original"], "estado": bd.PENDIENTE,
        })
    return terminos


def traducciones_imagen(lectura: dict, idioma_origen: str, idioma: str) -> dict[str, str]:
    """Rótulo original → texto que hay que escribir, con lo que sabe la memoria."""
    from . import imagenes
    cambia = idioma != idioma_origen
    rotulos = [imagenes.texto_rotulo(r) for r in imagenes.rotulos_validos(lectura)]
    conocidas = memoria.buscar_frases(idioma_origen, idioma, rotulos) if cambia else {}
    invariantes = set(memoria.frases_invariantes(idioma_origen, idioma, rotulos)) if cambia else set()
    return {t: conocidas.get(t) or t for t in rotulos
            if not cambia or t in conocidas or t in invariantes or not _es_traducible(t)}


def _reescribir_imagenes(origen: Path, destino: Path, idioma_origen: str, idioma: str,
                         comunidad: str | None) -> tuple[list, list[dict]]:
    """Rehace las imágenes de rótulos sobre fondo liso y apunta las que van a diseño.

    Se parte siempre de la imagen original del paquete. Una imagen que no se puede rehacer
    entera se queda como estaba y va a diseño, con sus rótulos y la traducción propuesta.
    """
    from . import imagenes
    nombre_origen = bd.IDIOMAS.get(idioma_origen, idioma_origen)
    hechas, avisos = [], []
    relecturas = imagenes.comprobaciones(origen)
    lecturas = imagenes.lecturas(origen)
    # Las copias de una misma imagen (galería, lección, impresión) salen de la principal,
    # que es la más grande: así quedan todas iguales, y no una traducida y otra no.
    copia_de = {h: lider for lider, hs in imagenes.grupos(origen).items() for h in hs if h != lider}
    por_huella: dict[str, dict] = {}
    for lectura in sorted(lecturas.values(), key=lambda l: l["huella"] in copia_de):
        lider = copia_de.get(lectura["huella"])
        previa = por_huella.get(lider) if lider else None
        if previa and previa.get("estado") == "reescrita" and previa.get("destino") != "diseño":
            fila = _copiar_rehecha(previa, origen / lectura["fichero"], destino / lectura["fichero"],
                                   lectura["fichero"])
        else:
            fila = _tratar_imagen(lectura, origen / lectura["fichero"], destino / lectura["fichero"],
                                  lectura["fichero"], idioma_origen, idioma, comunidad, relecturas)
        if fila is not None:
            fila["grupo"] = lider or lectura["huella"]
            por_huella[lectura["huella"]] = fila
            hechas.append(fila)
    avisos += _avisos_imagenes(hechas, "imagenes", "imágenes con texto", nombre_origen, comunidad,
                               idioma != idioma_origen)
    return avisos, hechas


def _tratar_imagen(lectura: dict, original: Path, salida: Path, nombre: str, idioma_origen: str,
                   idioma: str, comunidad: str | None, relecturas: dict) -> dict | None:
    """Rehace una imagen con texto o la manda a diseño. `None` si no hay nada que hacer.

    `salida` es donde va la imagen del trabajo, que al empezar es una copia de la original.
    Si la imagen no se puede rehacer entera, o la relectura no la da por buena, `salida`
    queda como la original: una imagen a medio traducir es peor que la original.
    """
    import shutil

    from . import imagenes
    destino_img = imagenes.destino(lectura)
    if destino_img == "nada":
        return None
    cambia = idioma != idioma_origen
    rotulos = [imagenes.texto_rotulo(r) for r in imagenes.rotulos_validos(lectura)]
    traducciones = traducciones_imagen(lectura, idioma_origen, idioma)
    fila = {"fichero": nombre, "tipo": lectura.get("tipo"),
            "territorial": bool(lectura.get("territorial")), "motivo_tipo": lectura.get("motivo", ""),
            "rotulos": [{"original": t, "propuesta": traducciones.get(t)} for t in rotulos]}
    # En una adaptación, un rótulo atado al territorio no se traduce sin más: a diseño.
    if comunidad and any(_es_territorial(traducciones.get(t) or t) for t in rotulos):
        destino_img, fila["territorial"] = "diseño", True
    # Sin cambio de idioma solo importa lo territorial.
    if not cambia and not fila["territorial"]:
        return None
    if destino_img == "automático" and cambia:
        r = imagenes.reescribir(original, salida, lectura, traducciones, idioma_origen)
        fila.update({"estado": r["estado"], "detalle": r.get("detalle", ""),
                     "hechos": r.get("rotulos", [])})
        if r["estado"] == "reescrita":
            fila["antes"] = _miniatura(original)
            fila["despues"] = _miniatura(salida)
            # La relectura del asistente, si ya se hizo sobre esta misma imagen.
            fila["huella"] = imagenes.huella_fichero(salida)
            relectura = relecturas.get(fila["huella"])
            if relectura:
                fila["comprobacion"] = imagenes.veredicto(relectura)
                if fila["comprobacion"]["estado"] == "mal":
                    destino_img = "diseño"
                    c = fila["comprobacion"]
                    fila["detalle"] = "La relectura no la da por buena: " + "; ".join(
                        [f"se lee «{f['leido']}» donde debería decir «{f['esperado']}»" for f in c["fallos"][:2]]
                        + [f"queda «{x}» en el idioma de origen" for x in c["restos"][:2]])
        if r["estado"] not in ("reescrita", "sin cambios"):
            destino_img = "diseño"
    else:
        fila.update({"estado": "a diseño", "detalle": (
            "Contenido territorial: hay que rehacer la imagen para la comunidad de destino."
            if fila["territorial"] else f"Texto {lectura.get('tipo', '').replace('_', ' ')}: "
                                        "no se rehace automáticamente.")})
    if destino_img == "diseño" and salida.exists() and salida.read_bytes() != original.read_bytes():
        shutil.copyfile(original, salida)
    fila["destino"] = destino_img
    fila["salida"] = str(salida)
    fila["miniatura"] = _miniatura(original, 200)
    return fila


def _copiar_rehecha(principal: dict, original: Path, salida: Path, nombre: str) -> dict:
    """Una copia de una imagen ya rehecha: la principal, al tamaño y formato de la copia."""
    from PIL import Image
    with Image.open(original) as o:
        tamano, formato = o.size, o.format
        alfa = o.getchannel("A").copy() if "A" in o.getbands() else None
    hecha = Path(principal["salida"])
    with Image.open(hecha) as h:
        nueva = h.convert("RGB").resize(tamano, Image.LANCZOS)
    if alfa is not None:
        nueva.putalpha(alfa)
    if (formato or "").upper() == "JPEG":
        nueva.convert("RGB").save(salida, "JPEG", quality=93, subsampling=0)
    else:
        nueva.save(salida, formato or "PNG")
    fila = {k: v for k, v in principal.items() if k not in ("antes", "despues", "miniatura", "huella")}
    fila.update({"fichero": nombre, "detalle": f"Copia de {Path(principal['fichero']).name}, rehecha con ella.",
                 "copia_de": principal["fichero"], "salida": str(salida),
                 "antes": _miniatura(original), "despues": _miniatura(salida),
                 "miniatura": _miniatura(original, 200)})
    return fila


def _avisos_imagenes(hechas: list[dict], clave: str, que: str, nombre_origen: str,
                     comunidad: str | None, cambia: bool) -> list:
    """GRAVE por las que van a diseño y AVISO por las rehechas, con lo que dijo la relectura."""
    avisos = []
    a_diseno = [h for h in hechas if h["destino"] == "diseño"]
    rehechas = [h for h in hechas if h["estado"] == "reescrita" and h["destino"] != "diseño"]
    if a_diseno and (cambia or any(h["territorial"] for h in a_diseno)):
        ejemplos = "; ".join(f"«{Path(h['fichero']).name}» ({h['detalle'][:60]})" for h in a_diseno[:3])
        avisos.append(("GRAVE", clave,
                       f"{len(a_diseno)} {que} van a diseño y siguen en {nombre_origen}"
                       f"{' o atadas al territorio de origen' if comunidad else ''}: {ejemplos}."))
    if rehechas:
        estados = [h.get("comprobacion", {}).get("estado") for h in rehechas]
        bien, revisar, sin = estados.count("bien"), estados.count("revisar"), estados.count(None)
        texto = f"{len(rehechas)} {que} rehechas automáticamente con sus rótulos traducidos"
        if bien or revisar:
            texto += (f": {bien} releídas por el asistente sin problemas"
                      + (f" y {revisar} con defectos visuales que hay que mirar" if revisar else ""))
        if sin:
            texto += (f"; {sin} sin releer todavía (operación «Comprobar imágenes»)")
        avisos.append(("AVISO", clave, texto + ". Revísalas en el informe."))
    return avisos


# ------------------------------------------------------- imágenes sustituidas

SUSTITUCIONES = TRABAJO / "sustituciones"
_FORMATOS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".gif": "GIF", ".webp": "WEBP"}


def huellas_imagenes(unidad: Path) -> dict[str, list[str]]:
    """Cada imagen de `resources/` por su huella: la misma imagen puede ir con dos nombres."""
    import hashlib
    ret: dict[str, list[str]] = {}
    carpeta = unidad / "resources"
    if not carpeta.is_dir():
        return ret
    for f in sorted(carpeta.rglob("*")):
        if f.is_file() and f.suffix.lower() in _FORMATOS:
            huella = hashlib.sha1(f.read_bytes()).hexdigest()[:16]
            ret.setdefault(huella, []).append(f.relative_to(unidad).as_posix())
    return ret


def preparar_imagen(nueva: Path, original: Path, ajuste: str = "recortar") -> bytes:
    """La imagen nueva con la proporción y el formato de la original.

    El hueco de la unidad está pensado para la proporción de la original: con otra, el
    reproductor la deforma o la corta por donde no toca. Se guarda con hasta el doble de
    resolución que la original, si la nueva la tiene, para que no pierda calidad.
    """
    from io import BytesIO

    from PIL import Image, ImageOps

    with Image.open(original) as o:
        ancho, alto = o.size
    formato = _FORMATOS.get(original.suffix.lower(), "PNG")
    with Image.open(nueva) as im:
        im = ImageOps.exif_transpose(im)
        escala = max(1.0, min(2.0, im.width / ancho, im.height / alto))
        tamano = (round(ancho * escala), round(alto * escala))
        con_alfa = im.mode in ("RGBA", "LA", "P") and formato in ("PNG", "WEBP", "GIF")
        im = im.convert("RGBA" if con_alfa else "RGB")
        if ajuste == "encajar":
            # Entera, con márgenes del color de su borde.
            esquina = im.getpixel((0, 0))
            lienzo = Image.new(im.mode, tamano, esquina)
            reducida = ImageOps.contain(im, tamano, Image.LANCZOS)
            lienzo.paste(reducida, ((tamano[0] - reducida.width) // 2,
                                    (tamano[1] - reducida.height) // 2))
            im = lienzo
        else:
            im = ImageOps.fit(im, tamano, Image.LANCZOS)
        salida = BytesIO()
        if formato == "JPEG":
            im.save(salida, "JPEG", quality=90, optimize=True)
        elif formato == "GIF":
            im.convert("P", palette=Image.ADAPTIVE).save(salida, "GIF")
        else:
            im.save(salida, formato)
        return salida.getvalue()


def aplicar_sustituciones(trabajo_id: int, origen: Path, destino: Path) -> set[str]:
    """Escribe en el resultado las imágenes sustituidas. Devuelve los ficheros tocados.

    Se hace en cada proceso, después de rehacer las imágenes con texto: lo que ha decidido
    una persona va por encima de lo automático, y reprocesar no lo pierde.
    """
    with bd.Sesion() as sesion:
        filas = [(s.huella, bd.ruta(s.ruta), s.ajuste)
                 for s in sesion.query(bd.Sustitucion).filter(bd.Sustitucion.trabajo_id == trabajo_id)]
    if not filas:
        return set()
    huellas = huellas_imagenes(origen)
    tocados = set()
    for huella, nueva, ajuste in filas:
        if not nueva or not nueva.is_file():
            continue
        for relativa in huellas.get(huella, []):
            (destino / relativa).write_bytes(preparar_imagen(nueva, origen / relativa, ajuste))
            tocados.add(relativa)
    return tocados


def resumen_imagenes(hechas: list[dict]) -> dict:
    """Lo que la pantalla del trabajo cuenta de las imágenes con texto: cuántas se han
    rehecho y cuáles van a diseño. El detalle, con las fichas, está en el informe."""
    rehechas = [h for h in hechas if h["estado"] == "reescrita" and h["destino"] != "diseño"]
    # Las copias de una misma imagen cuentan una vez: la pantalla habla de imágenes, no de
    # ficheros. Cada entrada de «diseño» lleva sus copias.
    diseno: dict[str, dict] = {}
    for h in hechas:
        if h["destino"] != "diseño":
            continue
        clave = h.get("grupo") or h["fichero"]
        if clave in diseno:
            diseno[clave]["copias"].append(h["fichero"])
            continue
        diseno[clave] = {"fichero": h["fichero"], "detalle": h.get("detalle", ""),
                         "territorial": bool(h.get("territorial")), "pdf": h.get("pdf"), "copias": []}
    return {"total": len({h.get("grupo") or h["fichero"] for h in hechas}),
            "rehechas": len({h.get("grupo") or h["fichero"] for h in rehechas}),
            "sustituidas": sum(1 for h in hechas if h["destino"] == "sustituida"),
            "por_mirar": len({h.get("grupo") or h["fichero"] for h in rehechas
                              if (h.get("comprobacion") or {}).get("estado") != "bien"}),
            "diseno": list(diseno.values())}


def _miniatura(ruta: Path, lado: int = 260) -> str:
    """Una imagen pequeña, embebida, para el informe."""
    import base64
    from io import BytesIO

    from PIL import Image
    try:
        with Image.open(ruta) as im:
            im = im.convert("RGB")
            im.thumbnail((lado, lado))
            salida = BytesIO()
            im.save(salida, "JPEG", quality=82)
    except Exception:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(salida.getvalue()).decode("ascii")


def _sello(h: dict, esc) -> str:
    """El resultado de la relectura, junto a cada imagen rehecha."""
    c = h.get("comprobacion")
    if not c:
        return "<span class='apagado'>· sin releer</span>"
    if c["estado"] == "bien":
        return "<b style='color:#2E7D32'>· releída: bien</b>"
    detalles = [f"se lee «{f['leido']}» por «{f['esperado']}»" for f in c["fallos"]] + [
        f"resto: «{x}»" for x in c["restos"]] + c["defectos"]
    color = "#B00020" if c["estado"] == "mal" else "#8A6D00"
    return (f"<b style='color:{color}'>· releída: {esc(c['estado'])}</b>"
            f"<br><i>{esc('; '.join(detalles)[:400])}</i>")


def _seccion_imagenes(destino: Path, hechas: list[dict], esc) -> str:
    """El apartado del informe: las rehechas, con el antes y el después, y las fichas de las
    que van a diseño."""
    if not hechas:
        return ""
    rehechas = [h for h in hechas if h["estado"] == "reescrita" and h["destino"] != "diseño"]
    diseno = [h for h in hechas if h["destino"] == "diseño"]
    partes = [f"<h2 id='imagenes'>Imágenes con texto ({len(hechas)})</h2><p>{len(rehechas)} rehechas "
              f"automáticamente con sus rótulos traducidos y {len(diseno)} para diseño. Las "
              "rehechas están <b>por revisar</b>: compara el antes y el después.</p>"]
    for h in rehechas:
        partes.append(
            f"<div style='display:flex;gap:10px;align-items:flex-start;margin:10px 0'>"
            f"<img src='{h.get('antes', '')}' style='border:1px solid #C8D0DC'>"
            f"<img src='{h.get('despues') or _miniatura(destino / h['fichero'])}' style='border:1px solid #C8D0DC'>"
            f"<div><code>{esc(Path(h['fichero']).name)}</code> {_sello(h, esc)}<br>"
            + "<br>".join(f"{esc(x['original'])} → <b>{esc(x['nuevo'])}</b>"
                          + (f" <i>({esc(x['estado'])}, {x.get('tamano_original')} → {x.get('tamano')} px)</i>"
                             if x.get("estado") != "reescrito" else "")
                          for x in h.get("hechos", []))
            + "</div></div>")
    if diseno:
        partes.append("<h3>Para diseño</h3><table><thead><tr><th>Imagen</th><th>Motivo</th>"
                      "<th>Rótulos y traducción propuesta</th></tr></thead><tbody>")
        for h in diseno:
            rotulos = "<br>".join(f"{esc(r['original'])} → {esc(r['propuesta'] or '—')}"
                                  for r in h["rotulos"])
            partes.append(f"<tr><td><img src='{h.get('miniatura') or _miniatura(destino / h['fichero'], 200)}'><br>"
                          f"<code>{esc(Path(h['fichero']).name)}</code></td>"
                          f"<td class='motivo'>{esc(h['detalle'])}</td><td>{rotulos}</td></tr>")
        partes.append("</tbody></table>")
    return "".join(partes)


def _informe(destino: Path, trabajo_id: int, tipo: str, idioma: str | None,
             comunidad: str | None, aplicados: list, terminos: list, avisos: list,
             impresion_hecha: dict | None = None, pdfs_hechos: list[dict] | None = None,
             imagenes_hechas: list[dict] | None = None) -> str:
    """Informe de cambios, en HTML para poder enlazarlo desde la interfaz."""
    ruta = TRABAJO / "informes" / f"trabajo-{trabajo_id}.html"
    ruta.parent.mkdir(parents=True, exist_ok=True)

    def esc(t: str) -> str:
        return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    filas = "".join(
        f"<tr><td class='clave'>{esc(c)}</td>"
        f"<td class='antes'>{esc(texto_plano(a)[:400]) or '<i>(vacío)</i>'}</td>"
        f"<td class='despues'>{esc(texto_plano(d)[:400]) or '<i>(vacío)</i>'}</td>"
        f"<td class='motivo'>{esc(m)}</td></tr>"
        for c, a, d, m in aplicados)

    pendientes = "".join(
        f"<tr><td class='clave'>{esc(t['clave'])}</td><td>{esc(t['leccion'] or '')}</td>"
        f"<td>{esc(t['propuesta'][:300])}</td><td class='motivo'>{esc(t['motivo'])}</td></tr>"
        for t in terminos)

    lista_avisos = "".join(
        f"<li><b>{esc(g)}</b> <code>{esc(k)}</code> — {esc(d)}</li>" for g, k, d in avisos)

    seccion_pdf = ""
    if impresion_hecha:
        i = impresion_hecha
        seccion_pdf = (f"<h2>Versión imprimible</h2><p>Regenerada desde la unidad adaptada, con el "
                       f"modo de impresión de la plantilla: <code>{esc(i['fichero'])}</code>, "
                       f"{i['paginas']} páginas (la original tenía {i['paginas_original']}), "
                       f"{i['imagenes']} imágenes, en {i['segundos']} s. Se abre desde el botón "
                       f"«Versión imprimible» de la propia unidad.</p>")

    if pdfs_hechos:
        filas_pdf = "".join(
            f"<tr><td class='clave'>{esc(p['fichero'])}</td><td>{p['textos']}</td>"
            f"<td>{p['reescritos']}</td><td>{p['a_su_tamano']}</td><td>{p['reducidos']}</td>"
            f"<td>{p['sin_traducir']}</td><td>{p['leidos'] or ''}</td>"
            f"<td>{p.get('imagenes_con_texto') or ''}</td></tr>" for p in pdfs_hechos)
        seccion_pdf += (
            "<h2>Otros PDF de la unidad</h2><p>El texto se reescribe en su sitio, sin tocar el "
            "dibujo, con la fuente con la que se hizo el documento. Lo que no cabe a su tamaño "
            "se reduce como mucho un 15 %; lo que sigue sin traducir aparece en la comprobación "
            "de arriba. «Leídos» son rótulos pintados en la imagen sin capa de texto, que el "
            "asistente ha leído de la propia imagen: conviene cotejarlos.</p><table><thead><tr>"
            "<th>Fichero</th><th>Textos</th><th>Reescritos</th><th>A su tamaño</th>"
            "<th>Reducidos</th><th>Sin traducir</th><th>Leídos</th>"
            "<th>Imágenes con texto</th></tr></thead><tbody>"
            + filas_pdf + "</tbody></table>")

    idioma_txt = bd.IDIOMAS.get(idioma or "", idioma or "")
    destino_txt = (idioma_txt if tipo == bd.TRADUCCION
                   else f"{comunidad}" + (f" · {idioma_txt}" if idioma_txt else ""))

    ruta.write_text(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Informe de cambios · trabajo {trabajo_id}</title><style>
*{{box-sizing:border-box}} body{{font-family:Aptos,Calibri,'Segoe UI',sans-serif;color:#1A1A1A;
background:#D8DCE2;margin:0;padding:32px 16px}}
.hoja{{max-width:1100px;margin:0 auto;background:#fff;padding:32px 44px;
box-shadow:0 4px 24px rgba(27,43,75,.13)}}
h1{{font-size:21px;color:#1B2B4B;border-bottom:2.5px solid #2E5FA3;padding-bottom:7px}}
h2{{font-size:16px;color:#1B2B4B;margin-top:28px}}
.meta{{color:#5A6474;font-size:13px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin-bottom:8px}}
th{{background:#EEF1F5;color:#1B2B4B;font-size:10.5px;text-transform:uppercase;
letter-spacing:.6px;padding:8px;border:1px solid #C8D0DC;text-align:left}}
td{{padding:8px;border:1px solid #C8D0DC;vertical-align:top;line-height:1.45}}
.clave{{font-family:Consolas,monospace;font-size:11.5px;white-space:nowrap;color:#1B3D7A}}
.antes{{color:#5A6474}} .despues{{color:#1A1A1A}}
.motivo{{color:#5A6474;font-size:11.5px;font-style:italic}}
ul{{font-size:13px;line-height:1.7}} code{{font-family:Consolas,monospace;font-size:11.5px}}
.vacio{{color:#5A6474;font-style:italic}}
</style></head><body><div class="hoja">
<h1>Informe de cambios</h1>
<div class="meta">Trabajo {trabajo_id} · {esc(tipo)} · destino: <b>{esc(destino_txt or '—')}</b>
 · {len(aplicados)} elementos modificados · {len(terminos)} términos pendientes
 · generado el {dt.datetime.now():%d/%m/%Y %H:%M}</div>

<h2>Comprobación de actividades</h2>
{'<ul>' + lista_avisos + '</ul>' if lista_avisos
 else '<p class="vacio">La adaptación no ha roto ninguna actividad.</p>'}

{seccion_pdf}

{_seccion_imagenes(destino, imagenes_hechas or [], esc)}

<h2>Términos pendientes de decisión editorial ({len(terminos)})</h2>
{'<table><thead><tr><th>Elemento</th><th>Lección</th><th>Contenido</th><th>Motivo</th></tr></thead><tbody>'
 + pendientes + '</tbody></table>' if pendientes
 else '<p class="vacio">No queda nada por decidir.</p>'}

<h2>Cambios aplicados ({len(aplicados)})</h2>
{'<table><thead><tr><th>Elemento</th><th>Antes</th><th>Después</th><th>Motivo</th></tr></thead><tbody>'
 + filas + '</tbody></table>' if filas
 else '<p class="vacio">El sistema no tenía ninguna decisión guardada aplicable a este paquete.</p>'}
</div></body></html>""", encoding="utf-8")
    return str(ruta)


def _reemplazar(destino: Path, paquete) -> None:
    """Sustituye el paquete de destino por la versión en memoria, sin dejarlo a medias.

    El orden importa: primero se escribe la versión nueva completa, y solo cuando está
    lista se aparta la anterior. Si se borra antes y el renombrado falla —en Windows basta
    con que alguien tenga la carpeta abierta— el editor se queda sin resultado y sin copia.
    """
    nuevo = destino.with_name(destino.name + "-nuevo")
    viejo = destino.with_name(destino.name + "-anterior")
    for sobrante in (nuevo, viejo):
        if sobrante.exists():
            shutil.rmtree(sobrante, ignore_errors=True)

    paquete.reconstruir(nuevo)

    if destino.exists():
        try:
            destino.rename(viejo)
        except OSError:
            # La carpeta está retenida: se copia encima en vez de moverla.
            shutil.copytree(nuevo, destino, dirs_exist_ok=True)
            shutil.rmtree(nuevo, ignore_errors=True)
            return
    nuevo.rename(destino)
    shutil.rmtree(viejo, ignore_errors=True)


def empaquetar(destino: Path) -> Path:
    """Comprime el paquete y devuelve la ruta del zip.

    El zip es un artefacto derivado, así que **nunca debe impedir guardar**. En Windows
    basta con que el editor lo haya descargado hace un momento para que el fichero siga
    abierto y no se pueda sustituir; en ese caso se escribe con otro nombre y se entrega
    ese, en lugar de tumbar la operación entera por un fichero que se puede regenerar.
    """
    final = destino.with_suffix(".zip")
    provisional = destino.with_name(f"{destino.name}-{int(time.time())}.zip")

    if es_fuente(destino):
        _empaquetar_fuente(destino, provisional)
    else:
        with zipfile.ZipFile(provisional, "w", zipfile.ZIP_DEFLATED) as z:
            for fichero in sorted(destino.rglob("*")):
                if fichero.is_file():
                    z.write(fichero, fichero.relative_to(destino).as_posix())

    try:
        os.replace(provisional, final)
        return final
    except OSError:
        return provisional


def comprimir_proyecto(carpeta: Path, excluir=(), sustituir=None) -> bytes:
    """El zip de un proyecto de Author, como lo exporta learningCloud.

    `sustituir(ruta_relativa, contenido)` puede devolver otro contenido para un fichero:
    el envío a learningCloud lo usa para reenlazar la unidad con sus actividades.
    """
    import io

    datos = io.BytesIO()
    with zipfile.ZipFile(datos, "w", zipfile.ZIP_DEFLATED) as z:
        for fichero in sorted(carpeta.rglob("*")):
            relativa = fichero.relative_to(carpeta)
            if not fichero.is_file() or relativa.parts[0] in excluir:
                continue
            if sustituir is None:
                z.write(fichero, relativa.as_posix())
            else:
                z.writestr(relativa.as_posix(), sustituir(relativa.as_posix(), fichero.read_bytes()))
    return datos.getvalue()


def nombre_unidad(destino: Path) -> str:
    """El nombre del zip de la unidad: el que tenía al subirla."""
    import json

    meta = destino / "_fuente.json"
    return (json.loads(meta.read_text(encoding="utf-8")).get("unidad") if meta.exists() else None) or destino.name


def _empaquetar_fuente(destino: Path, salida: Path) -> None:
    """Un zip con un zip por proyecto, como los descarga Author: el de la unidad, con el
    nombre con que se subió, y uno por cada actividad."""
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"{nombre_unidad(destino)}.zip",
                   comprimir_proyecto(destino, excluir=(ACTIVIDADES, "_fuente.json")))
        actividades = destino / ACTIVIDADES
        for carpeta in sorted(actividades.iterdir()) if actividades.is_dir() else []:
            if carpeta.is_dir():
                z.writestr(f"{carpeta.name}.zip", comprimir_proyecto(carpeta))


def zip_vigente(destino: Path) -> Path:
    """Zip del paquete, regenerándolo solo si el contenido ha cambiado desde el último."""
    final = destino.with_suffix(".zip")
    if final.is_file():
        ultimo = max((f.stat().st_mtime for f in destino.rglob("*") if f.is_file()),
                     default=0)
        if final.stat().st_mtime >= ultimo:
            return final
    return empaquetar(destino)


def aplicar_decision(base: str, valor: str) -> str:
    """El valor definitivo de un elemento a partir de lo que ha decidido el editor.

    - Si llega **HTML** (empieza por una etiqueta), se escribe tal cual: es una decisión
      deliberada sobre el marcado, típica de la API o del MCP.
    - Si el elemento no tiene marcado, el texto va entero.
    - Si el editor escribió **solo el término** que sustituye a un `[PENDIENTE]` —como
      se hacía cuando la caja venía vacía—, se sustituye solo el marcador.
    - En cualquier otro caso el editor ha corregido el texto visible completo, que es lo
      que se le propone ahora, y el cambio se aplica sin perder enlaces ni formato.
    """
    if valor.lstrip().startswith("<"):
        return valor
    if "<" not in base:
        # Viene de un área de texto: los saltos de línea que teclee el editor no son
        # contenido en un título o un rótulo.
        return " ".join(valor.split())
    if _MARCA.search(base):
        resto = texto_plano(_MARCA.sub(" ", base)).split()
        propio = valor.split()
        parecido = difflib.SequenceMatcher(a=resto, b=propio, autojunk=False).ratio()
        if resto and parecido < 0.5:
            return _MARCA.sub(lambda _: valor, base)
    return editar_texto(base, valor)


def guardar_decisiones(trabajo_id: int) -> Path:
    """Aplica al paquete los términos que el editor ha resuelto.

    Resolver términos solo toca texto, y el texto vive en tres ficheros JSON. No hace
    falta reconstruir el paquete entero ni mover carpetas: se reescriben esos tres en su
    sitio y la copia embebida de `index.html`, que es la que lee el reproductor. Mover
    directorios en Windows con el contenido abierto en un navegador falla, y además
    copiaba 14 MB de recursos que no cambian.
    """
    from maker_paquete.serializacion import VOLCADORES

    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        destino = bd.ruta(trabajo.ruta_resultado)
        idioma = trabajo.idioma_destino
        decisiones = [(x.clave, x.valor_final, x.valor_base)
                      for x in trabajo.pendientes if x.valor_final]

    paquete = abrir_unidad(destino)
    mapa = MapaEditorial(paquete)
    glosario = claves_glosario(mapa)

    for clave, valor, base in decisiones:
        try:
            elemento = mapa.elemento(clave)
        except KeyError:
            continue
        nuevo_valor = aplicar_decision(base if base is not None else elemento.valor, valor)
        if nuevo_valor != elemento.valor:
            mapa.aplicar_cambio(clave, nuevo_valor)
    # Si el editor ha cambiado el nombre de un término, sus enlaces lo siguen.
    reenlazar_glosario(mapa, glosario)

    if es_fuente(destino):
        # Una sola copia del contenido: los XML vigentes, en su sitio. Sin reproductor, no
        # hay índice ni versión imprimible que regenerar.
        paquete.guardar()
        with bd.Sesion() as sesion:
            t = sesion.get(bd.Trabajo, trabajo_id)
            original = bd.ruta(t.paquete.ruta_extraido)
            idioma_origen, comunidad = t.paquete.idioma_origen, t.comunidad
            regenerar = t.regenerar_imagenes is not False
        # El editor puede haber reescrito el título de la unidad: el nombre lo sigue.
        _nombrar_fuente(original, destino, idioma_origen, idioma)
        decididas = decisiones_pdf(trabajo_id)
        if decididas:
            _reescribir_pdfs(original, destino, idioma_origen, idioma or idioma_origen,
                             comunidad, decididas, regenerar_imagenes=regenerar)
        return empaquetar(destino)

    modulo = _cargar_script("finalizar")
    modulo.sincronizar_anidados(paquete)
    if idioma:
        modulo.sincronizar_respuestas_cmi5(paquete, idioma)

    for nombre, obj in (("project.json", paquete.project),
                        ("components.json", paquete.components),
                        ("cmi5.json", paquete.cmi5)):
        (destino / nombre).write_bytes(VOLCADORES[nombre](obj))
    modulo.sincronizar_index(destino, paquete, paquete.idiomas[0], None)

    from . import impresion
    with bd.Sesion() as sesion:
        t = sesion.get(bd.Trabajo, trabajo_id)
        original = bd.ruta(t.paquete.ruta_extraido)
        idioma_origen, comunidad = t.paquete.idioma_origen, t.comunidad
        cambia = bool(t.idioma_destino and t.idioma_destino != t.paquete.idioma_origen)
        regenerar = t.regenerar_imagenes is not False
    avisos_pdf, _ = impresion.regenerar(destino, original, cambia)
    # Los PDF de la unidad, con lo que el editor ha decidido sobre sus textos.
    decididas = decisiones_pdf(trabajo_id)
    if decididas:
        avisos_maquetados, _, _, _ = _reescribir_pdfs(original, destino, idioma_origen,
                                                      idioma or idioma_origen, comunidad, decididas,
                                                      regenerar_imagenes=regenerar)
        avisos_pdf += avisos_maquetados
    for nivel, clave, texto in avisos_pdf:
        print(f"[{nivel}] {clave}: {texto}")

    return empaquetar(destino)


# --------------------------------------------------- traducción asistida por lotes

# Cadenas que aparecen entre el texto pero no son lenguaje. Traducirlas no es inútil: es
# destructivo. Los hexadecimales de 32 son identificadores de componente guardados en
# `correct` —la clave de respuesta de las actividades de relacionar—, y `true`/`false` son
# el valor de las de verdadero/falso. Tocarlos deja la actividad sin solución posible.
_HEXADECIMAL = re.compile(r"^[0-9a-f]{16,}$", re.I)
_SIN_LETRAS = re.compile(r"^[\W\d_]+$", re.UNICODE)


def _es_traducible(texto: str) -> bool:
    """Si la cadena es lenguaje o un valor que hay que dejar en paz."""
    limpio = texto.strip()
    if not limpio:
        return False
    if _HEXADECIMAL.match(limpio):
        return False
    if limpio.lower() in {"true", "false", "null", "undefined"}:
        return False
    # Relleno de la plantilla que no llega al alumno: en «Avalíate» de Prehistoria, los
    # ítems de los desplegables guardan «lorem ipsum» de la versión original y no se ven.
    if limpio.lower().startswith("lorem ipsum"):
        return False
    # Números, signos de puntuación y símbolos sueltos: iguales en cualquier lengua.
    return not _SIN_LETRAS.match(limpio)



def frases_pendientes(trabajo_id: int, tamano: int = 40,
                      max_caracteres: int = 6000) -> dict[str, Any]:
    """Frases de la unidad que el sistema todavía no sabe traducir.

    Se devuelven **sin repetir** y en orden de lectura. Sin repetir porque la misma frase
    aparece decenas de veces en una unidad —en la de Biología, 417 de 1.082— y traducirla
    una vez basta. En orden de lectura porque quien traduce necesita el hilo del texto,
    no una lista alfabética.

    Cada frase va con su contexto: en qué lección está y qué tipo de componente la
    contiene. «Rochas» no se traduce igual si es el título de una lección que si es una
    opción de respuesta.
    """
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        if trabajo is None:
            raise ValueError(f"No existe el trabajo {trabajo_id}")
        origen = bd.ruta(trabajo.paquete.ruta_extraido)
        idioma_destino = trabajo.idioma_destino
        idioma_origen = trabajo.paquete.idioma_origen

    if not idioma_destino or idioma_destino == idioma_origen:
        return {"trabajo_id": trabajo_id, "mensaje": "Este trabajo no cambia de idioma.",
                "lote": [], "pendientes": 0}

    paquete = abrir_unidad(origen)
    mapa = MapaEditorial(paquete)

    orden: list[str] = []
    contexto: dict[str, dict[str, Any]] = {}
    for elemento in mapa.redactables:
        for frase in extraer_frases(elemento.valor):
            clave = memoria.normalizar(frase)
            if not clave:
                continue
            if clave not in contexto:
                orden.append(clave)
                contexto[clave] = {"leccion": elemento.titulo_leccion,
                                   "componente": elemento.tipo, "veces": 0}
            contexto[clave]["veces"] += 1

    # Los rótulos de las imágenes con texto: los de las que se rehacen solas y los de las
    # que van a diseño, que así llegan con la traducción propuesta.
    from . import imagenes
    con_lectura = [(f"Imagen «{Path(l['fichero']).name}»", l) for l in imagenes.lecturas(origen).values()]
    con_lectura += [(f"Imagen del PDF «{pdf.name}», página {l['pagina']}", l)
                    for pdf, l in imagenes_pdf(origen)]
    for leccion, lectura in con_lectura:
        if imagenes.destino(lectura) == "nada":
            continue
        for rotulo in imagenes.rotulos_validos(lectura):
            clave = memoria.normalizar(imagenes.texto_rotulo(rotulo))
            if clave and _es_traducible(clave) and clave not in contexto:
                orden.append(clave)
                contexto[clave] = {"leccion": leccion, "componente": "imagen", "veces": 0}
            if clave in contexto:
                contexto[clave]["veces"] += 1

    # Y el texto de los PDF que acompañan a la unidad: fichas, rúbricas, diagramas.
    for fichero, texto in _textos_pdf(origen):
        clave = memoria.normalizar(texto)
        if clave and clave not in contexto:
            orden.append(clave)
            contexto[clave] = {"leccion": f"PDF «{fichero}»", "componente": "pdf", "veces": 0}
        if clave:
            contexto[clave]["veces"] += 1

    conocidas = memoria.buscar_frases(idioma_origen, idioma_destino, orden)
    # Las invariantes ya se miraron y no hay que tocarlas. Si no se descontaran, volverían
    # a salir en cada lote y el ciclo no llegaría nunca a cero.
    invariantes = memoria.frases_invariantes(idioma_origen, idioma_destino, orden)
    faltan = [f for f in orden
              if f not in conocidas and f not in invariantes and _es_traducible(f)]

    lote, caracteres = [], 0
    for frase in faltan:
        if len(lote) >= tamano or caracteres + len(frase) > max_caracteres:
            break
        caracteres += len(frase)
        lote.append({"id": memoria.huella(frase)[:12], "texto": frase,
                     "leccion": contexto[frase]["leccion"],
                     "componente": contexto[frase]["componente"],
                     "veces_en_la_unidad": contexto[frase]["veces"]})

    return {
        "trabajo_id": trabajo_id,
        "idioma_origen": idioma_origen, "idioma_destino": idioma_destino,
        "frases_distintas": len(orden), "ya_conocidas": len(conocidas),
        "sin_traduccion_necesaria": len(invariantes),
        "no_son_texto": sum(1 for f in orden if not _es_traducible(f)),
        "pendientes": len(faltan), "lote": lote,
    }


def aplicar_traducciones(trabajo_id: int, pares: list[dict[str, str]],
                         autor: str | None = None) -> dict[str, Any]:
    """Guarda las traducciones en memoria y vuelve a procesar el trabajo.

    Las traducciones llegan por `id`, que es la firma de la frase de origen: así quien
    traduce no tiene que devolver el texto original y no hay forma de desparejar las
    entradas.
    """
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        if trabajo is None:
            raise ValueError(f"No existe el trabajo {trabajo_id}")
        origen = bd.ruta(trabajo.paquete.ruta_extraido)
        idioma_destino, idioma_origen = trabajo.idioma_destino, trabajo.paquete.idioma_origen

    # Reconstruir la correspondencia id → texto desde el propio paquete.
    paquete = abrir_unidad(origen)
    por_id: dict[str, str] = {}
    for elemento in MapaEditorial(paquete).redactables:
        for frase in extraer_frases(elemento.valor):
            clave = memoria.normalizar(frase)
            if clave:
                por_id.setdefault(memoria.huella(clave)[:12], clave)

    guardadas, desconocidas, iguales, rechazadas = 0, [], 0, []
    for par in pares:
        identificador, destino = par.get("id"), (par.get("destino") or "").strip()
        texto = por_id.get(identificador or "")
        if texto is None:
            desconocidas.append(identificador)
            continue
        if not _es_traducible(texto):
            # Un identificador o un booleano llegó traducido. No se guarda: aplicarlo
            # rompería la actividad. Se anota como invariante y se avisa.
            memoria.marcar_invariante(idioma_origen, idioma_destino, texto,
                                      procedencia=f"lote trabajo {trabajo_id}")
            if memoria.normalizar(destino) != texto:
                rechazadas.append(texto)
            iguales += 1
            continue
        if not destino or memoria.normalizar(destino) == texto:
            # Devolver la frase igual es una respuesta válida: un rótulo de plantilla, una
            # sigla, un nombre propio. Se anota para no volver a preguntarla, aquí ni en
            # las próximas unidades.
            memoria.marcar_invariante(idioma_origen, idioma_destino, texto,
                                      procedencia=f"lote trabajo {trabajo_id}",
                                      aprobada_por=autor)
            iguales += 1
            continue
        memoria.guardar_frase(idioma_origen, idioma_destino, texto, destino,
                              procedencia=f"lote trabajo {trabajo_id}", aprobada_por=autor)
        guardadas += 1

    reprocesar(trabajo_id)

    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        cobertura = {"elementos_adaptados": trabajo.elementos_adaptados,
                     "elementos_totales": trabajo.elementos_totales,
                     "frases_conocidas": trabajo.frases_conocidas,
                     "frases_totales": trabajo.frases_totales}

    resultado = {"guardadas": guardadas, "marcadas_sin_traduccion": iguales,
                 "identificadores_desconocidos": desconocidas, **cobertura}
    if rechazadas:
        resultado["rechazadas_por_no_ser_texto"] = rechazadas
    return resultado


def reprocesar(trabajo_id: int) -> None:
    """Vuelve a ejecutar un trabajo conservando lo que el editor ya decidió.

    Los términos con decisión tomada no se tocan: lo que ha resuelto una persona no se
    borra porque el sistema haya aprendido algo nuevo.
    """
    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        origen = bd.ruta(trabajo.paquete.ruta_extraido)
        destino = (bd.ruta(trabajo.ruta_resultado)
                   or (TRABAJO / "resultados" / f"trabajo-{trabajo_id}"))
        resueltos = {t.clave for t in trabajo.pendientes if t.valor_final or t.detectado_por}
        for termino in list(trabajo.pendientes):
            if not termino.valor_final and not termino.detectado_por:
                sesion.delete(termino)
        sesion.commit()

    resumen = _ejecutar(trabajo_id, origen, destino)

    with bd.Sesion() as sesion:
        trabajo = sesion.get(bd.Trabajo, trabajo_id)
        trabajo.estado = bd.LISTO
        trabajo.terminado_en = dt.datetime.now()
        trabajo.ruta_resultado = bd.relativa(destino)
        trabajo.ruta_informe = bd.relativa(resumen["informe"])
        trabajo.elementos_totales = resumen["totales"]
        trabajo.elementos_adaptados = resumen["adaptados"]
        trabajo.avisos_validacion = resumen["avisos"]
        trabajo.frases_totales = resumen["frases_totales"]
        trabajo.frases_conocidas = resumen["frases_conocidas"]
        trabajo.imagenes = json.dumps(resumen["imagenes"], ensure_ascii=False)
        # El valor base de lo que se conserva sale del paquete recién generado, que es el
        # estado previo a decidir: si entretanto se corrigió una traducción, se parte de ella.
        mapa_nuevo = MapaEditorial(abrir_unidad(destino))
        for termino in trabajo.pendientes:
            if termino.valor_final or termino.detectado_por:
                try:
                    termino.valor_base = mapa_nuevo.elemento(termino.clave).valor
                except KeyError:
                    pass
        for termino in resumen["terminos"]:
            if termino["clave"] not in resueltos:
                sesion.add(bd.Termino(trabajo_id=trabajo_id, **termino))
        sesion.commit()
