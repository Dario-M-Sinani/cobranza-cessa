#!/usr/bin/env bash
# Configura las alertas de pagos web sin factura (Telegram y/o correo) en el gateway de
# PRODUCCIÓN, reinicia sus servicios y manda un mensaje de prueba. Se corre en la .88, con sudo:
#
#   sudo bash ~soporte/configurar_alertas_prod.sh
#
# Antes: crear el bot con @BotFather (/newbot), agregarlo al grupo de Telegram y escribir
# cualquier mensaje en el grupo (ej. "/start@NombreDelBot"). El script busca solo el chat_id del
# grupo con getUpdates. El token se escribe acá sin eco: no pasa por el chat ni por el repo.
# Enter deja el valor que ya tenía. Ver apps/facturacion_externa/alertas.py.
set -euo pipefail

PROD=/opt/cobranza-cessa-prod
ENV="$PROD/.env"
[ "$(id -u)" = 0 ] || { echo "Correr con sudo."; exit 1; }
[ -f "$ENV" ] || { echo "No existe $ENV"; exit 1; }

actual() { grep -E "^$1=" "$ENV" | head -1 | cut -d= -f2- | tr -d '\r'; }

previo=$(actual ALERTAS_TELEGRAM_BOT_TOKEN)
read -r -s -p "Token del bot de @BotFather [$( [ -n "$previo" ] && echo 'cargado' || echo 'vacío' )]: " TOKEN; echo
TOKEN=${TOKEN:-$previo}
[ -n "$TOKEN" ] || { echo "Sin token no hay alertas por Telegram."; exit 1; }

echo "== Bot"
python3 - "$TOKEN" <<'PY' || exit 1
import json, sys, urllib.request
t = sys.argv[1]
try:
    me = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getMe", timeout=15))
except Exception as e:
    sys.exit(f"Token inválido o sin salida a api.telegram.org: {e}")
print(f"OK: @{me['result']['username']}")
PY

echo "== Chats donde está el bot (getUpdates)"
CHATS=$(python3 - "$TOKEN" <<'PY'
import json, sys, urllib.request
t = sys.argv[1]
try:
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{t}/getUpdates?allowed_updates=%5B%5D", timeout=15))
except urllib.error.HTTPError as e:
    body = e.read().decode()
    sys.exit("getUpdates falló (" + body[:200] + "). Si el bot tiene webhook (ej. es el de otro sistema), usar un bot nuevo de @BotFather.")
chats, migrados = {}, {}
for u in r.get("result", []):
    for k in ("message", "my_chat_member", "edited_message", "channel_post"):
        m = u.get(k)
        if not m:
            continue
        c = m["chat"]
        if m.get("migrate_to_chat_id"):
            migrados[c["id"]] = m["migrate_to_chat_id"]
        if c["type"] in ("group", "supergroup"):
            chats[c["id"]] = c.get("title", "")
for viejo, nuevo in migrados.items():
    titulo = chats.pop(viejo, "")
    chats.setdefault(nuevo, titulo)
for cid, titulo in chats.items():
    print(f"{cid}\t{titulo}")
PY
)
if [ -n "$CHATS" ]; then
  echo "$CHATS" | nl -w2 -s') '
else
  echo "(ninguno: agregar el bot al grupo y escribir un mensaje ahí, ej. /start@NombreDelBot; después volver a correr)"
fi

previo=$(actual ALERTAS_TELEGRAM_CHAT_IDS)
read -r -p "chat_id del grupo (el número negativo de arriba; varios separados por coma) [${previo:-vacío}]: " CHAT_IDS
CHAT_IDS=${CHAT_IDS:-$previo}
[ -n "$CHAT_IDS" ] || { echo "Falta el chat_id."; exit 1; }

previo=$(actual ALERTAS_EMAIL_DESTINOS)
read -r -p "Correos que también reciben (opcional, separados por coma) [${previo:-vacío}]: " EMAILS
EMAILS=${EMAILS:-$previo}

previo=$(actual PANEL_URL)
read -r -p "URL del panel para el enlace 'Ver' (opcional) [${previo:-vacío}]: " PANEL
PANEL=${PANEL:-$previo}

export NUEVO_ALERTAS_TELEGRAM_BOT_TOKEN="$TOKEN" NUEVO_ALERTAS_TELEGRAM_CHAT_IDS="$CHAT_IDS" \
  NUEVO_ALERTAS_EMAIL_DESTINOS="$EMAILS" NUEVO_PANEL_URL="$PANEL"

cp -p "$ENV" "$ENV.antes-alertas-$(date +%Y%m%d-%H%M%S)"
python3 - "$ENV" <<'PY'
import os, sys
ruta = sys.argv[1]
nuevos = {k[6:]: v for k, v in os.environ.items() if k.startswith("NUEVO_")}
lineas, vistos = [], set()
for linea in open(ruta, encoding="utf-8").read().splitlines():
    clave = linea.split("=", 1)[0]
    if clave in nuevos and "=" in linea and not linea.lstrip().startswith("#"):
        linea = f"{clave}={nuevos[clave]}"
        vistos.add(clave)
    lineas.append(linea)
lineas += [f"{k}={v}" for k, v in nuevos.items() if k not in vistos]
open(ruta, "w", encoding="utf-8").write("\n".join(lineas) + "\n")
PY
chown cobranza:cobranza "$ENV"; chmod 600 "$ENV"
echo "Guardado ($ENV; respaldo al lado, .antes-alertas-*)."

echo "== Reinicio de servicios prod (gunicorn, worker, beat)"
for s in gunicorn celery-worker celery-beat; do
  systemctl restart "cobranza-cessa-prod-$s" && echo "  $s: $(systemctl is-active cobranza-cessa-prod-$s)"
done

echo "== Mensaje de prueba"
cd "$PROD"
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py shell -c \
  "from services.alertas import notificar; n = notificar('Alertas de Cobranza CESSA activadas', 'Este grupo va a recibir los avisos de pagos web por QR sin factura (prod).'); print('Enviados:', n)"
echo "Si dice 'Enviados: 0': revisar el chat_id y que el bot siga en el grupo. Para volver atrás: copiar el respaldo .antes-alertas-* sobre .env y reiniciar."
