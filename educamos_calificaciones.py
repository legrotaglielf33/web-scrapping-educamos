"""
Scrapea tus calificaciones de Educamos (el "Cuaderno del profesor" de cada materia).

Instalación (una sola vez):
    pip install playwright
    python -m playwright install chromium

Uso:
    python educamos_calificaciones.py

Cómo funciona:
  1. Se abre Chromium. Si pide login, iniciá sesión A MANO (la sesión queda en sesion.json).
  2. Entrá vos a CALIFICACIONES haciendo clic en el menú, como siempre.
  3. Volvé a la terminal y apretá Enter.
  4. El script elige la división, recorre TODAS las evaluaciones (1er Período, 2do Período...),
     y en cada una abre el cuaderno de cada materia y copia las notas.

Archivos que genera (tienen tus datos personales: no los compartas):
    calificaciones.csv           evaluacion, materia, docente, nivel, nombre, nota
    mis_calificaciones.html      la página con promedios; se abre sola al terminar
    grilla_<evaluacion>.txt      el texto de la tabla principal (para revisar)
    grilla_<evaluacion>_N.csv    las tablas de la grilla principal
"""

import csv
import json
import os
import re
import webbrowser
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

INICIO_URL = "https://edison-mza.educamos.com/"
SESION = "sesion.json"  # cookies de tu sesión: es privado, no lo compartas

# --- Pedacitos de JavaScript que se ejecutan dentro de la página -------------------------

# Lista de materias de la grilla, con su docente y el id del botón que abre el cuaderno.
JS_MATERIAS = """
() => [...document.querySelectorAll('.calificaciones-alumno-para-padres .bodyPlaceHolder tbody tr')].map(tr => {
    const t = tr.querySelector('.titulo-wrap');
    const b = tr.querySelector('[id^="accion-2-mostrar-cuaderno-"]');
    return {
        materia: t ? t.innerText.trim() : '',
        docente: t ? (t.getAttribute('title') || '').trim() : '',
        botonId: b ? b.id : null
    };
})
"""

# Hay un diálogo (la ventana del cuaderno) visible y ya tiene filas.
JS_HAY_DIALOGO = """
() => [...document.querySelectorAll('.ui-dialog')]
    .some(d => d.getClientRects().length > 0 && d.querySelector('tbody tr'))
"""

# No queda ningún diálogo visible.
JS_NO_HAY_DIALOGO = """
() => ![...document.querySelectorAll('.ui-dialog')].some(d => d.getClientRects().length > 0)
"""

# Lee las filas del cuaderno que está abierto.
JS_LEER_CUADERNO = """
() => {
    const d = [...document.querySelectorAll('.ui-dialog')].filter(x => x.getClientRects().length > 0).pop();
    if (!d) return [];
    return [...d.querySelectorAll('tbody tr')].map(tr => {
        const td = tr.querySelector('td');
        const t = td ? td.querySelector('.titulo-wrap') : null;
        const notas = [...tr.querySelectorAll('td.nota')].map(c => c.innerText.trim());
        return {
            nivel: td && td.className.includes('k-nivel3') ? 'item' : 'grupo',
            nombre: t ? t.innerText.trim() : (td ? td.innerText.trim() : ''),
            nota: notas.join(' | ')
        };
    });
}
"""

# Cierra el cuaderno que está abierto (botón de la X).
JS_CERRAR_CUADERNO = """
() => {
    const d = [...document.querySelectorAll('.ui-dialog')].filter(x => x.getClientRects().length > 0).pop();
    const x = d ? d.querySelector('.ui-dialog-titlebar-close') : null;
    if (x) x.click();
}
"""

# Todas las tablas de la grilla principal, como listas de filas.
JS_TABLAS_GRILLA = """
() => [...document.querySelectorAll('.calificaciones-alumno-para-padres table')].map(t =>
    [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim()))
)
"""


# --- Funciones ---------------------------------------------------------------------------

def nombre_seguro(texto):
    """Deja un texto apto para nombre de archivo."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", texto).strip("_") or "sin_nombre"


def iniciar_sesion_si_hace_falta(pag, contexto):
    pag.goto(INICIO_URL, wait_until="domcontentloaded")
    pag.wait_for_timeout(2000)

    if "sso1.educamos.com" in pag.url:
        print("Iniciá sesión en la ventana del navegador. Tenés 5 minutos...")
        pag.wait_for_url("**://edison-mza.educamos.com/**", timeout=300_000)
        print("Sesión iniciada.")

    contexto.storage_state(path=SESION)


def esperar_carga(pag):
    """Espera a que desaparezca el cartel de 'cargando' y un poquito más."""
    try:
        pag.wait_for_selector("#dvLoading", state="hidden", timeout=15000)
    except Exception:
        pass
    pag.wait_for_timeout(1500)


def elegir_division(pag):
    """Si el desplegable de Divisiones está sin elegir, elige la primera."""
    sel = "#cmbNivelesEducativos"
    if pag.locator(sel).count() == 0:
        return
    sin_elegir = pag.eval_on_selector(sel, "s => s.selectedIndex") == 0
    if sin_elegir and pag.locator(f"{sel} option").count() > 1:
        pag.select_option(sel, index=1)
        esperar_carga(pag)


def abrir_cuaderno(pag, boton_id):
    """Hace clic en el botón de la materia y espera a que aparezca el cuaderno."""
    boton = pag.locator(f"#{boton_id}")
    try:
        boton.click(timeout=3000)
    except Exception:
        boton.evaluate("el => el.click()")  # si algo lo tapa, clic directo
    pag.wait_for_function(JS_HAY_DIALOGO, timeout=10000)
    pag.wait_for_timeout(300)


def cerrar_cuaderno(pag):
    pag.evaluate(JS_CERRAR_CUADERNO)
    try:
        pag.wait_for_function(JS_NO_HAY_DIALOGO, timeout=5000)
    except Exception:
        pass
    pag.wait_for_timeout(300)


def guardar_grilla(pag, evaluacion):
    """Guarda la grilla principal tal como se ve (por si las notas están ahí también)."""
    base = f"grilla_{nombre_seguro(evaluacion)}"
    try:
        texto = pag.locator(".calificaciones-alumno-para-padres").first.inner_text(timeout=3000)
        with open(f"{base}.txt", "w", encoding="utf-8") as f:
            f.write(texto)
    except Exception:
        pass
    for i, tabla in enumerate(pag.evaluate(JS_TABLAS_GRILLA), start=1):
        with open(f"{base}_{i}.csv", "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerows(tabla)


def recorrer_materias(pag, evaluacion, filas_csv):
    """Abre el cuaderno de cada materia de la evaluación actual y acumula las notas."""
    materias = pag.evaluate(JS_MATERIAS)
    print(f"  {len(materias)} materias encontradas")

    for m in materias:
        if not m["botonId"]:
            print(f"  - {m['materia']}: no tiene cuaderno (mirá la grilla)")
            filas_csv.append([evaluacion, m["materia"], m["docente"], "sin_cuaderno", "", ""])
            continue

        try:
            abrir_cuaderno(pag, m["botonId"])
            filas = pag.evaluate(JS_LEER_CUADERNO)
            print(f"  - {m['materia']}: {len(filas)} filas")
            for f in filas:
                filas_csv.append([evaluacion, m["materia"], m["docente"], f["nivel"], f["nombre"], f["nota"]])
        except Exception as e:
            print(f"  - {m['materia']}: no se pudo abrir el cuaderno ({type(e).__name__})")
            filas_csv.append([evaluacion, m["materia"], m["docente"], "error", "", ""])
        finally:
            cerrar_cuaderno(pag)


def generar_reporte(archivo_csv="calificaciones.csv", salida="mis_calificaciones.html"):
    """Mete los datos del CSV adentro de la página y la guarda como un HTML que funciona solo."""
    with open(archivo_csv, encoding="utf-8-sig") as f:
        texto = f.read()
    seguro = json.dumps(texto, ensure_ascii=False).replace("</", "<\\/")
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")
    html = PLANTILLA_HTML.replace("const DATOS_INCRUSTADOS = null;", f"const DATOS_INCRUSTADOS = {seguro};")
    html = html.replace("const FECHA_DATOS = null;", f"const FECHA_DATOS = {json.dumps(fecha)};")
    with open(salida, "w", encoding="utf-8") as f:
        f.write(html)
    return salida


def main():
    filas_csv = []

    with sync_playwright() as p:
        nav = p.chromium.launch(headless=False)
        contexto = nav.new_context(
            storage_state=SESION if os.path.exists(SESION) else None
        )
        pag = contexto.new_page()

        iniciar_sesion_si_hace_falta(pag, contexto)

        print("\nAhora, en la ventana del navegador, entrá a CALIFICACIONES haciendo clic como siempre.")
        print("Cuando veas la pantalla de Calificaciones, volvé acá y apretá Enter.")
        input("Enter para continuar... ")

        pag = contexto.pages[-1]  # por si se abrió en una pestaña nueva
        pag.wait_for_load_state("networkidle")
        pag.wait_for_timeout(2000)

        elegir_division(pag)

        evaluaciones = pag.locator("#cmbEvaluaciones option").all_inner_texts()
        # La posición 0 es "Seleccione evaluación"; las reales empiezan en 1.
        reales = evaluaciones[1:] if len(evaluaciones) > 1 else []
        print(f"\nEvaluaciones encontradas: {reales or '(ninguna, uso la pantalla como está)'}")

        if reales:
            for i, nombre in enumerate(reales, start=1):
                print(f"\n== {nombre} ==")
                pag.select_option("#cmbEvaluaciones", index=i)
                esperar_carga(pag)
                guardar_grilla(pag, nombre)
                recorrer_materias(pag, nombre, filas_csv)
        else:
            print("\n== Pantalla actual ==")
            guardar_grilla(pag, "actual")
            recorrer_materias(pag, "actual", filas_csv)

        nav.close()

    with open("calificaciones.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["evaluacion", "materia", "docente", "nivel", "nombre", "nota"])
        w.writerows(filas_csv)

    print(f"\nListo: {len(filas_csv)} filas guardadas en calificaciones.csv")

    ruta = generar_reporte()
    print(f"Página lista: {ruta}. Abriéndola en el navegador...")
    webbrowser.open(Path(ruta).resolve().as_uri())


# La página con los promedios (misma que se ve en el navegador). Los datos se le agregan al final.
PLANTILLA_HTML = r'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Mis calificaciones</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f1f4f7; --panel: #ffffff; --ink: #14212b; --suave: #5b6b78; --linea: #d9e0e6;
  --bad: #c8372d; --bad-t: #f6d9d6; --warn: #b87d00; --warn-t: #fbe9b5; --ok: #23764a; --ok-t: #d3ecdc;
  --marca: #14212b;
  box-sizing: border-box;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f171e; --panel: #172430; --ink: #e8eef3; --suave: #93a4b2; --linea: #2a3b49;
    --bad: #ff7b70; --bad-t: #4a2420; --warn: #f0be3c; --warn-t: #4a3a10; --ok: #5fcf8f; --ok-t: #17402b;
    --marca: #e8eef3;
  }
}
:root[data-theme="dark"] {
  --bg: #0f171e; --panel: #172430; --ink: #e8eef3; --suave: #93a4b2; --linea: #2a3b49;
  --bad: #ff7b70; --bad-t: #4a2420; --warn: #f0be3c; --warn-t: #4a3a10; --ok: #5fcf8f; --ok-t: #17402b;
  --marca: #e8eef3;
}
html { scroll-padding-top: env(safe-area-inset-top, 0px); }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: "Bricolage Grotesque", system-ui, -apple-system, "Segoe UI", sans-serif; line-height: 1.45; }
main { max-width: 860px; margin: 0 auto; padding: 28px 18px 64px; }
h1 { font-size: clamp(1.9rem, 5vw, 2.6rem); font-weight: 800; letter-spacing: -0.02em; margin: 0 0 4px; }
.sub { color: var(--suave); margin: 0 0 22px; max-width: 60ch; }
.num { font-variant-numeric: tabular-nums; }
.caja { background: var(--panel); border: 1px solid var(--linea); border-radius: 14px; padding: 16px 18px; margin-bottom: 16px; }
.carga { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
.carga p { margin: 0; color: var(--suave); font-size: .92rem; flex-basis: 100%; }
button, select, input[type=number] { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--linea); border-radius: 8px; padding: 8px 12px; }
button { cursor: pointer; font-weight: 600; }
button:hover { border-color: var(--marca); }
:focus-visible { outline: 3px solid var(--warn); outline-offset: 2px; }
input[type=file] { font: inherit; max-width: 100%; }
.controles { display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: end; }
.controles label { display: flex; flex-direction: column; gap: 4px; font-size: .85rem; color: var(--suave); }
.controles input[type=number] { width: 96px; }
.resumen { display: grid; grid-template-columns: auto 1fr; gap: 6px 26px; align-items: center; }
.grande { font-size: clamp(3.4rem, 12vw, 5.2rem); font-weight: 800; line-height: 1; letter-spacing: -0.03em; }
.grande small { display: block; font-size: .9rem; font-weight: 400; letter-spacing: 0; color: var(--suave); margin-top: 6px; }
.conteos { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { padding: 5px 12px; border-radius: 999px; font-weight: 600; font-size: .9rem; }
.pill.ok, .tag.ok { background: var(--ok-t); color: var(--ok); }
.pill.warn, .tag.warn { background: var(--warn-t); color: var(--warn); }
.pill.bad, .tag.bad { background: var(--bad-t); color: var(--bad); }
.pill.nada, .tag.nada { background: var(--linea); color: var(--suave); }
.aviso { border-left: 6px solid var(--warn); background: var(--warn-t); border-radius: 10px; padding: 12px 16px; margin-bottom: 16px; }
.aviso b { display: block; margin-bottom: 2px; }
.materia { padding: 14px 18px 16px; }
.cab { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.cab h2 { font-size: 1.15rem; margin: 0; font-weight: 600; }
.cab .doc { color: var(--suave); font-size: .85rem; }
.prom { font-size: 2rem; font-weight: 800; letter-spacing: -0.02em; white-space: nowrap; }
.prom.ok { color: var(--ok); } .prom.warn { color: var(--warn); } .prom.bad { color: var(--bad); } .prom.nada { color: var(--suave); }
.escala { position: relative; height: 12px; border-radius: 999px; margin: 12px 0 6px;
  background: linear-gradient(to right, var(--bad-t) 0 calc(var(--flo) * 10%), var(--warn-t) calc(var(--flo) * 10%) calc(var(--apr) * 10%), var(--ok-t) calc(var(--apr) * 10%) 100%); }
.escala i { position: absolute; top: -4px; width: 4px; height: 20px; border-radius: 2px; background: var(--marca); transform: translateX(-50%); }
.escala.sin i { display: none; }
.pie { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; font-size: .85rem; color: var(--suave); }
.tag { padding: 3px 10px; border-radius: 999px; font-weight: 600; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chips span { background: var(--bg); border: 1px solid var(--linea); border-radius: 6px; padding: 2px 8px; }
details { margin-top: 10px; }
summary { cursor: pointer; font-weight: 600; font-size: .92rem; }
.ev { margin: 10px 0 4px; font-weight: 600; font-size: .9rem; }
.item { display: flex; justify-content: space-between; gap: 14px; padding: 5px 0; border-bottom: 1px dashed var(--linea); font-size: .92rem; }
.item.g { font-weight: 600; border-bottom: 0; padding-top: 10px; color: var(--suave); }
.vacio { text-align: center; color: var(--suave); padding: 34px 10px; }
.priv { color: var(--suave); font-size: .82rem; margin-top: 22px; }
</style>
</head>
<body>
<main>
  <h1>Mis calificaciones</h1>
  <p class="sub">Cargá el archivo <b>calificaciones.csv</b> que genera el script y mirá el promedio de cada materia. Entre 6,5 y 7 te avisa que estás en la cuerda floja.</p>

  <section class="caja carga">
    <input type="file" id="archivo" accept=".csv,text/csv">
    <button id="ejemplo" type="button">Ver con datos de ejemplo</button>
    <p>El archivo se lee en tu navegador y no se sube a ningún lado.</p>
  </section>

  <section id="app" hidden>
    <div class="caja controles">
      <label>Evaluación <select id="scope"></select></label>
      <label>Aprobada desde <input type="number" id="apr" value="7" min="1" max="10" step="0.1"></label>
      <label>Cuerda floja desde <input type="number" id="flo" value="6.5" min="1" max="10" step="0.1"></label>
    </div>
    <div id="resumen" class="caja resumen"></div>
    <div id="aviso"></div>
    <div id="lista"></div>
    <p class="priv">El promedio es el simple de todos los ítems del cuaderno. El que calcula el colegio puede ponderar distinto, así que tomalo como una referencia.</p>
  </section>
  <p id="vacio" class="vacio">Todavía no cargaste ningún archivo.</p>
</main>

<script>
const $ = (s) => document.querySelector(s);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => n.toFixed(2).replace(".", ",");
const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length;

const DATOS_INCRUSTADOS = null; // lo reemplaza el script educamos_calificaciones.py
const FECHA_DATOS = null;
let evals = [];
let mats = new Map();

function parseCSV(texto) {
  texto = texto.replace(/^\uFEFF/, "");
  const filas = []; let fila = [], campo = "", comillas = false;
  for (let i = 0; i < texto.length; i++) {
    const c = texto[i];
    if (comillas) {
      if (c === '"' && texto[i + 1] === '"') { campo += '"'; i++; }
      else if (c === '"') comillas = false;
      else campo += c;
    } else if (c === '"') comillas = true;
    else if (c === ",") { fila.push(campo); campo = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && texto[i + 1] === "\n") i++;
      fila.push(campo); campo = ""; filas.push(fila); fila = [];
    } else campo += c;
  }
  if (campo !== "" || fila.length) { fila.push(campo); filas.push(fila); }
  return filas.filter((f) => f.some((x) => x.trim() !== ""));
}

function notaNum(s) {
  for (const parte of String(s || "").split("|")) {
    const t = parte.trim();
    if (/^\d+([.,]\d+)?$/.test(t)) return parseFloat(t.replace(",", "."));
  }
  return NaN;
}

function cargarDatos(texto) {
  const filas = parseCSV(texto);
  const cab = filas.shift().map((h) => h.trim().toLowerCase());
  const ix = (n) => cab.indexOf(n);
  evals = []; mats = new Map();
  const grupoActual = new Map();
  for (const f of filas) {
    const ev = f[ix("evaluacion")] || "Sin nombre", mat = f[ix("materia")] || "", nivel = f[ix("nivel")] || "";
    if (!mat) continue;
    if (!evals.includes(ev)) evals.push(ev);
    if (!mats.has(mat)) mats.set(mat, { docente: f[ix("docente")] || "", porEval: new Map() });
    const m = mats.get(mat);
    if (!m.porEval.has(ev)) m.porEval.set(ev, { items: [] });
    const clave = mat + "||" + ev;
    if (nivel === "grupo") { grupoActual.set(clave, f[ix("nombre")]); continue; }
    if (nivel !== "item") continue;
    const nota = notaNum(f[ix("nota")]);
    if (Number.isFinite(nota)) m.porEval.get(ev).items.push({ grupo: grupoActual.get(clave) || "", nombre: f[ix("nombre")], nota });
  }
  if (!mats.size) { alert("No encontré materias en ese archivo. ¿Es el calificaciones.csv del script?"); return; }

  const sel = $("#scope");
  sel.innerHTML = '<option value="__todas">Todas (promedio de evaluaciones)</option>' + evals.map((e) => `<option>${esc(e)}</option>`).join("");
  const conNotas = evals.filter((e) => [...mats.values()].some((m) => m.porEval.get(e)?.items.length));
  sel.value = conNotas.length ? conNotas[conNotas.length - 1] : "__todas";
  $("#app").hidden = false; $("#vacio").hidden = true;
  render();
}

function estado(p, apr, flo) {
  if (p === null) return ["nada", "Sin notas"];
  if (p >= apr) return ["ok", "Aprobada"];
  if (p >= flo) return ["warn", "En la cuerda floja"];
  return ["bad", "Por debajo de " + String(flo).replace(".", ",")];
}

function render() {
  const scope = $("#scope").value;
  const apr = parseFloat($("#apr").value) || 7, flo = parseFloat($("#flo").value) || 6.5;
  document.documentElement.style.setProperty("--apr", apr);
  document.documentElement.style.setProperty("--flo", flo);

  const filas = [...mats].map(([nombre, m]) => {
    const evAvg = evals.map((e) => { const d = m.porEval.get(e); return d && d.items.length ? { e, avg: mean(d.items.map((i) => i.nota)), items: d.items } : null; }).filter(Boolean);
    let avg = null, detalle = [];
    if (scope === "__todas") { if (evAvg.length) { avg = mean(evAvg.map((x) => x.avg)); detalle = evAvg; } }
    else { const x = evAvg.find((x) => x.e === scope); if (x) { avg = x.avg; detalle = [x]; } }
    return { nombre, docente: m.docente, avg, detalle, evAvg };
  }).sort((a, b) => (a.avg ?? 99) - (b.avg ?? 99));

  const con = filas.filter((f) => f.avg !== null);
  const cuenta = { ok: 0, warn: 0, bad: 0 };
  con.forEach((f) => cuenta[estado(f.avg, apr, flo)[0]]++);
  const general = con.length ? mean(con.map((f) => f.avg)) : null;
  const [cg, tg] = estado(general, apr, flo);

  $("#resumen").innerHTML = `
    <div class="grande num">${general === null ? "–" : fmt(general)}<small>promedio general</small></div>
    <div class="conteos">
      <span class="pill ok">${cuenta.ok} aprobadas</span>
      <span class="pill warn">${cuenta.warn} en la cuerda floja</span>
      <span class="pill bad">${cuenta.bad} por debajo de ${String(flo).replace(".", ",")}</span>
    </div>`;

  const flojas = con.filter((f) => estado(f.avg, apr, flo)[0] === "warn");
  $("#aviso").innerHTML = flojas.length
    ? `<div class="aviso"><b>Estás en la cuerda floja en ${flojas.length === 1 ? "1 materia" : flojas.length + " materias"}</b>${flojas.map((f) => `${esc(f.nombre)} (${fmt(f.avg)})`).join(", ")}. Te faltan pocas décimas para llegar a ${String(apr).replace(".", ",")}.</div>`
    : "";

  $("#lista").innerHTML = filas.map((f) => {
    const [c, t] = estado(f.avg, apr, flo);
    const pos = f.avg === null ? 0 : Math.min(100, f.avg * 10);
    const chips = f.evAvg.length > 1 || scope === "__todas"
      ? `<div class="chips">${f.evAvg.map((x) => `<span>${esc(x.e)}: ${fmt(x.avg)}</span>`).join("")}</div>` : "<span></span>";
    const detalle = f.detalle.map((d) => {
      let ult = null;
      const lineas = d.items.map((i) => {
        const g = i.grupo !== ult ? (ult = i.grupo, i.grupo ? `<div class="item g"><span>${esc(i.grupo)}</span></div>` : "") : "";
        return g + `<div class="item"><span>${esc(i.nombre)}</span><span class="num">${fmt(i.nota)}</span></div>`;
      }).join("");
      return (f.detalle.length > 1 ? `<div class="ev">${esc(d.e)}</div>` : "") + lineas;
    }).join("");
    return `<article class="caja materia">
      <div class="cab"><div><h2>${esc(f.nombre)}</h2><div class="doc">${esc(f.docente)}</div></div>
        <div class="prom num ${c}">${f.avg === null ? "–" : fmt(f.avg)}</div></div>
      <div class="escala ${f.avg === null ? "sin" : ""}"><i style="left:${pos}%"></i></div>
      <div class="pie">${chips}<span class="tag ${c}">${t}</span></div>
      ${detalle ? `<details><summary>Ver las notas (${f.detalle.reduce((n, d) => n + d.items.length, 0)})</summary>${detalle}</details>` : ""}
    </article>`;
  }).join("");
}

$("#archivo").addEventListener("change", (e) => {
  const f = e.target.files[0]; if (!f) return;
  const r = new FileReader(); r.onload = () => cargarDatos(r.result); r.readAsText(f, "utf-8");
});
["scope", "apr", "flo"].forEach((id) => $("#" + id).addEventListener("input", render));

// Si el script dejó los datos adentro de la página, se muestran solos.
if (DATOS_INCRUSTADOS) {
  $(".carga").style.display = "none";
  if (FECHA_DATOS) $(".sub").textContent = "Datos del " + FECHA_DATOS + ". Para actualizarlos, volvé a correr el script.";
  cargarDatos(DATOS_INCRUSTADOS);
}

$("#ejemplo").addEventListener("click", () => {
  const filas = [["evaluacion", "materia", "docente", "nivel", "nombre", "nota"]];
  const dato = (ev, mat, doc, notas) => { filas.push([ev, mat, doc, "grupo", "Unidad 1", ""]); notas.forEach((n, i) => filas.push([ev, mat, doc, "item", "Trabajo " + (i + 1), n])); };
  dato("1er Período", "Matemática (ejemplo)", "Docente A", ["8,00", "7,50", "9,00"]);
  dato("1er Período", "Programación (ejemplo)", "Docente B", ["6,00", "7,00", "6,50"]);
  dato("1er Período", "Inglés (ejemplo)", "Docente C", ["5,00", "6,00", "6,50"]);
  dato("2do Período", "Matemática (ejemplo)", "Docente A", ["10,00", "9,00"]);
  dato("2do Período", "Programación (ejemplo)", "Docente B", ["6,50", "6,75"]);
  dato("2do Período", "Inglés (ejemplo)", "Docente C", ["7,00", "8,00"]);
  cargarDatos(filas.map((f) => f.map((x) => '"' + x + '"').join(",")).join("\n"));
});
</script>
</body>
</html>
'''


if __name__ == "__main__":
    main()