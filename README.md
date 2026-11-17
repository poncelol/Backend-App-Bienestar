# BienestarBackend

Servidor y herramientas de administración de la app **Abasto Bienestar** (repositorio `BienestarApp`, en la
carpeta vecina `App Movil/BienestarApp`).

| Carpeta | Qué es |
|---|---|
| `cloudflare/` | Servidor en Cloudflare Workers (plan gratis), con base de datos D1 y almacenamiento KV |
| `admin/` | `admin_bienestar.py`: cambia imágenes, regiones y el vínculo del catálogo |

```
SharePoint (Excel del catálogo) ──cada 15 min──▶ Cloudflare Worker ──▶ App
                                                   │  D1: imágenes, regiones, config
admin_bienestar.py (Excel de imágenes, fotos) ─────┘  KV: copia del Excel y fotos subidas
```

- **Cada 15 minutos** el servidor descarga el Excel del catálogo. Si cambió, borra la copia anterior y guarda la
  nueva; si no cambió, no reescribe nada, para no gastar la cuota gratis.
- La app **lee el Excel ella misma**, como antes. En el plan gratis un Worker tiene 10 ms de CPU, y leer el Excel
  toma más de 100 ms. Con el plan de pago ($5 USD al mes) se podría mover esa lectura al servidor.
- **Los vínculos de los Excel no salen del servidor.** Se guardan como secreto (`LINK_CATALOGO`) o en la base de
  datos, y `/api/estado` no los muestra. Solo `/api/admin/estado`, con la clave, los enseña.

## 1. Publicar el servidor (una sola vez)

Necesita una cuenta gratis en https://dash.cloudflare.com (no pide tarjeta) y Node.js 22 o más reciente.

```powershell
cd cloudflare
npm install
npx wrangler login                              # abre el navegador para autorizar
npx wrangler d1 create abasto-bienestar         # copie el "database_id" a wrangler.jsonc
npx wrangler kv namespace create ARCHIVOS       # copie el "id" a wrangler.jsonc
npm run tablas                                  # crea las tablas de schema.sql
npx wrangler secret put ADMIN_TOKEN             # invente una clave larga; la usa admin_bienestar.py
npx wrangler secret put LINK_CATALOGO           # pegue el vínculo para compartir del Excel del catálogo
npm run deploy                                  # imprime: https://abasto-bienestar.<cuenta>.workers.dev
```

Para comprobarlo, abra `https://abasto-bienestar.<cuenta>.workers.dev/api/estado` en el navegador. En
`catalogo.actualizado` debe aparecer una fecha; si sale `null`, espere al siguiente cuarto de hora o pulse
**Descargar el catálogo ahora** en `admin_bienestar.py`.

## 2. Administrar imágenes y regiones

```powershell
pip install requests openpyxl pillow truststore playwright
python admin/admin_bienestar.py
```

`admin/` tiene tres archivos: `admin_bienestar.py` (conexión al servidor, Excel y línea de comandos),
`interfaz.py` (la ventana) y `navegador.py` (Chrome para buscar fotos).

La ventana se conecta al servidor y muestra el catálogo completo en tres pestañas: **Artículos**, **Productos** y
**Proveedores**.
- Los renglones sin imagen salen en rojo, con un contador y una barra de avance.
- Los productos que toman la foto de uno de sus artículos salen en amarillo, igual que en la app.
- Se puede filtrar (Todos / Sin imagen / Con imagen) y buscar por nombre, clave o marca.
- Al elegir un renglón se ve la foto y su vínculo.

Para poner o cambiar una foto:

- **🔎 Buscar en Chrome:** abre Google o Bing Imágenes con el nombre, la marca y el código de barras. Al hacer
  clic en la foto correcta se guarda sola, con su FUENTE, y pasa al siguiente sin imagen.
- **🔗 Agregar a mano:** pegar un vínculo, o **Subir foto** (PNG, JPG, WEBP…). La foto se reduce a 1200 px y se
  guarda en el servidor.
- **🗑 Quitar** y **↩ Deshacer** el último cambio.

Cada cambio se guarda al momento en la base de datos, y la app lo ve la siguiente vez que descarga.

Primera vez:

1. Pestaña **Servidor**: escriba la dirección y la clave (ADMIN_TOKEN) y pulse **Guardar ajustes**. Se guardan en
   `admin/admin_config.json`, que no se sube a git.
2. Si quiere, active **Guardar también cada cambio en ese Excel** para que el Excel de imágenes siga al día.
3. Pestaña **Regiones**: **Escribir hoja REGIONES en el Excel**, edítela en Excel y pulse **Subir hoja REGIONES**.

El programa busca la app en `../BienestarApp`, de donde toma `datos/config.json`. Si está en otro lugar, agregue
`"app": "C:\\ruta\\a\\BienestarApp"` en `admin_config.json`.

## Panel web de administración

Lo mismo que `admin_bienestar.py`, pero en el navegador y sin instalar nada:
**https://abasto-bienestar-admin.abasto-bienestar-servidor.workers.dev**

- **Para entrar:** Cloudflare Access pide el correo y manda un código de 6 dígitos. Solo entran los correos
  autorizados.
- **Para agregar o quitar personas:** en dash.cloudflare.com, ve a *Zero Trust › Access › Applications*, abre la
  aplicación de `abasto-bienestar-admin` y edita su política (*Include › Emails*). El plan gratis permite hasta
  50 personas.
- **Qué se puede hacer:**
  - Ver los artículos, productos y proveedores sin foto, con filtros y búsqueda.
  - Buscar la foto en Google/Bing, pegar un vínculo o una imagen con Ctrl+V, arrastrar o elegir un archivo.
  - Quitar o deshacer una foto.
  - Editar las regiones.
  - Descargar el catálogo ahora, cambiar su vínculo o cargar el Excel de imágenes completo.

Es un Worker aparte (`src/admin.ts` + `src/panel.html`, configurado en `wrangler.admin.jsonc`) con la misma base de
datos que el público. Toda su dirección está detrás de Access y, además, el Worker comprueba la firma del pase de
Access en cada petición (`src/acceso.ts`): si Access se desactivara por error, el panel queda cerrado, no abierto.

```powershell
npm run deploy:admin                                            # publicar el panel
npx wrangler secret put ACCESS_EQUIPO -c wrangler.admin.jsonc   # p. ej. miequipo.cloudflareaccess.com
npx wrangler secret put ACCESS_AUD -c wrangler.admin.jsonc      # «Audience (AUD) tag» de la aplicación de Access
npm run dev:admin                                               # probar en http://127.0.0.1:8788 (con DESARROLLO_SIN_ACCESO=si en .dev.vars)
```

## 3. Conectar la app

En `BienestarApp/src/data/vinculos.ts`:

```ts
export const SERVIDOR = 'https://abasto-bienestar.<cuenta>.workers.dev';
```

Publique una versión nueva de la app (EAS / Vercel). Desde entonces la app pide el catálogo, las imágenes y las
regiones al servidor, y cada 15 minutos vuelve a descargar mientras está abierta.

## 4. Claves y vínculos: nada en git

Ninguna clave ni vínculo de SharePoint va en el código de los dos repositorios:

| Dato | Dónde vive |
|---|---|
| Clave de administración (`ADMIN_TOKEN`) | Secreto en Cloudflare y en `admin/admin_config.json` (git lo ignora) |
| Vínculo del Excel del catálogo | Secreto `LINK_CATALOGO` o tabla `config` del servidor, y en `BienestarApp/datos/config.json` |
| Vínculo del Excel de imágenes | Solo en `BienestarApp/datos/config.json` (lo usan los scripts de esta computadora) |
| Pruebas locales del servidor | `cloudflare/.dev.vars` (git lo ignora) |

`.gitignore` excluye `.dev.vars`, `.env*`, `admin_config.json` y los `*.xlsx`. En la app, `LINK_EXCEL` y
`LINK_IMAGENES` (en `vinculos.ts`) están vacíos: la app solo conoce la dirección del servidor, que es pública como
la de cualquier página. Lo que protege la administración es la clave.

**Pendiente:** los vínculos anteriores quedaron en el historial de git de BienestarApp y en las versiones ya
instaladas de la app, así que hay que darlos por conocidos. Cuando la mayoría tenga la versión nueva, genere
vínculos nuevos en OneDrive/SharePoint: *Compartir › Administrar acceso*, elimine el viejo y cree uno nuevo
("Cualquier persona con el vínculo puede ver"). El del catálogo lo tiene que hacer su dueño. Después guarde los
nuevos:

- Catálogo: en `admin_bienestar.py`, *Servidor › Cambiar Excel del catálogo* (o `npx wrangler secret put LINK_CATALOGO`).
- Imágenes: en `BienestarApp/datos/config.json`.

> Ojo: así se oculta el **vínculo**, no el **contenido**. `/api/catalogo` entrega el Excel a quien lo pida, igual
> que la app se lo muestra a quien la usa. Para restringir quién lo ve haría falta un inicio de sesión en la app.

## Probar en la computadora

```powershell
cd cloudflare
"ADMIN_TOKEN=prueba-local`nLINK_CATALOGO=<vínculo del catálogo>" | Out-File -Encoding ascii .dev.vars
npm run tablas:local
npx wrangler dev --test-scheduled               # http://127.0.0.1:8787
# Simular el cron:  curl "http://127.0.0.1:8787/__scheduled?cron=*/15+*+*+*+*"
```

## Límites del plan gratis (revise los vigentes en cloudflare.com)

- Workers: 100 000 peticiones al día, 10 ms de CPU por petición.
- D1: 5 GB y 100 000 renglones escritos al día. Subir el Excel completo de imágenes escribe ~1 500.
- KV: 1 GB y 1 000 escrituras al día. El cron solo escribe cuando el Excel cambia; las fotos se reducen a 1200 px.
