#!/usr/bin/env bash
# Configura el correo saliente del gateway de PRODUCCIÓN (lo usan las alertas de pagos web sin
# factura, ALERTAS_EMAIL_DESTINOS), reinicia sus servicios y manda un correo de prueba. En la .88:
#
#   sudo bash /opt/cobranza-cessa-prod/deploy/configurar_correo_prod.sh
#
# Con Gmail (ej. notificacionescessa@gmail.com, la misma cuenta que usa la web): servidor
# smtp.gmail.com, puerto 587, TLS, y una "contraseña de aplicación" de 16 letras (Cuenta de
# Google → Seguridad → Verificación en 2 pasos → Contraseñas de aplicaciones), no la clave normal.
# La contraseña se escribe acá sin eco y va directo al .env. Enter deja el valor que ya tenía.
set -euo pipefail

PROD=/opt/cobranza-cessa-prod
ENV="$PROD/.env"
[ "$(id -u)" = 0 ] || { echo "Correr con sudo."; exit 1; }
[ -f "$ENV" ] || { echo "No existe $ENV"; exit 1; }

actual() { { grep -E "^$1=" "$ENV" || true; } | head -1 | cut -d= -f2- | tr -d '\r'; }
pedir() { # pedir VARIABLE "texto" [secreto] [valor por defecto si está vacío]
  local var=$1 texto=$2 secreto=${3:-} defecto=${4:-} previo valor
  previo=$(actual "$var"); [ -n "$previo" ] || previo=$defecto
  if [ -n "$secreto" ]; then
    read -r -s -p "$texto [$( [ -n "$previo" ] && echo 'cargado' || echo 'vacío' )]: " valor; echo
  else
    read -r -p "$texto [${previo:-vacío}]: " valor
  fi
  export "NUEVO_$var=${valor:-$previo}"
}

echo "Correo saliente del gateway PROD (Enter = dejar el actual)"
pedir EMAIL_HOST "Servidor SMTP" "" smtp.gmail.com
pedir EMAIL_PORT "Puerto" "" 587
pedir EMAIL_USE_TLS "TLS (true/false)" "" true
pedir EMAIL_HOST_USER "Cuenta (usuario SMTP)" "" notificacionescessa@gmail.com
pedir EMAIL_HOST_PASSWORD "Contraseña de aplicación" secreto
pedir DEFAULT_FROM_EMAIL "Remitente" "" "Cobranza CESSA <${NUEVO_EMAIL_HOST_USER}>"
pedir ALERTAS_EMAIL_DESTINOS "Destinatarios de las alertas (separados por coma)"
[ -n "$NUEVO_EMAIL_HOST_PASSWORD" ] || { echo "Falta la contraseña de aplicación."; exit 1; }
[ -n "$NUEVO_ALERTAS_EMAIL_DESTINOS" ] || { echo "Faltan los destinatarios."; exit 1; }

echo "== Prueba de conexión SMTP (sin guardar todavía)"
python3 - <<'PY' || { echo "No se guardó nada."; exit 1; }
import os, smtplib, sys
e = os.environ
host, port = e["NUEVO_EMAIL_HOST"], int(e["NUEVO_EMAIL_PORT"])
try:
    s = smtplib.SMTP(host, port, timeout=15)
    if e["NUEVO_EMAIL_USE_TLS"].lower() in ("1", "true", "yes", "si"):
        s.starttls()
    s.login(e["NUEVO_EMAIL_HOST_USER"], e["NUEVO_EMAIL_HOST_PASSWORD"])
    s.quit()
except OSError as exc:
    sys.exit(f"Sin conexión a {host}:{port} ({exc}). ¿La .88 tiene salida a ese puerto?")
except smtplib.SMTPAuthenticationError as exc:
    sys.exit(f"Usuario o contraseña rechazados ({exc.smtp_code}). Con Gmail va la contraseña de aplicación.")
except smtplib.SMTPException as exc:
    sys.exit(f"Error SMTP: {exc}")
print(f"OK: {host}:{port} aceptó el usuario.")
PY

cp -p "$ENV" "$ENV.antes-correo-$(date +%Y%m%d-%H%M%S)"
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
echo "Guardado ($ENV; respaldo al lado, .antes-correo-*)."

echo "== Reinicio de servicios prod (gunicorn, worker, beat)"
for s in gunicorn celery-worker celery-beat; do
  systemctl restart "cobranza-cessa-prod-$s" && echo "  $s: $(systemctl is-active cobranza-cessa-prod-$s)"
done

echo "== Alerta de prueba (Telegram + correo)"
cd "$PROD"
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py shell -c \
  "from services.alertas import notificar; n = notificar('Correo de alertas activado', 'Prueba del correo de alertas de pagos web sin factura (prod).'); print('Enviados:', n)"
echo "Tiene que decir Enviados: 1 + la cantidad de correos, sin 'alerta email:'. Volver atrás: copiar .antes-correo-* sobre .env y reiniciar."
