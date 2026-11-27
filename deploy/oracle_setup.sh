#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="${SUDO_USER:-$(whoami)}"
SERVICE_NAME="firsat-takip"
PORT="${DASHBOARD_PORT:-8050}"

echo "=== [1/6] Saat dilimi Europe/Istanbul olarak ayarlaniyor ==="
sudo timedatectl set-timezone Europe/Istanbul || true

echo "=== [2/6] Sistem paketleri kuruluyor (Python3, venv, pip, güvenlik duvarı) ==="
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip python3-dev build-essential libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev curl unzip

echo "=== [3/6] Python sanal ortami (.venv) ve bagimliliklar kuruluyor ==="
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel -q
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "=== [4/6] config.yaml ve .env Oracle dis erisim (0.0.0.0:$PORT) icin dogrulaniyor ==="
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" - <<PY
from pathlib import Path
import secrets, yaml

app_dir = Path("$APP_DIR")
cfg_path = app_dir / "config.yaml"
data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
dash = data.setdefault("dashboard", {})
dash["enabled"] = True
dash["host"] = "0.0.0.0"
port = int(dash.get("port", 8050))
cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

env_path = app_dir / ".env"
env_txt = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
if "DASHBOARD_PASSWORD=" not in env_txt:
    pwd = secrets.token_urlsafe(9)
    env_txt = env_txt.rstrip() + f"\nDASHBOARD_PASSWORD={pwd}\n"
    env_path.write_text(env_txt.lstrip("\n"), encoding="utf-8")
    print(f"[GUVENLIK] Otomatik Panel Sifresi olusturuldu: {pwd}")
PY

echo "=== [5/6] Oracle Ubuntu iptables ve UFW uzerinde TCP $PORT portu aciliyor ==="
if command -v iptables >/dev/null 2>&1; then
    sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport "$PORT" -j ACCEPT
    if command -v netfilter-persistent >/dev/null 2>&1; then
        sudo netfilter-persistent save || true
    fi
fi
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow "$PORT/tcp" >/dev/null 2>&1 || true
fi

echo "=== [6/6] systemd 7/24 otomatik baslatma servisi ($SERVICE_NAME.service) kuruluyor ==="
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Firsat Takip Telegram Kanal & Web Yonetim Merkezi (7/24)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="TZ=Europe/Istanbul"
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py --daemon
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
sudo systemctl restart "${SERVICE_NAME}.service"

PUB_IP="$(curl -4 -s --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
echo ""
echo "=================================================================="
echo "KURULUM TAMAMLANDI! 7/24 SERVIS AKTIF:"
sudo systemctl --no-pager --full status "${SERVICE_NAME}.service" | head -n 12 || true
echo "------------------------------------------------------------------"
echo "Web Yonetim Paneli: http://${PUB_IP}:${PORT}"
echo "ONEMLI: Oracle Cloud Console -> Networking -> Virtual Cloud Networks"
echo "-> Security Lists -> Ingress Rules altinda 0.0.0.0/0 TCP ${PORT} izin verin."
echo "=================================================================="
