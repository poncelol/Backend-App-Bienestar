# -*- coding: utf-8 -*-
"""
Navegador para buscar fotos (Chrome, controlado con Playwright). Viene de completar_articulos.py.

Se abre Google o Bing Imágenes con la búsqueda; al hacer clic en una foto, la página avisa a Python con el vínculo
de la imagen y el de la página de origen (para la columna FUENTE, igual que en el Excel).

Instalación (una sola vez):  pip install playwright   (usa el Chrome instalado; si no hay: playwright install chromium)
"""

import json
import os
import queue
import threading
from urllib.parse import quote_plus, urlparse

MOTORES = {
    "Bing": "https://www.bing.com/images/search?q={q}",
    "Google": "https://www.google.com/search?tbm=isch&hl=es&q={q}",
}

# Se inyecta en todas las páginas: detecta el clic en una foto y se la manda a Python (window.pyPick)
JS_CAPTURA = r"""
(() => {
  if (window.__pickInstalled) return;
  window.__pickInstalled = true;
  let last = {u: '', t: 0};

  function toast(msg, ok) {
    try {
      let d = document.getElementById('__pick_toast');
      if (!d) {
        d = document.createElement('div');
        d.id = '__pick_toast';
        d.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;padding:10px 16px;' +
          'border-radius:8px;font:600 14px sans-serif;color:#fff;box-shadow:0 2px 10px rgba(0,0,0,.3);' +
          'pointer-events:none;transition:opacity .3s';
        document.documentElement.appendChild(d);
      }
      d.style.background = ok ? '#1a7f37' : '#b42318';
      d.textContent = msg;
      d.style.opacity = '1';
      clearTimeout(d.__t);
      d.__t = setTimeout(() => { d.style.opacity = '0'; }, 2200);
    } catch (_) {}
  }

  function leer(t) {
    // 1) Bing: <a class="iusc" m='{"murl": imagen, "purl": página}'>
    const bing = t.closest('a.iusc');
    if (bing) {
      try {
        const m = JSON.parse(bing.getAttribute('m'));
        if (m && m.murl) return {img: m.murl, page: m.purl || location.href, title: m.t || ''};
      } catch (_) {}
    }
    // 2) Google: <a href="/imgres?imgurl=...&imgrefurl=...">
    const g = t.closest('a[href*="imgres"]');
    if (g) {
      try {
        const u = new URL(g.href, location.href);
        const img = u.searchParams.get('imgurl');
        if (img) return {img: img, page: u.searchParams.get('imgrefurl') || location.href,
                         title: g.getAttribute('aria-label') || ''};
      } catch (_) {}
    }
    // 3) Respaldo: clic directo en una <img> con URL real (p. ej. el panel de vista grande de Google)
    const im = t.closest('img');
    if (im && /^https?:/.test(im.src) && im.naturalWidth >= 100) {
      let host = '';
      try { host = new URL(im.src).hostname; } catch (_) {}
      if (!/(^|\.)(google\.[a-z.]+|gstatic\.com|bing\.com|bing\.net)$/.test(host)) {
        return {img: im.src, page: location.href, title: im.alt || ''};
      }
    }
    return null;
  }

  document.addEventListener('click', (e) => {
    if (!window.pyPick || !e.target || !e.target.closest) return;
    const info = leer(e.target);
    if (!info || !info.img) return;
    const now = Date.now();
    if (info.img === last.u && now - last.t < 1500) return;
    last = {u: info.img, t: now};
    window.pyPick(JSON.stringify(info))
      .then((r) => toast(r || '✔ Imagen capturada', true))
      .catch(() => toast('No se pudo guardar', false));
  }, true);
})();
"""


def url_busqueda(motor: str, consulta: str) -> str:
    return MOTORES.get(motor, MOTORES["Bing"]).format(q=quote_plus(consulta))


def fuente_de(motor: str, captura: dict) -> str:
    """Texto de la columna FUENTE, con el mismo formato que completar_articulos.py."""
    pagina = (captura.get("page") or "").strip()
    dominio = urlparse(pagina).netloc or urlparse(captura.get("img") or "").netloc
    return f"Búsqueda manual ({motor}): {pagina or dominio}"[:250]


class Navegador(threading.Thread):
    """Chrome en su propio hilo (Playwright no se puede usar desde el hilo de la ventana).
    Avisa por `cola`: ("pick", dict) al capturar una foto y ("msg", texto) para la bitácora."""

    def __init__(self, cola: queue.Queue):
        super().__init__(daemon=True)
        self.cola = cola
        self.cmds = queue.Queue()
        self.vivo = True

    def ir(self, url: str):
        self.cmds.put(url)

    def cerrar(self):
        self.cmds.put(None)

    def _pick(self, raw):
        try:
            self.cola.put(("pick", json.loads(raw)))
        except ValueError:
            return "No se entendió la imagen"
        return "✔ Imagen capturada"

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.cola.put(("msg", "Falta Playwright:  pip install playwright   (y si no tiene Chrome: playwright install chromium)"))
            self.vivo = False
            return
        try:
            with sync_playwright() as p:
                # Mismo perfil que completar_articulos.py: conserva cookies y avisos aceptados de Google/Bing
                perfil = os.path.join(os.path.expanduser("~"), ".buscador_imagenes_perfil")
                ctx, error = None, None
                for extra in ({"channel": "chrome"}, {}):  # primero Chrome instalado; si no, Chromium
                    try:
                        ctx = p.chromium.launch_persistent_context(
                            perfil, headless=False, no_viewport=True,
                            args=["--disable-blink-features=AutomationControlled"], **extra)
                        break
                    except Exception as e:  # noqa: BLE001
                        error = e
                if ctx is None:
                    self.cola.put(("msg", f"No se pudo abrir Chrome: {error}"))
                    return
                ctx.expose_function("pyPick", self._pick)
                ctx.add_init_script(JS_CAPTURA)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                while True:
                    try:
                        cmd = self.cmds.get_nowait()
                    except queue.Empty:
                        try:
                            page.wait_for_timeout(100)  # también atiende los clics del navegador
                        except Exception:  # noqa: BLE001
                            break  # el usuario cerró el navegador
                        continue
                    if cmd is None:
                        break
                    try:
                        if page.is_closed():
                            page = ctx.new_page()
                        page.goto(cmd, wait_until="domcontentloaded")
                        page.bring_to_front()
                    except Exception as e:  # noqa: BLE001
                        if "closed" in str(e).lower():
                            break
                        self.cola.put(("msg", f"Error al abrir la búsqueda: {e}"))
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self.vivo = False
            self.cola.put(("msg", "Chrome se cerró; se abrirá de nuevo en la próxima búsqueda."))
