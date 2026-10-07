# Trabajar en la .88 por SSH — gateway de pagos web (cobranza-cessa)

Guía para retomar desde la .88 (o cualquier equipo con SSH a ella). Estado al **2026-10-06**.
Pendientes para producción, con diagrama del flujo: documento "Pagos QR web vía gateway:
pendientes para producción" (claude.ai, compartido por Dario).

## 1. Estado

| Qué | Dónde | Estado |
|---|---|---|
| Gateway (`cobranza-cessa`) | `/opt/cobranza-cessa` en la .88, `test01.cessa.com.bo` | Desplegado. Es de **test**: paga en api-cobranzas-test. |
| Deuda "como banco" | `GET /api/externo/consulta/cliente/` (X-Api-Key) | En producción del gateway de test desde 2026-10-06. |
| cessa-laravel (`CessaLanding`) | Hostinger | Commit `d6dce24` en `main` (flag `COBRANZAS_DEUDA_VIA_GATEWAY`), **sin desplegar**. |
| Front de deuda | `tools/deuda-test-front/` | Ver §7. |

## 2. Cadena de pagos en test (confirmada 2026-10-06)

```
cessa-laravel (Hostinger)
  │  deuda: GET  /api/externo/consulta/cliente/      pago: POST /api/externo/recibos-web/liquidar/
  ▼
gateway .88 (gunicorn 127.0.0.1:8001, nginx /api/externo/)
  │  deuda: GET /v1/consulta/deuda                   pago: POST /v1/transacciones + PUT …/pagar-otro-documento
  ▼
api-cobranzas test (Lumen)  10.1.1.102:6004   ~/public_html/cessa-api-cobranzas_test-2   login CABISAQR
  │  SIIC_SERVICE_BASE_URL=https://10.1.1.18:6003
  ▼
SIIC test (Lumen)  10.1.1.18:6003   ~/public_html/cessa-api-siic_test   DSN AS400DSNTEST, usuario PRCESSA
  ▼
BKLDTA (AS400 10.1.1.7): COMSAL (COMPLOC=999 al pagar), CNN30001, CNN3000, TCNN3050/3051
```

- La deuda y el pago salen del **mismo** SIIC. Antes cessa-laravel leía SIIC **prod**
  (`CESSA_API_URL=api-siic-prod-1`) y pagaba en test → "La deuda no existe con los datos
  proporcionados" (solicitud #26, cliente 115997, NC 20253/20254 que solo existen en prod).
- Un pago real en test (solicitud #23, transacción 3894, lote 6, caja 8198, 05/10 14:51)
  se grabó con commit en BKLDTA y aparece en el cierre de caja. Que la deuda vuelva a
  aparecer después es normal: **el administrador del AS400 revierte COMSAL para repetir pruebas**.
- La API Nest de test (`:6012`) lee la misma BKLDTA que la .18: sirve para verificar.

## 3. Acceso y reglas

- SSH: `ssh soporte@10.1.1.88`. `sudo` pide contraseña (pedirla al responsable, no guardarla).
- `/opt/cobranza-cessa` es de `cobranza:cobranza` → todo con `sudo -u cobranza`.
- Comandos de Django **siempre** con `DJANGO_SETTINGS_MODULE=config.settings.production`
  (el default de `manage.py` es dev, sin avisar).
- Los demás servicios de la .88 (biométricos, API Nest prod, bot) **no se tocan**.
- .102 y .18: usuario `rootcode`, sin llave para nosotros; los datos se piden a quien tiene acceso.

Atajo para la sesión:
```bash
cd /opt/cobranza-cessa
dj() { sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py "$@"; }
```

## 4. Actualizar el gateway

```bash
cd /opt/cobranza-cessa
sudo -u cobranza git pull --ff-only          # la .88 llega a GitHub
dj showmigrations | grep '\[ \]'             # pendientes → dj migrate
sudo systemctl restart cobranza-cessa-gunicorn cobranza-cessa-celery-worker cobranza-cessa-celery-beat
systemctl is-active cobranza-cessa-gunicorn cobranza-cessa-celery-worker cobranza-cessa-celery-beat
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/api/   # 401 = vivo
```
Si el cambio solo toca `docs/` o `tools/`, alcanza con el `git pull` (sin reinicio).

## 5. Tests

El `.venv` de `/opt` es de producción (sin pytest). Venv aparte, con SQLite, sin tocar
Postgres ni el servicio:
```bash
D=/tmp/cobranza-test; rm -rf $D/src; mkdir -p $D/src
git -C /opt/cobranza-cessa archive HEAD | tar x -C $D/src     # o copiar el árbol a probar
[ -x $D/venv/bin/python ] || python3 -m venv $D/venv
$D/venv/bin/pip install -q -r $D/src/requirements/dev.txt
cd $D/src && DATABASE_URL=sqlite:///$D/t.db SECRET_KEY=x ../venv/bin/python -m pytest -q
```
`/tmp` se borra al reiniciar la .88: si falta el venv, se recrea con lo de arriba.
Al 2026-10-07: 185 tests verdes.

## 6. Diagnosticar un pago

```bash
dj ver_liquidaciones                                  # las 5 más nuevas
dj ver_liquidaciones --cliente 115997 --detalle --remoto
dj verificar_cobranzas_banco                          # credenciales, caja del día y catálogos (solo lectura)
journalctl -u cobranza-cessa-gunicorn -n 50 --no-pager
sudo grep externo /var/log/nginx/access.log | tail    # POST de cessa-laravel (User-Agent GuzzleHttp)
```

Cómo leer el resultado:

| Error en la solicitud | Causa | Qué hacer |
|---|---|---|
| `La deuda no existe con los datos proporcionados` | Documentos leídos de otro SIIC (prod) o deuda que cambió desde el QR | Que cessa-laravel lea la deuda por el gateway (flag) |
| `El operador no puede aperturar caja fuera de horario (07:50–18:50)` | Pago fuera de horario de caja | Se reintenta; decisión pendiente para prod |
| `ha fallado previamente` | La transacción en api-cobranzas quedó FALLIDA | El gateway crea una nueva al reintentar |
| Errores OAuth en el log de la .102 | El gateway renueva su token | Normal si después el pago sigue |

Logs fuera de la .88 (los lee quien tiene acceso):

- .102: `~/public_html/cessa-api-cobranzas_test-2/storage/logs/lumen-AAAA-MM-DD.log`
- .18: `~/public_html/cessa-api-siic_test/storage/logs/lumen-AAAA-MM-DD.log`.
  Cada pago deja pasos `[md5] 1.`…`12. Commit de la transacción`. Si llega al paso 12, quedó
  grabado en BKLDTA. Si falta algún paso, ahí se cortó.

## 7. Ver la deuda en vivo (front de solo lectura)

```bash
python3 /opt/cobranza-cessa/tools/deuda-test-front/server.py      # 127.0.0.1:5180
```
Desde la PC: `ssh -L 5180:127.0.0.1:5180 soporte@10.1.1.88` → <http://localhost:5180>.
Toma el token de `/opt/cessa-api-siicnest/.env.test`. Detalles en `tools/deuda-test-front/README.md`.

Clientes de test con deuda (06/10): 101194, 101591, 102782, 105164, 107943, 115997, 153896.

## 8. Probar el flujo completo con cessa-laravel

1. Desplegar `CessaLanding` (`main`) en el test de Hostinger con
   `COBRANZAS_DEUDA_VIA_GATEWAY=true` (además de `COBRANZAS_GATEWAY_BASE_URL` y `_API_KEY`).
2. Consultar la deuda en la web: tiene que coincidir con el front (§7).
3. Pagar con QR un cliente de test; `dj ver_liquidaciones -n 1 --remoto` → `facturado`.
4. ↻ Actualizar en el front: la deuda baja.

Si el panel de cajeras también tiene que leer la deuda por la .102:
`DEUDA_CLIENT_CLASS=services.deuda_client.CobranzasBancoDeudaClient` en el `.env` + reinicio.

## 9. Panel de cajera (cobro en ventanilla)

Pantalla **Cobrar** (`/deuda` del frontend `cobranza-cessa-frontend`), desde 2026-10-07:

- Lista los comprobantes pendientes en el orden en que SIIC exige pagarlos. Se cobra un
  **prefijo**: marcar uno marca los anteriores. Las notas de crédito restan en su lugar.
- Backend: `cantidad_comprobantes` en `POST /api/cobros-efectivo/` y `/api/transacciones-qr/`;
  cada cobro guarda `items_cobrados` y al facturar se pagan **solo esos** (antes un cobro
  parcial mandaba a pagar la deuda entera). Un `monto` suelto solo se acepta si coincide con
  el total de un prefijo.
- Efectivo: montos sugeridos, vuelto en vivo y desglose en billetes/monedas. Teclado: Enter
  busca y cobra, Esc pasa al siguiente cliente.
- **Varios clientes en un pago** (efectivo): se buscan uno tras otro y se suman al cobro;
  `POST /api/cobros-agrupados/` registra todo o nada. Cada cliente queda como su propio
  `CobroEfectivo` (con su factura y su transacción en api-cobranzas) dentro de un
  `CobroAgrupado` que guarda lo recibido y el vuelto. El QR sigue siendo de a un cliente.
- **Consumo (kWh)** en la deuda y en las facturas anteriores: sale de `/v1/clientes/{c}/facturas`
  del SIIC, cruzado por período + importe (best effort; pagadas: solo las 12 más nuevas).
- **Facturas anteriores**: todas las pagadas (`/v1/clientes/{c}/pagos`) con PDF real.
- **F9 en cualquier pantalla**: reimprime el comprobante del último cobro del usuario
  (`GET /api/comprobantes/ultimo/`: efectivo, grupo o QR pagado). También hay botón en Cobrar.

Desplegar el frontend (el `dist/` va en el repo; `/opt/cobranza-cessa-frontend` es de root):
```bash
sudo git -C /opt/cobranza-cessa-frontend -c safe.directory=/opt/cobranza-cessa-frontend pull --ff-only
```
nginx sirve `dist/` directo: no hace falta reiniciar nada.

## 10. Pendientes

- **Producción:** gateway de prod (.102 prod + SIIC prod + credenciales propias), alta de CESSA Web
  como banco, horario y cierre de caja, deuda que cambia entre QR y pago, anulaciones, alertas.
  Detalle en el documento de pendientes.
- **cessa-laravel:** `ultimosPagos()` / `comprobantePdf()` siguen leyendo `CESSA_API_URL`
  (en test muestran datos de prod).
- **SIIC test:** usa el usuario DB2 `PRCESSA`, que ya tuvo intentos fallidos. Si se bloquea, se cortan los pagos de test.
