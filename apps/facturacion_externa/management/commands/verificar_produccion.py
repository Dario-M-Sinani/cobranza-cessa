"""Chequeo SOLO LECTURA de que el gateway está listo para producción (o bien configurado para test).

    python manage.py verificar_produccion                 # lo que exige producción
    python manage.py verificar_produccion --entorno test  # lo mismo, sin exigir URLs/usuario de prod

Revisa la configuración (.env) y la conectividad: login en api-cobranzas-bancos, catálogo de
bancos y entes (que los ids configurados existan y sean BISA / BANCO NACIONAL), el SIIC de la
deuda y una consulta de deuda como banco. Cada punto sale OK / AVISO / ERROR; con algún ERROR
termina con código 1. Nunca apertura caja ni crea, paga o anula nada. Ver docs/PASE_A_PRODUCCION.md.
"""

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from services.cobranzas_banco_client import CobranzasBancoError, get_cobranzas_banco_client, verificacion_tls
from services.deuda_client import DeudaClientError, consultar_cliente_via_cobranzas

OK, AVISO, ERROR = "OK", "AVISO", "ERROR"


class Command(BaseCommand):
    help = "Verifica (solo lectura) configuración y conectividad del gateway para producción."

    def add_arguments(self, parser):
        parser.add_argument("--entorno", choices=["prod", "test"], default="prod")
        parser.add_argument("--cliente", help="nro de cliente para probar la consulta de deuda como banco")

    def handle(self, *args, **opciones):
        self.prod = opciones["entorno"] == "prod"
        self.resultados = []
        self._configuracion()
        self._identidad_cobranzas()
        self._cobranzas()
        self._siic_deuda()
        if opciones.get("cliente"):
            self._consulta_como_banco(opciones["cliente"])

        estilos = {OK: self.style.SUCCESS, AVISO: self.style.WARNING, ERROR: self.style.ERROR}
        for nivel, mensaje in self.resultados:
            self.stdout.write(estilos[nivel](f"[{nivel:5}] ") + mensaje)
        errores = sum(1 for nivel, _ in self.resultados if nivel == ERROR)
        avisos = sum(1 for nivel, _ in self.resultados if nivel == AVISO)
        self.stdout.write(f"\n{errores} errores, {avisos} avisos (entorno esperado: {opciones['entorno']}).")
        if errores:
            raise CommandError("Hay errores: no está listo.")

    def _r(self, nivel, mensaje):
        self.resultados.append((nivel, mensaje))

    def _solo_prod(self, condicion_mala: bool, mensaje: str, ok: str):
        """En prod es ERROR; en test, lo esperado."""
        if condicion_mala:
            self._r(ERROR if self.prod else OK, mensaje if self.prod else f"{ok} (test)")
        else:
            self._r(OK if self.prod else AVISO, ok if self.prod else f"{ok} -- ¿es test?")

    # --- .env -------------------------------------------------------------------------------

    def _configuracion(self):
        self._r(ERROR if settings.DEBUG else OK, f"DEBUG={settings.DEBUG}")
        clave = settings.SECRET_KEY or ""
        self._r(AVISO if len(clave) < 40 else OK, f"SECRET_KEY de {len(clave)} caracteres")
        hosts = list(settings.ALLOWED_HOSTS)
        self._r(ERROR if "*" in hosts else OK, f"ALLOWED_HOSTS={','.join(hosts)}")

        api_key = settings.API_KEY_CESSA_LARAVEL or ""
        if not api_key:
            self._r(ERROR, "API_KEY_CESSA_LARAVEL vacía: cessa-laravel no puede llamar al gateway")
        else:
            self._r(AVISO if len(api_key) < 32 else OK, f"API_KEY_CESSA_LARAVEL de {len(api_key)} caracteres")

        base = settings.COBRANZAS_BANCO_BASE_URL or ""
        self._solo_prod("test" in base.lower(), f"COBRANZAS_BANCO_BASE_URL apunta a test: {base}", f"COBRANZAS_BANCO_BASE_URL={base}")
        usuario = settings.COBRANZAS_BANCO_USERNAME or ""
        self._solo_prod(
            usuario.upper() == "CABISAQR",
            "COBRANZAS_BANCO_USERNAME=CABISAQR es el cajero BISA QR de TEST",
            f"COBRANZAS_BANCO_USERNAME={usuario}",
        )
        siic = settings.SIIC_DEUDA_BASE_URL or ""
        self._solo_prod(
            ":6012" in siic or "test" in siic.lower(),
            f"SIIC_DEUDA_BASE_URL apunta al SIIC de test: {siic}",
            f"SIIC_DEUDA_BASE_URL={siic}",
        )
        self._r(OK, f"DEUDA_CLIENT_CLASS={settings.DEUDA_CLIENT_CLASS.rsplit('.', 1)[-1]} (panel de cajeras)")
        self._r(AVISO, f"MC4_BASE_URL={settings.MC4_BASE_URL or '(vacío)'}: confirmar que sea el de {'producción' if self.prod else 'test'} (QR del panel)")

        for nombre in ("ENTE_ID", "BANCO_ID"):
            valor = getattr(settings, f"COBRANZAS_BANCO_DOCUMENTO_{nombre}", "")
            self._r(OK if valor else ERROR, f"COBRANZAS_BANCO_DOCUMENTO_{nombre}={valor or '(vacío)'}")
        for canal, valor in settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS.items():
            self._r(
                OK if valor else ERROR,
                f"banco_id para {canal}={valor or '(vacío: quedaría como BANCO_ID por defecto)'}",
            )

    # --- api-cobranzas-bancos ---------------------------------------------------------------

    def _identidad_cobranzas(self):
        """Qué instancia contesta de verdad: la de test de la .102 dice "... TEST" en su portada.
        Un dominio de prod cuyo vhost no está activo en el proxy cae en la de test sin avisar
        (pasaba con api-cobranzas-prod-6002.bo-com-assec.net al 2026-10-09): la URL no basta."""
        verify = verificacion_tls()
        if verify is False:
            self._r(ERROR if self.prod else AVISO, "COBRANZAS_BANCO_VERIFY=false: TLS sin verificar hacia api-cobranzas")
        elif verify is not True:
            self._r(OK, f"COBRANZAS_BANCO_VERIFY={verify} (certificado propio de la .102)")
        base = (settings.COBRANZAS_BANCO_BASE_URL or "").rstrip("/")
        if not base:
            self._r(ERROR, "COBRANZAS_BANCO_BASE_URL vacío")
            return
        try:
            portada = requests.get(f"{base}/", timeout=15, verify=verify).text.strip()[:80]
        except requests.exceptions.SSLError as exc:
            self._r(ERROR, f"Certificado de {base} no verificable ({exc.__class__.__name__}): usar COBRANZAS_BANCO_VERIFY=<ruta .pem>")
            return
        except requests.RequestException as exc:
            self._r(ERROR, f"api-cobranzas sin conexión: {exc}")
            return
        es_test = "TEST" in portada.upper()
        if self.prod:
            self._r(ERROR if es_test else OK, f"api-cobranzas responde: {portada!r}" + (" -- ES LA DE TEST" if es_test else ""))
        else:
            self._r(OK if es_test else AVISO, f"api-cobranzas responde: {portada!r}" + ("" if es_test else " -- ¿no es la de test?"))

    def _cobranzas(self):
        try:
            cliente = get_cobranzas_banco_client()
            cliente._obtener_token(forzar_refresco=True)
        except (CobranzasBancoError, requests.RequestException) as exc:
            self._r(ERROR, f"Login en api-cobranzas-bancos falló: {exc}")
            return
        self._r(OK, "Login en api-cobranzas-bancos")

        catalogos = {}
        for nombre, path in (("bancos", "/v1/bancos"), ("entes", "/v1/entes")):
            try:
                r = cliente._request_autenticado("get", path)
                filas = r.json() if r.ok else []
                catalogos[nombre] = {str(f.get("id")): str(f.get("descripcion", "")).strip() for f in filas if isinstance(f, dict)}
            except (CobranzasBancoError, requests.RequestException, ValueError) as exc:
                self._r(ERROR, f"No se pudo leer {path}: {exc}")
                catalogos[nombre] = {}

        bancos = catalogos.get("bancos") or {}
        esperado = {"sip_bisa": "BISA", "bnb": "NACIONAL"}
        for canal, valor in settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS.items():
            if not valor or not bancos:
                continue
            descripcion = bancos.get(str(valor))
            bien = descripcion and esperado.get(canal, "") in descripcion.upper()
            self._r(OK if bien else ERROR, f"banco_id {valor} para {canal} = {descripcion or 'NO EXISTE en el catálogo'}")
        defecto = settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID
        if defecto and bancos:
            self._r(OK if str(defecto) in bancos else ERROR, f"BANCO_ID por defecto {defecto} = {bancos.get(str(defecto), 'NO EXISTE')}")
        entes = catalogos.get("entes") or {}
        ente = settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID
        if ente and entes:
            self._r(OK if str(ente) in entes else ERROR, f"ENTE_ID {ente} = {entes.get(str(ente), 'NO EXISTE')}")

        try:
            r = cliente._request_autenticado("get", "/v1/cajas/existe")
            self._r(OK, f"Caja del día: HTTP {r.status_code} (404 = todavía no se abrió hoy; se abre sola con el primer pago)")
        except (CobranzasBancoError, requests.RequestException) as exc:
            self._r(AVISO, f"/v1/cajas/existe: {exc}")

    # --- SIIC de la deuda (historial, PDF, consumo) -----------------------------------------

    def _siic_deuda(self):
        base = (settings.SIIC_DEUDA_BASE_URL or "").rstrip("/")
        if not base:
            self._r(ERROR, "SIIC_DEUDA_BASE_URL vacío")
            return
        try:
            r = requests.get(
                f"{base}/v1/clientes/1/pagos", params={"limit": 1},
                headers={"Authorization": settings.SIIC_DEUDA_TOKEN}, timeout=20,
            )
        except requests.RequestException as exc:
            self._r(ERROR, f"SIIC de la deuda sin conexión: {exc}")
            return
        if r.status_code in (401, 403):
            self._r(ERROR, f"SIIC de la deuda rechazó SIIC_DEUDA_TOKEN (HTTP {r.status_code})")
        else:
            self._r(OK if r.status_code < 500 else ERROR, f"SIIC de la deuda responde (HTTP {r.status_code})")

    def _consulta_como_banco(self, nro_cliente):
        try:
            status, body = consultar_cliente_via_cobranzas({"nro_cliente": nro_cliente, "ver_deuda": "si"})
        except DeudaClientError as exc:
            self._r(ERROR, f"Consulta de deuda como banco falló: {exc}")
            return
        if status == 200 and body.get("nro_cliente"):
            self._r(OK, f"Consulta como banco: cliente {nro_cliente}, {len(body.get('deuda') or [])} comprobantes pendientes")
        else:
            self._r(AVISO, f"Consulta como banco: HTTP {status} {str(body)[:120]}")
