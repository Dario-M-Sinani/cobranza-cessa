from datetime import timedelta
from pathlib import Path

import environ

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
]

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
# Catálogos GET /v1/entes y GET /v1/bancos de api-cobranzas-bancos -- los IDs
# que corresponden a "pago por QR/transferencia electrónica" y al banco
# destino real (hoy Banco BISA). Hay que consultarlos una vez que haya red
# hacia esa API para saber qué valor poner acá (ver FacturacionRecibo.php,
# construirDocumento(), del lado cessa-laravel).
COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = env("COBRANZAS_BANCO_DOCUMENTO_ENTE_ID", default="")
COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = env("COBRANZAS_BANCO_DOCUMENTO_BANCO_ID", default="")

# API keys de servicios externos autorizados a llamar apps.facturacion_externa
# (ver apps/facturacion_externa/permissions.py) -- hoy solo cessa-laravel.
# Server-a-servidor, no es un usuario/rol del sistema: nunca usar el JWT de
# cajera/supervisor para esto.
API_KEY_CESSA_LARAVEL = env("API_KEY_CESSA_LARAVEL", default="")

# Horario operativo dentro del cual un cajero puede abrir su Caja sin
# intervención de un supervisor/administrador (ver apps/cobranza/horario.py).
# Horas 0-23, real de CESSA todavía sin confirmar -- ajustar por entorno.
CAJA_HORARIO_INICIO = env.int("CAJA_HORARIO_INICIO", default=8)
CAJA_HORARIO_FIN = env.int("CAJA_HORARIO_FIN", default=18)
