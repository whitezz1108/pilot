"""Interactive login update, run by the server owner in an SSH terminal."""
from __future__ import annotations

import getpass
import json
import re
import subprocess
import tempfile
from pathlib import Path


def main():
    access = Path.home() / '.pilot01-access.json'
    accounts = json.loads(access.read_text(encoding='utf-8'))
    print('Existing accounts: ' + ', '.join(accounts))
    previous = input('Account to change [researcher]: ').strip() or 'researcher'
    if previous not in accounts:
        raise SystemExit('Account does not exist; no change made.')
    username = input(f'New username [{previous}]: ').strip() or previous
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,31}', username):
        raise SystemExit('Use 2-32 lowercase letters, numbers, underscore or hyphen; start with a letter.')
    if username != previous and username in accounts:
        raise SystemExit('New username already exists; no change made.')
    password = getpass.getpass('New password (hidden, 8+ characters): ')
    if len(password) < 8 or any(c in password for c in '\r\n\x00'):
        raise SystemExit('Use at least 8 characters with no newline; no change made.')
    if password != getpass.getpass('Confirm new password: '):
        raise SystemExit('Passwords differ; no change made.')
    updated = dict(accounts)
    del updated[previous]
    updated[username] = password
    caddy = '/usr/local/bin/pilot01-caddy'
    hashes = []
    for name, secret in updated.items():
        result = subprocess.run([caddy, 'hash-password', '--plaintext', secret], capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('Password hashing failed; no change made.')
        hashes.append(f'{name} {result.stdout.strip()}\n')
    original = subprocess.run(['sudo', '-n', 'cat', '/etc/pilot01/users.caddy'], capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory(prefix='pilot01-login-') as temporary:
        candidate, rollback = Path(temporary)/'users.caddy', Path(temporary)/'previous.caddy'
        candidate.write_text(''.join(hashes), encoding='utf-8')
        rollback.write_bytes(original)
        candidate.chmod(0o600)
        rollback.chmod(0o600)
        def install(source):
            subprocess.run(['sudo', '-n', 'install', '-o', 'root', '-g', 'pilot01-caddy', '-m', '640', str(source), '/etc/pilot01/users.caddy'], check=True, capture_output=True)
        try:
            install(candidate)
            subprocess.run(['sudo', '-n', caddy, 'adapt', '--config', '/etc/pilot01/Caddyfile', '--adapter', 'caddyfile'], check=True, capture_output=True)
            subprocess.run(['sudo', '-n', 'systemctl', 'reload', 'pilot01-caddy'], check=True, capture_output=True)
            temp = access.with_suffix('.json.tmp')
            with temp.open('w', encoding='utf-8') as handle:
                temp.chmod(0o600)
                json.dump(updated, handle, indent=2)
            temp.replace(access)
        except Exception:
            install(rollback)
            subprocess.run(['sudo', '-n', 'systemctl', 'reload', 'pilot01-caddy'], check=True, capture_output=True)
            raise SystemExit('Update failed; original login restored.') from None
    print(f'Login updated for {username}. Other accounts and running experiments are unchanged.')
    print('Use a private browser window if the browser keeps sending the previous login.')


if __name__ == '__main__':
    main()
