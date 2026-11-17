/**
 * Servidor de Abasto Bienestar (Cloudflare Workers, plan gratuito).
 *
 * Cada 15 minutos (cron en wrangler.jsonc) descarga el Excel del catálogo de SharePoint y, si cambió, reemplaza la
 * copia guardada. La app lo pide aquí en vez de a SharePoint y lo lee ella misma: en el plan gratuito un Worker solo
 * tiene 10 ms de CPU y leer el Excel toma más de 100 ms.
 *
 * Las imágenes y las regiones viven en la base de datos D1 y se cambian con admin/admin_bienestar.py.
 *
 * El vínculo del Excel del catálogo nunca sale del servidor: está en la tabla config (lo cambia admin_bienestar.py)
 * o, si ahí no hay, en el secreto LINK_CATALOGO (npx wrangler secret put LINK_CATALOGO). La app no lo necesita.
 *
 * Público (la app):
 *   GET  /api/catalogo            Excel del catálogo (.xlsx)
 *   GET  /api/imagenes            { proveedores, productos, articulos }: clave -> URL de la foto
 *   GET  /api/regiones            [{ sigla, nombre, clave, unidades }]
 *   GET  /api/estado              Última descarga del catálogo, errores y conteos (sin el vínculo)
 *   GET  /fotos/<nombre>          Fotos subidas con admin/admin_bienestar.py
 * Administración (encabezado Authorization: Bearer <ADMIN_TOKEN>):
 *   PUT    /api/admin/imagenes    { proveedores?, productos?, articulos?: {clave: url}, reemplazar?: bool }
 *                                 url vacía = quitar la foto; reemplazar = borrar las que no vengan
 *   PUT    /api/admin/regiones    [{ sigla, nombre, clave, unidades: [] }] (reemplaza todas)
 *   GET    /api/admin/estado      Igual que /api/estado, con el vínculo del catálogo
 *   PUT    /api/admin/config      { link_catalogo }: cambia el Excel del catálogo (se prueba antes de guardarlo)
 *   POST   /api/admin/sincronizar Descarga el catálogo ahora
 *   PUT    /api/admin/fotos/<nombre>   Cuerpo = la imagen (Content-Type image/...). Devuelve { url }
 *   DELETE /api/admin/fotos/<nombre>
 */

export interface Env {
  DB: D1Database;
  ARCHIVOS: KVNamespace;
  ADMIN_TOKEN?: string;
  /** Vínculo para compartir del Excel del catálogo, si todavía no se guardó uno en la tabla config. */
  LINK_CATALOGO?: string;
}

const TIPOS = ['proveedores', 'productos', 'articulos'] as const;
const CLAVE_CATALOGO = 'catalogo.xlsx';
const MAX_FOTO = 5 * 1024 * 1024;
// Sin un User-Agent de navegador SharePoint responde 403 aunque el vínculo sea público
const NAVEGADOR = { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' };

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Allow-Headers': 'Cache-Control, If-None-Match',
  'Access-Control-Expose-Headers': 'ETag, X-Actualizado',
};

class ErrorHttp extends Error {
  constructor(public status: number, mensaje: string) {
    super(mensaje);
  }
}

const json = (datos: unknown, status = 200, extra: HeadersInit = {}) =>
  new Response(JSON.stringify(datos), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...CORS, ...extra },
  });

const ahora = () => new Date().toISOString();

// ──────────────────────────────────────────────────────────────
// Configuración
// ──────────────────────────────────────────────────────────────
async function leerConfig(env: Env): Promise<Record<string, string>> {
  const { results } = await env.DB.prepare('SELECT clave, valor FROM config').all<{ clave: string; valor: string }>();
  return Object.fromEntries(results.map((r) => [r.clave, r.valor]));
}

const guardar = (env: Env, clave: string, valor: string) =>
  env.DB.prepare('INSERT INTO config (clave, valor) VALUES (?1, ?2) ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor')
    .bind(clave, valor);

/**
 * Vínculo para compartir de SharePoint -> descarga directa (igual que urlDescarga en BienestarApp/src/data/vinculos.ts).
 * download.aspx?share= entrega el archivo en una sola petición, sin la cookie de invitado de «?download=1».
 */
function urlDescarga(link: string) {
  const limpio = link.trim();
  const m = limpio.match(/^(https:\/\/[^/]+\.sharepoint\.com)\/:[a-z]:\/[a-z]\/(personal|sites)\/([^/]+)\/([A-Za-z0-9_-]+)/i);
  if (m) return `${m[1]}/${m[2]}/${m[3]}/_layouts/15/download.aspx?share=${m[4]}`;
  return limpio + (limpio.includes('?') ? '&' : '?') + 'download=1';
}

const vinculoCatalogo = (env: Env, config: Record<string, string>) => config.link_catalogo || env.LINK_CATALOGO?.trim() || '';

// ──────────────────────────────────────────────────────────────
// Catálogo: copia del Excel de SharePoint
// ──────────────────────────────────────────────────────────────
type Resultado = { cambio: boolean; sha256: string; bytes: number; actualizado: string };

/**
 * Descarga el Excel del catálogo y, si cambió (o con `forzar`), borra la copia guardada y guarda la nueva.
 * Con `link` prueba otro vínculo y, si funciona, lo deja como el del catálogo.
 */
async function sincronizar(env: Env, { forzar = false, link }: { forzar?: boolean; link?: string } = {}): Promise<Resultado> {
  const config = await leerConfig(env);
  const vinculo = link ?? vinculoCatalogo(env, config);
  const revisado = ahora();
  try {
    if (!vinculo) throw new Error('Falta el vínculo del catálogo: guárdelo con admin_bienestar.py o con npx wrangler secret put LINK_CATALOGO.');
    let r: Response;
    try {
      r = await fetch(urlDescarga(vinculo), { headers: NAVEGADOR });
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
      await env.ARCHIVOS.delete(CLAVE_CATALOGO);
      await env.ARCHIVOS.put(CLAVE_CATALOGO, contenido, { metadata: { sha256, actualizado } });
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
    ]);
    return { cambio, sha256, bytes: contenido.byteLength, actualizado };
  } catch (e) {
    const mensaje = e instanceof Error ? e.message : String(e);
    // Un vínculo nuevo que no funciona no se guarda ni cuenta como error del catálogo actual
    if (!link) await env.DB.batch([guardar(env, 'catalogo_revisado', revisado), guardar(env, 'catalogo_error', mensaje)]);
    throw new ErrorHttp(502, mensaje);
  }
}

async function catalogo(env: Env, request: Request): Promise<Response> {
  let guardado = await env.ARCHIVOS.getWithMetadata<{ sha256: string; actualizado: string }>(CLAVE_CATALOGO, 'arrayBuffer');
  if (!guardado.value) {
    // Primera vez: aún no corre el cron
    await sincronizar(env, { forzar: true });
    guardado = await env.ARCHIVOS.getWithMetadata<{ sha256: string; actualizado: string }>(CLAVE_CATALOGO, 'arrayBuffer');
    if (!guardado.value) throw new ErrorHttp(503, 'El catálogo aún no está disponible; intente en un minuto.');
  }
  const etag = `"${guardado.metadata?.sha256 ?? ''}"`;
  const encabezados = { ...CORS, ETag: etag, 'X-Actualizado': guardado.metadata?.actualizado ?? '', 'Cache-Control': 'no-cache' };
  if (request.headers.get('If-None-Match') === etag) return new Response(null, { status: 304, headers: encabezados });
  return new Response(guardado.value, {
    headers: { ...encabezados, 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
  });
}

// ──────────────────────────────────────────────────────────────
// Imágenes y regiones (D1)
// ──────────────────────────────────────────────────────────────
// SQLite arma el JSON publicado: así el Worker casi no gasta CPU y la app lee un solo renglón
const publicarImagenes = (env: Env, cuando: string) =>
  env.DB.prepare(`INSERT OR REPLACE INTO publicado (nombre, json, actualizado)
    SELECT 'imagenes', json_object(
      'proveedores', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'proveedores')),
      'productos', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'productos')),
      'articulos', json((SELECT json_group_object(clave, url) FROM imagenes WHERE tipo = 'articulos'))
    ), ?1`).bind(cuando);

const publicarRegiones = (env: Env, cuando: string) =>
  env.DB.prepare(`INSERT OR REPLACE INTO publicado (nombre, json, actualizado)
    SELECT 'regiones', json_group_array(json_object('sigla', sigla, 'nombre', nombre, 'clave', clave, 'unidades', json(unidades))), ?1
    FROM (SELECT * FROM regiones ORDER BY clave, sigla)`).bind(cuando);

async function publicado(env: Env, nombre: string, vacio: string): Promise<Response> {
  const fila = await env.DB.prepare('SELECT json, actualizado FROM publicado WHERE nombre = ?1').bind(nombre)
    .first<{ json: string; actualizado: string }>();
  return new Response(fila?.json ?? vacio, {
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-cache', 'X-Actualizado': fila?.actualizado ?? '', ...CORS },
  });
}

async function guardarImagenes(env: Env, cuerpo: unknown) {
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

async function guardarRegiones(env: Env, cuerpo: unknown) {
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

async function estado(env: Env, conVinculo = false) {
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
function nombreFoto(nombre: string) {
  const limpio = decodeURIComponent(nombre);
  if (!/^[A-Za-z0-9._-]{1,120}$/.test(limpio)) throw new ErrorHttp(400, 'Nombre de foto inválido: use letras, números, punto, guion o guion bajo.');
  return limpio;
}

async function subirFoto(env: Env, request: Request, nombre: string) {
  const tipo = request.headers.get('Content-Type') ?? '';
  if (!tipo.startsWith('image/')) throw new ErrorHttp(415, 'La foto debe enviarse con Content-Type image/...');
  const contenido = await request.arrayBuffer();
  if (!contenido.byteLength) throw new ErrorHttp(400, 'La foto vino vacía.');
  if (contenido.byteLength > MAX_FOTO) throw new ErrorHttp(413, 'La foto pesa más de 5 MB.');
  await env.ARCHIVOS.put(`foto:${nombre}`, contenido, { metadata: { tipo } });
  // ?v= cambia en cada subida para que los teléfonos no muestren la foto anterior guardada en caché
  return { ok: true, url: `${new URL(request.url).origin}/fotos/${nombre}?v=${Date.now().toString(36)}`, bytes: contenido.byteLength };
}

async function verFoto(env: Env, nombre: string) {
  const { value, metadata } = await env.ARCHIVOS.getWithMetadata<{ tipo: string }>(`foto:${nombre}`, 'stream');
  if (!value) throw new ErrorHttp(404, 'No existe esa foto.');
  return new Response(value, {
    headers: { 'Content-Type': metadata?.tipo ?? 'image/jpeg', 'Cache-Control': 'public, max-age=604800', ...CORS },
  });
}

// ──────────────────────────────────────────────────────────────
// Rutas
// ──────────────────────────────────────────────────────────────
async function autorizado(env: Env, request: Request) {
  const token = env.ADMIN_TOKEN;
  if (!token) throw new ErrorHttp(503, 'Falta configurar la clave: npx wrangler secret put ADMIN_TOKEN');
  const dado = (request.headers.get('Authorization') ?? '').replace(/^Bearer\s+/i, '');
  // Se comparan las huellas (mismo largo) en tiempo constante
  const huella = async (s: string) => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  const [a, b] = await Promise.all([huella(dado), huella(token)]);
  if (!crypto.subtle.timingSafeEqual(a, b)) throw new ErrorHttp(401, 'Clave de administración incorrecta.');
}

async function leerCuerpo(request: Request) {
  try {
    return await request.json();
  } catch {
    throw new ErrorHttp(400, 'El cuerpo no es JSON válido.');
  }
}

async function atender(request: Request, env: Env): Promise<Response> {
  const { pathname } = new URL(request.url);
  const metodo = request.method;
  if (metodo === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

  if (metodo === 'GET' || metodo === 'HEAD') {
    if (pathname === '/api/catalogo') return catalogo(env, request);
    if (pathname === '/api/imagenes') return publicado(env, 'imagenes', '{"proveedores":{},"productos":{},"articulos":{}}');
    if (pathname === '/api/regiones') return publicado(env, 'regiones', '[]');
    if (pathname === '/api/estado') return json(await estado(env));
    if (pathname.startsWith('/fotos/')) return verFoto(env, nombreFoto(pathname.slice(7)));
    if (pathname === '/') return json({ servicio: 'Abasto Bienestar', estado: '/api/estado' });
  }

  if (pathname.startsWith('/api/admin/')) {
    await autorizado(env, request);
    const ruta = pathname.slice('/api/admin/'.length);
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
      if (metodo === 'PUT') return json(await subirFoto(env, request, nombre));
      if (metodo === 'DELETE') {
        await env.ARCHIVOS.delete(`foto:${nombre}`);
        return json({ ok: true });
      }
    }
  }
  throw new ErrorHttp(404, `No existe ${metodo} ${pathname}`);
}

export default {
  async fetch(request, env) {
    try {
      return await atender(request, env);
    } catch (e) {
      if (e instanceof ErrorHttp) return json({ error: e.message }, e.status);
      console.error(e);
      return json({ error: 'Error interno del servidor.' }, 500);
    }
  },

  async scheduled(_controlador, env, ctx) {
    ctx.waitUntil(sincronizar(env).then(
      (r) => console.log(r.cambio ? `Catálogo actualizado (${r.bytes} bytes)` : 'Catálogo sin cambios'),
      (e) => console.error('No se pudo descargar el catálogo:', e.message),
    ));
  },
} satisfies ExportedHandler<Env>;
