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
    grilla_<evaluacion>.txt      el texto de la tabla principal (para revisar)
    grilla_<evaluacion>_N.csv    las tablas de la grilla principal
"""

import csv
import os
import re

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


if __name__ == "__main__":
    main()