# Pase a producción — pagos QR web vía gateway

Qué cambiar, en qué orden y cómo verificarlo. Estado al **2026-10-08**: en test el flujo
completo funciona (cessa-laravel → gateway .88 → api-cobranzas .102 → SIIC → BKLDTA, pagos
BISA y BNB facturados). Guía de trabajo diario: `docs/TRABAJO_REMOTO.md`.

```
cessa-laravel (Hostinger, prod)
  │  deuda / historial / PDF:  GET  /api/externo/consulta/...      ┐  X-Api-Key de PROD
  │  pago:                     POST /api/externo/recibos-web/liquidar/ ┘
  ▼
gateway PROD (.88, instancia aparte)  ── deuda/pago como banco ──►  api-cobranzas PROD (.102)  ──►  SIIC prod (LIBSUCDTA)
                                      ── historial, PDF, kWh ────►  API Nest prod :6013 (solo lectura)
```

## 1. Lo que no depende de nosotros (bloquea el pase)

| Quién | Qué | Para qué variable |
|---|---|---|
| Administrador SIIC / .102 | Cuál es la api-cobranzas de **producción** real (en la .102 hay `prod-1` :6001, `prod-2` :6002, `prod` :443) y a qué SIIC prod apunta | `COBRANZAS_BANCO_BASE_URL` |
| Administrador SIIC / .102 | Alta de **CESSA Web como banco**: usuario cajero (rol CAJA), caja, agencia, cliente OAuth | `COBRANZAS_BANCO_CLIENT_ID/_SECRET/_USERNAME/_PASSWORD`, `_AGENCIA_SIGLA` |
| Administrador SIIC | Horario de la caja web (hoy 07:50–18:50: lo pagado de noche se factura al otro día) y quién la cierra cada día | — |
| Contabilidad | Confirmar el **ente** del documento (en test, `ENTE_ID=3` = "DEPOSITO") | `COBRANZAS_BANCO_DOCUMENTO_ENTE_ID` |
| BNB / tesorería | A qué cuenta debe llegar el dinero en bolivianos (`BNB_QR_SIMPLE_DESTINATION_ACCOUNT_ID` está en `2`, que según la config sería la cuenta en dólares) | cessa-laravel |
| Red de CESSA | Dominio público del gateway de prod (DNS en Cloudflare + NAT a la .88:443), por ejemplo `pagos.cessa.com.bo` | nginx, `ALLOWED_HOSTS`, `COBRANZAS_GATEWAY_BASE_URL` |

## 2. Gateway de producción (instancia aparte en la .88)

El gateway de `/opt/cobranza-cessa` queda como **test**. Producción es otra copia del mismo
código, con su base, su Redis, su puerto y su dominio: así se prueba en test sin tocar prod.

| | Test (hoy) | Producción |
|---|---|---|
| Carpeta | `/opt/cobranza-cessa` | `/opt/cobranza-cessa-prod` |
| Servicios | `cobranza-cessa-{gunicorn,celery-worker,celery-beat}` | `cobranza-cessa-prod-{gunicorn,celery-worker,celery-beat}` |
| Gunicorn | `127.0.0.1:8001` | `127.0.0.1:8002` |
| Base Postgres | `cobranza_cessa` | `cobranza_cessa_produccion` (nueva, vacía) |
| Redis (Celery) | `redis://localhost:6379/0` | `redis://localhost:6379/1` (**distinto**: si no, se mezclan las tareas) |
| Dominio | `test01.cessa.com.bo` | el que asigne la red (ej. `pagos.cessa.com.bo`) |

Pasos (como `soporte`, con sudo):
```bash
sudo -u cobranza git clone https://github.com/Dario-M-Sinani/cobranza-cessa.git /opt/cobranza-cessa-prod
cd /opt/cobranza-cessa-prod && sudo -u cobranza python3 -m venv .venv
sudo -u cobranza .venv/bin/pip install -r requirements/production.txt
sudo -u postgres createdb -O <usuario_db> cobranza_cessa_produccion
sudo -u cobranza cp /opt/cobranza-cessa/.env .env      # y editar TODO lo de la tabla de §3
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py migrate
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py collectstatic --noinput
# units: copiar deploy/*.service como cobranza-cessa-prod-*.service, cambiando
#   /opt/cobranza-cessa -> /opt/cobranza-cessa-prod  y  --bind 127.0.0.1:8001 -> 127.0.0.1:8002
sudo systemctl daemon-reload && sudo systemctl enable --now cobranza-cessa-prod-gunicorn cobranza-cessa-prod-celery-worker cobranza-cessa-prod-celery-beat
# nginx: server nuevo para el dominio de prod, SOLO con `location /api/externo/` -> 127.0.0.1:8002
#   (el panel de cajeras y /admin no se exponen en el dominio de prod)
```

## 3. `.env` del gateway de producción

| Variable | Test (hoy) | Producción |
|---|---|---|
| `SECRET_KEY` | (la de test) | **nueva** (`python -c "import secrets;print(secrets.token_urlsafe(50))"`) |
| `ALLOWED_HOSTS` | `10.1.1.88,localhost,127.0.0.1,test01.cessa.com.bo` | `localhost,127.0.0.1,<dominio prod>` |
| `DATABASE_URL` | `.../cobranza_cessa` | `.../cobranza_cessa_produccion` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | `redis://localhost:6379/1` |
| `API_KEY_CESSA_LARAVEL` | la de test | **nueva** (la misma va a `COBRANZAS_GATEWAY_API_KEY` de cessa-laravel prod) |
| `COBRANZAS_BANCO_BASE_URL` | `https://api-cobranzas-test.bo-com-assec.net` | la api-cobranzas prod (§1) |
| `COBRANZAS_BANCO_CLIENT_ID` / `_SECRET` | test | los de prod (§1) |
| `COBRANZAS_BANCO_USERNAME` / `_PASSWORD` | `CABISAQR` (cajero BISA QR de test) | el cajero de CESSA Web (§1) |
| `COBRANZAS_BANCO_AGENCIA_SIGLA` | `WEB` | la que den con el alta |
| `COBRANZAS_BANCO_DOCUMENTO_ENTE_ID` | `3` (DEPOSITO) | confirmar con contabilidad |
| `COBRANZAS_BANCO_DOCUMENTO_BANCO_ID` | `4` | el de BISA en el catálogo prod |
| `COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BISA` / `_BNB` | `4` / `5` | los ids de BANCO BISA / BANCO NACIONAL en el catálogo **prod** |
| `SIIC_DEUDA_BASE_URL` | `http://127.0.0.1:6012` (Nest test) | `http://127.0.0.1:6013` (Nest prod: las lecturas funcionan, probado 08/10) |
| `SIIC_DEUDA_TOKEN` | token de test | token aceptado por la API Nest prod |
| `DEUDA_CLIENT_CLASS` | `SiicDeudaClient` | igual (solo lo usa el panel de cajeras) |
| `MC4_*` | SIP de test | solo si el panel de cajeras va a prod (§6) |
| `SECURE_SSL_REDIRECT` / `*_COOKIE_SECURE` | `False` | `False` (TLS lo termina nginx/Cloudflare) |

Los ids de banco y ente los muestra `manage.py verificar_produccion` (§5) leyendo el catálogo.

## 4. cessa-laravel (Hostinger, por FileZilla)

Código: `main` actual (incluye `d6dce24` deuda vía gateway y `c7c1756` historial/PDF vía gateway).

| Variable | Producción |
|---|---|
| `COBRANZAS_GATEWAY_BASE_URL` | `https://<dominio prod del gateway>` |
| `COBRANZAS_GATEWAY_API_KEY` | igual a `API_KEY_CESSA_LARAVEL` del gateway prod |
| `COBRANZAS_DEUDA_VIA_GATEWAY` | `true` |
| `COBRANZAS_FACTURACION_ENABLED` | `true` |
| `PAGOS_SIMULACION_HABILITADA` | **`false`** (si no, se puede "pagar" sin dinero y factura en prod) |
| `CESSA_API_URL` / `CESSA_API_TOKEN` | SIIC prod (catálogos, trámites, calculadora) |
| `SIP_*` | credenciales SIP/BISA de **producción**; el callback de SIP apuntando a la web prod |
| `BNB_QR_SIMPLE_BASE_URL` | `https://marketapi.bnb.com.bo` |
| `BNB_QR_SIMPLE_ACCOUNT_ID` / `_AUTHORIZATION_ID` | los de producción |
| `BNB_QR_SIMPLE_DESTINATION_ACCOUNT_ID` | el que confirme tesorería (§1) |
| `APP_ENV` / `APP_DEBUG` | `production` / `false` |

Después de cambiar el `.env`: borrar `bootstrap/cache/config.php` si existe, y confirmar que
corre el scheduler de Laravel (cron de Hostinger), que es el que consulta el estado de los QR
BNB y SIP cada minuto.

## 5. Verificar antes de abrir

```bash
cd /opt/cobranza-cessa-prod
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py verificar_produccion --cliente <nro con deuda>
```
Tiene que terminar con **0 errores**. Revisa la configuración (URLs y usuario que no sean de
test, keys), el login en api-cobranzas prod, que los ids de banco sean BANCO BISA / BANCO
NACIONAL y que el ente exista, el SIIC de la deuda y una consulta de deuda como banco. En el
gateway de test se corre con `--entorno test` (al 08/10: 0 errores; en modo prod marca los 3
cambios pendientes: URL de api-cobranzas, usuario CABISAQR y SIIC de test).

Luego, un **pago real controlado**: un cliente con deuda chica, pagar **un** comprobante con
QR (BISA y otro con BNB), y comprobar:
1. `manage.py ver_liquidaciones -n 1 --remoto` → `facturado`, transacción PAGADA;
2. la deuda del cliente bajó (API Nest :6013);
3. el banco del documento en el SIIC es el correcto;
4. al día siguiente, el cierre de la caja web cuadra con lo cobrado.

## 6. Decisión pendiente: el panel de cajeras

El panel (pantalla Cobrar, efectivo con vuelto, varios clientes) vive en la misma aplicación,
pero factura con el **mismo usuario de banco** del gateway. Para producción hay que decidir si
las cajeras de CESSA cobran por el panel (y con qué usuario/caja del SIIC: el propio de cada
cajera o uno compartido) o si en una primera etapa solo sale a prod el pago web. Mientras no
se decida, el dominio de prod expone solo `/api/externo/` y el panel sigue en test.

## 7. Volver atrás

- cessa-laravel: `COBRANZAS_DEUDA_VIA_GATEWAY=false` y `COBRANZAS_FACTURACION_ENABLED=false`
  (los QR se siguen cobrando; la factura queda pendiente y se procesa al reactivar).
- Gateway prod: `sudo systemctl stop cobranza-cessa-prod-*`; el de test no se toca.
