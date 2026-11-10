/* Marcador «Sarela» para learningCloud (Author).

   Es un puente entre learningCloud y la página /learningcloud de Sarela, que se abre en
   otra ventana y decide qué hacer. Dos encargos:

   - Traer: lista la carpeta abierta y sus subcarpetas, descarga el fuente de la unidad y
     de cada actividad y se lo entrega a Sarela, con la ruta de la carpeta.
   - Enviar: crea en el proyecto destino la misma ruta de carpetas, importa las
     actividades, devuelve a Sarela el id nuevo de cada una, recibe la unidad ya
     reenlazada, la importa y comprueba que abre sus actividades nuevas.

   Todo con la sesión del usuario en learningCloud, que nunca sale de learningCloud. Los
   mensajes solo se aceptan de la ventana de Sarela que abre el propio marcador.

   Sarela lo sirve sustituyendo __SARELA__ por su dirección. Va entero dentro del
   marcador (la política de seguridad de learningCloud impediría cargar un script de
   fuera, y también pedirle nada a Sarela: los zips llegan por postMessage), así que
   solo comentarios de bloque y punto y coma explícitos. */
(function () {
  var SARELA = "__SARELA__";
  var PLANTILLA_ACTIVIDADES = 574;
  var TIPOS = "&type%5B%5D=FolderContent&type%5B%5D=AuthoringUnitContent&type%5B%5D=QuestionBankContent&type%5B%5D=GlossaryContent";
  var csrf = window.envVars && window.envVars.csrfToken;
  if (!csrf) {
    alert("Sarela: este marcador se usa dentro de learningCloud, con la sesión iniciada.");
    return;
  }
  var proyecto = (location.pathname.match(/\/project\/([0-9a-f-]{36})/) || [])[1] || null;
  var carpeta = new URLSearchParams(location.search).get("folder");
  var ventana = window.open(SARELA + "/learningcloud?desde=marcador", "sarela_learningcloud");
  if (!ventana) {
    alert("Sarela: el navegador ha bloqueado la ventana. Permite las ventanas emergentes de esta página y vuelve a pulsar el marcador.");
    return;
  }

  /* Aviso de progreso sobre la página de learningCloud. Allí no hay tokens del tema: el
     morado es brand de Sarela Coach, escrito tal cual. */
  var aviso = document.getElementById("sarela-aviso") || document.createElement("div");
  aviso.id = "sarela-aviso";
  aviso.style.cssText = "position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:380px;padding:14px 16px;" +
    "border-radius:12px;background:#6C4596;color:#fff;font:14px/1.45 system-ui,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.3)";
  document.body.appendChild(aviso);
  function decir(texto) { aviso.textContent = "Sarela · " + texto; }
  decir("conectando con Sarela (si pide iniciar sesión, entra en su ventana)…");

  function responder(mensaje, transferir) { ventana.postMessage(mensaje, SARELA, transferir || []); }
  function progreso(trabajo, texto) { decir(texto); responder({ tipo: "lc-progreso", trabajo: trabajo, texto: texto }); }

  async function api(metodo, ruta, cuerpo, aceptar) {
    var opciones = { method: metodo, credentials: "include",
      headers: { "X-XSRF-TOKEN": csrf, accept: aceptar || "application/ld+json" } };
    if (cuerpo instanceof FormData) { opciones.body = cuerpo; }
    else if (cuerpo) { opciones.headers["Content-Type"] = "application/json"; opciones.body = JSON.stringify(cuerpo); }
    var r = await fetch(ruta, opciones);
    if (!r.ok) {
      var detalle = "";
      try { var j = await r.json(); detalle = j.detail || j["hydra:description"] || ""; } catch (e) { }
      var error = new Error("learningCloud ha respondido " + r.status + (detalle ? ": " + detalle : "") + " (" + metodo + " " + ruta.split("?")[0] + ").");
      error.estado = r.status;
      throw error;
    }
    return r.json();
  }

  async function hijos(padre, tipos) {
    var salida = [];
    for (var pagina = 1; pagina < 50; pagina++) {
      var d = await api("GET", "/api/content_managers?parentId=" + encodeURIComponent(padre) +
        "&itemsPerPage=30&order%5Bname%5D=asc" + tipos + "&page=" + pagina);
      var miembros = d["hydra:member"] || [];
      salida = salida.concat(miembros);
      if (!miembros.length || salida.length >= d["hydra:totalItems"]) { break; }
    }
    return salida;
  }

  async function listar(padre, profundidad) {
    var salida = [], miembros = await hijos(padre, TIPOS);
    for (var i = 0; i < miembros.length; i++) {
      if (miembros[i]["@type"] === "FolderContent" && profundidad < 3) {
        salida = salida.concat(await listar(miembros[i].id, profundidad + 1));
      } else { salida.push(miembros[i]); }
    }
    return salida;
  }

  /* ------------------------------------------------------------------ traer */

  async function traer() {
    if (!carpeta) { throw new Error("abre en learningCloud la carpeta de la unidad (la dirección lleva ?folder=…)."); }
    decir("leyendo la carpeta…");
    var item = await api("GET", "/api/content_managers/" + carpeta);
    var todo = await listar(carpeta, 0);
    var unidades = [], actividades = [], omitidos = [];
    todo.forEach(function (m) {
      var puede = m.computedPermissions && m.computedPermissions.permissionExport_UnitCourse;
      if (m["@type"] !== "AuthoringUnitContent") { omitidos.push(m.name + " (no es una unidad ni una actividad)"); }
      else if (!puede) { omitidos.push(m.name + " (sin permiso de exportar)"); }
      else if (m.theme && m.theme.templateMakerId === PLANTILLA_ACTIVIDADES) { actividades.push(m); }
      else { unidades.push(m); }
    });
    if (unidades.length !== 1) {
      throw new Error(unidades.length ? "en esta carpeta y sus subcarpetas hay " + unidades.length + " unidades (" +
        unidades.map(function (m) { return "«" + m.name + "», plantilla " + ((m.theme && m.theme.templateMakerId) || "desconocida"); }).join("; ") +
        "). Sarela trae una sola unidad con sus actividades: abre la carpeta que contiene solo la que quieres." : "en esta carpeta no hay ninguna unidad.");
    }
    var lista = unidades.concat(actividades), ficheros = [], usados = {};
    for (var i = 0; i < lista.length; i++) {
      decir("descargando " + (i + 1) + " de " + lista.length + ": " + lista[i].name);
      var r = await fetch("/api/content_managers/authoring_units/" + lista[i].id + "/export", { credentials: "include" });
      if (!r.ok) { throw new Error("no se pudo descargar " + lista[i].name + " (" + r.status + ")."); }
      var nombre = (lista[i].name || lista[i].id).replace(/[^\w.\- ]+/g, "_");
      while (usados[nombre]) { nombre += "_"; }
      usados[nombre] = true;
      ficheros.push({ nombre: nombre + ".zip", datos: await r.arrayBuffer() });
    }
    var ruta = item.path || [];
    var origen = { entorno: location.origin, url: location.href, unidad: unidades[0].name, actividades: actividades.length,
      proyecto: ruta.length ? { id: ruta[0].id, nombre: ruta[0].name } : null,
      carpeta: carpeta, ruta: ruta.slice(1).map(function (n) { return n.name; }) };
    responder({ tipo: "sarela-paquete", origen: origen, omitidos: omitidos, ficheros: ficheros },
      ficheros.map(function (f) { return f.datos; }));
    decir("entregado a Sarela: la unidad y " + actividades.length + " actividades.");
  }

  /* ------------------------------------------------------------------ enviar */

  var envios = {};

  async function importar(padre, fichero, datos) {
    var formulario = new FormData();
    formulario.append("file", new Blob([datos], { type: "application/zip" }), fichero);
    formulario.append("parentIri", padre["@id"]);
    formulario.append("overwrite", "");
    var nuevo = await api("POST", "/api/content_managers/import", formulario);
    /* El id de curso nuevo, que es por el que la unidad enlaza sus actividades. */
    var datosNtx = await api("GET", "/api/content_managers/authoring_units/" + nuevo.id + "/get_ntxdata", null, "application/json");
    return { id: nuevo.id, curso: String(datosNtx.ntxdat.project.id), nombre: nuevo.name };
  }

  async function enviar(m) {
    var t = m.trabajo;
    progreso(t, "comprobando el proyecto destino…");
    var destino;
    try { destino = await api("GET", "/api/content_managers/" + m.proyecto); }
    catch (e) { throw new Error("no se puede abrir el proyecto destino " + m.proyecto + " con tu usuario (" + (e.estado || e.message) + ")."); }
    if (destino["@type"] !== "ProjectContent") { throw new Error("el destino configurado no es un proyecto de learningCloud."); }

    var ruta = m.ruta;
    if (!ruta) {
      progreso(t, "leyendo la carpeta de origen…");
      var item = await api("GET", "/api/content_managers/" + m.carpeta_origen);
      if ((item.path || [])[0] && item.path[0].id === m.proyecto) { throw new Error("el proyecto destino es el de origen: la copia acabaría junto a la unidad original."); }
      ruta = (item.path || []).slice(1).map(function (n) { return n.name; });
    }
    if (!ruta.length) { throw new Error("la unidad estaba en la raíz de su proyecto: no hay carpeta que replicar."); }

    var padre = destino, creadas = [];
    for (var i = 0; i < ruta.length; i++) {
      var existentes = await hijos(padre.id, "&type%5B%5D=FolderContent");
      var siguiente = existentes.filter(function (f) { return f.name === ruta[i]; })[0];
      if (!siguiente) {
        progreso(t, "creando la carpeta " + ruta.slice(0, i + 1).join(" / ") + "…");
        siguiente = await api("POST", "/api/content_managers/folders", { name: ruta[i], parentIri: padre["@id"] });
        creadas.push(ruta[i]);
      }
      padre = siguiente;
    }
    var url = location.origin + "/app/project/" + m.proyecto + "/content?folder=" + padre.id;
    var resultado = { trabajo: t, proyecto: { id: m.proyecto, nombre: destino.name }, ruta: ruta, carpeta: padre.id, url: url };
    var envio = envios[t] = { padre: padre, mapa: {}, ids: [], resultado: resultado };

    var dentro = await hijos(padre.id, TIPOS);
    if (dentro.length) {
      delete envios[t];
      resultado.estado = "conflicto";
      resultado.mensaje = "La carpeta " + ruta.join(" / ") + " de " + destino.name + " ya tiene " + dentro.length +
        (dentro.length === 1 ? " elemento" : " elementos") + ". No se importa nada: vacíala o bórrala en learningCloud y vuelve a enviar.";
      return resultado;
    }

    for (var j = 0; j < m.actividades.length; j++) {
      var a = m.actividades[j];
      progreso(t, "importando la actividad " + (j + 1) + " de " + m.actividades.length + ": " + a.fichero);
      var nueva = await importar(padre, a.fichero, a.datos);
      envio.mapa[a.id] = nueva.curso;
      envio.ids.push(nueva.id);
      resultado.actividades = envio.ids;
    }
    progreso(t, "actividades importadas; esperando la unidad reenlazada…");
    responder({ tipo: "lc-mapa", trabajo: t, mapa: envio.mapa, carpeta: padre.id, url: url });
    return null;
  }

  async function terminar(m) {
    var envio = envios[m.trabajo];
    if (!envio) { throw new Error("no hay un envío en curso para el trabajo " + m.trabajo + "."); }
    var resultado = envio.resultado;
    progreso(m.trabajo, "importando la unidad…");
    var unidad = await importar(envio.padre, m.nombre, m.datos);
    resultado.unidad = unidad.id;

    /* Comprobación: los conectores de la unidad importada tienen que abrir exactamente
       las actividades recién importadas, ni una de las originales. */
    var texto = JSON.stringify(await api("GET", "/api/content_managers/authoring_units/" + unidad.id + "/get_ntxdata", null, "application/json"));
    var enlazados = {}, esperado = {}, re = /"projectDataLink":\{"project":\{"id":(\d+)/g, x;
    while ((x = re.exec(texto))) { enlazados[x[1]] = true; }
    Object.keys(envio.mapa).forEach(function (k) { esperado[envio.mapa[k]] = true; });
    var ajenos = Object.keys(enlazados).filter(function (k) { return !esperado[k]; });
    var perdidos = Object.keys(esperado).filter(function (k) { return !enlazados[k]; });
    if (ajenos.length || perdidos.length) {
      resultado.estado = "error";
      resultado.mensaje = "La unidad se ha importado, pero sus enlaces no casan con las actividades nuevas" +
        (ajenos.length ? " (abre otros proyectos: " + ajenos.join(", ") + ")" : "") +
        (perdidos.length ? " (no abre: " + perdidos.join(", ") + ")" : "") + ". Revísala en learningCloud antes de usarla.";
    } else {
      resultado.estado = "enviado";
      resultado.mensaje = "Unidad y " + (envio.ids.length === 1 ? "1 actividad importadas" : envio.ids.length + " actividades importadas") +
        " y enlazadas entre sí.";
    }
    delete envios[m.trabajo];
    return resultado;
  }

  /* ------------------------------------------------------------------ mensajes */

  window.addEventListener("message", function (e) {
    if (e.origin !== SARELA || e.source !== ventana || !e.data) { return; }
    var m = e.data;
    if (m.tipo === "sarela-listo") {
      responder({ tipo: "lc-hola", entorno: location.origin, proyecto: proyecto, carpeta: carpeta, titulo: document.title });
      decir("conectado. Sigue en la ventana de Sarela.");
      return;
    }
    if (m.tipo === "lc-importar") {
      traer().catch(function (err) { decir("error: " + err.message); responder({ tipo: "lc-error", mensaje: err.message }); });
      return;
    }
    if (m.tipo === "lc-enviar" || m.tipo === "lc-unidad") {
      (m.tipo === "lc-enviar" ? enviar(m) : terminar(m)).then(function (resultado) {
        if (resultado) { decir(resultado.mensaje || "hecho."); responder({ tipo: "lc-resultado", resultado: resultado }); }
      }).catch(function (err) {
        var previo = (envios[m.trabajo] || {}).resultado || { trabajo: m.trabajo };
        previo.estado = "error";
        previo.mensaje = err.message;
        delete envios[m.trabajo];
        decir("error: " + err.message);
        responder({ tipo: "lc-resultado", resultado: previo });
      });
    }
  });
})();
