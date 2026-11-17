/**
 * Cloudflare Access: quién puede entrar al panel de administración.
 *
 * Access pide el correo y manda un código; si el correo está autorizado, deja pasar la petición y le agrega un pase
 * firmado (JWT) en el encabezado Cf-Access-Jwt-Assertion. Aquí se comprueba ese pase con las llaves públicas del
 * equipo de Access. Así, aunque Access se desactivara por error, el panel queda cerrado, no abierto.
 *
 * Secretos del Worker de administración (los muestra el panel de Cloudflare al activar Access):
 *   ACCESS_EQUIPO  dominio del equipo, p. ej. «miequipo.cloudflareaccess.com»
 *   ACCESS_AUD     «Audience (AUD) tag» de la aplicación de Access
 */
import { ErrorHttp, type Env } from './comun';

type Llave = JsonWebKey & { kid: string };
let cache: { equipo: string; hasta: number; llaves: Llave[] } | null = null;

const deBase64Url = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));
const leerJson = (s: string) => JSON.parse(new TextDecoder().decode(deBase64Url(s)));

async function llaves(equipo: string, renovar = false): Promise<Llave[]> {
  if (!renovar && cache && cache.equipo === equipo && cache.hasta > Date.now()) return cache.llaves;
  const r = await fetch(`https://${equipo}/cdn-cgi/access/certs`);
  if (!r.ok) throw new ErrorHttp(503, `No se pudieron leer las llaves de Cloudflare Access (HTTP ${r.status}).`);
  const { keys } = (await r.json()) as { keys: Llave[] };
  cache = { equipo, hasta: Date.now() + 3600_000, llaves: keys };
  return keys;
}

function pase(request: Request) {
  const encabezado = request.headers.get('Cf-Access-Jwt-Assertion');
  if (encabezado) return encabezado;
  const galleta = (request.headers.get('Cookie') ?? '').match(/(?:^|;\s*)CF_Authorization=([^;]+)/);
  return galleta?.[1] ?? null;
}

/** Comprueba el pase de Access y devuelve el correo de quien entró. Lanza 401/503 si no puede entrar. */
export async function verificarAcceso(request: Request, env: Env): Promise<string> {
  const { hostname } = new URL(request.url);
  if (env.DESARROLLO_SIN_ACCESO === 'si' && (hostname === '127.0.0.1' || hostname === 'localhost')) return 'prueba-local';

  const equipo = (env.ACCESS_EQUIPO ?? '').trim().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
  const aud = (env.ACCESS_AUD ?? '').trim();
  if (!equipo || !aud) {
    throw new ErrorHttp(503, 'Falta configurar Cloudflare Access: npx wrangler secret put ACCESS_EQUIPO / ACCESS_AUD -c wrangler.admin.jsonc');
  }
  const token = pase(request);
  if (!token) throw new ErrorHttp(401, 'Entre por Cloudflare Access (falta el pase de acceso).');

  const partes = token.split('.');
  if (partes.length !== 3) throw new ErrorHttp(401, 'Pase de acceso inválido.');
  let cabecera: { alg?: string; kid?: string };
  let datos: { aud?: string | string[]; exp?: number; nbf?: number; iss?: string; email?: string };
  try {
    cabecera = leerJson(partes[0]);
    datos = leerJson(partes[1]);
  } catch {
    throw new ErrorHttp(401, 'Pase de acceso inválido.');
  }
  if (cabecera.alg !== 'RS256') throw new ErrorHttp(401, 'Pase de acceso inválido.');

  let llave = (await llaves(equipo)).find((k) => k.kid === cabecera.kid);
  if (!llave) llave = (await llaves(equipo, true)).find((k) => k.kid === cabecera.kid);   // Access rota sus llaves
  if (!llave) throw new ErrorHttp(401, 'Pase de acceso firmado con una llave desconocida.');

  const clave = await crypto.subtle.importKey('jwk', llave, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
  const valido = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', clave, deBase64Url(partes[2]),
    new TextEncoder().encode(`${partes[0]}.${partes[1]}`));
  if (!valido) throw new ErrorHttp(401, 'La firma del pase de acceso no es válida.');

  const ahora = Math.floor(Date.now() / 1000);
  const audiencias = Array.isArray(datos.aud) ? datos.aud : [datos.aud];
  if (!audiencias.includes(aud)) throw new ErrorHttp(401, 'El pase de acceso es de otra aplicación.');
  if (!datos.exp || datos.exp < ahora - 30) throw new ErrorHttp(401, 'El pase de acceso venció: vuelva a entrar.');
  if (datos.nbf && datos.nbf > ahora + 30) throw new ErrorHttp(401, 'El pase de acceso todavía no es válido.');
  if (datos.iss !== `https://${equipo}`) throw new ErrorHttp(401, 'El pase de acceso es de otro equipo.');
  return datos.email ?? 'desconocido';
}
