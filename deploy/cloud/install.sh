#!/usr/bin/env bash
# Run as the SSH login user, after uploading verified runtime archives and secrets.
set -euo pipefail
umask 077
PUBLIC_IP="${1:?Usage: bash deploy/cloud/install.sh PUBLIC_IP}"
APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME="$HOME/pilot01-runtime"
ACCESS="$HOME/.pilot01-access.json"
if [[ "$(uname -m)" != x86_64 ]]; then
    echo 'This runtime bundle requires an x86_64 Ubuntu server.' >&2
    exit 1
fi
if systemctl is-active --quiet pilot01-console; then
    echo 'An existing console is running. Finish experiments and back up before upgrading.' >&2
    exit 1
fi
test -f "$APP_ROOT/config/api.local.env"
test -f "$ACCESS"
chmod 600 "$APP_ROOT/config/api.local.env" "$ACCESS"
/usr/bin/python3 - "$RUNTIME" <<'PY'
import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); expected=json.loads((p/'verified.json').read_text())
for name in ('python','caddy'):
    actual=hashlib.sha256((p/(name+'.tar.gz')).read_bytes()).hexdigest()
    if actual != expected[name]['sha256']:
        raise SystemExit('Runtime archive integrity check failed')
for relative,digest in expected.get('wheels',{}).items():
    if hashlib.sha256((p/relative).read_bytes()).hexdigest() != digest:
        raise SystemExit('Dependency wheel integrity check failed')
print('Runtime archives: verified')
PY
if [[ ! -x "$RUNTIME/python/bin/python3" ]]; then
    tar -xzf "$RUNTIME/python.tar.gz" -C "$RUNTIME"
fi
tar -xzf "$RUNTIME/caddy.tar.gz" -C "$RUNTIME" caddy
sudo -n install -m 755 "$RUNTIME/caddy" /usr/local/bin/pilot01-caddy
"$RUNTIME/python/bin/python3" -m venv "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/python" -m pip install --no-index --find-links "$RUNTIME/wheels" -r "$APP_ROOT/deploy/cloud/requirements.lock.txt"
PYTHONPATH="$APP_ROOT/src" "$APP_ROOT/.venv/bin/python" - "$APP_ROOT" <<'PY'
import json,sys
from pathlib import Path
from pilot01.experiment.console import Console
app=Console(root=Path(sys.argv[1]))
result={'registry_cases':len(app.registry.cases),'development_cases':len(app.case_ids),
        'profile_fingerprint':app.loaded_fingerprint,'api_ready':[x['ready'] for x in app.configured_slots],
        'historical_batches':len(app.records.list()),'paid_calls_started':0}
(app.root/'deploy/cloud/server_preflight.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
if not any(x['ready'] for x in app.configured_slots):
    raise SystemExit('No ready API credential slots')
PY
"$APP_ROOT/.venv/bin/python" "$APP_ROOT/deploy/cloud/render_config.py" \
    --ip "$PUBLIC_IP" --root "$APP_ROOT" --user "$(id -un)" \
    --access "$ACCESS" --output "$RUNTIME/generated"
if ! id pilot01-caddy >/dev/null 2>&1; then
    sudo -n useradd --system --home /var/lib/pilot01-caddy --shell /usr/sbin/nologin pilot01-caddy
fi
sudo -n install -d -o root -g pilot01-caddy -m 750 /etc/pilot01
sudo -n install -d -o pilot01-caddy -g pilot01-caddy -m 700 /var/lib/pilot01-caddy
sudo -n install -o root -g pilot01-caddy -m 640 "$RUNTIME/generated/Caddyfile" /etc/pilot01/Caddyfile
sudo -n install -o root -g pilot01-caddy -m 640 "$RUNTIME/generated/users.caddy" /etc/pilot01/users.caddy
sudo -n install -m 644 "$RUNTIME/generated/pilot01-console.service" /etc/systemd/system/pilot01-console.service
sudo -n install -m 644 "$RUNTIME/generated/pilot01-caddy.service" /etc/systemd/system/pilot01-caddy.service
sudo -n /usr/local/bin/pilot01-caddy adapt --config /etc/pilot01/Caddyfile --adapter caddyfile >/dev/null
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now pilot01-console.service
for attempt in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:8766/api/health >/dev/null; then break; fi
    sleep 1
done
curl --fail --silent http://127.0.0.1:8766/api/health
sudo -n systemctl enable --now pilot01-caddy.service
printf '\nServices started. Public address: https://%s\n' "$PUBLIC_IP"
printf 'The cloud firewall must allow inbound TCP 80 and 443; keep port 8766 private.\n'
