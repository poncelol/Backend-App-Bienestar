-- Tablas del servidor (D1 = SQLite). Se pueden volver a ejecutar sin perder datos.

-- Foto de cada proveedor (CVE), producto (PROD.) o artículo (ART.). Antes estaban en el Excel interno de imágenes.
CREATE TABLE IF NOT EXISTS imagenes (
  tipo TEXT NOT NULL CHECK (tipo IN ('proveedores', 'productos', 'articulos')),
  clave TEXT NOT NULL,
  url TEXT NOT NULL,
  actualizado TEXT NOT NULL,
  PRIMARY KEY (tipo, clave)
);

-- Gerencias regionales: nombre, clave y unidades operativas (lista JSON). La sigla y los estados del mapa son fijos
-- en la app; si aquí no hay nada, la app usa los suyos.
CREATE TABLE IF NOT EXISTS regiones (
  sigla TEXT PRIMARY KEY,
  nombre TEXT NOT NULL,
  clave INTEGER NOT NULL,
  unidades TEXT NOT NULL
);

-- Configuración y estado: link_catalogo, catalogo_sha256, catalogo_actualizado, catalogo_revisado, catalogo_error…
CREATE TABLE IF NOT EXISTS config (
  clave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);

-- JSON que entrega la API, armado por SQLite al guardar cambios: así cada consulta de la app lee un solo renglón.
CREATE TABLE IF NOT EXISTS publicado (
  nombre TEXT PRIMARY KEY,
  json TEXT NOT NULL,
  actualizado TEXT NOT NULL
);
