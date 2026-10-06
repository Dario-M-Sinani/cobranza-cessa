# Front de deuda de SIIC test

Página de solo lectura para ver la deuda de BKLDTA (SIIC test) y confirmar que baja al
pagar. Lee de la API Nest de test (`:6012`); el token lo agrega el servidor y nunca llega
al navegador. Solo deja pasar `GET /v1/clientes`, `/v1/clientes/{n}` y
`/v1/clientes/{n}/deuda`.

## En la .88 (Python, sin dependencias)

```bash
cd /opt/cobranza-cessa            # o cualquier copia del repo
python3 tools/deuda-test-front/server.py
```

Escucha en `127.0.0.1:5180` y toma el token de `/opt/cessa-api-siicnest/.env.test`.
Desde la PC: `ssh -L 5180:127.0.0.1:5180 soporte@10.1.1.88` y abrir <http://localhost:5180>.

## En la PC (Node)

```powershell
$env:API_URL = 'http://10.1.1.88:6012'; $env:API_TOKEN = '<ACCEPTED_SECRETS de .env.test>'
& "C:\Program Files\nodejs\node.exe" tools\deuda-test-front\server.mjs
```

O guardar el token en `tools/deuda-test-front/.token` (ignorado por git).

## Uso

- Arranca con los clientes de test conocidos. **Agregar** suma números; **Explorar rango**
  consulta de a 500 (máx. 20 000 por corrida).
- **↻ Actualizar** vuelve a consultar los clientes de la tabla: pagar en test, apretar y
  ver que la deuda baja.
- El total de la tabla sale de `/v1/clientes` y no incluye notas de débito/crédito
  (tipos 30/31); el detalle del cliente (clic en la fila) sí, y es lo que se cobra.
