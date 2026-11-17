/**
 * Worker de administración: el panel web (panel.html) y su API.
 *
 * Toda su dirección (abasto-bienestar-admin.<cuenta>.workers.dev) va protegida con Cloudflare Access y, además, cada
 * petición se comprueba aquí (acceso.ts). Usa la misma base de datos D1 y el mismo KV que el Worker público.
 *
 *   GET  /                    El panel
 *   GET  /api/yo              Correo de quien entró
 *   GET  /api/estado          Estado del servidor, con el vínculo del catálogo
 *   GET  /api/catalogo, /api/imagenes, /api/regiones, /fotos/<nombre>   Igual que en el Worker público
 *   PUT  /api/imagenes, /api/regiones, /api/config, /api/fotos/<nombre>; POST /api/sincronizar; DELETE /api/fotos/<nombre>
 *        Igual que /api/admin/… del Worker público. Exigen el encabezado X-Panel: 1, que solo puede poner el propio
 *        panel (otra página no puede agregarlo sin permiso), para que nadie haga cambios desde un vínculo engañoso.
 */
import { verificarAcceso } from './acceso';
import { ErrorHttp, json, respuestaError, rutaAdmin, rutaLectura, type Env } from './comun';
import PANEL from './panel.html';

const SEGURIDAD = {
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
};

function paginaError(status: number, mensaje: string) {
  const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Abasto Bienestar · Administración</title>
<body style="font-family:system-ui,sans-serif;background:#F6F3EE;color:#1F1F1F;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px">
<div style="max-width:520px;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)">
<h1 style="color:#9B2247;font-size:20px;margin:0 0 8px">No se puede abrir el panel</h1>
<p style="margin:0">${mensaje.replace(/[<>&]/g, '')}</p></div></body>`;
  return new Response(html, { status, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', ...SEGURIDAD } });
}

async function atender(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const { pathname } = url;
  const metodo = request.method;

  let correo: string;
  try {
    correo = await verificarAcceso(request, env);
  } catch (e) {
    if (pathname.startsWith('/api/') || !(e instanceof ErrorHttp)) throw e;
    return paginaError(e.status, e.message);
  }

  if (metodo === 'GET' && (pathname === '/' || pathname === '/index.html')) {
    return new Response(PANEL, {
      headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', ...SEGURIDAD },
    });
  }
  if (metodo === 'GET' && pathname === '/api/yo') return json({ correo });
  if (metodo === 'GET' || metodo === 'HEAD') {
    const lectura = await rutaLectura(env, request, pathname);
    if (lectura) return lectura;
  }
  if (pathname.startsWith('/api/')) {
    if (metodo !== 'GET' && request.headers.get('X-Panel') !== '1') throw new ErrorHttp(403, 'Petición rechazada.');
    // Las fotos subidas se publican en la dirección pública: la de este panel está cerrada para la app
    const respuesta = await rutaAdmin(env, request, pathname.slice('/api/'.length), env.URL_PUBLICA || url.origin);
    if (respuesta) {
      if (metodo !== 'GET') console.log(`${correo}: ${metodo} ${pathname}`);
      return respuesta;
    }
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
} satisfies ExportedHandler<Env>;
