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
- [ ] **C6.** Como cajero, quiero ver un mensaje claro si el proveedor MC4 no responde al generar el QR (en vez de que la pantalla se quede colgada o rompa).
- [ ] **C7.** Como sistema, quiero marcar automáticamente como `vencido` un QR que no se pagó dentro del plazo, para que no quede "pendiente" indefinidamente.
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

## Orden sugerido para arrancar a construir

No es una decisión tomada, es una propuesta a validar con vos (pregunta 1 de
`REQUISITOS_COBRANZA.md` sigue abierta):

1. **Épica E** (caja) — es la base: sin esto, D y G/H no tienen de dónde sacar los totales.
2. **Épica D** (efectivo) — depende de E para vincular el cobro a una caja.
3. **Épica G** (dashboard supervisor) — depende de que E y D ya generen datos reales.
4. **Épica H** (dashboard admin) — extiende G.
5. **A3/A4** (cambio de contraseña) — independiente, rápido, se puede intercalar en cualquier momento.
6. **F3/F5** (facturación real/fiscal) y credenciales reales de MC4/SIIC — quedan bloqueadas por terceros (SIIC), no dependen del resto.
