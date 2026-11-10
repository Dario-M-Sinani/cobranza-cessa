from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# Orígenes del SPA (React) autorizados a llamar esta API vía CORS.
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5174"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "apps.usuarios",
    "apps.clientes",
    "apps.auditoria",
    "apps.cobranza",
    "apps.facturacion_externa",
    "apps.kiosco",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.usuarios.middleware.SoloConsultaMiddleware",
]

# Gateway de producción: el panel es solo para ver Pagos web/Dashboard; cobrar y caja dan 403
# (apps/usuarios/middleware.py) y el frontend los oculta.
PANEL_SOLO_CONSULTA = env.bool("PANEL_SOLO_CONSULTA", default=False)

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://cobranza:cobranza@localhost:5432/cobranza_cessa",
    ),
}

AUTH_USER_MODEL = "usuarios.Usuario"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es-bo"
TIME_ZONE = "America/La_Paz"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # Solo las vistas que lo piden (apps/kiosco/permissions.py: ConsultasPorKiosco).
    "DEFAULT_THROTTLE_RATES": {
        "kiosco_consulta": env("KIOSCO_CONSULTAS_POR_MINUTO", default="30/min"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
}

# Celery / Redis
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# Requiere correr `celery -A config beat` además del worker (no incluido acá
# como dependencia nueva: usa el scheduler de archivo que trae Celery por
# defecto; para producción con múltiples workers considerar django-celery-beat).
CELERY_BEAT_SCHEDULE = {
    "verificar-transacciones-pendientes": {
        "task": "apps.cobranza.tasks.verificar_transacciones_pendientes",
        "schedule": 60.0,
    },
    "enviar-facturas-pendientes": {
        "task": "apps.cobranza.tasks.enviar_facturas_pendientes",
        "schedule": 60.0,
    },
}

# Integración MC4/SIP (ver services/mc4_client.py) -- misma pasarela que ya usa
# cessa-laravel en producción (SipQrProvider.php) contra Banco BISA. Sin
# credenciales reales todavía: queda como stub documentado.
MC4_CLIENT_CLASS = env("MC4_CLIENT_CLASS", default="services.mc4_client.SipMC4Client")
MC4_BASE_URL = env("MC4_BASE_URL", default="")
MC4_APIKEY = env("MC4_APIKEY", default="")
MC4_USERNAME = env("MC4_USERNAME", default="")
MC4_PASSWORD = env("MC4_PASSWORD", default="")
MC4_APIKEY_SERVICIO = env("MC4_APIKEY_SERVICIO", default="")

# Consulta de deuda -- endpoint intermedio del SIIC (consulta-deuda), no
# conexión directa a Db2. Sin credenciales reales todavía.
DEUDA_CLIENT_CLASS = env("DEUDA_CLIENT_CLASS", default="services.deuda_client.SiicDeudaClient")
SIIC_DEUDA_BASE_URL = env("SIIC_DEUDA_BASE_URL", default="")
SIIC_DEUDA_TOKEN = env("SIIC_DEUDA_TOKEN", default="")

# api-cobranzas-bancos (ver services/cobranzas_banco_client.py) -- registra
# cobros ya pagados (QR o efectivo) como factura real en el SIIC. Este VPS
# está en la red de facturación a propósito: cessa-laravel (Hostinger) nunca
# pudo llegar a esta API interna, por eso la liquidación real se hace acá.
# Sin credenciales reales todavía: queda con FakeCobranzasBancoClient.
COBRANZAS_BANCO_CLIENT_CLASS = env(
    "COBRANZAS_BANCO_CLIENT_CLASS", default="services.fakes.FakeCobranzasBancoClient"
)
COBRANZAS_BANCO_BASE_URL = env("COBRANZAS_BANCO_BASE_URL", default="")
COBRANZAS_BANCO_CLIENT_ID = env("COBRANZAS_BANCO_CLIENT_ID", default="")
COBRANZAS_BANCO_CLIENT_SECRET = env("COBRANZAS_BANCO_CLIENT_SECRET", default="")
COBRANZAS_BANCO_USERNAME = env("COBRANZAS_BANCO_USERNAME", default="")
COBRANZAS_BANCO_PASSWORD = env("COBRANZAS_BANCO_PASSWORD", default="")
COBRANZAS_BANCO_AGENCIA_SIGLA = env("COBRANZAS_BANCO_AGENCIA_SIGLA", default="")
# Número de caja (dispositivo) con el que se apertura cada día, ej. 8199. Vacío: lo asigna el SIIC.
COBRANZAS_BANCO_CAJA_CODIGO = env("COBRANZAS_BANCO_CAJA_CODIGO", default="")
# TLS hacia api-cobranzas-bancos: "true" (por defecto) o la ruta a un .pem con el certificado de
# la .102 (su IP directa, ej. https://10.1.1.102:6002, no tiene un certificado verificable por la
# cadena pública). Nunca "false" en producción.
COBRANZAS_BANCO_VERIFY = env("COBRANZAS_BANCO_VERIFY", default="true")
# Catálogos GET /v1/entes y GET /v1/bancos de api-cobranzas-bancos -- los IDs
# que corresponden a "pago por QR/transferencia electrónica" y al banco
# destino real (hoy Banco BISA). Hay que consultarlos una vez que haya red
# hacia esa API para saber qué valor poner acá (ver FacturacionRecibo.php,
# construirDocumento(), del lado cessa-laravel).
COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = env("COBRANZAS_BANCO_DOCUMENTO_ENTE_ID", default="")
COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = env("COBRANZAS_BANCO_DOCUMENTO_BANCO_ID", default="")
# banco_id (catálogo GET /v1/bancos) por banco de origen del cobro QR web, para que lo pagado por
# BNB no quede registrado en el SIIC como BISA. Si falta uno, se usa el DOCUMENTO_BANCO_ID de arriba.
COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS = {
    "sip_bisa": env("COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BISA", default=""),
    "bnb": env("COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BNB", default=""),
}

# API keys de servicios externos autorizados a llamar apps.facturacion_externa
# (ver apps/facturacion_externa/permissions.py) -- hoy solo cessa-laravel.
# Server-a-servidor, no es un usuario/rol del sistema: nunca usar el JWT de
# cajera/supervisor para esto.
API_KEY_CESSA_LARAVEL = env("API_KEY_CESSA_LARAVEL", default="")

# Callback único de SIP/BISA (apps/facturacion_externa/sip_callback.py): SIP manda un solo Basic
# Auth por cuenta, así que todos los sistemas que generan QR SIP mandan esta misma URL en
# generaQr y el gateway decide por el prefijo del alias. Los "txqr-" son de este panel; el resto
# se reenvía según SIP_CALLBACK_REENVIOS ("PREFIJO=url,PREFIJO=url"), con X-Api-Key =
# API_KEY_CESSA_LARAVEL. Ej.: CESSA-=https://test.cessa.com.bo/api/pagos/sip/confirmar-pago
SIP_CALLBACK_USERNAME = env("SIP_CALLBACK_USERNAME", default="")
SIP_CALLBACK_PASSWORD = env("SIP_CALLBACK_PASSWORD", default="")
SIP_CALLBACK_URL = env("SIP_CALLBACK_URL", default="")
SIP_CALLBACK_REENVIOS = dict(
    par.strip().split("=", 1) for par in env("SIP_CALLBACK_REENVIOS", default="").split(",") if "=" in par
)
# Prefijos que se responden 0000 sin reenviar (se miran antes que SIP_CALLBACK_REENVIOS): el bot
# de Telegram (alias CESSA-BOT-...) no tiene URL pública y detecta el pago consultando a SIP.
SIP_CALLBACK_SIN_REENVIO = env.list("SIP_CALLBACK_SIN_REENVIO", default=["CESSA-BOT-"])

# Pantallas táctiles de autoservicio (apps/kiosco). Cada pantalla tiene su token:
# KIOSCO_TOKENS="oficina-central=token1,agencia-norte=token2". Sin tokens no atiende ninguna.
KIOSCO_TOKENS = dict(
    par.strip().split("=", 1) for par in env("KIOSCO_TOKENS", default="").split(",") if "=" in par
)
# Redes desde las que se acepta una pantalla (además del token). Nunca internet.
KIOSCO_REDES = env.list("KIOSCO_REDES", default=["10.0.0.0/8", "127.0.0.1/32"])
# Vigencia del QR en pantalla, como el bot (después se da de baja en SIP).
KIOSCO_MINUTOS_QR = env.int("KIOSCO_MINUTOS_QR", default=5)
# Barra de avisos de cortes (apps/kiosco/avisos.py). Programados: API interna de cortes (header
# Authorization con el token, sin "Bearer"; su certificado es de Cloudflare Origin CA: VERIFY con la
# ruta al .pem, como COBRANZAS_BANCO_VERIFY). Emergencias (y respaldo de los programados): la
# página de inicio de la web. Las dos vacías = sin barra.
KIOSCO_CORTES_API_URL = env("KIOSCO_CORTES_API_URL", default="")
KIOSCO_CORTES_API_TOKEN = env("KIOSCO_CORTES_API_TOKEN", default="")
KIOSCO_CORTES_API_VERIFY = env("KIOSCO_CORTES_API_VERIFY", default="true")
KIOSCO_AVISOS_URL = env("KIOSCO_AVISOS_URL", default="https://cessa.com.bo/")
KIOSCO_AVISOS_MINUTOS = env.int("KIOSCO_AVISOS_MINUTOS", default=10)
# Fotos del carrusel de la web (protector de pantalla y fondo): cada cuánto se relee la lista.
KIOSCO_GALERIA_MINUTOS = env.int("KIOSCO_GALERIA_MINUTOS", default=60)
# Correo y celular registrados en "Actualizar Datos" de cessa-laravel, para mandar la factura
# (ej. https://cessa.com.bo/api/gateway/clientes/{nro_cliente}/contacto, con X-Api-Key =
# API_KEY_CESSA_LARAVEL). Vacío = el cliente solo puede escribir el correo en la pantalla.
KIOSCO_CONTACTO_URL = env("KIOSCO_CONTACTO_URL", default="")
# Envío de la factura por WhatsApp: API de notificaciones de CESSA (Evolution API, solo texto),
# ej. http://10.1.1.45:3006/api/v1/notifications/text con header x-api-key. Vacío = sin WhatsApp.
KIOSCO_WHATSAPP_URL = env("KIOSCO_WHATSAPP_URL", default="")
KIOSCO_WHATSAPP_API_KEY = env("KIOSCO_WHATSAPP_API_KEY", default="")
CELERY_BEAT_SCHEDULE["revisar-pagos-kiosco"] = {
    "task": "apps.kiosco.tasks.revisar_pagos_kiosco",
    "schedule": 60.0,
}

# Horario operativo dentro del cual un cajero puede abrir su Caja sin
# intervención de un supervisor/administrador (ver apps/cobranza/horario.py).
# Horas 0-23, real de CESSA todavía sin confirmar -- ajustar por entorno.
CAJA_HORARIO_INICIO = env.int("CAJA_HORARIO_INICIO", default=8)

# Django, por defecto, con DEBUG=False solo manda los 500 sin capturar a
# ADMINS por email (AdminEmailHandler) -- si eso no está configurado (como
# acá), el error se pierde sin dejar ningún rastro, ni siquiera en el log de
# gunicorn. Se sobreescribe para que todo lo que llegue a nivel ERROR (o más)
# -- en particular 'django.request', que es donde Django loguea cada 500 con
# su traceback completo -- vaya siempre a stderr, sin el filtro
# require_debug_true del logging por defecto. gunicorn.service ya redirige
# stderr a /var/log/cobranza-cessa/gunicorn-error.log.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
CAJA_HORARIO_FIN = env.int("CAJA_HORARIO_FIN", default=18)

# Alertas al equipo cuando un pago web queda sin factura (apps/facturacion_externa/alertas.py,
# services/alertas.py). Todo opcional: sin canal configurado no se envía nada.
ALERTAS_TELEGRAM_BOT_TOKEN = env("ALERTAS_TELEGRAM_BOT_TOKEN", default="")
ALERTAS_TELEGRAM_CHAT_IDS = env.list("ALERTAS_TELEGRAM_CHAT_IDS", default=[])
ALERTAS_EMAIL_DESTINOS = env.list("ALERTAS_EMAIL_DESTINOS", default=[])
# Prefijo del asunto de los correos de alerta: "[Notificaciones Web CESSA] ...".
ALERTAS_EMAIL_PREFIJO = env("ALERTAS_EMAIL_PREFIJO", default="Notificaciones Web CESSA")
# Intentos de facturación tras los que ya no se reintenta solo (mismo tope que la web y el bot):
# se manda el aviso final "no se pudo facturar" (alertas.py).
ALERTAS_INTENTOS_MAXIMOS = env.int("ALERTAS_INTENTOS_MAXIMOS", default=5)
# Para el enlace "Ver: .../pagos-web" de las alertas (ej. https://10.1.1.88).
PANEL_URL = env("PANEL_URL", default="")

# Correo saliente (lo usa ALERTAS_EMAIL_DESTINOS). Sin EMAIL_HOST, Django intenta localhost:25
# y en la .88 no hay servidor de correo. Con Gmail: smtp.gmail.com, 587, TLS, la cuenta y una
# "contraseña de aplicación" (no la clave normal). Ver deploy/configurar_correo_prod.sh.
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=15)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER or "webmaster@localhost")

CELERY_BEAT_SCHEDULE["resumen-diario-pagos-web-con-error"] = {
    "task": "apps.facturacion_externa.tasks.resumen_diario_errores",
    "schedule": crontab(hour=8, minute=0),
}

# Un intento más, de madrugada, de los pagos que siguen en error (tasks.reintento_nocturno).
REINTENTO_NOCTURNO_HORA = env.int("REINTENTO_NOCTURNO_HORA", default=2)
CELERY_BEAT_SCHEDULE["reintento-nocturno-pagos-con-error"] = {
    "task": "apps.facturacion_externa.tasks.reintento_nocturno",
    "schedule": crontab(hour=REINTENTO_NOCTURNO_HORA, minute=0),
}

CELERY_BEAT_SCHEDULE["conciliacion-diaria-pagos"] = {
    "task": "apps.facturacion_externa.tasks.conciliacion_diaria",
    "schedule": crontab(hour=7, minute=30),
}
