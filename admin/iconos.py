# -*- coding: utf-8 -*-
"""
Íconos de la ventana, dibujados con la fuente de íconos de Windows (Segoe MDL2 Assets, la de los menús de Windows 10)
para que se vean como los del sistema. Si la fuente o Pillow no están, los botones quedan solo con texto.
"""

import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:  # sin Pillow: sin íconos
    Image = None

GLIFOS = {
    "articulos": 0xE7B8, "productos": 0xE8EC, "proveedores": 0xEC06, "regiones": 0xE826, "servidor": 0xE713,
    "buscar": 0xE721, "vinculo": 0xE71B, "foto": 0xEB9F, "borrar": 0xE74D, "deshacer": 0xE7A7,
    "recargar": 0xE72C, "sincronizar": 0xE895, "guardar": 0xE74E, "copiar": 0xE8C8, "abrir": 0xE8A7,
    "subir": 0xE898, "descargar": 0xE896, "editar": 0xE70F, "alerta": 0xE7BA, "check": 0xE73E,
    "carpeta": 0xE8B7, "revisar": 0xE9D5, "siguiente": 0xE72A, "excel": 0xE9F9, "rota": 0xE783,
    "error": 0xEA39, "ver": 0xE890,
}


def _fuente():
    for nombre in ("segmdl2.ttf", "SegoeIcons.ttf"):
        ruta = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / nombre
        if ruta.exists():
            return str(ruta)
    return None


class Iconos:
    """Iconos.get("buscar", "#9B2247", 16) -> PhotoImage (o None). Guarda las imágenes para que Tk no las pierda."""

    def __init__(self):
        self._ruta = _fuente() if Image else None
        self._cache = {}

    def get(self, nombre: str, color: str = "#3A3A3A", tam: int = 16):
        if not self._ruta or nombre not in GLIFOS:
            return None
        llave = (nombre, color, tam)
        if llave not in self._cache:
            escala = 4  # se dibuja grande y se reduce: bordes suaves
            lado = tam * escala
            fuente = ImageFont.truetype(self._ruta, int(lado * 0.82))
            img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
            dibujo = ImageDraw.Draw(img)
            glifo = chr(GLIFOS[nombre])
            x0, y0, x1, y1 = dibujo.textbbox((0, 0), glifo, font=fuente)
            dibujo.text(((lado - (x1 - x0)) / 2 - x0, (lado - (y1 - y0)) / 2 - y0), glifo, font=fuente, fill=color)
            self._cache[llave] = ImageTk.PhotoImage(img.resize((tam, tam), Image.LANCZOS))
        return self._cache[llave]

    def punto(self, color: str, tam: int = 10):
        """Círculo de color (indicador de conexión)."""
        if not Image:
            return None
        llave = ("punto", color, tam)
        if llave not in self._cache:
            img = Image.new("RGBA", (tam * 4, tam * 4), (0, 0, 0, 0))
            ImageDraw.Draw(img).ellipse((4, 4, tam * 4 - 4, tam * 4 - 4), fill=color)
            self._cache[llave] = ImageTk.PhotoImage(img.resize((tam, tam), Image.LANCZOS))
        return self._cache[llave]
