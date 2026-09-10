from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS")

# Asumen por defecto que hay TLS terminado en algún punto delante de gunicorn
# (nginx o un reverse-proxy externo) -- el caso real cuando esto se exponga a
# internet. El despliegue interno en 10.1.1.88 no tiene TLS (nginx sirve HTTP
# plano dentro de la red de CESSA), así que ese despliegue debe poner
# SECURE_SSL_REDIRECT=False (y los *_COOKIE_SECURE también) en su .env --
# si no, Django redirige todo a https y ahí no hay nada escuchando.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
