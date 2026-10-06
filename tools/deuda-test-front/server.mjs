// Front local para ver la deuda de SIIC test (BKLDTA) vía la API Nest de la .88.
// Sin dependencias: `node server.mjs` → http://localhost:5180
// El token NO va al navegador: este proxy lo agrega. Solo deja pasar GET a /v1/clientes...
import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = dirname(fileURLToPath(import.meta.url));
const PUERTO = Number(process.env.PORT ?? 5180);
const API = process.env.API_URL ?? 'http://10.1.1.88:6012';
const archivoToken = join(DIR, '.token');
const TOKEN = (process.env.API_TOKEN ?? (existsSync(archivoToken) ? readFileSync(archivoToken, 'utf8') : '')).trim();

if (!TOKEN) {
  console.error('Falta el token: definí API_TOKEN o creá el archivo .token (ACCEPTED_SECRETS de /opt/cessa-api-siicnest/.env.test en la .88).');
  process.exit(1);
}

/** Rutas de lectura permitidas (test tiene escrituras habilitadas: no dejar pasar nada más). */
const PERMITIDAS = [/^\/v1\/clientes$/, /^\/v1\/clientes\/\d+$/, /^\/v1\/clientes\/\d+\/deuda$/];

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');

  if (url.pathname === '/' || url.pathname === '/index.html') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    return res.end(readFileSync(join(DIR, 'index.html')));
  }

  if (url.pathname.startsWith('/api/')) {
    const ruta = url.pathname.slice(4);
    if (req.method !== 'GET' || !PERMITIDAS.some((r) => r.test(ruta))) {
      res.writeHead(403, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ message: 'Ruta no permitida en este front (solo lectura)' }));
    }
    try {
      const r = await fetch(API + ruta + url.search, {
        headers: { Authorization: TOKEN, Accept: 'application/json' },
        signal: AbortSignal.timeout(60_000),
      });
      res.writeHead(r.status, { 'content-type': r.headers.get('content-type') ?? 'application/json' });
      return res.end(Buffer.from(await r.arrayBuffer()));
    } catch (e) {
      res.writeHead(502, { 'content-type': 'application/json' });
      return res.end(JSON.stringify({ message: `No se pudo llegar a ${API}: ${e.message}` }));
    }
  }

  res.writeHead(404).end();
}).listen(PUERTO, () => console.log(`Deuda SIIC test → http://localhost:${PUERTO}  (API ${API})`));
