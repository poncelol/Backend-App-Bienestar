/**
 * Servidor de Abasto Bienestar (Cloudflare Workers, plan gratuito).
 *
 * Cada 15 minutos (cron en wrangler.jsonc) descarga el Excel del catálogo de SharePoint y, si cambió, reemplaza la
 * copia guardada. La app lo pide aquí en vez de a SharePoint y lo lee ella misma: en el plan gratuito un Worker solo
 * tiene 10 ms de CPU y leer el Excel toma más de 100 ms.
 *
 * Las imágenes y las regiones viven en la base de datos D1 y se cambian con admin/admin_bienestar.py o con el panel
 * web (admin.ts). El código que comparten está en comun.ts.
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

import { json, CORS, ErrorHttp, estado, respuestaError, rutaAdmin, rutaLectura, sincronizar, type Env } from './comun';

async function autorizado(env: Env, request: Request) {
  const token = env.ADMIN_TOKEN;
  if (!token) throw new ErrorHttp(503, 'Falta configurar la clave: npx wrangler secret put ADMIN_TOKEN');
  const dado = (request.headers.get('Authorization') ?? '').replace(/^Bearer\s+/i, '');
  // Se comparan las huellas (mismo largo) en tiempo constante
  const huella = async (s: string) => crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  const [a, b] = await Promise.all([huella(dado), huella(token)]);
  if (!crypto.subtle.timingSafeEqual(a, b)) throw new ErrorHttp(401, 'Clave de administración incorrecta.');
}

async function atender(request: Request, env: Env): Promise<Response> {
  const { pathname } = new URL(request.url);
  const metodo = request.method;
  if (metodo === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

  if (metodo === 'GET' || metodo === 'HEAD') {
    const lectura = await rutaLectura(env, request, pathname);
    if (lectura) return lectura;
    if (pathname === '/api/estado') return json(await estado(env));
    if (pathname === '/') return json({ servicio: 'Abasto Bienestar', estado: '/api/estado' });
  }

  if (pathname.startsWith('/api/admin/')) {
    await autorizado(env, request);
    const respuesta = await rutaAdmin(env, request, pathname.slice('/api/admin/'.length));
    if (respuesta) return respuesta;
  }
  throw new ErrorHttp(404, `No existe ${metodo} ${pathname}`);
}

export default {
  async fetch(request, env) {
    try {
      return await atender(request, env);
    } catch (e) {
      return respuestaError(e);
    }
  },

  async scheduled(_controlador, env, ctx) {
    ctx.waitUntil(sincronizar(env).then(
      (r) => console.log(r.cambio ? `Catálogo actualizado (${r.bytes} bytes)`
        : r.descargado ? 'Catálogo sin cambios (se descargó para comparar)' : 'Catálogo sin cambios (no se descargó)'),
      (e) => console.error('No se pudo descargar el catálogo:', e.message),
    ));
  },
} satisfies ExportedHandler<Env>;
