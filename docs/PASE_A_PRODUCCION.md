# Pase a producción — pagos QR web vía gateway

Qué cambiar, en qué orden y cómo verificarlo. En test el flujo completo funciona
(cessa-laravel → gateway .88 → api-cobranzas .102 → SIIC → BKLDTA, pagos BISA y BNB
facturados). Guía de trabajo diario: `docs/TRABAJO_REMOTO.md`.

## Estado al 2026-10-09

**Listo (apagado):** la instancia de producción está preparada en la .88 por
`deploy/instalar_prod.sh`: `/opt/cobranza-cessa-prod`, base `cobranza_cessa_produccion`, Redis `/1`,
gunicorn `:8002`, `.env` con claves nuevas y el SIIC prod de lectura (:6013, responde), CA de
Cloudflare Origin instalada, servicios `cobranza-cessa-prod-*` **deshabilitados** y sitio nginx
`cobranza-cessa-prod` **sin enlazar**. `verificar_produccion` da hoy 10 errores: son exactamente los
datos que faltan (abajo). En test: verificación de deuda y monto antes de pagar, alertas por
Telegram, conciliación diaria y pantalla "Pagos web" funcionando.

**Falta (bloquea el pase):**

| Quién | Qué |
|---|---|
| Administrador SIIC / .102 | Confirmar que producción es `.102:6002`; alta de CESSA Web como banco (cliente OAuth id/secreto, usuario cajero y contraseña, sigla de agencia); horario y cierre de esa caja |
| Red de CESSA | Dominio público del gateway prod (DNS Cloudflare + NAT a la .88:443); opcional: activar el vhost `api-cobranzas-prod-6002` (si no, opción B de §2: línea en `/etc/hosts`) |
| Contabilidad | Confirmar el ente del documento (test: 3 "DEPOSITO"); los ids de banco BISA/BNB salen del catálogo prod con `verificar_produccion` |
| BNB / tesorería | Cuenta de destino en bolivianos (`BNB_QR_SIMPLE_DESTINATION_ACCOUNT_ID`, hoy `2`) |
| Hostinger (FileZilla) | Subir CessaLanding `90d2868` (+ `public/build`); al pasar, el `.env` de §4 (la API key del gateway prod está en `/opt/cobranza-cessa-prod/.env`) |
| Decisión | Si el panel de cajeras sale a prod o primero solo el pago web (§6; recomendado: solo pago web) |

**El día del pase:** completar `/opt/cobranza-cessa-prod/.env` (§3) → `/etc/hosts` si se usa la
opción B → `verificar_produccion` en **0 errores** (§5) → encender (§2 "Encender") → cessa-laravel
prod (§4) → un pago real chico por BISA y otro por BNB → `conciliar_pagos` del día siguiente sin
diferencias.

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
| Administrador SIIC / .102 | Confirmar que la api-cobranzas de producción es `.102:6002` (§2: es la que responde como prod) y activar su vhost en el proxy (opción A), o aceptar la opción B | `COBRANZAS_BANCO_BASE_URL` |
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

Preparar (o actualizar) la instancia, **sin encenderla**:
```bash
sudo bash /opt/cobranza-cessa/deploy/instalar_prod.sh
```
Idempotente: clona/actualiza el código en `/opt/cobranza-cessa-prod`, crea el entorno y la base
`cobranza_cessa_produccion`, genera el `.env` de prod (solo si no existe: claves nuevas, Redis `/1`,
SIIC Nest prod, lo de api-cobranzas vacío), instala la CA de Cloudflare Origin, migra, y deja los
servicios `cobranza-cessa-prod-*` y el sitio nginx `cobranza-cessa-prod` **creados pero apagados**.

### api-cobranzas de producción: dirección y certificado (relevado 2026-10-09)

| Dirección | Qué responde |
|---|---|
| `10.1.1.102:6002` | **producción** ("Lumen …" sin "TEST"); certificado Cloudflare Origin `*.bo-com-assec.net` (no verificable por IP) |
| `10.1.1.102:443` | producción (idem) |
| `10.1.1.102:6001` | nada |
| `api-cobranzas-prod-6002.bo-com-assec.net` | **la de TEST** (su vhost en el proxy no está activo y cae al default) |

Dos formas de llegar a prod con TLS verificado:
- **A — la red activa el vhost** `api-cobranzas-prod-6002` en el proxy → `COBRANZAS_BANCO_BASE_URL=https://api-cobranzas-prod-6002.bo-com-assec.net`, `COBRANZAS_BANCO_VERIFY=true`.
- **B — directo a la .102 (probado):** en la .88 `/etc/hosts` con `10.1.1.102 api-cobranzas-prod-6002.bo-com-assec.net`,
  `COBRANZAS_BANCO_BASE_URL=https://api-cobranzas-prod-6002.bo-com-assec.net:6002` y
  `COBRANZAS_BANCO_VERIFY=/etc/ssl/certs/cloudflare-origin-ca-root.pem` (lo que deja el `.env` generado).

En los dos casos `verificar_produccion` lee la portada de la API y da **ERROR si responde "TEST"**.

### Encender (con el `.env` completo y `verificar_produccion` en 0 errores)
```bash
sudo systemctl enable --now cobranza-cessa-prod-gunicorn cobranza-cessa-prod-celery-worker cobranza-cessa-prod-celery-beat
sudo sed -i 's/DOMINIO_PROD/<dominio>/' /etc/nginx/sites-available/cobranza-cessa-prod
sudo ln -s /etc/nginx/sites-available/cobranza-cessa-prod /etc/nginx/sites-enabled/ && sudo nginx -t && sudo systemctl reload nginx
```
(nginx atiende otros servicios de la .88: `nginx -t` antes de recargar; recargar no corta conexiones.)

## 3. `.env` del gateway de producción

| Variable | Test (hoy) | Producción |
|---|---|---|
| `SECRET_KEY` | (la de test) | **nueva** (`python -c "import secrets;print(secrets.token_urlsafe(50))"`) |
| `ALLOWED_HOSTS` | `10.1.1.88,localhost,127.0.0.1,test01.cessa.com.bo` | `localhost,127.0.0.1,<dominio prod>` |
| `DATABASE_URL` | `.../cobranza_cessa` | `.../cobranza_cessa_produccion` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | `redis://localhost:6379/1` |
| `API_KEY_CESSA_LARAVEL` | la de test | **nueva** (la misma va a `COBRANZAS_GATEWAY_API_KEY` de cessa-laravel prod) |
| `COBRANZAS_BANCO_BASE_URL` / `_VERIFY` | `https://api-cobranzas-test.bo-com-assec.net` / `true` | opción A o B de §2 |
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
| `ALERTAS_TELEGRAM_BOT_TOKEN` / `ALERTAS_TELEGRAM_CHAT_IDS` | (sin configurar) | bot y chats del equipo que reciben los avisos de pagos sin factura |
| `ALERTAS_EMAIL_DESTINOS` | (sin configurar) | correos que reciben los avisos (requiere `EMAIL_HOST` etc.) |
| `PANEL_URL` | (sin configurar) | URL del panel para el enlace de las alertas |

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
