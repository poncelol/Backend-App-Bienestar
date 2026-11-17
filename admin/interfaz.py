# -*- coding: utf-8 -*-
"""
Ventana de administración (python admin_bienestar.py).

Se conecta al servidor: toma el catálogo (artículos, productos y proveedores) y las imágenes de la base de datos,
y muestra cuáles tienen foto y cuáles no. Para poner una foto:
  🔎 Buscar en Chrome (Google/Bing Imágenes): se hace clic en la foto correcta y se guarda sola.
  🔗 Pegar un vínculo.
  📁 Subir un archivo (PNG, JPG, WEBP…).
"""

import io
import queue
import threading
import tkinter as tk
import tkinter.simpledialog  # noqa: F401 — registra tk.simpledialog
import unicodedata
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

import admin_bienestar as nucleo
from navegador import MOTORES, Navegador, fuente_de, url_busqueda

try:
    from PIL import Image, ImageTk
except ImportError:  # sin Pillow no hay vista previa ni subida de archivos
    Image = ImageTk = None

C = {
    "guinda": "#9B2247", "guinda_osc": "#611232", "verde": "#1E5B4F", "dorado": "#A57F2C",
    "fondo": "#F6F3EE", "panel": "#FFFFFF", "texto": "#1F1F1F", "gris": "#6B6B6B", "borde": "#DDD6CC",
    "falta": "#FBE5E8", "hereda": "#FFF3D6", "ok": "#FFFFFF",
}
FUENTE = "Segoe UI"
FOTO_W, FOTO_H = 330, 200
ESTADOS = {"ok": "✓ Con foto", "hereda": "◐ De un artículo", "falta": "✗ Falta"}
TITULOS = {"articulos": "📦 Artículos", "productos": "🏷️ Productos", "proveedores": "🏭 Proveedores"}
SINGULAR = {"articulos": "artículo", "productos": "producto", "proveedores": "proveedor"}
COLUMNAS = {
    "articulos": [("estado", "Estado", 120), ("clave", "ART.", 80), ("nombre", "Artículo", 330),
                  ("marca", "Marca", 120), ("proveedor", "Proveedor", 220)],
    "productos": [("estado", "Estado", 130), ("clave", "PROD.", 70), ("nombre", "Producto", 380), ("articulos", "Artículos", 80)],
    "proveedores": [("estado", "Estado", 120), ("clave", "CVE", 70), ("nombre", "Proveedor", 400), ("articulos", "Artículos", 80)],
}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def hora_local(iso: str | None) -> str:
    """'2026-11-17T18:37:39Z' -> '17/11/2026 12:37' en la hora de esta computadora."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M")
    except (AttributeError, ValueError):
        return "—"


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def consulta_para(tipo: str, item: dict) -> str:
    """Texto para Google/Bing, como en completar_articulos.py."""
    nombre = item["nombre"]
    if tipo == "proveedores":
        import re
        corto = re.sub(r"[,\s]+(S\.?\s?A\.?\s*(P\.?\s?I\.?\s*)?DE\s*C\.?\s?V\.?|S\.?\s?DE\s*R\.?\s?L\.?.*)$", "",
                       nombre, flags=re.I).strip(" ,.")
        return corto + " logo"
    if tipo == "articulos":
        marca = item.get("marca") or ""
        if marca and marca.lower() not in nombre.lower():
            nombre += " " + marca
        barras = item.get("barras") or ""
        if barras.isdigit() and len(barras) >= 8 and set(barras) != {"0"}:
            nombre += " " + barras
    return nombre


# ──────────────────────────────────────────────────────────────
# Una pestaña de imágenes (artículos, productos o proveedores)
# ──────────────────────────────────────────────────────────────
class PanelImagenes(ttk.Frame):
    def __init__(self, padre, app: "App", tipo: str):
        super().__init__(padre, style="Fondo.TFrame", padding=10)
        self.app, self.tipo = app, tipo
        self.items: list = []
        self.por_clave: dict = {}
        self.actual: dict | None = None
        self.orden = ("estado", False)
        self._construir()

    # ── construcción
    def _construir(self):
        arriba = ttk.Frame(self, style="Fondo.TFrame")
        arriba.pack(fill="x")
        self.lbl_resumen = ttk.Label(arriba, text="Cargando…", style="Resumen.TLabel")
        self.lbl_resumen.pack(side="left")
        self.barra = ttk.Progressbar(arriba, length=220, maximum=100, style="Avance.Horizontal.TProgressbar")
        self.barra.pack(side="left", padx=12)
        self.lbl_faltan = ttk.Label(arriba, text="", style="Falta.TLabel")
        self.lbl_faltan.pack(side="left")

        filtros = ttk.Frame(self, style="Fondo.TFrame")
        filtros.pack(fill="x", pady=(8, 6))
        ttk.Label(filtros, text="🔍", style="Fondo.TLabel").pack(side="left")
        self.v_buscar = tk.StringVar()
        self.v_buscar.trace_add("write", lambda *_: self.refrescar())
        ttk.Entry(filtros, textvariable=self.v_buscar, width=36).pack(side="left", padx=(4, 12))
        self.v_filtro = tk.StringVar(value="Todos")
        for opcion in ("Todos", "Sin imagen", "Con imagen"):
            ttk.Radiobutton(filtros, text=opcion, value=opcion, variable=self.v_filtro, style="Filtro.TRadiobutton",
                            command=self.refrescar).pack(side="left", padx=2)
        self.lbl_mostrando = ttk.Label(filtros, text="", style="Gris.TLabel")
        self.lbl_mostrando.pack(side="right")

        cuerpo = ttk.PanedWindow(self, orient="horizontal")
        cuerpo.pack(fill="both", expand=True)

        # Lista
        izq = ttk.Frame(cuerpo, style="Panel.TFrame")
        cols = COLUMNAS[self.tipo]
        self.tabla = ttk.Treeview(izq, columns=[c for c, _, _ in cols], show="headings", selectmode="browse")
        for c, titulo, ancho in cols:
            self.tabla.heading(c, text=titulo, command=lambda c=c: self._ordenar_por(c))
            self.tabla.column(c, width=ancho, minwidth=50, anchor="w", stretch=c == "nombre")
        self.tabla.tag_configure("falta", background=C["falta"])
        self.tabla.tag_configure("hereda", background=C["hereda"])
        barra = ttk.Scrollbar(izq, orient="vertical", command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=barra.set)
        self.tabla.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.tabla.bind("<<TreeviewSelect>>", lambda _e: self._al_seleccionar())
        self.tabla.bind("<Return>", lambda _e: self.buscar())
        cuerpo.add(izq, weight=3)

        # Detalle
        der = ttk.Frame(cuerpo, style="Panel.TFrame", padding=(12, 8))
        cuerpo.add(der, weight=0)
        marco_foto = tk.Frame(der, width=FOTO_W, height=FOTO_H, bg="#FAFAFA", highlightthickness=1,
                              highlightbackground=C["borde"])
        marco_foto.pack_propagate(False)
        marco_foto.pack()
        self.lbl_foto = tk.Label(marco_foto, bg="#FAFAFA", fg=C["gris"], font=(FUENTE, 10),
                                 text="Elija un renglón de la lista")
        self.lbl_foto.pack(fill="both", expand=True)

        self.lbl_titulo = ttk.Label(der, text="", style="Titulo.TLabel", wraplength=FOTO_W)
        self.lbl_titulo.pack(anchor="w", pady=(6, 0))
        self.lbl_sub = ttk.Label(der, text="", style="GrisPanel.TLabel", wraplength=FOTO_W)
        self.lbl_sub.pack(anchor="w")
        self.lbl_estado = tk.Label(der, text="", font=(FUENTE, 9, "bold"), padx=8, pady=2)
        self.lbl_estado.pack(anchor="w", pady=(4, 0))

        self.v_url_actual = tk.StringVar()
        ttk.Entry(der, textvariable=self.v_url_actual, state="readonly").pack(fill="x", pady=(6, 0))
        fila_url = ttk.Frame(der, style="Panel.TFrame")
        fila_url.pack(fill="x", pady=(4, 0))
        ttk.Button(fila_url, text="Copiar vínculo", command=self._copiar).pack(side="left")
        ttk.Button(fila_url, text="Abrir", command=self._abrir).pack(side="left", padx=4)
        ttk.Button(fila_url, text="🗑 Quitar", command=self._quitar).pack(side="left")
        ttk.Button(fila_url, text="↩ Deshacer", command=self.app.deshacer).pack(side="right")

        # Opción 1: navegador
        caja1 = ttk.LabelFrame(der, text=" 🔎 Buscar en Chrome ", style="Caja.TLabelframe", padding=8)
        caja1.pack(fill="x", pady=(10, 0))
        self.v_consulta = tk.StringVar()
        ttk.Entry(caja1, textvariable=self.v_consulta).pack(fill="x")
        fila = ttk.Frame(caja1, style="Panel.TFrame")
        fila.pack(fill="x", pady=(6, 0))
        ttk.Combobox(fila, textvariable=app_motor(self.app), values=list(MOTORES), state="readonly",
                     width=8).pack(side="left")
        ttk.Button(fila, text="Buscar imagen", style="Accent.TButton", command=self.buscar).pack(side="left", padx=6)
        ttk.Checkbutton(caja1, text="Al capturar, pasar al siguiente sin imagen", variable=self.app.v_auto,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(4, 0))
        ttk.Label(caja1, text="En Chrome haga clic en la foto correcta: se guarda sola.",
                  style="GrisPanel.TLabel").pack(anchor="w")

        # Opción 2: vínculo / archivo
        caja2 = ttk.LabelFrame(der, text=" 🔗 Agregar a mano ", style="Caja.TLabelframe", padding=8)
        caja2.pack(fill="x", pady=(10, 0))
        self.v_url_nueva = tk.StringVar()
        fila2 = ttk.Frame(caja2, style="Panel.TFrame")
        fila2.pack(fill="x")
        ttk.Entry(fila2, textvariable=self.v_url_nueva).pack(side="left", fill="x", expand=True)
        ttk.Button(fila2, text="Guardar vínculo", command=self._guardar_vinculo).pack(side="left", padx=(4, 0))
        ttk.Button(caja2, text="📁 Subir foto (PNG, JPG, WEBP…)", command=self._subir_archivo).pack(fill="x", pady=(6, 0))


    # ── datos
    def poblar(self, items: list):
        self.items = items
        self.por_clave = {it["clave"]: it for it in items}
        self.refrescar()
        self.actualizar_resumen()

    def estado_de(self, item: dict):
        url = self.app.imagenes[self.tipo].get(item["clave"], "")
        if url:
            return "ok", url
        if self.tipo == "productos":
            # Igual que la app: si el producto no tiene foto usa la del primero de sus artículos que tenga
            for art in self.app.catalogo.get("articulos", []):
                if art["prod"] == item["clave"]:
                    u = self.app.imagenes["articulos"].get(art["clave"])
                    if u:
                        return "hereda", u
        return "falta", ""

    def actualizar_resumen(self):
        total = len(self.items)
        faltan = sum(1 for it in self.items if self.estado_de(it)[0] == "falta")
        con = total - faltan
        self.lbl_resumen.configure(text=f"{con:,} de {total:,} {TITULOS[self.tipo][2:].lower()} con imagen")
        self.barra["value"] = 100 * con / total if total else 0
        self.lbl_faltan.configure(text=f"✗ {faltan} sin imagen" if faltan else "✓ Todos tienen imagen",
                                  style="Falta.TLabel" if faltan else "Ok.TLabel")
        self.app.pestanas.tab(self, text=f"{TITULOS[self.tipo]}  ({faltan})" if faltan else TITULOS[self.tipo])

    def _valores(self, it: dict):
        estado = self.estado_de(it)[0]
        if self.tipo == "articulos":
            return (ESTADOS[estado], it["clave"], it["nombre"], it["marca"], it["proveedor"]), estado
        return (ESTADOS[estado], it["clave"], it["nombre"], it["articulos"]), estado

    def _ordenar_por(self, col):
        self.orden = (col, not self.orden[1]) if self.orden[0] == col else (col, False)
        self.refrescar()

    def refrescar(self):
        texto = sin_acentos(self.v_buscar.get().strip())
        filtro = self.v_filtro.get()
        filas = []
        for it in self.items:
            valores, estado = self._valores(it)
            if filtro == "Sin imagen" and estado != "falta":
                continue
            if filtro == "Con imagen" and estado == "falta":
                continue
            if texto and texto not in sin_acentos(" ".join(str(v) for v in valores)):
                continue
            filas.append((it, valores, estado))
        col, desc = self.orden
        i = [c for c, _, _ in COLUMNAS[self.tipo]].index(col)
        prioridad = {"falta": 0, "hereda": 1, "ok": 2}

        def llave(f):
            if col == "estado":
                return (prioridad[f[2]], f[1][2])
            v = f[1][i]
            return (0, int(v), "") if str(v).isdigit() else (1, 0, str(v))
        filas.sort(key=llave, reverse=desc)

        seleccion = self.actual["clave"] if self.actual else None
        self.tabla.delete(*self.tabla.get_children())
        for it, valores, estado in filas:
            self.tabla.insert("", "end", iid=it["clave"], values=valores, tags=(estado,))
        self.lbl_mostrando.configure(text=f"Mostrando {len(filas):,}")
        if seleccion and self.tabla.exists(seleccion):
            self.tabla.selection_set(seleccion)
            self.tabla.see(seleccion)

    def actualizar_fila(self, k: str):
        it = self.por_clave.get(k)
        if it and self.tabla.exists(k):
            valores, estado = self._valores(it)
            self.tabla.item(k, values=valores, tags=(estado,))
        if self.tipo == "articulos":
            self.app.paneles["productos"].refrescar()   # un producto puede tomar la foto de este artículo
            self.app.paneles["productos"].actualizar_resumen()
        self.actualizar_resumen()
        if self.actual and self.actual["clave"] == k:
            self.mostrar(self.actual)

    # ── selección y detalle
    def _al_seleccionar(self):
        sel = self.tabla.selection()
        if sel and sel[0] in self.por_clave:
            self.mostrar(self.por_clave[sel[0]])

    def mostrar(self, it: dict):
        cambio = not self.actual or self.actual["clave"] != it["clave"]
        self.actual = it
        estado, url = self.estado_de(it)
        self.lbl_titulo.configure(text=it["nombre"])
        clave = {"articulos": "ART.", "productos": "PROD.", "proveedores": "CVE"}[self.tipo]
        if self.tipo == "articulos":
            sub = f"{clave} {it['clave']} · {it['marca'] or 'sin marca'} · {it['producto'].capitalize()}\n{it['proveedor']}"
            if it.get("barras") and set(it["barras"]) != {"0"}:
                sub += f"\nCódigo de barras {it['barras']}"
        else:
            n = it["articulos"]
            sub = f"{clave} {it['clave']} · {n} artículo{'s' if n != 1 else ''}"
        self.lbl_sub.configure(text=sub)
        colores = {"ok": ("#E3F1EC", C["verde"]), "hereda": ("#FFF3D6", "#7A5C1E"), "falta": ("#FBE5E8", C["guinda"])}
        fondo, letra = colores[estado]
        self.lbl_estado.configure(text=ESTADOS[estado], bg=fondo, fg=letra)
        self.v_url_actual.set(url)
        if cambio:
            self.v_consulta.set(consulta_para(self.tipo, it))
            self.v_url_nueva.set("")
        self.app.cargar_foto(url, self.lbl_foto, lambda: self.actual is it)

    def siguiente_sin_imagen(self) -> bool:
        filas = self.tabla.get_children()
        if not filas:
            return False
        inicio = filas.index(self.actual["clave"]) + 1 if self.actual and self.actual["clave"] in filas else 0
        for k in list(filas[inicio:]) + list(filas[:inicio]):
            if self.estado_de(self.por_clave[k])[0] == "falta" and (not self.actual or k != self.actual["clave"]):
                self.tabla.selection_set(k)
                self.tabla.see(k)
                self.mostrar(self.por_clave[k])
                return True
        return False

    # ── acciones
    def _necesita_actual(self) -> bool:
        if not self.actual:
            messagebox.showinfo("Elija uno", f"Primero elija un {SINGULAR[self.tipo]} de la lista.")
            return False
        return True

    def buscar(self):
        if self._necesita_actual() and self.v_consulta.get().strip():
            self.app.buscar(self, self.actual, self.v_consulta.get().strip())

    def _guardar_vinculo(self):
        url = self.v_url_nueva.get().strip()
        if not self._necesita_actual():
            return
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("Vínculo", "Pegue un vínculo que empiece con http:// o https://")
            return
        self.app.guardar(self.tipo, self.actual["clave"], url, "Vínculo agregado a mano")

    def _subir_archivo(self):
        if not self._necesita_actual():
            return
        ruta = filedialog.askopenfilename(title="Foto", filetypes=[
            ("Imágenes", "*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff"), ("Todos", "*.*")])
        if ruta:
            self.app.subir_archivo(self.tipo, self.actual, ruta)

    def _quitar(self):
        if self._necesita_actual() and self.app.imagenes[self.tipo].get(self.actual["clave"]):
            if messagebox.askyesno("Quitar imagen", f"¿Quitar la imagen de «{self.actual['nombre']}»?"):
                self.app.guardar(self.tipo, self.actual["clave"], "")

    def _copiar(self):
        self.clipboard_clear()
        self.clipboard_append(self.v_url_actual.get())

    def _abrir(self):
        if self.v_url_actual.get():
            webbrowser.open(nucleo.url_visible(self.v_url_actual.get()))


def app_motor(app):
    return app.v_motor


# ──────────────────────────────────────────────────────────────
# Ventana principal
# ──────────────────────────────────────────────────────────────
class App:
    def __init__(self, raiz: tk.Tk):
        self.raiz = raiz
        self.config = nucleo.leer_config()
        self.cola: queue.Queue = queue.Queue()
        self.nav: Navegador | None = None
        self.objetivo = None            # (panel, item) de la última búsqueda en Chrome
        self.catalogo: dict = {}
        self.imagenes = {t: {} for t in COLUMNAS}
        self.historial: list = []       # (tipo, clave, url anterior) para deshacer
        self.fotos: dict = {}           # url -> imagen PIL reducida
        self.candado_excel = threading.Lock()

        self.v_servidor = tk.StringVar(value=self.config["servidor"])
        self.v_token = tk.StringVar(value=self.config["token"])
        self.v_excel = tk.StringVar(value=self.config["excel"])
        self.v_link_img = tk.StringVar(value=self.config["link_imagenes"])
        self.v_excel_tambien = tk.BooleanVar(value=bool(self.config.get("guardar_en_excel")))
        self.v_motor = tk.StringVar(value="Bing")
        self.v_auto = tk.BooleanVar(value=True)

        self._estilos()
        self._construir()
        self.raiz.after(150, self._sondear)
        self.raiz.protocol("WM_DELETE_WINDOW", self._salir)
        if self.config["servidor"] and self.config["token"]:
            self.recargar()
        else:
            self.pestanas.select(self.p_servidor)
            self.anotar("Escriba la dirección del servidor y la clave, y pulse «Guardar ajustes».")

    def servidor(self) -> nucleo.Servidor:
        return nucleo.Servidor(self.v_servidor.get().strip(), self.v_token.get().strip())

    # ── apariencia
    def _estilos(self):
        self.raiz.configure(bg=C["fondo"])
        s = ttk.Style(self.raiz)
        s.theme_use("clam")
        s.configure(".", font=(FUENTE, 10), background=C["fondo"], foreground=C["texto"])
        s.configure("Fondo.TFrame", background=C["fondo"])
        s.configure("Panel.TFrame", background=C["panel"])
        s.configure("Fondo.TLabel", background=C["fondo"])
        s.configure("Gris.TLabel", background=C["fondo"], foreground=C["gris"])
        s.configure("GrisPanel.TLabel", background=C["panel"], foreground=C["gris"], font=(FUENTE, 9))
        s.configure("Titulo.TLabel", background=C["panel"], font=(FUENTE, 12, "bold"))
        s.configure("Resumen.TLabel", background=C["fondo"], font=(FUENTE, 12, "bold"))
        s.configure("Falta.TLabel", background=C["fondo"], foreground=C["guinda"], font=(FUENTE, 10, "bold"))
        s.configure("Ok.TLabel", background=C["fondo"], foreground=C["verde"], font=(FUENTE, 10, "bold"))
        s.configure("Avance.Horizontal.TProgressbar", troughcolor="#E8E2D8", background=C["verde"],
                    bordercolor=C["borde"], lightcolor=C["verde"], darkcolor=C["verde"])
        s.configure("Treeview", rowheight=26, background=C["panel"], fieldbackground=C["panel"])
        s.configure("Treeview.Heading", font=(FUENTE, 10, "bold"), background="#EFE9E0", relief="flat")
        s.map("Treeview", background=[("selected", C["guinda"])], foreground=[("selected", "white")])
        s.configure("TNotebook", background=C["fondo"], borderwidth=0)
        s.configure("TNotebook.Tab", padding=(14, 6), font=(FUENTE, 10, "bold"))
        s.map("TNotebook.Tab", background=[("selected", C["panel"])], foreground=[("selected", C["guinda"])])
        s.configure("Caja.TLabelframe", background=C["panel"], bordercolor=C["borde"])
        s.configure("Caja.TLabelframe.Label", background=C["panel"], foreground=C["guinda_osc"], font=(FUENTE, 10, "bold"))
        s.configure("Panel.TCheckbutton", background=C["panel"])
        s.configure("Filtro.TRadiobutton", background=C["fondo"])
        s.configure("Accent.TButton", background=C["guinda"], foreground="white", font=(FUENTE, 10, "bold"))
        s.map("Accent.TButton", background=[("active", C["guinda_osc"]), ("disabled", "#C9A3AE")])

    def _construir(self):
        r = self.raiz
        cabeza = tk.Frame(r, bg=C["guinda"], padx=14, pady=10)
        cabeza.pack(fill="x")
        tk.Label(cabeza, text="Abasto Bienestar", bg=C["guinda"], fg="white", font=(FUENTE, 16, "bold")).pack(side="left")
        tk.Label(cabeza, text="  ·  Imágenes y datos de la app", bg=C["guinda"], fg="#F2D6DD",
                 font=(FUENTE, 11)).pack(side="left", pady=(4, 0))
        tk.Button(cabeza, text="⟳  Recargar", command=self.recargar, bg=C["guinda_osc"], fg="white", relief="flat",
                  activebackground="#4A0E26", activeforeground="white", font=(FUENTE, 10, "bold"), padx=10,
                  cursor="hand2").pack(side="right")
        self.lbl_conexion = tk.Label(cabeza, text="● Sin conectar", bg=C["guinda"], fg="#F2D6DD", font=(FUENTE, 10))
        self.lbl_conexion.pack(side="right", padx=12)

        # La bitácora se acomoda antes que las pestañas para que nunca quede fuera de la ventana
        pie = ttk.Frame(r, style="Fondo.TFrame")
        pie.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        self.bitacora = tk.Text(pie, height=3, wrap="word", state="disabled", font=("Consolas", 9), bg="#FBFAF7",
                                relief="flat", highlightthickness=1, highlightbackground=C["borde"])
        self.bitacora.pack(fill="x")

        self.pestanas = ttk.Notebook(r)
        self.pestanas.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.paneles = {}
        for tipo in COLUMNAS:
            self.paneles[tipo] = PanelImagenes(self.pestanas, self, tipo)
            self.pestanas.add(self.paneles[tipo], text=TITULOS[tipo])
        self.p_regiones = self._pestana_regiones()
        self.p_servidor = self._pestana_servidor()


    def _pestana_regiones(self):
        p = ttk.Frame(self.pestanas, style="Fondo.TFrame", padding=12)
        self.pestanas.add(p, text="🗺️ Regiones")
        ttk.Label(p, style="Fondo.TLabel", wraplength=900, justify="left", text=(
            "Las gerencias regionales se editan en la hoja REGIONES del Excel de imágenes: SIGLA, NOMBRE, CLAVE y "
            "UNIDADES (separadas por comas). Las siglas y los estados del mapa son fijos en la app.")).pack(anchor="w")
        cols = nucleo.COLUMNAS_REGIONES
        self.tabla_reg = ttk.Treeview(p, columns=cols, show="headings", height=13)
        for c, ancho in zip(cols, (70, 160, 70, 600)):
            self.tabla_reg.heading(c, text=c)
            self.tabla_reg.column(c, width=ancho, anchor="w", stretch=c == "UNIDADES")
        self.tabla_reg.pack(fill="both", expand=True, pady=8)
        fila = ttk.Frame(p, style="Fondo.TFrame")
        fila.pack(anchor="w")
        ttk.Button(fila, text="Ver las del servidor", command=lambda: self.tarea(
            "Consultando las regiones…", self._regiones_servidor, lambda r: self._mostrar_regiones(*r))).pack(side="left")
        ttk.Button(fila, text="📝 Escribir hoja REGIONES en el Excel", command=self._escribir_hoja).pack(side="left", padx=6)
        ttk.Button(fila, text="⬆ Subir hoja REGIONES al servidor", style="Accent.TButton",
                   command=self._subir_hoja).pack(side="left")
        return p

    def _pestana_servidor(self):
        p = ttk.Frame(self.pestanas, style="Fondo.TFrame", padding=12)
        self.pestanas.add(p, text="⚙️ Servidor")
        campos = [("Dirección del servidor", self.v_servidor, {}),
                  ("Clave de administración (ADMIN_TOKEN)", self.v_token, {"show": "•"}),
                  ("Excel de imágenes (en esta computadora)", self.v_excel, {}),
                  ("Vínculo del Excel de imágenes (OneDrive)", self.v_link_img, {})]
        for i, (texto, var, extra) in enumerate(campos):
            ttk.Label(p, text=texto, style="Fondo.TLabel").grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            ttk.Entry(p, textvariable=var, width=80, **extra).grid(row=i, column=1, sticky="ew", pady=3)
        ttk.Button(p, text="Elegir…", command=lambda: self.v_excel.set(
            filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")]) or self.v_excel.get())).grid(row=2, column=2, padx=4)
        p.columnconfigure(1, weight=1)
        ttk.Checkbutton(p, text="Guardar también cada cambio en ese Excel (columnas IMAGEN y FUENTE)",
                        variable=self.v_excel_tambien).grid(row=4, column=1, sticky="w", pady=(4, 0))

        fila = ttk.Frame(p, style="Fondo.TFrame")
        fila.grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Button(fila, text="💾 Guardar ajustes", style="Accent.TButton", command=self._guardar_ajustes).pack(side="left")
        ttk.Button(fila, text="Estado", command=lambda: self.tarea(
            "Consultando el estado…", lambda: self.servidor().estado(),
            lambda e: self.anotar(nucleo.texto_estado(e)))).pack(side="left", padx=6)
        ttk.Button(fila, text="🔄 Descargar el catálogo ahora", command=lambda: self.tarea(
            "Descargando el catálogo de SharePoint…", lambda: self.servidor().sincronizar(),
            lambda r: (self.anotar(f"✓ Catálogo descargado ({r['bytes'] / 1024:.0f} KB)."), self.recargar()))).pack(side="left")
        ttk.Button(fila, text="Cambiar Excel del catálogo…", command=self._cambiar_link).pack(side="left", padx=6)

        caja = ttk.LabelFrame(p, text=" Carga completa desde el Excel de imágenes ", style="Caja.TLabelframe", padding=10)
        caja.grid(row=6, column=0, columnspan=3, sticky="ew")
        ttk.Label(caja, style="GrisPanel.TLabel", wraplength=850, justify="left", text=(
            "La base de datos del servidor es la que manda. Use esto solo para cargar de golpe lo que está en el "
            "Excel: reemplaza en el servidor las imágenes de las claves que vienen en él.")).pack(anchor="w")
        self.v_reemplazar = tk.BooleanVar(value=False)
        ttk.Checkbutton(caja, text="Además, borrar del servidor las fotos de claves que no estén en el Excel",
                        variable=self.v_reemplazar, style="Panel.TCheckbutton").pack(anchor="w", pady=4)
        f2 = ttk.Frame(caja, style="Panel.TFrame")
        f2.pack(anchor="w")
        ttk.Button(f2, text="⬆ Subir el Excel de esta computadora",
                   command=lambda: self._subir_excel(lambda: self.v_excel.get())).pack(side="left")
        ttk.Button(f2, text="⬇⬆ Descargar el de OneDrive y subirlo",
                   command=lambda: self._subir_excel(lambda: nucleo.descargar_excel_onedrive(self.v_link_img.get().strip()))
                   ).pack(side="left", padx=6)
        return p

    # ── utilidades
    def anotar(self, texto: str):
        self.bitacora.configure(state="normal")
        self.bitacora.insert("end", texto.rstrip() + "\n")
        self.bitacora.see("end")
        self.bitacora.configure(state="disabled")

    def tarea(self, descripcion: str, fn, al_terminar=None, silencioso=False):
        """Corre fn en otro hilo para que la ventana no se congele; al_terminar(resultado) corre en la ventana."""
        if descripcion:
            self.anotar(f"… {descripcion}")

        def correr():
            try:
                resultado = fn()
            except Exception as e:  # noqa: BLE001 — cualquier error se muestra
                self.raiz.after(0, lambda e=e: (self.anotar(f"✗ {e}"), None if silencioso else messagebox.showerror("Error", str(e))))
                return
            if al_terminar:
                self.raiz.after(0, lambda: al_terminar(resultado))

        threading.Thread(target=correr, daemon=True).start()

    def _sondear(self):
        try:
            while True:
                tipo, dato = self.cola.get_nowait()
                if tipo == "pick":
                    self._al_capturar(dato)
                else:
                    self.anotar(dato)
        except queue.Empty:
            pass
        self.raiz.after(150, self._sondear)

    # ── datos
    def recargar(self):
        def hacer():
            srv = self.servidor()
            estado = srv.estado()
            return estado, nucleo.leer_catalogo(srv.catalogo_xlsx()), srv.imagenes()

        def listo(r):
            estado, catalogo, imagenes = r
            self.catalogo = catalogo
            self.imagenes = {t: dict(imagenes.get(t, {})) for t in COLUMNAS}
            for tipo, panel in self.paneles.items():
                panel.poblar(catalogo[tipo])
            c = estado["catalogo"]
            hora = hora_local(c.get("actualizado"))
            self.lbl_conexion.configure(text=f"● Conectado · catálogo del {hora}" + (" · ⚠ error" if c.get("error") else ""),
                                        fg="#BFF0D8")
            faltan = {t: sum(1 for it in self.paneles[t].items if self.paneles[t].estado_de(it)[0] == "falta") for t in COLUMNAS}
            self.anotar(f"✓ Datos cargados. Sin imagen: {faltan['articulos']} artículos, {faltan['productos']} productos, "
                        f"{faltan['proveedores']} proveedores.")
            if c.get("error"):
                self.anotar(f"⚠ Último error al descargar el catálogo: {c['error']}")

        self.lbl_conexion.configure(text="● Conectando…", fg="#F2D6DD")
        self.tarea("Cargando el catálogo y las imágenes del servidor…", hacer, listo)

    def cargar_foto(self, url: str, etiqueta: tk.Label, vigente):
        """Vista previa en otro hilo; solo se pone si el renglón sigue elegido (vigente())."""
        if Image is None:
            etiqueta.configure(image="", text="(instale Pillow para ver la foto)")
            return
        if not url:
            etiqueta.configure(image="", text="Sin imagen\n\nBúsquela en Chrome o agréguela a mano 👇")
            return
        if url in self.fotos:
            self._poner_foto(etiqueta, self.fotos[url])
            return
        etiqueta.configure(image="", text="Cargando la foto…")

        def bajar():
            try:
                r = requests.get(nucleo.url_visible(url), headers=UA, timeout=15)
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content))
                img.thumbnail((FOTO_W - 10, FOTO_H - 10))
                img = img.convert("RGBA")
                if len(self.fotos) > 300:
                    self.fotos.clear()
                self.fotos[url] = img
                self.raiz.after(0, lambda: vigente() and self._poner_foto(etiqueta, img))
            except Exception as e:  # noqa: BLE001
                aviso = f"No se pudo mostrar la foto\n({type(e).__name__})\n\nRevise el vínculo con «Abrir»"
                self.raiz.after(0, lambda: vigente() and etiqueta.configure(image="", text=aviso))

        threading.Thread(target=bajar, daemon=True).start()

    def _poner_foto(self, etiqueta, img):
        etiqueta.foto = ImageTk.PhotoImage(img)
        etiqueta.configure(image=etiqueta.foto, text="")

    # ── cambios
    def guardar(self, tipo: str, k: str, url: str, fuente: str | None = None, despues=None, deshacer=True):
        anterior = self.imagenes[tipo].get(k, "")

        def hacer():
            self.servidor().guardar_imagenes({tipo: {k: url}})

        def listo(_):
            if url:
                self.imagenes[tipo][k] = url
            else:
                self.imagenes[tipo].pop(k, None)
            if deshacer:
                self.historial.append((tipo, k, anterior))
            nombre = self.paneles[tipo].por_clave.get(k, {}).get("nombre", k)
            self.anotar(f"✓ {'Guardada' if url else 'Quitada'} la imagen de {SINGULAR[tipo]} {k} · {nombre}")
            self.paneles[tipo].actualizar_fila(k)
            if self.v_excel_tambien.get():
                self._escribir_excel(tipo, k, url, fuente)
            if despues:
                despues()

        self.tarea("", hacer, listo)

    def _escribir_excel(self, tipo, k, url, fuente):
        ruta = self.v_excel.get().strip()

        def hacer():
            with self.candado_excel:  # uno a la vez: dos guardados juntos dañarían el archivo
                nucleo.escribir_imagen_en_excel(ruta, tipo, k, url, fuente)
        self.tarea("", hacer, lambda _: self.anotar(f"  también en {Path(ruta).name}"), silencioso=True)

    def deshacer(self):
        if not self.historial:
            self.anotar("No hay cambios que deshacer.")
            return
        tipo, k, anterior = self.historial.pop()
        self.anotar(f"↩ Deshaciendo el cambio de {SINGULAR[tipo]} {k}…")
        self.guardar(tipo, k, anterior, deshacer=False)

    def subir_archivo(self, tipo: str, item: dict, ruta: str):
        def hacer():
            return nucleo.subir_foto(self.servidor(), ruta, tipo, item["clave"])
        self.tarea(f"Subiendo {Path(ruta).name}…", hacer,
                   lambda url: self.guardar(tipo, item["clave"], url, f"Archivo subido: {Path(ruta).name}"))

    # ── Chrome
    def buscar(self, panel: PanelImagenes, item: dict, consulta: str):
        if self.nav is None or not self.nav.vivo:
            self.nav = Navegador(self.cola)
            self.nav.start()
            self.anotar("Abriendo Chrome…")
        self.objetivo = (panel, item)
        self.nav.ir(url_busqueda(self.v_motor.get(), consulta))
        self.anotar(f"🔎 {consulta}  →  haga clic en la foto correcta en Chrome")

    def _al_capturar(self, info: dict):
        url = (info.get("img") or "").strip()
        if not url.lower().startswith("http"):
            self.anotar("Se ignoró un clic: la imagen no tiene un vínculo http válido.")
            return
        if not self.objetivo:
            self.anotar("Hizo clic en una foto, pero no hay ningún renglón buscándose.")
            return
        panel, item = self.objetivo
        fuente = fuente_de(self.v_motor.get(), info)

        def despues():
            if self.v_auto.get() and panel.actual is item:
                if panel.siguiente_sin_imagen():
                    self.raiz.after(500, panel.buscar)
                else:
                    self.anotar(f"🎉 Ya no quedan {TITULOS[panel.tipo][2:].lower()} sin imagen en la lista.")

        self.guardar(panel.tipo, item["clave"], url, fuente, despues)

    # ── regiones
    def _regiones_servidor(self):
        regiones = self.servidor().regiones()
        if regiones:
            return regiones, "del servidor"
        return [{"sigla": s, "nombre": n, "clave": c, "unidades": u} for s, n, c, u in nucleo.REGIONES_APP], \
            "el servidor no tiene: se muestran las de la app"

    def _mostrar_regiones(self, regiones, origen):
        self.tabla_reg.delete(*self.tabla_reg.get_children())
        for r in regiones:
            self.tabla_reg.insert("", "end", values=(r["sigla"], r["nombre"], r["clave"], ", ".join(r["unidades"])))
        self.anotar(f"✓ {len(regiones)} regiones ({origen}).")

    def _escribir_hoja(self):
        def hacer():
            regiones, origen = self._regiones_servidor()
            nucleo.escribir_regiones_en_excel(self.v_excel.get(), regiones)
            return regiones, f"{origen}; escritas en la hoja REGIONES"
        self.tarea("Escribiendo la hoja REGIONES…", hacer, lambda r: self._mostrar_regiones(*r))

    def _subir_hoja(self):
        def hacer():
            regiones = nucleo.leer_regiones(self.v_excel.get())
            self.servidor().guardar_regiones(regiones)
            return regiones, "de la hoja REGIONES, guardadas en el servidor"
        self.tarea("Subiendo la hoja REGIONES…", hacer, lambda r: self._mostrar_regiones(*r))

    # ── servidor
    def _guardar_ajustes(self):
        nucleo.guardar_config({**self.config, "servidor": self.v_servidor.get().strip(), "token": self.v_token.get().strip(),
                               "excel": self.v_excel.get().strip(), "link_imagenes": self.v_link_img.get().strip(),
                               "guardar_en_excel": self.v_excel_tambien.get()})
        self.anotar(f"✓ Ajustes guardados en {nucleo.ARCHIVO_CONFIG.name}.")
        self.recargar()

    def _cambiar_link(self):
        nuevo = tk.simpledialog.askstring("Excel del catálogo", "Vínculo para compartir del nuevo Excel del catálogo:",
                                          parent=self.raiz)
        if nuevo:
            self.tarea("Probando el vínculo nuevo…", lambda: self.servidor().cambiar_link_catalogo(nuevo.strip()),
                       lambda r: (self.anotar(f"✓ Vínculo cambiado; Excel descargado ({r['bytes'] / 1024:.0f} KB)."),
                                  self.recargar()))

    def _subir_excel(self, ruta_fn):
        if self.v_reemplazar.get() and not messagebox.askyesno(
                "Confirmar", "Se borrarán del servidor las fotos de las claves que no estén en el Excel, incluidas las "
                             "que agregó en este programa. ¿Continuar?"):
            return

        def hacer():
            mapas = nucleo.leer_imagenes(ruta_fn())
            # Sin reemplazar, las celdas IMAGEN vacías no borran lo que ya tiene el servidor
            if not self.v_reemplazar.get():
                mapas = {t: {k: u for k, u in m.items() if u} for t, m in mapas.items()}
            return mapas, self.servidor().guardar_imagenes(mapas, self.v_reemplazar.get())
        self.tarea("Subiendo las imágenes del Excel…", hacer,
                   lambda r: (self.anotar("✓ " + nucleo.texto_subida(*r)), self.recargar()))

    def _salir(self):
        if self.nav and self.nav.vivo:
            self.nav.cerrar()
        self.raiz.destroy()


def ventana():
    raiz = tk.Tk()
    raiz.title("Administración · Abasto Bienestar")
    raiz.geometry("1300x800")
    raiz.minsize(1050, 700)
    try:
        raiz.state("zoomed")  # maximizada: el panel de la derecha necesita todo el alto
    except tk.TclError:
        pass
    App(raiz)
    raiz.mainloop()
