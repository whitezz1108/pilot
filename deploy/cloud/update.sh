#!/usr/bin/env bash
# Update an existing deployment without copying local results over cloud data.
set -euo pipefail
umask 077
ARCHIVE="${1:?Usage: bash update.sh ARCHIVE MANIFEST}"
MANIFEST="${2:?Usage: bash update.sh ARCHIVE MANIFEST}"
APP="$HOME/pilot01"
ORIGINAL="$HOME/pilot01-runtime/Caddyfile.before-update"
MAINTENANCE="$HOME/pilot01-runtime/Caddyfile.maintenance"
RESTORE_CONFIG="$ORIGINAL"
sudo -n cat /etc/pilot01/Caddyfile > "$ORIGINAL"
/usr/bin/python3 - "$ORIGINAL" "$MAINTENANCE" <<'PY'
from pathlib import Path
import sys
source=Path(sys.argv[1]).read_text()
needle='    route {\n'
if source.count(needle) != 1:
    raise SystemExit('Gateway layout differs; refusing an unguarded update')
source=source.replace(needle, needle+'        @upgrading path /api/start /api/resume /api/prepare\n        respond @upgrading "Service update in progress; please refresh shortly" 503\n', 1)
Path(sys.argv[2]).write_text(source)
PY
restore_gateway() {
    sudo -n install -o root -g pilot01-caddy -m 640 "$RESTORE_CONFIG" /etc/pilot01/Caddyfile
    sudo -n systemctl reload pilot01-caddy
}
trap restore_gateway EXIT
sudo -n install -o root -g pilot01-caddy -m 640 "$MAINTENANCE" /etc/pilot01/Caddyfile
sudo -n systemctl reload pilot01-caddy
curl --fail --silent http://127.0.0.1:8766/api/state | "$APP/.venv/bin/python" -c 'import json,sys; state=json.load(sys.stdin); sys.exit("An experiment is active; update postponed") if state.get("busy") else print("Console idle; no active experiment will be interrupted")'
"$APP/.venv/bin/python" "$APP/deploy/cloud/backup.py" --destination "$HOME/pilot01-backups"
/usr/bin/python3 - "$ARCHIVE" "$MANIFEST" <<'PY'
import hashlib,json,sys,tarfile
from pathlib import Path
archive=Path(sys.argv[1]); manifest=json.loads(Path(sys.argv[2]).read_text())
if manifest.get('includes_history'):
    raise SystemExit('Cloud updates must exclude local history')
if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['archive_sha256']:
    raise SystemExit('Archive integrity failure')
with tarfile.open(archive,'r:gz') as handle:
    for member in handle:
        if not member.isfile() or not member.name.startswith('pilot01/') or '..' in Path(member.name).parts:
            raise SystemExit('Invalid archive member')
        relative=member.name.removeprefix('pilot01/')
        if relative.startswith('outputs/') or relative == 'config/api.local.env':
            raise SystemExit('History and credentials must not be replaced')
        if relative not in manifest['files'] or hashlib.sha256(handle.extractfile(member).read()).hexdigest()!=manifest['files'][relative]:
            raise SystemExit('Archive member integrity failure')
print('Update archive verified; cloud history and credentials excluded')
PY
sudo -n systemctl stop pilot01-console
/usr/bin/python3 - "$ARCHIVE" "$MANIFEST" <<'PY'
import hashlib,json,sys,tarfile
from pathlib import Path
with tarfile.open(sys.argv[1],'r:gz') as handle:
    handle.extractall(Path.home())
manifest=json.loads(Path(sys.argv[2]).read_text())
for relative,digest in manifest['files'].items():
    if hashlib.sha256((Path.home()/'pilot01'/relative).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Installed file integrity failure')
print('Installed files match manifest')
PY
sudo -n systemctl start pilot01-console
for attempt in $(seq 1 30); do
    if curl --fail --silent http://127.0.0.1:8766/api/health >/dev/null; then break; fi
    sleep 1
done
curl --fail --silent http://127.0.0.1:8766/api/health
# Keep existing TLS/auth settings, and bring the upload envelope in line with
# the app's per-format limits. Do this only after a successful app restart.
UPDATED_GATEWAY="$HOME/pilot01-runtime/Caddyfile.after-update"
/usr/bin/python3 - "$ORIGINAL" "$UPDATED_GATEWAY" <<'PY'
from pathlib import Path
import sys
source=Path(sys.argv[1]).read_text()
source=source.replace('max_size 64MiB', 'max_size 512MiB')
Path(sys.argv[2]).write_text(source)
PY
sudo -n /usr/local/bin/pilot01-caddy adapt --config "$UPDATED_GATEWAY" --adapter caddyfile >/dev/null
RESTORE_CONFIG="$UPDATED_GATEWAY"
printf '\nUpdate complete; gateway will reopen after this script exits.\n'
