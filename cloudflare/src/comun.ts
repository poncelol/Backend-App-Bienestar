/**
 * Lo que comparten los dos Workers: el público (index.ts: la app y admin_bienestar.py) y el de administración
 * (admin.ts: el panel web, protegido con Cloudflare Access). Los dos usan la misma base de datos D1 y el mismo KV.
 */

export interface Env {
  DB: D1Database;
  ARCHIVOS: KVNamespace;
  ADMIN_TOKEN?: string;
  /** Vínculo para compartir del Excel del catálogo, si todavía no se guardó uno en la tabla config. */
  LINK_CATALOGO?: string;
  /** Solo el Worker de administración: dirección del Worker público, para las fotos que se suben. */
  URL_PUBLICA?: string;
  /** Solo el Worker de administración: datos de Cloudflare Access (ver acceso.ts). */
  ACCESS_AUD?: string;
  ACCESS_EQUIPO?: string;
  /** Solo en .dev.vars: "si" deja entrar al panel sin Access, y solo desde 127.0.0.1 / localhost. */
  DESARROLLO_SIN_ACCESO?: string;
  /** Solo para probar el panel en la computadora (npm run dev:admin). */
  PANEL_LOCAL?: string;
}

export const TIPOS = ['proveedores', 'productos', 'articulos'] as const;
export const CLAVE_CATALOGO = 'catalogo.xlsx';
/** La versión inmediata anterior del catálogo: con ella la app calcula las novedades (qué cambió en la última versión). */
export const CLAVE_ANTERIOR = 'catalogo.anterior.xlsx';
type MetaCatalogo = { sha256: string; actualizado: string; anterior?: string; anteriorFecha?: string };
export const MAX_FOTO = 5 * 1024 * 1024;
// Sin un User-Agent de navegador SharePoint responde 403 aunque el vínculo sea público
export const NAVEGADOR = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' };

export const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Cache-Control, If-None-Match',
  'Access-Control-Expose-Headers': 'ETag, X-Actualizado, X-Anterior, X-Anterior-Fecha',
};

export class ErrorHttp extends Error {
  constructor(public status: number, mensaje: string) {
    super(mensaje);
  }
}

export const json = (datos: unknown, status = 200, extra: HeadersInit = {}) =>
  new Response(JSON.stringify(datos), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...CORS, ...extra },
  });

export const ahora = () => new Date().toISOString();

// ──────────────────────────────────────────────────────────────
// Configuración
// ──────────────────────────────────────────────────────────────
export async function leerConfig(env: Env): Promise<Record<string, string>> {
  const { results } = await env.DB.prepare('SELECT clave, valor FROM config').all<{ clave: string; valor: string }>();
  return Object.fromEntries(results.map((r) => [r.clave, r.valor]));
}

export const guardar = (env: Env, clave: string, valor: string) =>
  env.DB.prepare('INSERT INTO config (clave, valor) VALUES (?1, ?2) ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor')
    .bind(clave, valor);

/**
 * Vínculo para compartir de SharePoint -> descarga directa (igual que urlDescarga en BienestarApp/src/data/vinculos.ts).
 * download.aspx?share= entrega el archivo en una sola petición, sin la cookie de invitado de «?download=1».
 */
export function urlDescarga(link: string) {
  const limpio = link.trim();
  const m = limpio.match(/^(https:\/\/[^/]+\.sharepoint\.com)\/:[a-z]:\/[a-z]\/(personal|sites)\/([^/]+)\/([A-Za-z0-9_-]+)/i);
  if (m) return `${m[1]}/${m[2]}/${m[3]}/_layouts/15/download.aspx?share=${m[4]}`;
  return limpio + (limpio.includes('?') ? '&' : '?') + 'download=1';
}

export const vinculoCatalogo = (env: Env, config: Record<string, string>) => config.link_catalogo || env.LINK_CATALOGO?.trim() || '';

// ──────────────────────────────────────────────────────────────
// Catálogo: copia del Excel de SharePoint
// ──────────────────────────────────────────────────────────────
export type Resultado = { cambio: boolean; sha256: string; bytes: number; actualizado: string; descargado: boolean };

/** Versión del archivo según SharePoint (ETag, p. ej. "{…},19"), o '' si no la da. */
async function versionSharePoint(url: string): Promise<string> {
  try {
    const r = await fetch(url, { method: 'HEAD', headers: NAVEGADOR });
    return r.ok ? (r.headers.get('ETag') ?? '') : '';
  } catch {
    return '';
  }
}

/**
 * Revisa el Excel del catálogo y, si cambió (o con `forzar`), borra la copia guardada y guarda la nueva.
 *
 * Primero le pregunta a SharePoint solo la versión del archivo (petición HEAD, sin descargarlo): si es la misma que la
 * de la copia guardada, no descarga nada. SharePoint no responde «304 sin cambios», por eso se compara su ETag.
 * Si cambió (o SharePoint no da la versión), lo descarga y además compara la huella SHA-256 del contenido.
 * Con `link` prueba otro vínculo y, si funciona, lo deja como el del catálogo.
 */
export async function sincronizar(env: Env, { forzar = false, link }: { forzar?: boolean; link?: string } = {}): Promise<Resultado> {
  const config = await leerConfig(env);
  const vinculo = link ?? vinculoCatalogo(env, config);
  const revisado = ahora();
  try {
    if (!vinculo) throw new Error('Falta el vínculo del catálogo: guárdelo con admin_bienestar.py o con npx wrangler secret put LINK_CATALOGO.');
    const url = urlDescarga(vinculo);
    const version = forzar || link ? '' : await versionSharePoint(url);
    if (version && version === config.catalogo_version && config.catalogo_sha256) {
      await env.DB.batch([guardar(env, 'catalogo_revisado', revisado), guardar(env, 'catalogo_error', '')]);
      return { cambio: false, descargado: false, sha256: config.catalogo_sha256, bytes: Number(config.catalogo_bytes ?? 0),
        actualizado: config.catalogo_actualizado };
    }
    let r: Response;
    try {
      r = await fetch(url, { headers: NAVEGADOR });
    } catch {
      throw new Error('No hubo conexión con SharePoint.');
    }
    if (!r.ok) throw new Error(`SharePoint respondió HTTP ${r.status}.`);
    const contenido = await r.arrayBuffer();
    const inicio = new Uint8Array(contenido, 0, Math.min(2, contenido.byteLength));
    // Un .xlsx es un zip (empieza con "PK"); si no, SharePoint devolvió su página de inicio de sesión
    if (inicio[0] !== 0x50 || inicio[1] !== 0x4b) {
      throw new Error('El vínculo no devolvió un Excel: revise que permita ver el archivo sin iniciar sesión.');
    }
    const sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', contenido))]
      .map((b) => b.toString(16).padStart(2, '0')).join('');
    const cambio = forzar || !!link || sha256 !== config.catalogo_sha256;
    const actualizado = cambio ? revisado : config.catalogo_actualizado;
    if (cambio) {
      // La versión que se reemplaza pasa a ser la «anterior» (solo si de verdad es otra; «Descargar ahora» con el
      // mismo Excel conserva la anterior que ya había)
      const actual = await env.ARCHIVOS.getWithMetadata<MetaCatalogo>(CLAVE_CATALOGO, 'arrayBuffer');
      let anterior = { anterior: actual.metadata?.anterior, anteriorFecha: actual.metadata?.anteriorFecha };
      if (actual.value && actual.metadata?.sha256 && actual.metadata.sha256 !== sha256) {
        await env.ARCHIVOS.put(CLAVE_ANTERIOR, actual.value, { metadata: actual.metadata });
        anterior = { anterior: actual.metadata.sha256, anteriorFecha: actual.metadata.actualizado };
      }
      await env.ARCHIVOS.delete(CLAVE_CATALOGO);
      await env.ARCHIVOS.put(CLAVE_CATALOGO, contenido, { metadata: { sha256, actualizado, ...anterior } satisfies MetaCatalogo });
    }
    await env.DB.batch([
      guardar(env, 'catalogo_revisado', revisado),
      guardar(env, 'catalogo_error', ''),
      ...(cambio ? [
        guardar(env, 'catalogo_sha256', sha256),
        guardar(env, 'catalogo_actualizado', actualizado),
        guardar(env, 'catalogo_bytes', String(contenido.byteLength)),
      ] : []),
      ...(link ? [guardar(env, 'link_catalogo', link)] : []),
      // La versión de SharePoint de esta descarga, para la próxima revisión
      guardar(env, 'catalogo_version', r.headers.get('ETag') ?? ''),
    ]);
    return { cambio, descargado: true, sha256, bytes: contenido.byteLength, actualizado };
  } catch (e) {
    const mensaje = e instanceof Error ? e.message : String(e);
    // Un vínculo nuevo que no funciona no se guarda ni cuenta como error del catálogo actual
    if (!link) await env.DB.batch([guardar(env, 'catalogo_revisado', revisado), guardar(env, 'catalogo_error', mensaje)]);
    throw new ErrorHttp(502, mensaje);
  }
}

/**
 * El catálogo actual (o, con `anterior`, la versión inmediata anterior). Encabezados: ETag = huella de la versión,
 * X-Actualizado = cuándo apareció, X-Anterior / X-Anterior-Fecha = huella y fecha de la versión anterior.
 */
export async function catalogo(env: Env, request: Request, anterior = false): Promise<Response> {
  if (anterior) {
    const previo = await env.ARCHIVOS.getWithMetadata<MetaCatalogo>(CLAVE_ANTERIOR, 'arrayBuffer');
    if (!previo.value) throw new ErrorHttp(404, 'Todavía no hay una versión anterior del catálogo.');
    return new Response(previo.value, { headers: {
      ...CORS, ETag: `"${previo.metadata?.sha256 ?? ''}"`, 'X-Actualizado': previo.metadata?.actualizado ?? '',
      'Cache-Control': 'no-cache', 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } });
  }
  let guardado = await env.ARCHIVOS.getWithMetadata<MetaCatalogo>(CLAVE_CATALOGO, 'arrayBuffer');
  if (!guardado.value) {
    // Primera vez: aún no corre el cron
    await sincronizar(env, { forzar: true });
    guardado = await env.ARCHIVOS.getWithMetadata<MetaCatalogo>(CLAVE_CATALOGO, 'arrayBuffer');
    if (!guardado.value) throw new ErrorHttp(503, 'El catálogo aún no está disponible; intente en un minuto.');
  }
  const etag = `"${guardado.metadata?.sha256 ?? ''}"`;
  const encabezados = {
    ...CORS, ETag: etag, 'X-Actualizado': guardado.metadata?.actualizado ?? '', 'Cache-Control': 'no-cache',
    'X-Anterior': guardado.metadata?.anterior ? `"${guardado.metadata.anterior}"` : '',
    'X-Anterior-Fecha': guardado.metadata?.anteriorFecha ?? '',
  };
  if (request.headers.get('If-None-Match') === etag) return new Response(null, { status: 304, headers: encabezados });
  return new Response(guardado.value, {
    headers: { ...encabezados, 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  });
}

// ──────────────────────────────────────────────────────────────
// Imágenes y regiones (D1)
// ──────────────────────────────────────────────────────────────
// SQLite arma el JSON publicado: así el Worker casi no gasta CPU y la app lee un solo renglón
export const publicarImagenes = (env: Env, cuando: string) =>
  env.DB.prepare(`INSERT OR REPLACE INTO publicado (nombre, json, actualizado)
    SELECT 'imagenes', json_object(
      'proveedores', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'proveedores')),
      'productos', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'productos')),
      'articulos', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'articulos'))
    ), ?1`).bind(cuando);

export const publicarRegiones = (env: Env, cuando: string) =>
  env.DB.prepare(`INSERT OR REPLACE INTO publicado (nombre, json, actualizado)
    SELECT 'regiones', json_group_array(json_object('sigla', sigla, 'nombre', nombre, 'clave', clave, 'unidades', json(unidades))), ?1
    FROM (SELECT * FROM regiones ORDER BY clave, sigla)`).bind(cuando);

/** JSON publicado (imágenes o regiones). Con If-None-Match igual a su versión responde 304 sin cuerpo. */
export async function publicado(env: Env, request: Request, nombre: string, vacio: string): Promise<Response> {
  const fila = await env.DB.prepare('SELECT json, actualizado FROM publicado WHERE nombre = ?1').bind(nombre)
    .first<{ json: string; actualizado: string }>();
  const etag = `"${fila?.actualizado ?? 'vacio'}"`;
  const encabezados = { 'Cache-Control': 'no-cache', 'X-Actualizado': fila?.actualizado ?? '', ETag: etag, ...CORS };
  if (request.headers.get('If-None-Match') === etag) return new Response(null, { status: 304, headers: encabezados });
  return new Response(fila?.json ?? vacio, { headers: { 'Content-Type': 'application/json; charset=utf-8', ...encabezados } });
}

export async function guardarImagenes(env: Env, cuerpo: unknown) {
  if (!cuerpo || typeof cuerpo !== 'object') throw new ErrorHttp(400, 'Se esperaba un objeto JSON.');
  const datos = cuerpo as Record<string, unknown>;
  const reemplazar = datos.reemplazar === true;
  const cuando = ahora();
  const sentencias: D1PreparedStatement[] = [];
  const recibidas: Record<string, number> = {};
  for (const tipo of TIPOS) {
    const mapa = datos[tipo];
    if (mapa === undefined) continue;
    if (!mapa || typeof mapa !== 'object' || Array.isArray(mapa)) throw new ErrorHttp(400, `«${tipo}» debe ser un objeto {clave: url}.`);
    const limpio: Record<string, string> = {};
    for (const [k, v] of Object.entries(mapa)) {
      const clave = k.trim().replace(/\.0$/, '');
      const url = String(v ?? '').trim();
      if (!clave) continue;
      if (url && !/^https?:\/\//i.test(url)) throw new ErrorHttp(400, `La imagen de ${tipo} ${clave} no es un vínculo http(s): ${url}`);
      limpio[clave] = url;
    }
    recibidas[tipo] = Object.keys(limpio).length;
    const lista = JSON.stringify(limpio);
    if (reemplazar) sentencias.push(env.DB.prepare('DELETE FROM imagenes WHERE tipo = ?1').bind(tipo));
    sentencias.push(
      env.DB.prepare(`INSERT INTO imagenes (tipo, clave, url, actualizado)
        SELECT ?1, key, value, ?3 FROM json_each(?2) WHERE value <> ''
        ON CONFLICT (tipo, clave) DO UPDATE SET url = excluded.url, actualizado = excluded.actualizado
        WHERE imagenes.url <> excluded.url`).bind(tipo, lista, cuando),
      env.DB.prepare(`DELETE FROM imagenes WHERE tipo = ?1 AND clave IN (SELECT key FROM json_each(?2) WHERE value = '')`)
        .bind(tipo, lista),
    );
  }
  if (!sentencias.length) throw new ErrorHttp(400, 'No vino ninguna de: proveedores, productos, articulos.');
  // batch = una sola transacción: o se guarda todo o nada
  await env.DB.batch([...sentencias, publicarImagenes(env, cuando)]);
  return { ok: true, recibidas, reemplazar, actualizado: cuando };
}

export async function guardarRegiones(env: Env, cuerpo: unknown) {
  if (!Array.isArray(cuerpo) || !cuerpo.length) throw new ErrorHttp(400, 'Se esperaba una lista de regiones.');
  const regiones = cuerpo.map((r: Record<string, unknown>, i) => {
    const sigla = String(r?.sigla ?? '').trim().toUpperCase();
    const nombre = String(r?.nombre ?? '').trim();
    const clave = Number(r?.clave);
    const unidades = Array.isArray(r?.unidades) ? r.unidades.map((u) => String(u).trim()).filter(Boolean) : null;
    if (!/^[A-Z]{2,4}$/.test(sigla) || !nombre || !Number.isInteger(clave) || !unidades) {
      throw new ErrorHttp(400, `Región ${i + 1} incompleta: necesita sigla, nombre, clave (número) y unidades (lista).`);
    }
    return { sigla, nombre, clave, unidades };
  });
  const cuando = ahora();
  await env.DB.batch([
    env.DB.prepare('DELETE FROM regiones'),
    env.DB.prepare(`INSERT INTO regiones (sigla, nombre, clave, unidades)
      SELECT json_extract(value, '$.sigla'), json_extract(value, '$.nombre'), json_extract(value, '$.clave'), json(json_extract(value, '$.unidades'))
      FROM json_each(?1)`).bind(JSON.stringify(regiones)),
    publicarRegiones(env, cuando),
  ]);
  return { ok: true, regiones: regiones.length, actualizado: cuando };
}

export async function estado(env: Env, conVinculo = false) {
  const [config, conteo, regiones] = await Promise.all([
    leerConfig(env),
    env.DB.prepare('SELECT tipo, COUNT(*) AS n FROM imagenes GROUP BY tipo').all<{ tipo: string; n: number }>(),
    env.DB.prepare('SELECT COUNT(*) AS n FROM regiones').first<number>('n'),
  ]);
  return {
    catalogo: {
      // El vínculo solo se muestra a quien tiene la clave de administración
      ...(conVinculo ? { vinculo: vinculoCatalogo(env, config) || null } : {}),
      actualizado: config.catalogo_actualizado ?? null,
      revisado: config.catalogo_revisado ?? null,
      bytes: Number(config.catalogo_bytes ?? 0),
      error: config.catalogo_error || null,
    },
    imagenes: Object.fromEntries(TIPOS.map((t) => [t, conteo.results.find((r) => r.tipo === t)?.n ?? 0])),
    regiones,
  };
}

// ──────────────────────────────────────────────────────────────
// Fotos subidas (KV)
// ──────────────────────────────────────────────────────────────
export function nombreFoto(nombre: string) {
  const limpio = decodeURIComponent(nombre);
  if (!/^[A-Za-z0-9._-]{1,120}$/.test(limpio)) throw new ErrorHttp(400, 'Nombre de foto inválido: use letras, números, punto, guion o guion bajo.');
  return limpio;
}

export async function subirFoto(env: Env, request: Request, nombre: string, origen = new URL(request.url).origin) {
  const tipo = request.headers.get('Content-Type') ?? '';
  if (!tipo.startsWith('image/')) throw new ErrorHttp(415, 'La foto debe enviarse con Content-Type image/...');
  const contenido = await request.arrayBuffer();
  if (!contenido.byteLength) throw new ErrorHttp(400, 'La foto vino vacía.');
  if (contenido.byteLength > MAX_FOTO) throw new ErrorHttp(413, 'La foto pesa más de 5 MB.');
  await env.ARCHIVOS.put(`foto:${nombre}`, contenido, { metadata: { tipo } });
  // ?v= cambia en cada subida para que los teléfonos no muestren la foto anterior guardada en caché
  return { ok: true, url: `${origen}/fotos/${nombre}?v=${Date.now().toString(36)}`, bytes: contenido.byteLength };
}

export async function verFoto(env: Env, nombre: string) {
  const { value, metadata } = await env.ARCHIVOS.getWithMetadata<{ tipo: string }>(`foto:${nombre}`, 'stream');
  if (!value) throw new ErrorHttp(404, 'No existe esa foto.');
  return new Response(value, {
    headers: { 'Content-Type': metadata?.tipo ?? 'image/jpeg', 'Cache-Control': 'public, max-age=604800', ...CORS },
  });
}

// ──────────────────────────────────────────────────────────────
// Rutas compartidas
// ──────────────────────────────────────────────────────────────
export async function leerCuerpo(request: Request) {
  try {
    return await request.json();
  } catch {
    throw new ErrorHttp(400, 'El cuerpo no es JSON válido.');
  }
}

/**
 * Rutas de administración, iguales para admin_bienestar.py (/api/admin/…, con la clave) y para el panel web
 * (/api/…, con Cloudflare Access). Quien llama ya comprobó el permiso. Devuelve null si la ruta no existe.
 */
export async function rutaAdmin(env: Env, request: Request, ruta: string, origenFotos?: string): Promise<Response | null> {
  const metodo = request.method;
  if (metodo === 'PUT' && ruta === 'imagenes') return json(await guardarImagenes(env, await leerCuerpo(request)));
  if (metodo === 'PUT' && ruta === 'regiones') return json(await guardarRegiones(env, await leerCuerpo(request)));
  if (metodo === 'GET' && ruta === 'estado') return json(await estado(env, true));
  if (metodo === 'POST' && ruta === 'sincronizar') return json(await sincronizar(env, { forzar: true }));
  if (metodo === 'PUT' && ruta === 'config') {
    const { link_catalogo } = (await leerCuerpo(request)) as { link_catalogo?: string };
    if (!link_catalogo || !/^https:\/\/[^/]+\.sharepoint\.com\//i.test(link_catalogo.trim())) {
      throw new ErrorHttp(400, 'link_catalogo debe ser un vínculo para compartir de SharePoint/OneDrive (https://….sharepoint.com/…).');
    }
    return json(await sincronizar(env, { link: link_catalogo.trim() }));
  }
  if (ruta.startsWith('fotos/')) {
    const nombre = nombreFoto(ruta.slice(6));
    if (metodo === 'PUT') return json(await subirFoto(env, request, nombre, origenFotos));
    if (metodo === 'DELETE') {
      await env.ARCHIVOS.delete(`foto:${nombre}`);
      return json({ ok: true });
    }
  }
  return null;
}

/** Rutas de solo lectura que también usa el panel. Devuelve null si la ruta no existe. */
export async function rutaLectura(env: Env, request: Request, pathname: string): Promise<Response | null> {
  if (pathname === '/api/catalogo') return catalogo(env, request);
  if (pathname === '/api/catalogo/anterior') return catalogo(env, request, true);
  if (pathname === '/api/imagenes') return publicado(env, request, 'imagenes', '{"proveedores":{},"productos":{},"articulos":{}}');
  if (pathname === '/api/regiones') return publicado(env, request, 'regiones', '[]');
  if (pathname.startsWith('/fotos/')) return verFoto(env, nombreFoto(pathname.slice(7)));
  return null;
}

/** Errores -> respuesta JSON. */
export function respuestaError(e: unknown) {
  if (e instanceof ErrorHttp) return json({ error: e.message }, e.status);
  console.error(e);
  return json({ error: 'Error interno del servidor.' }, 500);
}
