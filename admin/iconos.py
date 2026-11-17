# -*- coding: utf-8 -*-
"""
Íconos de la ventana, de la librería Lucide (https://lucide.dev, licencia ISC: fuentes/LICENSE).

Se usa su fuente de íconos (fuentes/lucide.ttf) y su índice de nombres (fuentes/info.json), que vienen junto a este
archivo: no hay que instalar nada. Cada ícono se dibuja con Pillow en el color y tamaño que pide la ventana.
Si falta la fuente, se usa la de íconos de Windows (Segoe MDL2); sin ninguna, los botones quedan solo con texto.
"""

import json
import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:  # sin Pillow: sin íconos
    Image = None

FUENTES = Path(__file__).resolve().parent / "fuentes"

# Nombre en la ventana -> nombre del ícono en Lucide (los nombres están en https://lucide.dev/icons)
LUCIDE = {
    "articulos": "package", "productos": "tag", "proveedores": "building-2", "regiones": "map", "servidor": "settings",
    "buscar": "search", "vinculo": "link", "foto": "image", "borrar": "trash-2", "deshacer": "undo-2",
    "recargar": "refresh-cw", "sincronizar": "refresh-cw", "guardar": "save", "copiar": "copy", "abrir": "external-link",
    "subir": "upload", "descargar": "download", "editar": "pencil", "alerta": "triangle-alert", "check": "circle-check",
    "carpeta": "folder-open", "revisar": "scan-search", "siguiente": "arrow-right", "excel": "sheet",
    "rota": "image-off", "error": "circle-x", "ver": "eye",
}

# Respaldo: fuente de íconos de Windows 10
SEGOE = {
    "articulos": 0xE7B8, "productos": 0xE8EC, "proveedores": 0xEC06, "regiones": 0xE826, "servidor": 0xE713,
    "buscar": 0xE721, "vinculo": 0xE71B, "foto": 0xEB9F, "borrar": 0xE74D, "deshacer": 0xE7A7,
    "recargar": 0xE72C, "sincronizar": 0xE895, "guardar": 0xE74E, "copiar": 0xE8C8, "abrir": 0xE8A7,
    "subir": 0xE898, "descargar": 0xE896, "editar": 0xE70F, "alerta": 0xE7BA, "check": 0xE73E,
    "carpeta": 0xE8B7, "revisar": 0xE9D5, "siguiente": 0xE72A, "excel": 0xE9F9, "rota": 0xE783,
    "error": 0xEA39, "ver": 0xE890,
}


def _lucide():
    """(ruta de la fuente, {nombre: carácter}) de Lucide, o None si no está."""
    try:
        indice = json.loads((FUENTES / "info.json").read_text(encoding="utf-8"))
        ruta = FUENTES / "lucide.ttf"
        if not ruta.exists():
            return None
        # encodedCode viene como "\e12d"
        return str(ruta), {n: chr(int(d["encodedCode"].lstrip("\\"), 16)) for n, d in indice.items()}
    except (OSError, ValueError, KeyError):
        return None


def _segoe():
    for nombre in ("segmdl2.ttf", "SegoeIcons.ttf"):
        ruta = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / nombre
        if ruta.exists():
            return str(ruta), {k: chr(v) for k, v in SEGOE.items()}
    return None


class Iconos:
    """Iconos.get("buscar", "#9B2247", 16) -> PhotoImage (o None). Guarda las imágenes para que Tk no las pierda."""

    def __init__(self):
        self._ruta, self._glifos, self.libreria = None, {}, "ninguna"
        if Image:
            lucide = _lucide()
            if lucide:
                ruta, todos = lucide
                self._ruta, self.libreria = ruta, "Lucide"
                self._glifos = {k: todos[v] for k, v in LUCIDE.items() if v in todos}
            elif segoe := _segoe():
                self._ruta, self._glifos = segoe
                self.libreria = "Segoe MDL2"
        self._cache = {}

    def get(self, nombre: str, color: str = "#3A3A3A", tam: int = 16):
        if not self._ruta or nombre not in self._glifos:
            return None
        llave = (nombre, color, tam)
        if llave not in self._cache:
            escala = 4  # se dibuja grande y se reduce: bordes suaves
            lado = tam * escala
            fuente = ImageFont.truetype(self._ruta, int(lado * 0.86))
            img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
            dibujo = ImageDraw.Draw(img)
            glifo = self._glifos[nombre]
            x0, y0, x1, y1 = dibujo.textbbox((0, 0), glifo, font=fuente)
            dibujo.text(((lado - (x1 - x0)) / 2 - x0, (lado - (y1 - y0)) / 2 - y0), glifo, font=fuente, fill=color)
            self._cache[llave] = ImageTk.PhotoImage(img.resize((tam, tam), Image.LANCZOS))
        return self._cache[llave]

    def punto(self, color: str, tam: int = 10):
        """Círculo de color (indicador de conexión y de estado)."""
        if not Image:
            return None
        llave = ("punto", color, tam)
        if llave not in self._cache:
            img = Image.new("RGBA", (tam * 4, tam * 4), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((4, 4, tam * 4 - 4, tam * 4 - 4), fill=color)
            self._cache[llave] = ImageTk.PhotoImage(img.resize((tam, tam), Image.LANCZOS))
        return self._cache[llave]
