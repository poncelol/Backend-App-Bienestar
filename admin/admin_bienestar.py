#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Administración del servidor de Abasto Bienestar (Cloudflare).

Cambia lo que la app toma del servidor, sin publicar una versión nueva de la app:
  · Imágenes de proveedores (CVE), productos (PROD.) y artículos (ART.): desde el Excel interno de imágenes
    (hojas PROVEEDORES, PRODUCTOS y ARTICULOS, columna IMAGEN) o una por una, con un vínculo o con un archivo
    de foto que se sube al servidor.
  · Gerencias regionales (nombre, clave y unidades operativas): hoja REGIONES del mismo Excel.
  · Vínculo del Excel del catálogo, y descargarlo ya en vez de esperar los 15 minutos.

Instalación (una sola vez):   pip install requests openpyxl pillow truststore
Uso con ventana:              python admin_bienestar.py
Uso sin ventana (p. ej. desde el Programador de tareas de Windows):
    python admin_bienestar.py --estado
    python admin_bienestar.py --subir-imagenes [ruta.xlsx] [--reemplazar]
    python admin_bienestar.py --subir-regiones [ruta.xlsx]
    python admin_bienestar.py --sincronizar
    python admin_bienestar.py --link-catalogo "https://….sharepoint.com/:x:/g/…"

La dirección del servidor y la clave (ADMIN_TOKEN) se guardan en admin_config.json, junto a este archivo.
Cierre el Excel antes de escribir en él: Excel bloquea el archivo mientras está abierto.
"""

import argparse
import io
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

import requests
from openpyxl import load_workbook

try:
    # Usa los certificados de Windows: en redes con proxy que revisa HTTPS (como la de la oficina) Python no confía
    # en el certificado del proxy y falla con CERTIFICATE_VERIFY_FAILED. pip install truststore
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

AQUI = Path(__file__).resolve().parent
ARCHIVO_CONFIG = AQUI / "admin_config.json"


def carpeta_app() -> Path:
    """Proyecto de la app (de ahí se toman datos/ y src/data/vinculos.ts): "app" en admin_config.json o, si no,
    la carpeta BienestarApp junto a este repositorio (App Movil/BienestarApp)."""
    try:
        ruta = json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8")).get("app")
        if ruta:
            return Path(ruta)
    except (OSError, ValueError):
        pass
    return AQUI.parent.parent / "BienestarApp"


RAIZ = carpeta_app()

# Hoja del Excel -> (tipo en el servidor, columna con la clave)
HOJAS = {"PROVEEDORES": ("proveedores", "CVE"), "PRODUCTOS": ("productos", "PROD."), "ARTICULOS": ("articulos", "ART.")}
TIPO_A_HOJA = {tipo: hoja for hoja, (tipo, _) in HOJAS.items()}
HOJA_REGIONES = "REGIONES"
COLUMNAS_REGIONES = ["SIGLA", "NOMBRE", "CLAVE", "UNIDADES"]

# Las de la app (BienestarApp/src/data/abasto.ts), por si el servidor aún no tiene regiones
REGIONES_APP = [
    ("NOE", "Noroeste", 41, ["Sinaloa", "Baja California", "Baja California Sur", "Sonora"]),
    ("NOR", "Norte", 42, ["Chihuahua", "Durango", "Coahuila"]),
    ("NC", "Norte Centro", 43, ["Zacatecas", "San Luis Potosí", "Aguascalientes", "Nuevo León"]),
    ("GLF", "Golfo", 44, ["Oficina Central Xalapa", "Acayucan", "Orizaba", "Poza Rica", "Tamaulipas"]),
    ("OCC", "Occidente", 45, ["Jalisco", "Nayarit", "Colima"]),
    ("CEN", "Centro", 46, ["Hidalgo", "Querétaro", "Guanajuato"]),
    ("OAX", "Oaxaca", 47, ["Oaxaca", "Istmo", "Mixteca"]),
    ("MET", "Metropolitana", 48, ["Estado de México", "Morelos"]),
    ("PEN", "Peninsular", 49, ["Yucatán", "Campeche", "Quintana Roo"]),
    ("STE", "Sureste", 50, ["Tabasco", "Tuxtla Gutiérrez", "Tapachula"]),
    ("PSR", "Pacífico Sur", 51, ["Michoacán", "Guerrero"]),
    ("PUE", "Puebla", 52, ["Puebla", "Tlaxcala"]),
]
NAVEGADOR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# Configuración
def excel_por_defecto() -> str:
    """El mismo Excel que usa `npm run imagenes`: datos/config.json (excelImagenes) o datos/imagenes.xlsx."""
    config = RAIZ / "datos" / "config.json"
    try:
        ruta = json.loads(config.read_text(encoding="utf-8")).get("excelImagenes")
        if ruta:
            return ruta
    except (OSError, ValueError):
        pass
    return str(RAIZ / "datos" / "imagenes.xlsx")


def link_imagenes_app() -> str:
    """Vínculo del Excel de imágenes en OneDrive: datos/config.json (linkImagenes) o LINK_IMAGENES de
    src/data/vinculos.ts (vacío cuando la app ya no lo trae)."""
    try:
        ruta = json.loads((RAIZ / "datos" / "config.json").read_text(encoding="utf-8")).get("linkImagenes")
        if ruta:
            return ruta
    except (OSError, ValueError):
        pass
    try:
        texto = (RAIZ / "src" / "data" / "vinculos.ts").read_text(encoding="utf-8")
        m = re.search(r"LINK_IMAGENES\s*=\s*'([^']*)'", texto)
        return m.group(1) if m else ""
    except OSError:
        return ""


def leer_config() -> dict:
    config = {"servidor": "", "token": "", "excel": excel_por_defecto(), "link_imagenes": link_imagenes_app()}
    try:
        config.update({k: v for k, v in json.loads(ARCHIVO_CONFIG.read_text(encoding="utf-8")).items() if v})
    except (OSError, ValueError):
        pass
    return config


def guardar_config(config: dict):
    ARCHIVO_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# Servidor
class Servidor:
    def __init__(self, url: str, token: str):
        if not url:
            raise ValueError("Falta la dirección del servidor (pestaña Servidor o admin_config.json).")
        self.url = url.rstrip("/")
        self.token = token

    def _pedir(self, metodo, ruta, admin=False, **kw):
        encabezados = kw.pop("headers", {})
        if admin:
            if not self.token:
                raise ValueError("Falta la clave de administración (ADMIN_TOKEN).")
            encabezados["Authorization"] = f"Bearer {self.token}"
        try:
            r = requests.request(metodo, self.url + ruta, headers=encabezados, timeout=120, **kw)
        except requests.RequestException as e:
            raise RuntimeError(f"No hubo conexión con el servidor: {e}") from e
        try:
            datos = r.json()
        except ValueError:
            datos = None
        if not r.ok:
            detalle = datos.get("error") if isinstance(datos, dict) else r.text[:200]
            raise RuntimeError(f"El servidor respondió {r.status_code}: {detalle}")
        return datos

    def estado(self):
        """Con la clave se ve también el vínculo del catálogo; sin ella, el estado público."""
        if self.token:
            return self._pedir("GET", "/api/admin/estado", admin=True)
        return self._pedir("GET", "/api/estado")

    def imagenes(self):
        return self._pedir("GET", "/api/imagenes")

    def regiones(self):
        return self._pedir("GET", "/api/regiones")

    def guardar_imagenes(self, mapas: dict, reemplazar=False):
        return self._pedir("PUT", "/api/admin/imagenes", admin=True, json={**mapas, "reemplazar": reemplazar})

    def guardar_regiones(self, regiones: list):
        return self._pedir("PUT", "/api/admin/regiones", admin=True, json=regiones)

    def sincronizar(self):
        return self._pedir("POST", "/api/admin/sincronizar", admin=True)

    def cambiar_link_catalogo(self, link: str):
        return self._pedir("PUT", "/api/admin/config", admin=True, json={"link_catalogo": link})

    def catalogo_xlsx(self) -> bytes:
        """El Excel del catálogo tal como lo guarda el servidor."""
        try:
            r = requests.get(self.url + "/api/catalogo", timeout=120)
        except requests.RequestException as e:
            raise RuntimeError(f"No hubo conexión con el servidor: {e}") from e
        if not r.ok or not r.content.startswith(b"PK"):
            raise RuntimeError(f"El servidor no entregó el catálogo (HTTP {r.status_code}).")
        return r.content

    def subir_foto(self, nombre: str, contenido: bytes, tipo_mime: str):
        return self._pedir("PUT", f"/api/admin/fotos/{nombre}", admin=True, data=contenido,
                           headers={"Content-Type": tipo_mime})


# Excel
def clave(v) -> str:
    """653.0 -> "653", como en la app."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return re.sub(r"\.0$", "", s)


def encabezados(ws) -> dict:
    fila = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return {str(c).strip().upper(): i for i, c in enumerate(fila) if c is not None}


def leer_imagenes(ruta: str) -> dict:
    """{tipo: {clave: url}} de las hojas PROVEEDORES, PRODUCTOS y ARTICULOS. Una IMAGEN vacía se manda como ""
    para que el servidor quite la foto que tenía."""
    libro = load_workbook(ruta, read_only=True, data_only=True)
    try:
        salida = {}
        for hoja, (tipo, llave) in HOJAS.items():
            if hoja not in libro.sheetnames:
                continue
            ws = libro[hoja]
            col = encabezados(ws)
            if llave not in col or "IMAGEN" not in col:
                raise ValueError(f"La hoja {hoja} necesita las columnas {llave} e IMAGEN.")
            mapa = {}
            for fila in ws.iter_rows(min_row=2, values_only=True):
                k = clave(fila[col[llave]] if col[llave] < len(fila) else None)
                if k:
                    url = fila[col["IMAGEN"]] if col["IMAGEN"] < len(fila) else None
                    mapa[k] = str(url).strip() if url else ""
            salida[tipo] = mapa
        if not salida:
            raise ValueError("El Excel no tiene ninguna de las hojas PROVEEDORES, PRODUCTOS o ARTICULOS.")
        return salida
    finally:
        libro.close()


def escribir_imagen_en_excel(ruta: str, tipo: str, k: str, url: str, fuente: str | None = None):
    """Pone la URL en la columna IMAGEN (y la FUENTE, si la hoja la tiene) de la fila de esa clave, o agrega la fila."""
    hoja = TIPO_A_HOJA[tipo]
    llave = HOJAS[hoja][1]
    libro = load_workbook(ruta)
    if hoja not in libro.sheetnames:
        raise ValueError(f"El Excel no tiene la hoja {hoja}.")
    ws = libro[hoja]
    col = encabezados(ws)
    if llave not in col or "IMAGEN" not in col:
        raise ValueError(f"La hoja {hoja} necesita las columnas {llave} e IMAGEN.")
    for fila in range(2, ws.max_row + 1):
        if clave(ws.cell(fila, col[llave] + 1).value) == k:
            break
    else:
        fila = ws.max_row + 1
        ws.cell(fila, col[llave] + 1, k)
    ws.cell(fila, col["IMAGEN"] + 1, url or None)
    if "FUENTE" in col:
        ws.cell(fila, col["FUENTE"] + 1, (fuente or None) if url else None)
    guardar_libro(libro, ruta)


def _norm(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().upper()


def leer_catalogo(contenido: bytes) -> dict:
    """Artículos, productos y proveedores del Excel del catálogo (hoja VIGENTES), con las mismas reglas que la app:
    la fila de encabezados se busca sola y cada artículo cuenta una vez por ART.
    Devuelve {tipo: [ {clave, nombre, ...} ]}."""
    libro = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    try:
        ws = libro["VIGENTES"] if "VIGENTES" in libro.sheetnames else libro[libro.sheetnames[0]]
        filas = ws.iter_rows(values_only=True)
        col = None
        for _, fila in zip(range(30), filas):
            if any(_norm(c) in ("NEGOCIACION", "CVE") for c in fila):
                col = {}
                for i, c in enumerate(fila):
                    col.setdefault(_norm(c), i)
                break
        if col is None:
            raise ValueError("No encontré la fila de encabezados del catálogo.")
        val = lambda f, nombre: f[col[nombre]] if nombre in col and col[nombre] < len(f) else None  # noqa: E731

        articulos, prods, provs = {}, {}, {}
        for f in filas:
            art = clave(val(f, "ART."))
            if not art:
                continue
            cve, prod = clave(val(f, "CVE")), clave(val(f, "PROD."))
            proveedor = _norm(val(f, "PROVEEDOR"))
            producto = _norm(val(f, "PRODUCTOS")) or "SIN CLASIFICAR"
            prod = prod or producto
            if art not in articulos:
                articulos[art] = {"clave": art, "nombre": _norm(val(f, "ARTICULOS")) or "SIN NOMBRE",
                                  "marca": _norm(val(f, "MARCA")), "proveedor": proveedor, "producto": producto,
                                  "prod": prod, "barras": clave(val(f, "CODIGO BARRAS"))}
            p = prods.setdefault(prod, {"clave": prod, "nombres": {}, "arts": set()})
            p["nombres"][producto] = p["nombres"].get(producto, 0) + 1
            p["arts"].add(art)
            v = provs.setdefault(cve, {"clave": cve, "nombre": proveedor, "arts": set()})
            v["arts"].add(art)
        productos = [{"clave": k, "nombre": sorted(p["nombres"].items(), key=lambda x: (-x[1], x[0]))[0][0],
                      "articulos": len(p["arts"])} for k, p in prods.items()]
        proveedores = [{"clave": k, "nombre": v["nombre"], "articulos": len(v["arts"])} for k, v in provs.items() if k]
        return {"articulos": list(articulos.values()), "productos": productos, "proveedores": proveedores}
    finally:
        libro.close()


def url_visible(link: str) -> str:
    """Vínculo de la columna IMAGEN -> URL que se puede descargar (como urlImagen en la app)."""
    link = (link or "").strip()
    if re.search(r"sharepoint\.com", link, re.I):
        return url_descarga(link)
    if re.search(r"1drv\.ms|onedrive\.live\.com", link, re.I):
        import base64
        codigo = base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{codigo}/root/content"
    return link


def separar_unidades(texto) -> list:
    return [u.strip() for u in re.split(r"[,;·\n]", str(texto or "")) if u.strip()]


def leer_regiones(ruta: str) -> list:
    libro = load_workbook(ruta, read_only=True, data_only=True)
    try:
        if HOJA_REGIONES not in libro.sheetnames:
            raise ValueError("El Excel no tiene la hoja REGIONES: créela con «Escribir hoja REGIONES en el Excel».")
        ws = libro[HOJA_REGIONES]
        col = encabezados(ws)
        faltan = [c for c in COLUMNAS_REGIONES if c not in col]
        if faltan:
            raise ValueError(f"A la hoja REGIONES le faltan las columnas: {', '.join(faltan)}.")
        regiones = []
        for n, fila in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            val = lambda c: fila[col[c]] if col[c] < len(fila) else None  # noqa: E731
            sigla = str(val("SIGLA") or "").strip().upper()
            if not sigla:
                continue
            try:
                numero = int(float(val("CLAVE")))
            except (TypeError, ValueError):
                raise ValueError(f"REGIONES fila {n}: la CLAVE de {sigla} debe ser un número.")
            regiones.append({"sigla": sigla, "nombre": str(val("NOMBRE") or "").strip(), "clave": numero,
                             "unidades": separar_unidades(val("UNIDADES"))})
        return regiones
    finally:
        libro.close()


def escribir_regiones_en_excel(ruta: str, regiones: list):
    libro = load_workbook(ruta)
    if HOJA_REGIONES in libro.sheetnames:
        del libro[HOJA_REGIONES]
    ws = libro.create_sheet(HOJA_REGIONES)
    ws.append(COLUMNAS_REGIONES)
    for r in regiones:
        ws.append([r["sigla"], r["nombre"], r["clave"], ", ".join(r["unidades"])])
    for letra, ancho in zip("ABCD", (8, 18, 8, 70)):
        ws.column_dimensions[letra].width = ancho
    guardar_libro(libro, ruta)


def guardar_libro(libro, ruta: str):
    try:
        libro.save(ruta)
    except PermissionError:
        raise RuntimeError(f"No se pudo guardar {Path(ruta).name}: ciérrelo en Excel y vuelva a intentar.")


def url_descarga(link: str) -> str:
    """Vínculo para compartir de SharePoint -> descarga directa (igual que urlDescarga en src/data/vinculos.ts)."""
    m = re.match(r"^(https://[^/]+\.sharepoint\.com)/:[a-z]:/[a-z]/(personal|sites)/([^/]+)/([A-Za-z0-9_-]+)", link.strip(), re.I)
    if m:
        return f"{m[1]}/{m[2]}/{m[3]}/_layouts/15/download.aspx?share={m[4]}"
    return link.strip() + ("&" if "?" in link else "?") + "download=1"


def descargar_excel_onedrive(link: str) -> str:
    if not link:
        raise ValueError("No hay vínculo del Excel de imágenes (LINK_IMAGENES).")
    r = requests.get(url_descarga(link), headers=NAVEGADOR, timeout=120)
    r.raise_for_status()
    if not r.content.startswith(b"PK"):
        raise RuntimeError("El vínculo no devolvió un Excel: revise que permita ver el archivo sin iniciar sesión.")
    destino = Path(tempfile.gettempdir()) / "imagenes_onedrive.xlsx"
    destino.write_bytes(r.content)
    return str(destino)


# Fotos
def preparar_foto(ruta: str, max_px=800):
    """Acepta PNG, JPG, WEBP, GIF, BMP o TIFF y la deja ligera para no llenar el almacenamiento del servidor: a lo más
    800 px de lado (lo más grande que muestra la app es la ficha, de 800) y en WEBP, que pesa unos 40-80 KB y conserva
    la transparencia de los logotipos. Así caben miles de fotos en el 1 GB gratuito."""
    from PIL import Image  # solo hace falta para subir archivos

    with Image.open(ruta) as img:
        img.thumbnail((max_px, max_px))
        transparente = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if transparente else "RGB")
        salida = io.BytesIO()
        img.save(salida, "WEBP", quality=80, method=6)
        return salida.getvalue(), "image/webp", "webp"


def revisar_imagen(url: str) -> tuple[bool, str]:
    """¿La foto se puede ver? Pide solo el inicio del archivo y revisa que sea una imagen. (ok, motivo)."""
    try:
        r = requests.get(url_visible(url), headers=NAVEGADOR, timeout=12, stream=True, allow_redirects=True)
    except requests.exceptions.SSLError:
        return False, "certificado del sitio inválido"
    except requests.exceptions.Timeout:
        return False, "el sitio tardó demasiado"
    except requests.RequestException:
        return False, "no hubo conexión con el sitio"
    try:
        if r.status_code != 200:
            return False, {403: "el sitio no permite usarla (403)", 404: "ya no existe (404)"}.get(
                r.status_code, f"el sitio respondió {r.status_code}")
        inicio = next(r.iter_content(64), b"")
        firmas = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")
        if inicio.startswith(firmas) or inicio[8:12] in (b"WEBP", b"avif", b"heic") or inicio.lstrip().startswith(b"<svg"):
            return True, ""
        if (r.headers.get("Content-Type") or "").startswith("image/"):
            return True, ""
        return False, "el vínculo es una página, no una imagen"
    except requests.RequestException:
        return False, "se cortó la descarga"
    finally:
        r.close()


def subir_foto(srv: Servidor, ruta: str, tipo: str, k: str) -> str:
    contenido, mime, ext = preparar_foto(ruta)
    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", f"{tipo}-{k}.{ext}")
    return srv.subir_foto(nombre, contenido, mime)["url"]


# Resúmenes
def texto_estado(e: dict) -> str:
    c = e["catalogo"]
    lineas = [
        f"Catálogo: actualizado {c['actualizado'] or '—'} · revisado {c['revisado'] or '—'} · {c['bytes'] / 1024:.0f} KB",
    ]
    if "vinculo" in c:
        lineas.append(f"  Vínculo: {c['vinculo'] or 'falta (pestaña Servidor › Cambiar Excel del catálogo)'}")
    if c.get("error"):
        lineas.append(f"  Último error: {c['error']}")
    i = e["imagenes"]
    lineas.append(f"Imágenes: {i['proveedores']} proveedores · {i['productos']} productos · {i['articulos']} artículos")
    lineas.append(f"Regiones en el servidor: {e['regiones'] or 'ninguna (la app usa las suyas)'}")
    return "\n".join(lineas)


def texto_subida(mapas: dict, r: dict) -> str:
    partes = [f"{len([u for u in m.values() if u])} {t} con foto" for t, m in mapas.items()]
    extra = " (se borraron las que ya no están en el Excel)" if r.get("reemplazar") else ""
    return f"Imágenes guardadas: {', '.join(partes)}{extra}."


# Línea de comandos
def linea_de_comandos(args) -> int:
    config = leer_config()
    srv = Servidor(config["servidor"], config["token"])
    if args.link_catalogo:
        r = srv.cambiar_link_catalogo(args.link_catalogo)
        print(f"Vínculo del catálogo cambiado; Excel descargado ({r['bytes'] / 1024:.0f} KB).")
    if args.sincronizar:
        r = srv.sincronizar()
        print(f"Catálogo descargado de nuevo ({r['bytes'] / 1024:.0f} KB).")
    if args.subir_imagenes is not None:
        ruta = args.subir_imagenes or config["excel"]
        mapas = leer_imagenes(ruta)
        print(texto_subida(mapas, srv.guardar_imagenes(mapas, args.reemplazar)))
    if args.subir_regiones is not None:
        regiones = leer_regiones(args.subir_regiones or config["excel"])
        srv.guardar_regiones(regiones)
        print(f"Regiones guardadas: {len(regiones)}.")
    if args.estado:
        print(texto_estado(srv.estado()))
    return 0


def main():
    p = argparse.ArgumentParser(description="Administración del servidor de Abasto Bienestar.")
    p.add_argument("--estado", action="store_true", help="muestra el estado del servidor")
    p.add_argument("--sincronizar", action="store_true", help="descarga el Excel del catálogo ahora")
    p.add_argument("--subir-imagenes", nargs="?", const="", metavar="EXCEL", help="sube las imágenes del Excel")
    p.add_argument("--reemplazar", action="store_true", help="con --subir-imagenes: borra las claves que no estén")
    p.add_argument("--subir-regiones", nargs="?", const="", metavar="EXCEL", help="sube la hoja REGIONES")
    p.add_argument("--link-catalogo", metavar="URL", help="cambia el vínculo del Excel del catálogo")
    args = p.parse_args()
    if not any([args.estado, args.sincronizar, args.subir_imagenes is not None, args.subir_regiones is not None,
                args.link_catalogo]):
        from interfaz import ventana  # noqa: PLC0415 — solo hace falta tkinter si se abre la ventana

        ventana()
        return 0
    try:
        return linea_de_comandos(args)
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
