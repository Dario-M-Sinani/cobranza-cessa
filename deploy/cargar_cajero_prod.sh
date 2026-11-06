#!/usr/bin/env bash
# Carga en el .env del gateway de PRODUCCIÓN los datos del cajero web que da el administrador del
# SIIC (alta de CESSA Web como banco en api-cobranzas .102:6002) y corre verificar_produccion.
# Ver docs/PASE_A_PRODUCCION.md §1 y §3. Se corre en la .88, como soporte con sudo:
#
#   sudo bash /opt/cobranza-cessa/deploy/cargar_cajero_prod.sh [nro_cliente_con_deuda]
#
# Las contraseñas y el client_secret se escriben acá (sin eco) y van directo al .env: no pasan
# por el chat, por el repo ni por la terminal de nadie más. Enter deja el valor que ya tenía.
# No enciende nada, no apertura caja ni crea transacciones.
set -euo pipefail

PROD=/opt/cobranza-cessa-prod
ENV="$PROD/.env"
[ "$(id -u)" = 0 ] || { echo "Correr con sudo."; exit 1; }
[ -f "$ENV" ] || { echo "No existe $ENV: primero sudo bash deploy/instalar_prod.sh"; exit 1; }

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

echo "Datos del cajero web de api-cobranzas PRODUCCIÓN (Enter = dejar el actual)"
pedir COBRANZAS_BANCO_USERNAME "Usuario cajero (ej. CAWEBQR)"
pedir COBRANZAS_BANCO_PASSWORD "Contraseña del cajero" secreto
pedir COBRANZAS_BANCO_CLIENT_ID "client_id OAuth"
pedir COBRANZAS_BANCO_CLIENT_SECRET "client_secret OAuth" secreto
pedir COBRANZAS_BANCO_AGENCIA_SIGLA "Sigla de la agencia"
pedir COBRANZAS_BANCO_DOCUMENTO_ENTE_ID "ente_id (contabilidad; en test 3 = DEPOSITO)" "" 3
pedir COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BISA "banco_id de BANCO BISA en el catálogo prod (en test 4)" "" 4
export NUEVO_COBRANZAS_BANCO_DOCUMENTO_BANCO_ID="$NUEVO_COBRANZAS_BANCO_DOCUMENTO_BANCO_ID_BISA"

cp -p "$ENV" "$ENV.antes-cajero-$(date +%Y%m%d-%H%M%S)"
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
echo "Guardado ($ENV; respaldo al lado, .antes-cajero-*)."

grep -q "api-cobranzas-prod-6002.bo-com-assec.net" /etc/hosts \
  || echo "AVISO: /etc/hosts no tiene '10.1.1.102 api-cobranzas-prod-6002.bo-com-assec.net' (opción B): sin eso cae en la API de TEST."

echo "== verificar_produccion"
cd "$PROD"
sudo -u cobranza env DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py verificar_produccion ${1:+--cliente "$1"} || true
echo
echo "Faltante típico en este punto: dominio público (ALLOWED_HOSTS / SIP_CALLBACK_*) -- ver PASE_A_PRODUCCION.md."
