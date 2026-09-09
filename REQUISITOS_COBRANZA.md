# Checklist de requisitos — Panel de Cobranza CESSA (cobranza_cessa)

Documento de trabajo para revisar qué tiene el proyecto, qué falta, y qué hay
que decidir **antes** de escribir las historias de usuario. No es historias
de usuario todavía — es el insumo para armarlas.

Convención: `[x]` = ya construido y verificado, `[ ]` = falta o está sin
definir. Fuente: código actual en `cobranza_cessa/` y `cobranza-cessa-frontend/`
más `README.md` del proyecto.

---

## 1. Autenticación y usuarios

- [x] Login JWT (access/refresh) — `POST /api/auth/token/`
- [x] Roles: cajera / supervisor / administrador (`Usuario.rol`)
- [x] Flag `activo` independiente de `is_active` (habilitar/deshabilitar operativamente sin borrar la cuenta)
- [x] CRUD de usuarios vía Django admin y API (`/api/usuarios/`, solo admin)
- [ ] 2FA para supervisor/admin (deferido a propósito, README lo marca pendiente)
- [x] UI de gestión de usuarios en el SPA (implementado 2026-09-07: página
  `/usuarios`, admin — crear usuario, cambiar rol, activar/desactivar; el
  backend `/api/usuarios/` ya existía, solo faltaba la pantalla)
- [ ] Cambio de contraseña propio desde el SPA — implementado 2026-09-07,
  **retirado el mismo día** a pedido del usuario. No confundir con el
  reseteo por admin/supervisor (sigue activo, ver más abajo).
- [x] Reseteo de contraseña de un cajero por su supervisor (corregido
  2026-09-07 — la asunción original tenía esto al revés, ver
  `HISTORIAS_USUARIO.md` A4.1): `/caja` tiene un widget para supervisor/admin,
  acotado a cajeros
- [ ] Registro de sesión/expiración visible para el usuario (hoy el refresh es automático vía `apiFetch`, ¿hace falta avisar "tu sesión expiró"?)

## 2. Consulta de deuda

- [x] `POST /api/deudas/consultar/ {codigo_externo}` — crea snapshot de `Deuda`
- [x] Cliente real para `/v1/consulta/cliente` del SIIC (implementado
  2026-09-07, reutilizando las credenciales de `cessa-laravel`
  `CESSA_API_URL`/`CESSA_API_TOKEN` → `SIIC_DEUDA_BASE_URL`/`SIIC_DEUDA_TOKEN`
  acá) — ya no es un stub, contrato confirmado contra el `CessaApiService.php`
  real. Endpoint, header de auth (sin "Bearer") y parseo de la respuesta
  (incl. signo de `importe` derivado de `debito_credito`, mismo criterio que
  Laravel) corregidos — el stub anterior tenía los tres mal.
- [x] `FakeDeudaClient` para desarrollo (nombre/monto variados y estables por código) — sigue disponible para desarrollo local sin pegarle a producción
- [x] Credenciales reales del SIIC para `consulta-deuda` — reutilizadas de `cessa-laravel` a pedido del usuario 2026-09-07
- [x] Manejo de "cliente no encontrado" / código inválido en la UI — `404` con mensaje claro (`ClienteNoEncontradoError`), separado de `502` (SIIC realmente caído)
- [x] Manejo de "cliente sin deuda pendiente" (monto 0) — se bloquea (400,
  `DeudaSinSaldoError`) tanto para QR como para efectivo; UI oculta los
  botones de cobro cuando `deuda.monto <= 0`

## 3. Generación y cobro de QR (MC4/SIP)

- [x] Generación de QR (`TransaccionQR`, solo cajera activa)
- [x] Máquina de estados con transiciones válidas (`generado → pendiente_confirmacion → pagado`, o `vencido`/`error`/`cancelado`), enforced en `save()` y auditado
- [x] Cliente MC4/SIP modelado sobre `SipQrProvider.php` real de cessa-laravel (stub, sin credenciales propias)
- [x] `FakeMC4Client` para desarrollo
- [x] Guardia anti-duplicado: una cajera no puede tener dos QR abiertos a la vez (`TransaccionEnCursoError`, 409)
- [x] Tarea Celery cada 60s que consulta estado y confirma pagos
- [x] Cancelar transacción (`POST /transacciones-qr/{id}/cancelar/`)
- [x] Mostrar imagen del QR generado en el frontend (base64, no se persiste)
- [ ] Credenciales reales de MC4/SIP (bloqueante para producción)
- [ ] ¿Qué pasa si el QR vence sin pagarse? (`vencido` existe como estado — ¿hay alguna acción automática, notificación, o solo queda ahí?)
- [ ] ¿Reintento manual de una transacción en `error` (no solo cancelar)?
- [ ] Timeout / manejo de caída del proveedor MC4 durante la generación (¿reintento automático, mensaje claro a la cajera?)
- [ ] Límite de monto por transacción / por día (¿existe algún tope operativo o de fraude a validar?)

## 4. Facturación

- [x] Modelo `Factura` (1:1 con `TransaccionQR`), estados pendiente/enviado/error
- [x] Creación automática de `Factura` al confirmarse el pago
- [x] Reintentar facturación (supervisor/admin) — hoy solo resetea a `pendiente`
- [ ] Envío real de la factura al sistema de facturación del SIIC (no existe cliente real todavía — ver `NECESIDADES_SIIC_FACTURACION.md` en `cessa-laravel`)
- [ ] Descarga/impresión del comprobante de factura para el cliente
- [ ] Validez fiscal (¿esto necesita numeración oficial, código de control, QR de la Dirección de Impuestos como en cessa-laravel? Definir si aplica el mismo requisito legal)

## 5. Reportes y exportación

- [x] Exportar CSV de transacciones (supervisor/admin, filtro por `?estado=`)
- [ ] Filtros adicionales en el listado/export (por fecha, por cajera, por monto)
- [ ] Reporte de cierre de caja por cajera/turno descargable — el resumen ya existe en el dashboard (ver abajo), falta la versión "descargable" tipo CSV
- [x] Dashboard/resumen (implementado 2026-09-07): `GET /api/dashboard/resumen/`
  + página `/dashboard` — totales del día (o rango de fechas para admin),
  desglose por forma de pago (QR/efectivo), cantidad de facturas, estado de
  cajas, y comparativa por cajero (solo admin). Ver Épicas G/H en
  `HISTORIAS_USUARIO.md` para el detalle de qué ve cada rol.

## 6. Roles y permisos (repaso)

- [x] `EsCajeraActiva`, `EsSupervisorOAdmin`, `EsAdmin` — permisos por rol en cada endpoint
- [x] Listado de transacciones scoped: cajera ve solo las suyas, supervisor/admin ven todas
- [ ] ¿Hace falta un cuarto rol o sub-permiso (ej. "solo lectura" para auditoría externa)?

### 6.1 Alcance definido por el usuario (2026-09-07) — sistema 100% interno para CESSA

Confirmado: este panel es de uso **interno** (no público). Los 3 roles de negocio
del modelo actual (`cajera`/`supervisor`/`admin`) se mantienen, pero se redefine
su alcance real:

- [ ] **Cajero/a**: cobra (QR y efectivo — ver §13), abre/cierra su propia caja.
  No puede reabrir una caja que él mismo cerró.
- [x]/[ ] **Supervisor de cobranza**: alcance acotado a
  - [x] resetear la contraseña de sus cajeros (corregido 2026-09-07: la
    "gestión de contraseña" del supervisor es sobre sus cajeros, no la
    propia — la propia la cambia como cualquier rol, ver §1)
  - [ ] dashboard de **"sus cajas"** del día: formas de pago usadas, cantidad de
    facturas emitidas, totales — ver §12/§13. **Pendiente de definir**: qué
    determina "sus cajas" (¿cajeros asignados a su sucursal/turno? hoy el
    modelo no tiene ese vínculo — hace falta una relación supervisor↔cajero o
    supervisor↔sucursal)
  - [ ] puede reabrir una caja cerrada por un cajero (ver regla de horario en §12)
  - [ ] **no** gestiona usuarios ni ve todo el sistema — su rol se limita a lo de arriba
- [ ] **Administrador**: todo lo del supervisor más un dashboard con **más
  detalle** (a definir con el usuario qué detalle exacto: ¿todas las cajas de
  todas las sucursales a la vez, histórico completo, gestión de usuarios,
  log de auditoría completo?)
- [ ] **Administrador TIC**: mencionado como quien "ya tiene potestad completa".
  **Asunción a confirmar**: esto es el superusuario/IT (Django admin /
  infraestructura), **fuera** del alcance de roles de negocio de este SPA —
  no se le construye un dashboard propio, ya tiene acceso total vía Django
  admin. Avisar si en realidad es un cuarto rol de negocio distinto de
  "Administrador".

## 7. Auditoría y trazabilidad

- [x] `LogAuditoria` registra cada transición de estado
- [x] Pantalla para revisar el log de auditoría desde el panel (implementado
  2026-09-07: `GET /api/auditoria/` + página `/auditoria`, exclusivo admin)
- [ ] Retención/exportación del log de auditoría (¿cuánto tiempo se guarda, hay requisito de compliance?) — sigue sin definir; hoy no hay purga automática ni export

## 8. Frontend / UX

- [x] Login, Consulta de deuda (con botón "Generar QR" solo para cajera), Transacciones (listado scoped + acciones)
- [x] Modo oscuro (toggle, persistido en localStorage)
- [x] Descarga de CSV vía blob
- [ ] Pantalla de detalle de una transacción individual (hoy es solo listado + acciones inline, ¿falta una vista de detalle con historial de estados?)
- [ ] Responsive / uso en tablet o solo desktop (¿en qué dispositivo va a estar la cajera parada en caja?)
- [x] Accesos directos / atajos de teclado para agilizar el cobro — implementado
  2026-09-07: `ConsultaDeudaPage` rediseñada como pantalla única tipo caja
  registradora — QR y Efectivo lado a lado desde que carga la deuda (antes
  efectivo pedía un clic extra para "revelar" el formulario), monto recibido
  precargado con pago exacto (cero tipeo en el caso común), Enter en el
  código dispara la consulta, y "Siguiente cliente" limpia y devuelve el
  foco al campo código listo para el próximo. No se agregaron atajos de
  teclado explícitos (ctrl+algo) más allá del Enter nativo de los `<form>`.
- [x] Impresión directa del comprobante/QR desde el navegador — ver F4 (comprobante interno imprimible, `ComprobantePage.tsx`)

## 9. Infraestructura y despliegue

- [ ] VPS Ubuntu propio (nginx + gunicorn + systemd + certbot) — mencionado en README, no hecho todavía
- [ ] PostgreSQL de producción (hoy solo probado con sqlite de smoke-test y local)
- [ ] Redis + Celery worker/beat corriendo como servicios persistentes (systemd units)
- [ ] Variables de entorno de producción (`.env` real, `CORS_ALLOWED_ORIGINS` apuntando al dominio final, no a `localhost:5174`)
- [ ] Dominio/subdominio definido para el panel y para la API
- [ ] Backups de la base de datos

## 10. Seguridad (repaso previo a producción)

- [x] Ninguna credencial real de MC4/SIIC cargada todavía (sin exposición de secretos reales)
- [ ] 2FA (ver §1)
- [ ] Rate limiting / bloqueo por intentos fallidos de login
- [ ] Revisión de CORS y hosts permitidos para producción
- [ ] HTTPS end-to-end (certbot, mencionado en README, no confirmado hecho)

## 11. Datos de prueba / demo

- [x] Comando `generar_datos_demo` (usuarios demo + ~40 transacciones realistas vía la máquina de estados real)
- [ ] ¿Se necesita un modo "demo" separado y protegido para mostrar a terceros sin tocar datos reales?

## 12. Apertura y cierre de caja (confirmado 2026-09-07, backend implementado el mismo día)

- [x] Modelo `Caja` (`apps/cobranza/models.py`): cajero asociado, estado
  (abierta/cerrada), `abierta_en`/`abierta_por`, `cerrada_en`/`cerrada_por`
- [x] Cajero abre su caja (`Caja.abrir()` / `POST /api/cajas/`) y la cierra
  (`Caja.cerrar()` / `POST /api/cajas/{id}/cerrar/`)
- [x] Regla: una caja **cerrada** por un cajero **no puede ser reabierta por
  ese mismo cajero** (enforced en `Caja.reabrir()`)
- [x] Solo **supervisor o administrador** puede reabrir una caja cerrada
  (rol + permiso `EsSupervisorOAdmin` en la vista)
- [x] Regla de horario: una caja no puede abrirse fuera de horario operativo
  (`CAJA_HORARIO_INICIO`/`CAJA_HORARIO_FIN`, default 08:00–18:00) —
  **sigue sin confirmar el horario real de CESSA** (pregunta 7), queda
  configurable por `.env` para no bloquear el desarrollo
- [x] Excepción a la regla de horario: solo un supervisor/administrador
  puede abrir/reabrir fuera de hora (`Caja.abrir()`/`reabrir()` con
  `motivo` obligatorio en ese caso)
- [x] Reporte automático (`AperturaCajaFueraDeHorario`: motivo, usuario,
  fecha/hora) cada vez que se abre/reabre fuera de horario, consultable en
  `GET /api/cajas/aperturas_fuera_de_horario/` (supervisor/admin) y en el
  Django admin
- [ ] Ese reporte de apertura fuera de norma: ¿además de quedar consultable
  en el sistema, hace falta notificarlo activamente a alguien (admin, TIC)?
  — sigue sin definir, hoy no notifica a nadie
- [ ] Vincular cada `TransaccionQR` y cada pago en efectivo a la caja
  abierta del cajero que lo realizó — **pendiente a propósito**, se conecta
  cuando se construya el cobro en efectivo (§13) para no tocar el flujo QR
  actual (que ya tiene tests en producción) sin necesidad todavía
- [ ] Resumen de cierre de caja: totales por forma de pago, cantidad de
  facturas emitidas, etc. — depende del punto anterior
- [x] UI en el SPA: pantalla `/caja` (`cobranza-cessa-frontend/src/pages/CajaPage.tsx`)
  — cajero abre/cierra su caja; supervisor/admin ve todas, reabre con
  motivo, abre la caja de un cajero puntual, y consulta el listado de
  aperturas fuera de horario. Verificado en navegador end-to-end
  (2026-09-07).

## 13. Formas de pago

- [x] Pago por QR vía MC4/SIP (Banco BISA) — único método implementado hoy
- [x] **Pago en efectivo** (implementado 2026-09-07): modelo `CobroEfectivo`
  (`apps/cobranza/models.py`) + `registrar_cobro_efectivo()`
  (`apps/cobranza/services.py`) + `POST /api/cobros-efectivo/`. Definiciones
  tomadas:
  - [x] monto recibido ingresado manualmente por la cajera
  - [x] vuelto calculado y persistido (`monto_recibido - monto_snapshot`)
  - [x] genera el mismo tipo de `Factura` que el QR (`Factura.cobro_efectivo`,
    la relación ahora acepta origen QR **o** efectivo, nunca ambos ni ninguno
    — constraint `factura_tiene_exactamente_un_origen`)
  - [x] modelo propio (`CobroEfectivo`), no reutiliza la máquina de estados
    de `TransaccionQR` — se confirma al instante, no tiene estado "pendiente"
  - [ ] validación de arqueo de caja física contra el sistema — sigue sin
    definir (pregunta 11 de abajo)
- [ ] **Multi-banco a futuro** (confirmado: se quiere habilitar más bancos QR
  más adelante): el diseño actual asume un solo proveedor MC4/Banco BISA
  hardcodeado — no bloqueante ahora, sigue pendiente, sin candidato concreto
  todavía (pregunta 12 de abajo)
- [x] Selector de forma de pago en la UI de cobro (QR vs. efectivo) — botones
  "Generar QR de cobro" / "Cobrar en efectivo" en `ConsultaDeudaPage`; falta
  el selector banco X vs. banco Y (bloqueado por Multi-banco, arriba)
- [ ] Reporte/desglose por forma de pago para el dashboard de supervisor
  ("cuántas transacciones y cuánto monto por cada forma de pago hoy") — se
  resuelve en la Épica G (dashboard), no en esta

---

## Preguntas abiertas para vos (antes de armar las historias de usuario)

1. **Alcance de esta sesión**: ¿seguimos completando lo que ya está mapeado como pendiente en el README (credenciales reales, 2FA, envío real de factura), o arrancamos por lo nuevo confirmado (caja + efectivo + dashboards por rol)?
2. ~~**Cierre de caja / turno**~~ — **RESUELTO** (2026-09-07): sí existe el concepto, con las reglas del §12 (no reabre quien cerró, solo supervisor/admin reabre, no se abre fuera de hora salvo que lo haga un supervisor con reporte de motivo/hora/fecha).
3. **Facturación fiscal**: ¿el panel necesita emitir facturas con validez fiscal real (como cessa-laravel/SIIC), o por ahora es solo un registro interno ("Factura" como comprobante, no como documento fiscal)? — sigue sin definirse, y ahora también aplica al pago en efectivo (§13): ¿el efectivo genera el mismo tipo de "Factura" que el QR?
4. **Dispositivo de uso**: ¿la cajera va a usar esto en una PC de escritorio, tablet, o tiene que funcionar en ambos?
5. **Notificaciones**: ¿algún actor necesita ser notificado (email/SMS/push) cuando un QR se paga, vence, falla la facturación, o cuando se abre una caja fuera de horario? Hoy no hay nada de esto.
6. **Prioridad de las credenciales reales**: MC4/SIP y `consulta-deuda` del SIIC son bloqueantes para ir a producción — ¿ya hay fecha o contacto para conseguirlas, o seguimos desarrollando sobre los fakes por ahora?
7. **Horario operativo**: ¿cuál es el horario exacto dentro del cual un cajero SÍ puede abrir caja normalmente? ¿es el mismo para todas las sucursales/agencias de CESSA o varía?
8. **"Sus cajas" del supervisor**: hoy no existe ninguna relación en el modelo entre un supervisor y los cajeros/sucursal que le corresponden. ¿Un supervisor supervisa por sucursal, por turno, o se le asignan cajeros específicos? Sin esto no se puede filtrar el dashboard.
9. **Administrador vs. Administrador TIC**: ¿son el mismo rol de negocio "Administrador" (y "TIC" solo describe quién ocupa ese puesto hoy), o es un cuarto rol real que también hay que modelar aparte del `admin` actual?
10. **Detalle del dashboard de Administrador**: dijiste que necesita "más detalles" que el de supervisor — ¿cuáles concretamente? (¿todas las sucursales a la vez, comparativas entre cajeros/supervisores, histórico más allá del día, acceso a auditoría completa?)
11. **Efectivo — mecánica exacta**: ¿la cajera registra el monto recibido y el sistema calcula vuelto, o solo se confirma "cobrado en efectivo" sin manejo de cambio? ¿Hay alguna validación de caja física (arqueo) contra lo registrado en el sistema?
12. **Multi-banco QR**: ¿ya hay un segundo banco/proveedor concreto en mente para más adelante, o por ahora es solo "diseñar para que no cueste agregarlo después" sin un candidato todavía?
