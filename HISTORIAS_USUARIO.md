# Historias de usuario — Panel de Cobranza CESSA (cobranza_cessa)

Basado en `REQUISITOS_COBRANZA.md`. Sistema **interno**, 3 roles de negocio:
**Cajero/a**, **Supervisor de cobranza**, **Administrador** (más el acceso
total de TIC vía Django admin, fuera de estas historias — ver nota en Épica G).

Convención: `[x]` = ya implementado hoy, `[ ]` = falta construir.
Donde una pregunta de `REQUISITOS_COBRANZA.md` seguía abierta, se tomó una
**asunción razonable** marcada `⚠️ ASUNCIÓN` para no frenar el avance —
corregila en cualquier momento y se ajusta la historia.

---

## Épica A — Autenticación y sesión

- [x] **A1.** Como usuario (cualquier rol), quiero iniciar sesión con usuario y contraseña para acceder al panel según mi rol.
- [x] **A2.** Como sistema, quiero renovar el access token automáticamente con el refresh token para que no se corte la sesión en medio de un cobro.
- [x]→[ ] **A3.** Como cajero/supervisor/administrador, quiero cambiar mi propia contraseña desde el panel (sin depender de Django admin). — implementado 2026-09-07 (`POST /api/auth/cambiar-password/` + página `/cambiar-password`), **retirado el mismo día a pedido del usuario** ("quita lo de cambiar contraseña"): se borró el endpoint, el serializer, la página y el link del navbar. A4/A4.1 (reseteo por admin/supervisor) siguen intactos, no son lo mismo.
- [x] **A4.** Como administrador, quiero poder resetear la contraseña de cualquier cajero o supervisor. — ya existía vía `PATCH /api/usuarios/{id}/` (admin), se agregó la UI en `/usuarios` (2026-09-07).
- [x] **A4.1.** (corregido 2026-09-07 por el usuario — la ⚠️ ASUNCIÓN de A4 estaba al revés) Como supervisor, quiero poder resetear la contraseña de mis cajeros (no la mía propia — para eso está A3). — `POST /api/usuarios/resetear-password-cajero/` `{username, password}` (supervisor o admin; rechaza con 404 si el `username` no corresponde a un cajero, así un supervisor nunca puede tocar la de otro supervisor/admin). Widget "Resetear contraseña de un cajero" en `/caja`. Verificado en navegador: reset + login exitoso con la nueva contraseña.
- [ ] **A5.** Como usuario, quiero ver un aviso claro cuando mi sesión expira (en vez de que la pantalla falle silenciosamente).

## Épica B — Consulta de deuda

- [x] **B1.** Como cajero, quiero ingresar el código de cliente y consultar su deuda para saber cuánto tiene que cobrar. — **conectado a SIIC real 2026-09-07** (antes era un stub con endpoint/formato inventados): `SiicDeudaClient` ahora pega a `GET {SIIC_DEUDA_BASE_URL}/v1/consulta/cliente`, reutilizando las mismas credenciales que `cessa-laravel` (`CESSA_API_URL`/`CESSA_API_TOKEN` allá). Contrato confirmado leyendo `CessaApiService.php` real, no adivinado. **Verificado por el usuario contra un cliente real** (recuperó los datos correctamente). Encontró de paso un bug de codificación (nombres con "Ñ" venían como "SIÃANI...") — mismo problema ya documentado y resuelto en `CessaApiService::fixEncoding()` del lado Laravel (UTF-8 doble-codificado en la base del SIIC); se portó el mismo fix a `_reparar_codificacion()` en `deuda_client.py`, aplicado recursivamente a toda la respuesta antes de parsear campos.
- [x] **B2.** Como cajero, quiero ver un mensaje claro cuando el código no corresponde a ningún cliente. — resuelto de paso al conectar B1: `ClienteNoEncontradoError` (subclase de `DeudaClientError`) → `404` con mensaje claro, separado de `502` (falla real de conexión con SIIC).
- [x] **B3.** Como cajero, quiero ver un mensaje claro cuando el cliente no tiene deuda pendiente (monto 0), y que el sistema no me deje generar un cobro sobre monto 0. — `DeudaSinSaldoError` (400) bloqueado tanto en `generar_transaccion_qr()` como en `registrar_cobro_efectivo()`; frontend oculta los botones de cobro y muestra "Este cliente no tiene deuda pendiente para cobrar." Verificado con tests (deuda en 0 → 400 en ambos endpoints); no se re-verificó visualmente en navegador porque ahora `ConsultaDeudaPage` pega a SIIC real y no hay forma de forzar una cuenta real en Bs. 0 sin datos de producción reales.

## Épica C — Cobro por QR (MC4/SIP)

- [x] **C1.** Como cajero, quiero generar un QR de pago para la deuda consultada.
- [x] **C2.** Como cajero, quiero ver la imagen del QR generado para mostrársela/enviársela al cliente.
- [x] **C3.** Como sistema, quiero verificar automáticamente cada 60s si un QR pendiente ya fue pagado, para confirmar el cobro sin intervención manual.
- [x] **C4.** Como cajero, quiero cancelar un QR generado que ya no corresponde (cliente se arrepiente, error de monto, etc.).
- [x] **C5.** Como sistema, quiero impedir que un cajero tenga dos QR abiertos a la vez, para evitar cobros duplicados.
- [ ] **C6.** Como cajero, quiero ver un mensaje claro si el proveedor MC4 no responde al generar el QR (en vez de que la pantalla se quede colgada o rompa). — **alcance ampliado 2026-09-10**: el problema no es solo MC4. `ConsultaDeudaPage`/`client.ts` (`apiFetch`) muestra `data.detail` del backend tal cual en pantalla para las tres integraciones externas (SIIC, SIP/MC4, `api-cobranzas-bancos`), y esos `detail` son el `str(exc)` interno de cada cliente -- ej. `"consulta-deuda <codigo_externo>: SIIC rechazó las credenciales configuradas (HTTP 401: {'error': 'Unauthorized', 'code': 401})"`. Hasta ahora no se notaba porque todo corría contra `Fake*Client`, que casi no fallan. Desde hoy `SIIC_DEUDA_TOKEN`/`MC4_*`/`COBRANZAS_BANCO_*` en 10.1.1.88 son credenciales reales (ver nota de sesión más abajo) -- un fallo real de cualquiera de las tres ya le mostraría ese texto técnico a una cajera. Pendiente: mapear los `DeudaClientError`/`MC4ClientError`/`CobranzasBancoError` (o directamente el `status` de la respuesta: 404 vs 502) a mensajes de negocio en el frontend, sin perder el detalle técnico en los logs del backend (que ya está, ver gunicorn-error.log).
- [ ] **C7.** Como sistema, quiero marcar automáticamente como `vencido` un QR que no se pagó dentro del plazo, para que no quede "pendiente" indefinidamente.

### Nota de sesión 2026-09-10 — 10.1.1.88 quedó con integraciones reales, pendiente UX de errores

Contexto para retomar: esa noche se destrabó por completo la consulta/facturación real en producción (10.1.1.88), partiendo de que `/login` cargaba pero "no hacía las consultas". Encontrado y ya desplegado:

- Typo en `.env`: `DEUDA_CLIENT_CLASS=service.deuda_client...` (faltaba la "s") rompía el import y tiraba 500 en `/api/deudas/consultar/`. Corregido.
- Typo en el token: `SIIC_DEUDA_TOKEN` tenía un `0` donde iba una `O` mayúscula -- SIIC devolvía 401, y **el código lo interpretaba como "cliente no encontrado" (404)**, escondiendo el problema real. Se corrigió el token y se arregló `services/deuda_client.py` (`consultar_deuda`) para que un 401/403 de SIIC levante `DeudaClientError` (502) en vez de `ClienteNoEncontradoError` -- commit `bdae154`, con test de regresión en `services/test_deuda_client.py`.
- `COBRANZA_BANCO_*`/`COBRANZAS_BANCO_CLIENT_CLASS` en el `.env` del servidor tenían el mismo typo de "falta la s" que `DEUDA_CLIENT_CLASS` -- Django los ignoraba silenciosamente y caía siempre al default (`FakeCobranzasBancoClient`). Corregido el nombre de las 8 variables.
- Se activaron credenciales **reales** en el `.env` de 10.1.1.88 (antes todo vacío/fake):
  - `SIIC_DEUDA_TOKEN` corregido (ver arriba) -- consulta de deuda 100% real, verificado con un cliente real (código de prueba usado en esta sesión, sin saldo pendiente -- no sirve para probar el cobro).
  - `COBRANZAS_BANCO_*` con las credenciales de **test** de `api-cobranzas-bancos` (`CABISAQR`, `api-cobranzas-test.bo-com-assec.net`) -- **a propósito test, no producción**, mismo criterio que ya documentaba este README. Autenticación OAuth confirmada (`_obtener_token()` devolvió token real).
  - `MC4_*` con las credenciales SIP reales (antes `FakeMC4Client`) -- `MC4_CLIENT_CLASS=services.mc4_client.SipMC4Client`. Autenticación confirmada (`_obtener_token()` devolvió token real). Esto significa que **C6 ya no es un caso hipotético**: si SIP falla de verdad, hoy la cajera ve el error crudo.
- Se crearon 3 usuarios de prueba para testear los 3 roles: `cajero.test` / `supervisor.test` / `admin.test`, password `Cessa.Test2026`.
- **Pendiente, no arrancado todavía**: el fix de C6 (mensajes de error legibles para cajera) descrito arriba. Falta además probar de punta a punta un **cobro en efectivo real** (con un cliente que tenga saldo > 0 -- el código de prueba usado hoy está en Bs. 0.00, no sirve para probar el cobro) y confirmar que la `Factura` vuelve con `numero_factura`/comprobante PDF real de `api-cobranzas-bancos` (el envío es automático vía Celery, corre cada 60s).
- Los archivos `env1`/`env2` (con las credenciales de origen: `.env` local de `cessa-laravel` y borrador de `.env` de `cobranza-cessa`) quedaron en `C:\Users\adm.local\Documents\cajas\` (fuera de ambos repos git, no se suben a ningún lado) -- decidir si se borran una vez que todo esto esté también confirmado en el `.env` real del servidor.
- [ ] **C8.** Como supervisor/administrador, quiero reintentar manualmente una transacción en estado `error` (no solo cancelarla).
- [x] **C9.** (confirmado por el usuario 2026-09-07) Como cajero, quiero poder generar un QR por un adelanto (monto parcial, menor al total de la deuda), no solo por el monto completo. — mismo mecanismo que D7 (`generar_transaccion_qr(monto=...)`, `MontoInvalidoError` si no está entre `0` y `deuda.monto`). Ver D7 para la nota de que esto no actualiza ningún saldo vivo contra SIIC. Verificado en navegador end-to-end.

## Épica D — Cobro en efectivo (implementada 2026-09-07)

⚠️ **ASUNCIÓN** (pregunta 11 sin responder del todo): el cajero ingresa el
monto recibido, el sistema calcula el vuelto como `monto_recibido -
monto_deuda`, y el cobro en efectivo genera el mismo tipo de `Factura` que
el QR (mismo ciclo, pero confirmado al instante -- efectivo no tiene
"confirmación externa" como el QR). No hay validación de arqueo de caja
física contra el sistema todavía -- ajustar si hace falta.

Modelo `CobroEfectivo` + `Factura` generalizada para originarse de QR **o**
efectivo (constraint `factura_tiene_exactamente_un_origen`), endpoint
`POST /api/cobros-efectivo/`. 9 tests nuevos, 42/42 pasando en total.

- [x] **D1.** Como cajero, quiero elegir "efectivo" como forma de pago para la deuda consultada, como alternativa al QR. — botón "Cobrar en efectivo" junto a "Generar QR de cobro" en `ConsultaDeudaPage`, solo visible (y solo permitido en el backend, `EsCajeraActiva`) para cajera activa
- [x] **D2.** Como cajero, quiero ingresar el monto recibido y ver el vuelto calculado automáticamente. — cálculo en vivo en el frontend + `vuelto` persistido en el backend
- [x] **D3.** Como sistema, quiero registrar el cobro en efectivo con su propio identificador de forma de pago, para poder reportarlo por separado del QR. — modelo `CobroEfectivo` separado de `TransaccionQR`
- [x] **D4.** Como cajero, quiero que un cobro en efectivo también genere una `Factura` (o el comprobante interno correspondiente), igual que un cobro QR. — `Factura.cobro_efectivo` (antes solo `transaccion_qr`), creada atómicamente en `registrar_cobro_efectivo()`
- [x] **D5.** Como cajero, quiero que un cobro en efectivo quede vinculado a mi caja abierta del turno (ver Épica E), para que el cierre de caja cuadre. — `CobroEfectivo.caja` se resuelve automáticamente a la caja abierta del cajero si tiene una; **no bloqueante todavía** (si no abrió caja, el cobro igual se registra con `caja=None`) — endurecer esto a obligatorio es una decisión de negocio pendiente, no técnica
- [ ] **D6.** (nuevo) Listado/reporte de cobros en efectivo en el SPA — hoy solo se ve la confirmación al momento de cobrar y en el Django admin; no hay una pantalla tipo "Transacciones" para repasarlos después. Se resuelve junto con el dashboard (Épica G/I).
- [x] **D7.** (confirmado por el usuario 2026-09-07) Como cajero, quiero poder cobrar un adelanto (monto parcial, menor al total de la deuda) en efectivo, no solo el monto completo. — `registrar_cobro_efectivo(monto_a_cobrar=...)`, validado entre `> 0` y `<= deuda.monto` (`MontoInvalidoError` si no); `monto_recibido` debe cubrir el adelanto, no la deuda total. `ConsultaDeudaPage` tiene un input "Monto a cobrar" precargado con el total, editable; la tarjeta de confirmación muestra "Adelanto — queda pendiente Bs. X" cuando corresponde. **Importante**: esto es solo bookkeeping local — no existe un saldo vivo que se actualice, la próxima consulta de deuda vuelve a traer lo que diga SIIC (no se le informa el adelanto). Verificado en navegador end-to-end.

- [x] **D8.** (pedido por el usuario 2026-09-07) Como cajero, quiero cobrar en una sola pantalla tipo caja registradora, sin pasos separados para QR y efectivo. — `ConsultaDeudaPage` rediseñada: QR y Efectivo se muestran lado a lado apenas carga la deuda (antes "Cobrar en efectivo" era un clic extra que recién ahí revelaba el formulario), el monto recibido se precarga igual al monto a cobrar (pago exacto por defecto, cero tipeo en el caso común), y "Siguiente cliente" limpia todo y devuelve el foco al campo de código. Verificado en navegador: código → Enter → las dos opciones ya están ahí → un clic cobra.

## Épica E — Apertura y cierre de caja (implementada 2026-09-07, backend)

⚠️ **ASUNCIÓN** (pregunta 7 sin responder): horario operativo por defecto
08:00–18:00 (`CAJA_HORARIO_INICIO`/`CAJA_HORARIO_FIN` en `.env`, no
hardcodeado) — ajustar al horario real de CESSA cuando se confirme.

Modelo `Caja` + `AperturaCajaFueraDeHorario` (`apps/cobranza/models.py`),
horario configurable en `apps/cobranza/horario.py`, endpoints en
`/api/cajas/` (ver README). 20 tests nuevos en `tests/test_caja.py`, 33/33
pasando en total. **Solo backend** — falta la UI en el SPA.

- [x] **E1.** Como cajero, quiero abrir mi caja al iniciar mi turno, dentro del horario operativo permitido. — `POST /api/cajas/` (cajera activa, sin body)
- [x] **E2.** Como sistema, quiero impedir que un cajero abra caja fuera del horario operativo, mostrando el motivo del bloqueo. — 400 con detail claro
- [x] **E3.** Como cajero, quiero cerrar mi caja al terminar mi turno. — el cajero cierra la caja normalmente, pero **no ve el resumen de lo cobrado** (ver corrección abajo). `GET /api/cajas/{id}/resumen/` (`reportes.resumen_caja()`) existe y funciona, pero quedó restringido a supervisor/admin.
- [x] **E3.1.** (corregido por el usuario 2026-09-07, mismo día que se implementó E3) El cajero **no debe poder ver el total recaudado**, ni siquiera de su propio turno — el resumen de caja es exclusivo de supervisor/admin. `CajaViewSet.resumen` ahora exige `EsSupervisorOAdmin`; el frontend le sacó a la cajera el botón "Ver total del turno", la columna "Resumen" de la tabla, y el auto-mostrar-resumen al cerrar. Verificado en navegador: la cajera cierra su caja y no ve ningún total; supervisor/admin sí lo ven.
  - [x] Cerrar caja — `POST /api/cajas/{id}/cerrar/` (solo el cajero dueño)
  - [ ] Resumen de lo cobrado al cerrar — depende de E9 (vincular cobros a la caja), todavía no implementado
- [x] **E4.** Como sistema, quiero impedir que un cajero reabra una caja que él mismo cerró. — enforced en `Caja.reabrir()` (rol + `cerrada_por`)
- [x] **E5.** Como supervisor o administrador, quiero reabrir una caja cerrada por un cajero cuando sea necesario. — `POST /api/cajas/{id}/reabrir/`
- [x] **E6.** Como supervisor, quiero poder abrir una caja fuera del horario operativo cuando la situación lo requiera (un cajero no puede hacerlo directamente). — `POST /api/cajas/` con `cajero_id` + `motivo` (rol supervisor/admin)
- [x] **E7.** Como sistema, quiero generar automáticamente un reporte (motivo, hora, fecha, quién lo autorizó) cada vez que se abre una caja fuera de horario, para que quede trazado. — `AperturaCajaFueraDeHorario`, creado automáticamente en `Caja.abrir()`/`reabrir()`
- [x] **E8.** Como administrador, quiero poder consultar el listado de aperturas fuera de horario con su motivo, para revisión. — `GET /api/cajas/aperturas_fuera_de_horario/` (supervisor/admin)
  - ⚠️ **ASUNCIÓN** (pregunta relacionada sin responder): el reporte queda solo consultable en el sistema, no se notifica automáticamente a nadie por email/SMS — ajustar si hace falta notificación activa.
- [x] **E9.** Como cajero/supervisor, quiero que todo cobro (QR o efectivo) quede vinculado a la caja abierta al momento de cobrar, para que el cierre cuadre con lo realmente cobrado. — completado 2026-09-07: `TransaccionQR.caja` (antes solo `CobroEfectivo.caja`), `generar_transaccion_qr()` resuelve la caja abierta del cajero igual que `registrar_cobro_efectivo()`. Sigue sin ser obligatorio (si no hay caja abierta, el cobro se registra igual con `caja=None`) — endurecerlo es decisión de negocio, no técnica.
- [x] **E10.** UI en el SPA: pantalla `/caja` — cajero ve el estado de su caja y abre/cierra; supervisor/admin ve todas las cajas, reabre con motivo, abre la caja de un cajero puntual, y ve el listado de aperturas fuera de horario. Verificado en navegador end-to-end (2026-09-07): cajera abre → cierra su caja; supervisor la reabre y queda "Abierta" con `abierta_por = supervisor_demo`.

## Épica F — Facturación

- [x] **F1.** Como sistema, quiero crear automáticamente una `Factura` cuando se confirma un pago QR.
- [x] **F2.** Como supervisor/administrador, quiero reintentar la facturación de un pago que quedó en error.
- [ ] **F3.** Como sistema, quiero enviar la factura realmente al sistema de facturación del SIIC (hoy solo cambia de estado localmente, no se envía nada real — ver `NECESIDADES_SIIC_FACTURACION.md`).
- [x] **F4.** Como cliente/cajero, quiero poder descargar o imprimir el comprobante de la factura. — implementado 2026-09-07 como **comprobante interno imprimible, sin valor fiscal** (a propósito, ver F5): página `/comprobante/:tipo/:id` (`ComprobantePage.tsx`), usa `window.print()` del navegador con CSS de impresión dedicado (`@media print`, oculta el navbar). Accesible desde "Ver / imprimir comprobante" tras un cobro en efectivo, y desde "Ver comprobante" en Transacciones para QR ya `pagado` (no antes). No requirió cambios de backend — usa los endpoints de detalle que ya existían. Verificado en navegador.
- [ ] **F5.** Definir con el usuario: ¿la factura de este panel necesita validez fiscal real (numeración oficial, código de control) o es un comprobante interno? — pregunta 3 de `REQUISITOS_COBRANZA.md`, sigue abierta y bloquea el diseño final de F3/F4.
- [x] **F6.** (auditoría de roles 2026-09-07, a pedido del usuario: "fijate bien el tema de los roles y que se vea los reportes bien desde admin y supervisor") Se repasó `permission_classes`/`get_permissions()` de todos los ViewSets (`TransaccionQRViewSet`, `FacturaViewSet`, `CobroEfectivoViewSet`, `CajaViewSet`, `UsuarioViewSet`, `LogAuditoriaViewSet`) y se encontró **un bug real**: `FacturaViewSet.get_queryset()` filtraba las facturas de una cajera solo por `transaccion_qr__usuario`, así que sus propias facturas originadas en un cobro en **efectivo** (que tienen `transaccion_qr=None`) nunca aparecían en `GET /api/facturas/` — quedaba viendo la lista vacía aunque hubiera cobrado en efectivo. Corregido a `Q(transaccion_qr__usuario=...) | Q(cobro_efectivo__usuario=...)`, mismo patrón que ya usaba correctamente `reportes.py` (`resumen_dashboard`/`resumen_caja`). Se agregaron 3 tests de regresión (`test_facturas.py`); no se encontró el mismo patrón de bug en ningún otro lugar del código (se buscó explícitamente). El resto de la revisión (dashboard, resumen de caja, auditoría, usuarios) ya estaba correctamente restringido a supervisor/admin — no se encontraron más problemas.

## Épica G — Dashboard de Supervisor de cobranza (implementada 2026-09-07)

⚠️ **ASUNCIÓN** (pregunta 8 sin responder): "sus cajas" = los cajeros
asignados a la sucursal/agencia del supervisor. Como **no existe** todavía
un concepto de Sucursal/Agencia en el modelo de Usuario, esto **no se
implementó** — hoy el supervisor ve el mismo alcance de datos que el
administrador (todas las cajas/cobros), la diferencia entre ambos roles
quedó solo en qué secciones extra arma el frontend. Si en cambio es una
asignación manual supervisor↔cajero (no por sucursal), avisar y se agrega el
filtro real.

Endpoint `GET /api/dashboard/resumen/` (`?desde=&hasta=`, default hoy),
página `/dashboard` en el SPA. 4 tests nuevos.

- [x]/[ ] **G1.** Como supervisor, quiero ver un dashboard del día limitado a los cajeros de mi sucursal (mis cajas), no de todo CESSA. — el dashboard del día **sí** existe; el filtro por sucursal **no** (ver asunción arriba)
- [x] **G2.** Como supervisor, quiero ver en el dashboard: totales cobrados del día, desglose por forma de pago (QR / efectivo), y cantidad de facturas emitidas. — tarjetas de resumen en `DashboardPage`
- [x] **G3.** Como supervisor, quiero ver el estado actual de cada caja de mi sucursal (abierta/cerrada, quién la tiene abierta). — tabla "Cajas del período" (sin filtro de sucursal, ver G1)
- [ ] **G4.** Como supervisor, quiero cambiar mi propia contraseña (ver A3) — sigue pendiente, es de la Épica A, no se tocó en esta sesión.

## Épica H — Dashboard de Administrador (implementada 2026-09-07)

⚠️ **ASUNCIÓN** (pregunta 10 sin responder del todo): "más detalle" se
interpretó como: mismo resumen que el supervisor + selector de rango de
fechas + comparativa por cajero + acceso a auditoría completa + gestión de
usuarios. No hay filtro de sucursal que quitarle al admin porque el
supervisor tampoco lo tiene todavía (ver Épica G).

- [x] **H1.** Como administrador, quiero ver el dashboard de todas las sucursales/cajas a la vez, no solo una. — ya lo tenía por defecto (no hay sucursales que separar); además tiene selector de rango de fechas que el supervisor no tiene
- [x] **H2.** Como administrador, quiero comparar totales entre cajeros/supervisores (quién cobró más, cuántas facturas por cajero). — tabla "Comparativa por cajero", solo visible para admin
- [x] **H3.** Como administrador, quiero ver histórico más allá del día actual (por rango de fechas), no solo el resumen diario. — inputs "Desde"/"Hasta" + botón "Filtrar", solo para admin
- [x] **H4.** Como administrador, quiero acceder al log de auditoría completo desde el panel (hoy solo está en Django admin). — `GET /api/auditoria/` (nuevo, antes no existía API para `LogAuditoria`) + página `/auditoria`
- [x] **H5.** Como administrador, quiero gestionar usuarios (crear/desactivar cajeros y supervisores) desde el panel, sin entrar a Django admin. — página `/usuarios` (el backend `/api/usuarios/` ya existía, solo faltaba la UI); permite crear usuario, cambiar rol y activar/desactivar

**Nota sobre "Administrador TIC"**: se asume que es el mismo puesto/persona
que ya tiene acceso total vía Django admin/superusuario, y que **no** es un
quinto rol de negocio a modelar aparte de `Administrador`. Si en realidad TIC
necesita una vista distinta dentro del propio panel (no solo Django admin),
avisar y se agrega como Épica nueva.

Verificado en navegador end-to-end (2026-09-07): dashboard de admin muestra
Bs. 780.17 total (QR + efectivo combinados), comparativa por cajero, filtro
de fechas; dashboard de supervisor muestra el mismo resumen del día pero sin
filtro ni comparativa; se creó un usuario nuevo (`cajera_prueba`) desde
`/usuarios`; `/auditoria` lista el historial completo (aperturas de caja,
transiciones de estado, facturas creadas). Confirmado también que
`admin_demo` **no** puede generar QR ni cobrar en efectivo (ni por UI ni por
API directa, 403) — a raíz de una duda del usuario sobre si admin podía
"hacer cobros"; no se encontró tal hueco.

## Épica I — Reportes y exportación

- [x] **I1.** Como supervisor/administrador, quiero exportar a CSV el listado de transacciones, filtrando por estado.
- [ ] **I2.** Como supervisor/administrador, quiero filtrar el export por fecha, cajero y forma de pago (no solo por estado).
- [ ] **I3.** Como supervisor/administrador, quiero un reporte de cierre de caja por cajero/turno, descargable.

## Épica J — Multi-banco QR (backlog, no bloqueante)

⚠️ **ASUNCIÓN** (pregunta 12 sin responder): no hay un segundo banco
concreto todavía — esta épica queda en backlog, solo como recordatorio de
diseño (que el modelo/servicio de forma de pago no asuma "un solo banco para
siempre" cuando se construya efectivo/multi-forma en la Épica D).

- [ ] **J1.** Como cajero, quiero elegir entre distintos bancos/proveedores QR al momento de cobrar, cuando haya más de uno habilitado.
- [ ] **J2.** Como administrador, quiero habilitar/deshabilitar bancos QR disponibles sin tocar código.

---

### Nota de sesión 2026-09-16 — certbot bloqueado, diagnóstico hecho, en curso el cambio a DNS-01

Contexto para retomar: se intentó emitir el certificado HTTPS para
`test01.cessa.com.bo` (`10.1.1.88`, NAT público `200.87.9.163:80`) con
`certbot --nginx` (challenge HTTP-01) y siempre falla con `Timeout during
connect (likely firewall problem)`.

Diagnóstico completo (detalle técnico en README §"HTTPS / certbot"):
nginx y el servidor están bien (sin reglas de `iptables`, `curl` directo a
`200.87.9.163` responde el sitio real); con `tcpdump` en el puerto 80
durante un `certbot --dry-run` real se confirmó que **no llega ningún SYN**
de los validadores de Let's Encrypt -- el bloqueo está en el router/firewall
que hace el port-forward hacia `10.1.1.88`, o en el ISP, no en el servidor.
Pendiente que quien administra ese equipo revise la regla.

Mientras tanto, como el dominio `cessa.com.bo` está en Cloudflare, se decidió
cambiar a challenge **DNS-01** con `certbot-dns-cloudflare` (evita depender
del puerto 80 y mantiene la renovación automática). Falta: generar el API
Token de Cloudflare (permisos `Zone:DNS:Edit` + `Zone:Zone:Read`, acotado a
la zona `cessa.com.bo`), instalar el plugin en `10.1.1.88` y correr el
`--dry-run`. Ver pasos exactos en README.

### Nota de sesión 2026-09-17 — SSL habilitado en el origin con cert Origin CA de Cloudflare

Cambio de enfoque respecto a la nota anterior: en vez de certbot (DNS-01),
se generó un certificado **Origin CA de Cloudflare** para `*.cessa.com.bo`
(válido hasta 2041) desde el dashboard de Cloudflare. Se instaló en
`10.1.1.88`:

- Cert/key en `/etc/nginx/ssl/cessa-origin.{pem,key}` (644/600, root:root,
  no se commitean al repo).
- `deploy/nginx.conf` actualizado: el server block de `cobranza-cessa` ahora
  tiene `listen 443 ssl;` además del `listen 80;`, apuntando a ese cert/key.
  Ya desplegado en el servidor (`/etc/nginx/sites-available/cobranza-cessa`,
  con backup del archivo anterior al lado, `.bak-<timestamp>`), `nginx -t` ok,
  `systemctl reload nginx` hecho. Confirmado con `openssl s_client` y `curl`
  contra `200.87.9.163:443` que responde con el cert correcto (HTTP 200).

**Pendiente, no es de código:**

1. **NAT del puerto 443** -- el port-forward público hoy solo existe para el
   80 (`200.87.9.163:80 -> 10.1.1.88:80`). Falta que quien administra la red
   de CESSA agregue el mismo NAT para el 443. (La prueba de arriba se hizo
   desde una máquina dentro de la red de CESSA -- mismo caso que el
   diagnóstico de certbot, donde "redes más cercanas" sí llegaban aunque
   internet real no. No confirma alcance público real todavía.)
2. **Proxy de Cloudflare** -- el registro DNS de `test01.cessa.com.bo` hoy
   resuelve directo a `200.87.9.163` (DNS-only / nube gris, confirmado con
   `nslookup ... 1.1.1.1`), no está proxiado. Un cert Origin CA **no es de
   confianza pública** -- solo sirve para el tramo Cloudflare-edge <->
   origin. Para que esto realmente sirva HTTPS a los usuarios hay que activar
   el proxy (nube naranja) en Cloudflare para ese registro, y poner el modo
   SSL/TLS en **Full** o **Full (strict)** (nunca "Flexible", porque el
   origin ya sirve HTTPS real).
3. Una vez con proxy naranja + NAT 443, evaluar si conviene forzar redirect
   `http -> https` en el server block (hoy conviven los dos `listen` sin
   redirect, a propósito, para no romper accesos internos por IP/HTTP
   mientras se confirma lo anterior).

**Actualización misma fecha -- NAT y proxy ya activados, pero sigue sin
cerrar (error 522):**

Se agregaron la regla NAT del 443 en el firewall (Juniper SRX, gateway
`10.1.1.1` de la red de CESSA -- identificado por el banner "Juniper Web
Device Manager" en `http://10.1.1.1/`, J-Web) y se activó el proxy de
Cloudflare (nube naranja) para `test01.cessa.com.bo`. Confirmado con
`nslookup test01.cessa.com.bo 1.1.1.1`/`8.8.8.8`: el dominio ya resuelve a
IPs de Cloudflare (`104.21.x.x`/`172.67.x.x` + IPv6), no a la IP pública
directa -- el proxy sí está prendido.

Pero pedir `https://test01.cessa.com.bo/` (forzando resolución a una IP de
Cloudflare con `curl --resolve`, para asegurar que la request pasa por el
proxy) devuelve **`522 Connection timed out`**, generado por Cloudflare
mismo (`Server: cloudflare`, con `CF-RAY`) -- es decir, Cloudflare recibe la
conexión del cliente pero no logra conectar al origin (`200.87.9.163:443`)
dentro de su propio timeout. El puerto 80 vía Cloudflare no sirve como
prueba alternativa: devuelve un 301 a `https://` generado por el propio
Cloudflare (config "Always Use HTTPS"), sin siquiera tocar el origin.

Se descartó que el problema esté del lado de `10.1.1.88` (mismo patrón que
el diagnóstico de certbot del punto anterior, repetido para confirmar que no
cambió nada ahí):

- `ufw`: `inactive`; `iptables` (INPUT/FORWARD/OUTPUT): policy `ACCEPT`, sin
  reglas; `nft list ruleset`: vacío -- no hay firewall local bloqueando.
- nginx: `nginx -t` ok, `systemctl` `active`, un solo vhost habilitado
  (`cobranza-cessa`; el `default` de Ubuntu sigue deshabilitado), escuchando
  en `0.0.0.0:80` y `0.0.0.0:443` (`ss -tlnp`), cert/key legibles, sin errores
  en `/var/log/nginx/error.log`. Pedido local a `127.0.0.1` con
  `Host: test01.cessa.com.bo` responde `200` tanto en 80 como en 443.

**Conclusión:** el corte está específicamente en el tramo Cloudflare-edge -> 
`200.87.9.163:443` -> NAT -> `10.1.1.88:443` -- no en el servidor. Mismo tipo
de síntoma que el bloqueo de los validadores de Let's Encrypt en el puerto 80
(SYN de "internet real" no llega, tráfico de redes más cercanas sí). Falta
que quien administra el Juniper revise, en la regla NAT/destination-NAT del
443 recién creada:

1. Que la regla esté completa (protocolo `tcp`, puerto destino `443`, IP
   interna `10.1.1.88`, puerto interno `443`) y no sea una copia a medias de
   la regla del 80.
2. Que exista (o se haya clonado) la **security policy** que permite el
   tráfico ya traducido -- en SRX el destination-NAT y el policy que lo deja
   pasar son configuraciones separadas.
3. Si hay algún screen/IPS/anti-flood o geo-block activo, que no esté
   descartando los SYN entrantes de los rangos de IP de Cloudflare
   (publicados en `https://www.cloudflare.com/ips/`) -- ya pasó algo similar
   con las IPs de los validadores de Let's Encrypt.

### Nota de sesión 2026-09-22 — facturación QR llega a PAGADA, trabada en el comprobante (CFC510); causa probable: mezcla de ambientes SIIC prod / cobranzas test

Objetivo de la sesión: llevar una `SolicitudLiquidacion` a `FACTURADO` con
comprobante real, probando directo en la red interna (sin depender de la ruta
pública Hostinger → Cloudflare → `test01.cessa.com.bo`, que sigue con 502/522
aparte, ver nota del 2026-09-17).

**Hecho:**

- Fix de idempotencia en `liquidar_solicitud()` (commit `e9cdad8`): si
  `/pagar-otro-documento` responde "ya ha sido pagada" (reintento después de
  que se perdió la respuesta de un pago exitoso), se sigue directo a traer el
  comprobante en vez de dejar la solicitud en `ERROR` para siempre.
- Pago simulado (sin dinero real, vía `simular_pago_qr.php` de cessa-laravel)
  para el cliente **178894**: la Transacción quedó `PAGADA` en
  `api-cobranzas-test` (verificado con `GET /v1/transacciones/{uuid}`).
- **Bloqueo**: `GET /v1/transacciones/{uuid}/documentos` (pdf y json) falla con
  `No existe registro en CFC510 [(cliente, comprobante) = (178894, 892140)]`.
  Igual con los dos aliases de prueba (`CESSA-SIM-20260922125619-MSGC`, 35
  meses; `CESSA-SIM-20260922145238-E2BM`, 5 meses) -- siempre se pagan
  primero los meses más antiguos, así que 892140 (nov/2023) siempre encabeza
  el detalle.

**Hallazgo -- sí existe un SIIC de test** (antes se asumía que no). Revisando
las configs nginx del reverse-proxy `*.bo-com-assec.net` (tabla completa en
README §"Integraciones externas"): `api-siic-test` apunta a un upstream propio,
y el mismo `SIIC_DEUDA_TOKEN` de prod funciona ahí. El cliente 178894 tiene 35
ítems (Bs 717) en SIIC **prod**, pero **0 ítems en SIIC test**. Lo más probable
es que el pago de prueba de 35 meses (MSGC) haya consumido su deuda en la base
de test (SIIC test y cobranzas test compartirían base).

**Diagnóstico:** el `.env` de 10.1.1.88 toma la deuda de SIIC **prod**
(`SIIC_DEUDA_BASE_URL=https://api-siic-prod-1...`) y la paga en cobranzas
**test** -- se mezclan ambientes. El detalle mandado a pagar tiene que salir
del mismo ambiente que lo paga.

**Pendiente para la próxima prueba (se hará desde otra máquina):**

1. Usar un cliente que **hoy** tenga deuda en SIIC test (el usuario ya tiene
   uno identificado) -- 178894 ya no sirve en test, su deuda quedó en 0.
2. Durante la prueba, tomar la deuda de SIIC test: `SIIC_DEUDA_BASE_URL`
   apuntando directo al upstream de `api-siic-test` con `Host:
   api-siic-test.bo-com-assec.net` (por el dominio público, `api-siic-test`
   hoy cae al vhost default y responde cobranzas-test, ver README), o armar el
   `detalle` de la `SolicitudLiquidacion` a mano desde esa respuesta en Django
   shell.
3. **Pagar pocos meses** (1-2), no todo, para no consumir la deuda de test de
   una sola vez y poder repetir la prueba.
4. Correr `liquidar_solicitud()` y verificar comprobante PDF/JSON. Si con datos
   coherentes de test igual falla CFC510, recién ahí sospechar de un paso
   faltante (endpoint de emisión) y preguntar a quien administra
   `api-cobranzas-bancos`.
5. **Limpieza**: aliases MSGC/E2BM (`Recibo` en cessa-laravel +
   `SolicitudLiquidacion` acá) y borrar `simular_pago_qr.php` /
   `forzar_facturacion.php` de `cessa-laravel/public/` en Hostinger.

## Orden sugerido para arrancar a construir

No es una decisión tomada, es una propuesta a validar con vos (pregunta 1 de
`REQUISITOS_COBRANZA.md` sigue abierta):

1. **Épica E** (caja) — es la base: sin esto, D y G/H no tienen de dónde sacar los totales.
2. **Épica D** (efectivo) — depende de E para vincular el cobro a una caja.
3. **Épica G** (dashboard supervisor) — depende de que E y D ya generen datos reales.
4. **Épica H** (dashboard admin) — extiende G.
5. **A3/A4** (cambio de contraseña) — independiente, rápido, se puede intercalar en cualquier momento.
6. **F3/F5** (facturación real/fiscal) y credenciales reales de MC4/SIIC — quedan bloqueadas por terceros (SIIC), no dependen del resto.
