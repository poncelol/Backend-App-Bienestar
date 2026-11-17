/**
 * Punto de entrada único de los dos Workers (abasto-bienestar y abasto-bienestar-admin).
 *
 * Los dos publican este mismo código y cada petición se atiende según la dirección: la del panel
 * (abasto-bienestar-admin.…) va al panel (admin.ts, protegido con Cloudflare Access); cualquier otra, al servidor
 * público de la app (index.ts). Así no importa qué configuración o comando use cada publicación (manual o automática
 * desde GitHub): ninguno de los dos puede quedar con el código del otro.
 *
 * Las peticiones no pueden «saltar» al panel desde la dirección pública: cada dirección *.workers.dev llega solo a su
 * propio Worker, y el panel además exige el pase firmado de Access (acceso.ts).
 */
import admin from './admin';
import { leerConfig, type Env } from './comun';
import publico from './index';

/** Para probar el panel en la computadora: npm run dev:admin (pone PANEL_LOCAL=si; nunca va en la configuración). */
const esPanel = (request: Request, env: Env) =>
  new URL(request.url).hostname.startsWith('abasto-bienestar-admin.') || env.PANEL_LOCAL === 'si';

/** Si el otro Worker ya revisó el catálogo hace poco, esta vuelta no hace nada (los dos tienen el cron). */
const MINUTOS_ENTRE_REVISIONES = 10;

export default {
  fetch(request, env) {
    return esPanel(request, env) ? admin.fetch(request, env) : publico.fetch(request, env);
  },

  async scheduled(controlador, env, ctx) {
    const { catalogo_revisado: revisado } = await leerConfig(env);
    if (revisado && Date.now() - Date.parse(revisado) < MINUTOS_ENTRE_REVISIONES * 60000) return;
    await publico.scheduled!(controlador, env, ctx);
  },
} satisfies ExportedHandler<Env>;
