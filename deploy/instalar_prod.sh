#!/usr/bin/env bash
# Prepara (o actualiza) la instancia de PRODUCCIÓN del gateway en la .88, al lado de la de test.
# Ver docs/PASE_A_PRODUCCION.md. Se corre como soporte con sudo; idempotente.
#
#   sudo bash deploy/instalar_prod.sh            # preparar / actualizar código, deps, migraciones
#
# NO enciende nada: los servicios quedan creados pero deshabilitados y el sitio de nginx en
# sites-available sin enlazar. Encender es un paso aparte (§ "Encender" de PASE_A_PRODUCCION.md),
# recién con el .env completo y `verificar_produccion` en 0 errores.
set -euo pipefail

PROD=/opt/cobranza-cessa-prod
TEST=/opt/cobranza-cessa
REPO=https://github.com/Dario-M-Sinani/cobranza-cessa.git
BASE_DATOS=cobranza_cessa_produccion
LOGS=/var/log/cobranza-cessa-prod
CA_CLOUDFLARE=/etc/ssl/certs/cloudflare-origin-ca-root.pem
como_app() { sudo -u cobranza "$@"; }
dj() { (cd "$PROD" && como_app env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py "$@"); }

echo "== 1. Código ($PROD)"
if [ ! -d "$PROD/.git" ]; then
  install -d -o cobranza -g cobranza "$PROD"
  como_app git clone -q "$REPO" "$PROD"
else
  como_app git -C "$PROD" pull -q --ff-only
fi
como_app git -C "$PROD" log -1 --oneline

echo "== 2. Entorno Python"
[ -x "$PROD/.venv/bin/python" ] || como_app python3 -m venv "$PROD/.venv"
como_app "$PROD/.venv/bin/pip" install -q -r "$PROD/requirements/production.txt"

echo "== 3. Base de datos ($BASE_DATOS)"
DUENO=$(sudo -u postgres psql -Atc "select pg_get_userbyid(datdba) from pg_database where datname='cobranza_cessa'")
if ! sudo -u postgres psql -Atc "select 1 from pg_database where datname='$BASE_DATOS'" | grep -q 1; then
  sudo -u postgres createdb -O "$DUENO" "$BASE_DATOS"
  echo "creada, dueño $DUENO"
fi

echo "== 4. .env de producción"
if [ ! -f "$PROD/.env" ]; then
  # Desde el de test: misma credencial de Postgres (otra base), mismo token del SIIC (la API Nest
  # prod :6013 lo acepta para lecturas), mismas alertas. Claves nuevas. Lo de api-cobranzas PROD
  # queda vacío a propósito: lo dan el administrador del SIIC / .102 (alta de CESSA Web).
  valor() { grep -E "^$1=" "$TEST/.env" | head -1 | cut -d= -f2- | tr -d '\r'; }
  nueva() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
  BD=$(valor DATABASE_URL | sed -E "s#/[^/]+\$#/$BASE_DATOS#")
  install -m 600 -o cobranza -g cobranza /dev/null "$PROD/.env"
  cat > "$PROD/.env" <<EOF
# PRODUCCIÓN del gateway (generado por deploy/instalar_prod.sh el $(date +%F)). Ver docs/PASE_A_PRODUCCION.md §3.
SECRET_KEY=$(nueva 50)
ALLOWED_HOSTS=localhost,127.0.0.1
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
CSRF_COOKIE_SECURE=False
DATABASE_URL=$BD
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/1

# Nueva: la misma va a COBRANZAS_GATEWAY_API_KEY de cessa-laravel PROD.
API_KEY_CESSA_LARAVEL=$(nueva 40)

# api-cobranzas-bancos PRODUCCIÓN (.102:6002). Opción B de PASE_A_PRODUCCION.md: con
# "10.1.1.102 api-cobranzas-prod-6002.bo-com-assec.net" en /etc/hosts y la CA de Cloudflare Origin.
# OJO: sin esa línea de /etc/hosts el dominio cae en la API de TEST (verificar_produccion lo detecta).
COBRANZAS_BANCO_CLIENT_CLASS=services.cobranzas_banco_client.LumenCobranzasBancoClient
COBRANZAS_BANCO_BASE_URL=https://api-cobranzas-prod-6002.bo-com-assec.net:6002
COBRANZAS_BANCO_VERIFY=$CA_CLOUDFLARE
COBRANZAS_BANCO_CLIENT_ID=
COBRANZAS_BANCO_CLIENT_SECRET=
COBRANZAS_BANCO_USERNAME=
COBRANZAS_BANCO_PASSWORD=
COBRANZAS_BANCO_AGENCIA_SIGLA=
COBRANZAS_BANCO_DOCUMENTO_ENTE_ID=
COBRANZAS_BANCO_DOCUMENTO_BANCO_ID=
COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BISA=
COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BNB=

# SIIC de la deuda (historial, PDF, kWh): API Nest PROD, solo lectura.
DEUDA_CLIENT_CLASS=services.deuda_client.SiicDeudaClient
SIIC_DEUDA_BASE_URL=http://127.0.0.1:6013
SIIC_DEUDA_TOKEN=$(valor SIIC_DEUDA_TOKEN)

# Alertas (por ahora las mismas que test; para prod, bot/grupo del equipo).
ALERTAS_TELEGRAM_BOT_TOKEN=$(valor ALERTAS_TELEGRAM_BOT_TOKEN)
ALERTAS_TELEGRAM_CHAT_IDS=$(valor ALERTAS_TELEGRAM_CHAT_IDS)
PANEL_URL=$(valor PANEL_URL)
EOF
  chown cobranza:cobranza "$PROD/.env"; chmod 600 "$PROD/.env"
  echo "creado (completar lo vacío de api-cobranzas antes de encender)"
else
  echo "ya existe: no se toca"
fi

echo "== 5. CA de Cloudflare Origin (certificado de la .102)"
[ -s "$CA_CLOUDFLARE" ] || curl -sf -m 30 -o "$CA_CLOUDFLARE" https://developers.cloudflare.com/ssl/static/origin_ca_rsa_root.pem
openssl x509 -in "$CA_CLOUDFLARE" -noout -subject -enddate

echo "== 6. Migraciones y estáticos"
install -d -o cobranza -g cobranza "$LOGS"
dj migrate --noinput | tail -1
dj collectstatic --noinput | tail -1

echo "== 7. Servicios systemd (creados, NO habilitados)"
for unidad in gunicorn celery-worker celery-beat; do
  sed -e "s#/opt/cobranza-cessa/#$PROD/#g; s#/opt/cobranza-cessa\$#$PROD#g; s#WorkingDirectory=/opt/cobranza-cessa#WorkingDirectory=$PROD#" \
      -e "s#127.0.0.1:8001#127.0.0.1:8002#; s#/var/log/cobranza-cessa/#$LOGS/#g" \
      -e "s#^Description=cobranza_cessa#Description=cobranza_cessa PROD#" \
      "$PROD/deploy/$unidad.service" > "/etc/systemd/system/cobranza-cessa-prod-$unidad.service"
done
systemctl daemon-reload
systemctl is-enabled cobranza-cessa-prod-gunicorn cobranza-cessa-prod-celery-worker cobranza-cessa-prod-celery-beat || true

echo "== 8. Sitio nginx (en sites-available, NO enlazado)"
cp "$PROD/deploy/nginx-prod.conf" /etc/nginx/sites-available/cobranza-cessa-prod
echo "editar server_name (dominio de prod) antes de enlazarlo"

echo
echo "Listo (apagado). Siguiente: completar $PROD/.env y correr:"
echo "  cd $PROD && sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py verificar_produccion"
