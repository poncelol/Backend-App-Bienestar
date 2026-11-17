// wrangler importa los .html como texto (regla Text por defecto)
declare module '*.html' {
  const contenido: string;
  export default contenido;
}
