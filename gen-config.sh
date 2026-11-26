#!/usr/bin/env bash
# Genera config.yaml para el puente ToDus a partir de example-config.yaml
# y del registration real que tiene Beeper.
#
# OJO: NO usar "bbctl config" para este puente. Ese comando solo rellena las
# plantillas de los puentes oficiales (descarga su binario/plantilla); para un
# puente propio como sh-todus devuelve un esqueleto genérico con
# example.localhost / simplenetworkbot / tokens de mentira, y sin las secciones
# network ni command_prefix.
set -euo pipefail
cd /opt/bridge-todus

BRIDGE="${1:-sh-todus}"
REG="$(mktemp)"
trap 'rm -f "$REG"' EXIT

bbctl --color never register -g -o "$REG" "$BRIDGE" >/dev/null

get() { grep -E "^$1:" "$REG" | head -1 | sed -E "s/^$1:[[:space:]]*//; s/^[\"']//; s/[\"']\$//"; }
APP_ID="$(get id)"
AS_TOKEN="$(get as_token)"
HS_TOKEN="$(get hs_token)"
SENDER="$(get sender_localpart)"

cp -a example-config.yaml config.yaml

python3 - "$APP_ID" "$AS_TOKEN" "$HS_TOKEN" "$SENDER" "$BRIDGE" <<'PY'
import re, sys

app_id, as_token, hs_token, sender, bridge_name = sys.argv[1:6]
path = "/opt/bridge-todus/config.yaml"
text = open(path, encoding="utf-8").read()


def one(pattern, repl):
    global text
    text, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("no se pudo parchear: " + pattern)


one(r"^    id: .*$", "    id: " + app_id)
one(r"^    as_token: .*$", "    as_token: " + as_token)
one(r"^    hs_token: .*$", "    hs_token: " + hs_token)
one(r"^        username: .*$", "        username: " + sender)
one(r"^    username_template: .*$", "    username_template: " + bridge_name + "_{{.}}")
if re.search(r'^        beeper\.com: "as_token:.*"$', text, flags=re.M):
    one(r'^        beeper\.com: "as_token:.*"$',
        '        beeper.com: "as_token:' + as_token + '"')

open(path, "w", encoding="utf-8").write(text)
print("config.yaml generado para " + sender)
PY

chmod 600 config.yaml

echo "--- comprobaciones ---"
grep -nE '^network:|command_prefix|^    address: https|^    domain:|^        username:|^    id:|^    as_token:|^    hs_token:|websocket: true' config.yaml
