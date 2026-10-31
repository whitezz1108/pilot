"""Render the public gateway and services without changing experiment code."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
from pathlib import Path


def gateway(address: str, users_path: str = "/etc/pilot01/users.caddy") -> str:
    address = str(ipaddress.IPv4Address(address))
    return f"""{{
    admin localhost:2019
    default_sni {address}
}}

https://{address} {{
    tls {{
        issuer acme {{
            profile shortlived
        }}
    }}
    @cross_origin {{
        method POST
        header Origin *
        not header Origin https://{address}
    }}
    route {{
        respond @cross_origin "Cross-origin request rejected" 403
        basic_auth {{
            import {users_path}
        }}
        request_body {{
            max_size 512MiB
        }}
        reverse_proxy 127.0.0.1:8766 {{
            header_up Host 127.0.0.1:8766
            header_up Origin http://127.0.0.1:8766
            header_up -Authorization
            header_up X-Pilot01-User {{http.auth.user.id}}
        }}
    }}
}}
"""


def app_service(root: Path, user: str) -> str:
    # Fixed service paths must not contain systemd expansion or shell syntax.
    if not root.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", root.as_posix()):
        raise ValueError("Use an absolute installation path without spaces")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Invalid service username")
    return f"""[Unit]
Description=Pilot01 experiment console
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={root}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH={root}/src
ExecStart={root}/.venv/bin/python -m pilot01.experiment.console --port 8766
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
"""


CADDY_SERVICE = """[Unit]
Description=Pilot01 HTTPS and authentication gateway
After=network-online.target pilot01-console.service
Wants=network-online.target

[Service]
Type=simple
User=pilot01-caddy
Group=pilot01-caddy
Environment=XDG_DATA_HOME=/var/lib/pilot01-caddy
Environment=XDG_CONFIG_HOME=/var/lib/pilot01-caddy
ExecStart=/usr/local/bin/pilot01-caddy run --config /etc/pilot01/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/pilot01-caddy reload --config /etc/pilot01/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--access", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--caddy", default="/usr/local/bin/pilot01-caddy")
    args = parser.parse_args()
    config = gateway(args.ip)
    service = app_service(args.root, args.user)
    accounts = json.loads(args.access.read_text(encoding="utf-8"))
    if not accounts:
        raise ValueError("At least one account is required")
    lines = []
    for user, password in accounts.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", user) or len(password) < 8:
            raise ValueError("Use a valid username and a password of at least 8 characters")
        # Captured output only. Passwords are never emitted to logs.
        result = subprocess.run([args.caddy, "hash-password", "--plaintext", password],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Password hashing failed")
        lines.append(f"{user} {result.stdout.strip()}\n")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in {"Caddyfile": config, "users.caddy": "".join(lines),
                          "pilot01-console.service": service,
                          "pilot01-caddy.service": CADDY_SERVICE}.items():
        path = args.output / name
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o600)


if __name__ == "__main__":
    main()
