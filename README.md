# Panel de Cobranza CESSA

Backend Django + DRF para un panel de cobranza: consulta de deuda, generación
de QR de pago (MC4/SIP), confirmación de pago y emisión de factura real
contra `api-cobranzas-bancos` (SIIC) — para los cobros propios de este panel
(efectivo/QR de mostrador) y también, vía `apps/facturacion_externa`, como
**gateway para que `cessa-laravel` (Hostinger) registre sus propios pagos QR
web como factura real**, algo que no puede hacer directo por no tener ruta a
la red interna de CESSA. Por eso este proyecto va a correr **dentro de esa
red interna, en `10.1.1.88`** — no en un VPS externo como se planeó al
principio — independiente de todas formas de `cessa-laravel` (que tiene su
propio flujo de cobros QR en producción, vía Filament): este panel cubre otro
propósito (cajera de mostrador) y corre en infraestructura separada.

El frontend (React + Vite) vive en `../cobranza-cessa-frontend/`, repo
separado a propósito.

## Stack

- Django 5 + Django REST Framework, auth JWT (`djangorestframework-simplejwt`)
- PostgreSQL
- Celery + Redis para tareas asíncronas
- Frontend: SPA React separada, no incluida en este repo

## Estado actual

Modelos + migraciones, admin, capa de servicios externos (MC4/SIP, consulta
de deuda real, y `api-cobranzas-bancos` real), servicios de dominio
(`apps/cobranza/services.py`), vistas DRF + permisos por rol, y las tareas
Celery periódicas que confirman pagos y facturan de verdad. Ver "Endpoints" y
"Próximos pasos" abajo para lo que falta.

**Facturación real contra `api-cobranzas-bancos` ya implementada y probada**
(2026-09-09) para dos orígenes: los cobros propios de este panel
(`apps.cobranza.services.enviar_factura_a_siic`, endpoint `/pagar` a secas) y,
vía `apps/facturacion_externa`, los pagos QR web de `cessa-laravel`
(`liquidar_solicitud`, endpoint `/pagar-otro-documento`) — ver "Gateway para
cessa-laravel" abajo. 117 tests pasando; verificado en vivo contra
`api-cobranzas-test` (transacciones reales creadas, llegó hasta la validación
de negocio real del SIIC). Falta lo organizacional: credenciales de
producción (usuario Cajero + Caja reales, hoy se usa el mismo `CABISAQR` de
`-test`) y desplegar esto donde `api-cobranzas-bancos` de producción sea
alcanzable (`10.1.1.88`, ver "Despliegue en producción" abajo).

Backend de **apertura/cierre de caja** (turno de cobro) también implementado:
modelo `Caja` con máquina de estados (mismo patrón que `TransaccionQR`),
reglas de reapertura restringidas a supervisor/admin, horario operativo
configurable, y auditoría automática de aperturas fuera de horario
(`AperturaCajaFueraDeHorario`). Ver requisitos/historias completas en
`REQUISITOS_COBRANZA.md` y `HISTORIAS_USUARIO.md` — todavía sin UI en el SPA
y sin conectar a los cobros (QR/efectivo).

## Gateway para cessa-laravel (`apps/facturacion_externa`)

`cessa-laravel` corre en Hostinger (hosting compartido) y nunca pudo alcanzar
`api-cobranzas-bancos` directo (red interna de CESSA). Le pide a este backend
que lo haga por él:

- `POST /api/externo/recibos-web/liquidar/` — recibe el snapshot de un
  `Recibo` ya pagado (alias, nro_cliente, monto, moneda, detalle crudo de
  deuda, fecha_pago) y lo liquida contra `api-cobranzas-bancos`. Idempotente
  por `alias`. Auth: header `X-Api-Key` (no JWT, es server-a-servidor) contra
  `API_KEY_CESSA_LARAVEL`.
- `GET /api/externo/recibos-web/<alias>/` — estado de una liquidación.
- `GET /api/externo/recibos-web/<alias>/comprobante/` — PDF del comprobante.
- `GET /api/externo/recibos-web/<alias>/comprobante-json/` — datos
  estructurados del comprobante (nro_factura, cliente, etc.), para el ticket
  imprimible de `ComprobanteTicketController` del lado Laravel.

Del lado `cessa-laravel`: `App\Services\Cobranzas\CobranzasGatewayClient`
(config `services.cobranzas.gateway_base_url`/`gateway_api_key`,
`.env`: `COBRANZAS_GATEWAY_BASE_URL`/`COBRANZAS_GATEWAY_API_KEY`) es el único
punto que hay que apuntar a la URL real de este backend una vez desplegado.

## Endpoints

Autenticación JWT (`simplejwt`):

- `POST /api/auth/token/` — obtener access/refresh
- `POST /api/auth/token/refresh/`

Cobranza:

- `POST /api/deudas/consultar/` `{codigo_externo}` — crea un nuevo snapshot de `Deuda`
- `GET/POST /api/transacciones-qr/` — listar (propias para cajera, todas para
  supervisor/admin) / generar QR (solo cajera activa, `{deuda_id}`)
- `POST /api/transacciones-qr/{id}/cancelar/`
- `POST /api/transacciones-qr/{id}/reintentar_facturacion/` — solo supervisor/admin
- `GET /api/transacciones-qr/exportar_csv/` — solo supervisor/admin, exporta a CSV
  (acepta `?estado=pagado` etc.)
- `GET /api/facturas/` — listar (mismo alcance por rol que transacciones)

Cobro en efectivo (ver `HISTORIAS_USUARIO.md` Épica D):

- `GET/POST /api/cobros-efectivo/` — listar (propias para cajera, todas para
  supervisor/admin) / registrar (solo cajera activa, `{deuda_id,
  monto_recibido}`) — se confirma al instante y crea su `Factura` en la
  misma llamada, a diferencia del QR

Caja (turno de cobro, ver `HISTORIAS_USUARIO.md` Épica E):

- `GET/POST /api/cajas/` — listar (propias para cajera, todas para
  supervisor/admin) / abrir caja. Cajera activa: abre la suya (sin body).
  Supervisor/admin: puede abrir la de un cajero puntual con
  `{cajero_id, motivo}` (`motivo` obligatorio si es fuera del horario
  operativo).
- `POST /api/cajas/{id}/cerrar/` — solo el cajero dueño de la caja
- `POST /api/cajas/{id}/reabrir/` — solo supervisor/admin, nunca el mismo
  cajero que la cerró; `{motivo}` obligatorio si es fuera de horario
- `GET /api/cajas/aperturas_fuera_de_horario/` — solo supervisor/admin,
  listado auditable de aperturas/reaperturas fuera de horario con motivo

Usuarios (solo admin): `GET/POST/PATCH/DELETE /api/usuarios/`

## Tareas Celery

`apps.cobranza.tasks.verificar_transacciones_pendientes` (confirma pagos QR
contra MC4/SIP) y `enviar_facturas_pendientes` (envía Facturas pendientes/en
error a `api-cobranzas-bancos`, tope de 5 intentos automáticos) corren cada
60s (`CELERY_BEAT_SCHEDULE` en `config/settings/base.py`) y usan el scheduler
de archivo por defecto de Celery. Para correrlas localmente:

```bash
celery -A config worker -l info
celery -A config beat -l info
```

## Estructura

```
cobranza_cessa/
├── config/                 # settings (base/dev/production), urls, wsgi/asgi, celery.py
├── apps/
│   ├── usuarios/            # Usuario (rol: cajera/supervisor/admin, activo)
│   ├── clientes/             # Cliente
│   ├── cobranza/            # Deuda, TransaccionQR, CobroEfectivo, Caja, Factura
│   ├── facturacion_externa/ # gateway para cessa-laravel (SolicitudLiquidacion)
│   └── auditoria/            # LogAuditoria
├── services/
│   ├── mc4_client.py           # integración MC4/SIP (stub documentado)
│   ├── deuda_client.py         # consulta de deuda vía SIIC (real)
│   └── cobranzas_banco_client.py  # api-cobranzas-bancos (real)
├── deploy/                 # plantillas systemd (gunicorn/celery) + nginx para 10.1.1.88
├── requirements/
│   ├── base.txt / dev.txt / production.txt
└── .env.example
```

## Lógica de estados de `TransaccionQR`

`generado -> pendiente_confirmacion -> pagado` (o `vencido` / `error` /
`cancelado` desde cualquiera de los dos primeros). Todos son terminales salvo
`generado` y `pendiente_confirmacion`.

El único punto válido para cambiar `estado` es
`TransaccionQR.transicionar_estado(nuevo_estado, usuario=None)`: valida la
transición contra `TRANSICIONES_PERMITIDAS`, la aplica dentro de
`transaction.atomic()` y crea el `LogAuditoria` correspondiente. Un intento de
modificar `estado` directamente (`obj.estado = X; obj.save()`) lanza
`TransicionEstadoInvalida` — reforzado también en el admin, donde el campo
queda de solo lectura.

## Setup local

```bash
python -m venv .venv
source .venv/bin/activate  # o .venv\Scripts\activate en Windows
pip install -r requirements/dev.txt
cp .env.example .env        # completar SECRET_KEY y credenciales

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Tests:

```bash
pytest
```

## Integraciones externas

- **MC4/SIP** (`services/mc4_client.py`): misma pasarela que usa
  `cessa-laravel` en producción (`SipQrProvider.php`, Banco BISA). **Integración
  real desde 2026-09-10** en 10.1.1.88 — credenciales SIP reales (reusadas de
  `cessa-laravel`), autenticación (`_obtener_token()`) confirmada contra
  `sip.mc4.com.bo`. Falta todavía probar `generar_qr()` de punta a punta
  (generar un QR real y confirmarlo pagado).
- **Consulta de deuda** (`services/deuda_client.py`): **integración real**
  (desde 2026-09-07, token corregido 2026-09-10) contra
  `GET /v1/consulta/cliente` del SIIC — mismo endpoint y credenciales que usa
  `cessa-laravel` en producción (`CessaApiService`). No es una conexión
  directa a Db2.
- **api-cobranzas-bancos** (`services/cobranzas_banco_client.py`): credenciales
  de **test** activas en 10.1.1.88 desde 2026-09-10 (`CABISAQR` /
  `api-cobranzas-test.bo-com-assec.net`, a propósito no producción —
  autenticación OAuth confirmada). Falta probar `asegurar_caja_abierta()` /
  `crear_transaccion()` / `pagar_transaccion_propia()` de punta a punta con un
  cobro real (necesita un cliente con deuda real pendiente, no Bs. 0).

### Mapa de los dominios `*.bo-com-assec.net` (reverse-proxy nginx, relevado 2026-09-22)

Relevado de las configs nginx del proxy que publica las APIs del SIIC. Upstreams internos (IP:puerto) no se commitean -- están en las configs nginx del proxy; desde la
red interna de CESSA se llega directo a cada upstream; hay que mandar el
header `Host` del vhost y aceptar certificado no confiable (`curl -k`).

| Dominio público | Notas |
|---|---|
| `api-siic-prod-1` | SIIC prod (el que usa `SIIC_DEUDA_BASE_URL` hoy) |
| `api-siic-prod-2` | responde un frontend (Vite), no la API |
| `api-siic-prod-3` | SIIC prod (otra instancia) |
| `api-siic-test` | **SIIC test** -- acepta el mismo token que prod |
| `api-cobranzas-test` | sandbox de api-cobranzas-bancos (`COBRANZAS_BANCO_BASE_URL` hoy) |
| `api-cobranzas-prod-6002` | api-cobranzas-bancos **prod** (el nombre real, no "prod-1") |
| `cobranzas-web-test` | interfaz web del sandbox de cobranzas |
| `api-gateway-test` / `-prod` | piden auth propia (401) |

**Gotcha**: por el dominio público, `api-siic-test`, `api-siic-prod-2/3` y
`api-cobranzas-prod-6002` hoy devuelven el banner de cobranzas-test (`Lumen
(8.3.4) ... TEST`) -- sus vhosts no están activos en el proxy y caen al
`default_server`. Directo al upstream sí responden lo correcto. Para usarlos
por dominio hay que pedir a quien administra ese proxy que los habilite en
`sites-enabled`.

**Comprobante en el sandbox (`No existe registro en CFC510`)**: no se debe a
mezclar ambientes. Revisando el código real del SIIC (`POST /v1/comprobantes`,
`ComprobanteController`, 2026-09-23), el comprobante se arma leyendo
`SUMINI` → `COMSAL` → `SFECABE` → `CFC510` → `FACTUI` en DB2. La biblioteca de
test (`BKLDTA`) tiene `CFC510` incompleta; en prod (`LIBSUCDTA`) no hay un
solo error de este tipo en los logs. El sandbox puede **pagar** pero no
emitir el comprobante hasta que sistemas repueble `CFC510` en `BKLDTA`. Igual
conviene que la deuda salga del mismo ambiente que la paga.

**Límites del SIIC que respeta el gateway**: `documento.numero` ≤ 20
caracteres (si el alias no entra, se deriva uno estable, `CW` + hash; ver
`numero_documento_para()`). Una Transacción de api-cobranzas-bancos solo se
puede pagar el mismo día en que se creó: si un reintento cae otro día, se
crea una nueva. El SIIC exige pagar **primero la deuda más antigua**.

Para desarrollar sin credenciales reales, `services/fakes.py` trae
`FakeMC4Client`/`FakeDeudaClient` (deuda inventada pero estable por código de
cliente -- nombre y monto varían, no siempre es la misma; QR que se marca
"pagado" en la segunda consulta de estado). Apuntar
`MC4_CLIENT_CLASS`/`DEUDA_CLIENT_CLASS` a esas clases en un `.env` de
desarrollo -- nunca en producción.

## Datos de ejemplo

`python manage.py generar_datos_demo [--cantidad 40]` crea los usuarios de
prueba (`cajera_demo` / `supervisor_demo` / `admin_demo`, contraseña
`Demo12345!`) si no existen, y N transacciones ya en `pagado` con su
`Factura` en `enviado` (usa `transicionar_estado()`/`crear_factura()`, no
inventa filas a mano -- queda registrado en `LogAuditoria` igual que en el
flujo real). Pensado para probar el listado, permisos y la exportación a CSV
sin esperar credenciales reales de MC4/SIIC.

## Despliegue en producción (10.1.1.88)

Plantillas de systemd/nginx en `deploy/` (`gunicorn.service`,
`celery-worker.service`, `celery-beat.service`, `nginx.conf`) -- copiar,
ajustar rutas/usuario y habilitar. Checklist completo:

1. **Servidor**: Python 3.10+ (probado con 3.12), PostgreSQL, Redis, nginx --
   `git clone https://github.com/Dario-M-Sinani/cobranza-cessa.git /opt/cobranza-cessa`
   (repo privado -- necesita un token/deploy key con acceso, o clonar por SSH).
2. **Entorno**: `python3 -m venv .venv && .venv/bin/pip install -r requirements/production.txt`.
3. **`.env` real** (nunca el de `-test`/dev): `DATABASE_URL` a un Postgres
   real, `SECRET_KEY` nuevo, `ALLOWED_HOSTS`/`CORS_ALLOWED_ORIGINS` con el
   dominio real, `COBRANZAS_BANCO_CLIENT_CLASS=services.cobranzas_banco_client.LumenCobranzasBancoClient`,
   `COBRANZAS_BANCO_BASE_URL` apuntando a la **producción** real de
   `api-cobranzas-bancos` (no `api-cobranzas-test...`), y credenciales de
   Cajero/Caja **de producción** (pedir a quien administra el SIIC -- las de
   `CABISAQR` son solo del ambiente de test, no usar en prod). `MC4_*`/
   `SIIC_DEUDA_*` con credenciales reales. `API_KEY_CESSA_LARAVEL` nueva
   (no reusar la de test) -- copiarla también a `COBRANZAS_GATEWAY_API_KEY`
   del `.env` de `cessa-laravel`.
4. **IMPORTANTE -- gotcha real encontrado auditando esto**: `manage.py` y
   `config/celery.py` usan `os.environ.setdefault("DJANGO_SETTINGS_MODULE",
   "config.settings.dev")` -- el default es DEV. `config/wsgi.py` en cambio
   sí fuerza `production`. Si se corre `python manage.py <comando>` a mano en
   el servidor sin exportar `DJANGO_SETTINGS_MODULE=config.settings.production`
   primero, usa settings de dev sin avisar (`DEBUG=True`, etc.) -- exportarlo
   siempre en la shell del servidor, no solo en los `.service` de `deploy/`
   (que ya lo fuerzan).
5. `python manage.py migrate && python manage.py createsuperuser`.
6. Habilitar `deploy/*.service` (gunicorn + celery worker + celery beat) y
   `deploy/nginx.conf` (leer el comentario de seguridad ahí: decidir si
   `/api/externo/` se expone distinto del resto del panel).
7. **Confirmar en vivo, antes de nada más**: repetir la prueba de
   `ESTADO_SEGURIDAD_MIGRACION.md` §-1vicies/§-1unvicies (POST a
   `/api/externo/recibos-web/liquidar/` con un payload de prueba) pero ahora
   contra credenciales/URL de producción real -- confirmar que igual llega
   hasta la validación de negocio del SIIC antes de prender nada del lado
   `cessa-laravel`.

**Pendiente organizacional, no de código, para que `cessa-laravel` (Hostinger,
fuera de la red de CESSA) pueda llegar hasta acá**: `10.1.1.88` es una IP
privada. Alguien con acceso a la red/firewall de CESSA tiene que decidir cómo
se expone -- mismo patrón que ya existe para `api-cobranzas-test.bo-com-assec.net`/
`api-siic-prod-1.bo-com-assec.net` (un dominio público con NAT/reverse-proxy
hacia el servidor interno) sería lo más simple, reusando esa misma
infraestructura si la administra el mismo proveedor. Una vez que exista esa
URL pública, el único cambio del lado `cessa-laravel` es apuntar
`COBRANZAS_GATEWAY_BASE_URL`/`COBRANZAS_GATEWAY_API_KEY` ahí -- el código ya
está listo para eso, no hace falta tocar nada más.

### HTTPS / certbot -- diagnóstico 2026-09-16

Esa exposición pública ya existe: `test01.cessa.com.bo` (DNS en Cloudflare)
resuelve a `200.87.9.163`, con NAT/port-forward `200.87.9.163:80 →
10.1.1.88:80` (nginx real respondiendo ahí -- confirmado con `curl`, sirve el
build de `cobranza-cessa-frontend`). Pero `certbot --nginx` (challenge
HTTP-01) falla siempre con:

```
Detail: 200.87.9.163: Fetching http://test01.cessa.com.bo/.well-known/acme-challenge/...:
Timeout during connect (likely firewall problem)
```

**Diagnóstico confirmado en el servidor** (no es nginx ni el servidor
Linux): `iptables -L` en `10.1.1.88` está vacío (policy ACCEPT, sin reglas),
y corriendo `tcpdump` en el puerto 80 durante un `certbot ... --dry-run` real,
**no llega ni un solo paquete SYN** desde los validadores de Let's Encrypt.
O sea, la conexión nunca pasa del router/firewall que hace el NAT hacia
`10.1.1.88` (o del ISP) -- algo ahí filtra conexiones entrantes desde IPs de
internet "reales" aunque tráfico desde redes más cercanas sí llega y nginx
responde bien. Pendiente de quien administra ese router/firewall: revisar la
regla de port-forward del 80 por restricciones de IP origen/geo-bloqueo o
protección anti-flood que esté descartando los SYN antes de que lleguen al
servidor.

**Camino elegido para no depender de esa reja mientras se resuelve**:
challenge **DNS-01** con el plugin `certbot-dns-cloudflare` (el dominio ya
está en Cloudflare, permite automatizar creación/borrado del TXT vía API
Token, así que la renovación automática sigue funcionando sin intervención
manual y sin necesitar el puerto 80 alcanzable desde internet). Pasos:

1. Crear un API Token en Cloudflare (perfil → API Tokens → plantilla "Edit
   zone DNS"), permisos `Zone:DNS:Edit` + `Zone:Zone:Read`, acotado a la zona
   `cessa.com.bo` únicamente.
2. En `10.1.1.88`: `sudo apt install python3-certbot-dns-cloudflare`.
3. Guardar el token en `/root/.secrets/cloudflare.ini` (`chmod 600`,
   `dns_cloudflare_api_token = <token>`) -- **nunca commitear ese archivo ni
   el token a este repo**.
4. `sudo certbot certonly --dns-cloudflare --dns-cloudflare-credentials
   /root/.secrets/cloudflare.ini -d test01.cessa.com.bo --dry-run` primero;
   si pasa, repetir sin `--dry-run` para el certificado real.
5. Configurar el bloque `listen 443 ssl` en `deploy/nginx.conf` apuntando a
   los certs emitidos y recargar nginx. La renovación queda automática vía el
   timer systemd que instala `certbot` (`certbot.timer`), sin volver a tocar
   el puerto 80.

**Actualización 2026-09-17 -- se abandonó este plan de certbot, en uso un
cert Origin CA de Cloudflare:**

En vez de DNS-01, se generó desde el dashboard de Cloudflare un certificado
**Origin CA** para `*.cessa.com.bo` (válido hasta 2041) y se instaló
directo en `10.1.1.88` (`/etc/nginx/ssl/cessa-origin.{pem,key}`, no
commiteado). `deploy/nginx.conf` ya tiene el `listen 443 ssl` apuntando a
ese cert/key. Ventaja sobre certbot: no depende de que el puerto 80 sea
alcanzable desde internet para emitir/renovar (el cert dura hasta 2041, sin
renovación automática que mantener).

Importante: un cert Origin CA **no es de confianza pública** -- solo sirve
para el tramo Cloudflare-edge ↔ origin, con el proxy de Cloudflare (nube
naranja) activo y el modo SSL/TLS en **Full** o **Full (strict)** (nunca
"Flexible"). Un navegador conectando directo a la IP, sin pasar por
Cloudflare, va a marcar el cert como no confiable -- es esperado.

**Estado actual (2026-09-17), con nube naranja y NAT del 443 ya activados**:
Cloudflare devuelve `522 Connection timed out` al intentar llegar al origin
por el 443 -- confirmado que no es nginx ni el servidor (mismo diagnóstico
de arriba: `ufw`/`iptables`/`nft` vacíos, nginx sirve `200` en local en 80 y
443). El corte sigue estando en el firewall (Juniper SRX en `10.1.1.1`) o
más allá -- pendiente revisar que la regla NAT del 443 esté completa, que
tenga su security policy asociada, y que ningún screen/IPS/geo-block esté
descartando los SYN de los rangos de IP de Cloudflare (mismo síntoma que el
bloqueo de los validadores de Let's Encrypt descrito arriba). Detalle
completo de las pruebas en `HISTORIAS_USUARIO.md`, notas de sesión
2026-09-17.

## Próximos pasos

- Credenciales reales de MC4/SIP (consulta de deuda ya conectada a producción desde 2026-09-07)
- Credenciales de producción para `api-cobranzas-bancos` (usuario Cajero/Caja
  reales, `ente_id`/`banco_id` de producción) -- ver "Despliegue en
  producción" arriba
- 2FA para supervisor/admin (deferido a propósito en esta fase)
- Frontend React
