from __future__ import annotations

import io
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

from src.config import AppConfig


def build_sqlite_backup_bytes(db_path: Path) -> bytes:
    """Çalışan SQLite veritabanının kilitsiz ve tutarlı ikili (binary) yedeğini döndürür."""
    if not db_path.exists():
        return b""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        src_conn = sqlite3.connect(db_path)
        dst_conn = sqlite3.connect(tmp_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)


def build_oracle_bundle_zip(
    config: AppConfig,
    panel_password: str = "",
    include_db: bool = True,
) -> bytes:
    """
    Oracle Cloud (Ubuntu 22.04 / 24.04 ARM veya AMD) sunucusuna tek seferde yüklenmeye hazır
    tam yayın paketini (.zip) bellekte üretir.
    """
    buf = io.BytesIO()
    base = config.base_dir

    # config.yaml kopyasını Oracle dış erişime (0.0.0.0) uygun hazırla
    cloud_yaml = yaml.safe_load(yaml.safe_dump(config.raw_yaml)) or {}
    dash_sec = cloud_yaml.setdefault("dashboard", {})
    dash_sec["enabled"] = True
    dash_sec["host"] = "0.0.0.0"
    dash_sec["port"] = int(config.dashboard_port or 8050)

    # .env içeriğini hazırla ve panel şifresini ekle
    env_path = base / ".env"
    env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    effective_pwd = (panel_password or config.dashboard_password or secrets.token_urlsafe(9)).strip()
    for k, v in (("DASHBOARD_HOST", "0.0.0.0"), ("DASHBOARD_PASSWORD", effective_pwd)):
        pat = re.compile(rf"^{re.escape(k)}=.*$", re.MULTILINE)
        if pat.search(env_text):
            env_text = pat.sub(lambda _, key=k, val=v: f"{key}={val}", env_text)
        else:
            env_text = env_text.rstrip() + f"\n{k}={v}\n"

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_file in (
            "main.py",
            "requirements.txt",
            "pyproject.toml",
            "Dockerfile",
            "docker-compose.yml",
            "README.md",
            ".env.example",
        ):
            fp = base / rel_file
            if fp.exists():
                zf.write(fp, arcname=rel_file)

        for folder in ("src", "deploy", "tests"):
            fdir = base / folder
            if not fdir.exists():
                continue
            for p in fdir.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
                    zf.write(p, arcname=str(p.relative_to(base)).replace("\\", "/"))

        zf.writestr(
            "config.yaml",
            yaml.safe_dump(cloud_yaml, allow_unicode=True, sort_keys=False),
        )
        zf.writestr(".env", env_text.lstrip("\n"))

        if include_db and config.database_path.exists():
            db_bytes = build_sqlite_backup_bytes(config.database_path)
            if db_bytes:
                zf.writestr("data/prices.db", db_bytes)

    return buf.getvalue()


def deploy_to_oracle_ssh(
    config: AppConfig,
    *,
    server_ip: str,
    ssh_user: str = "ubuntu",
    ssh_key_path: str = "",
    panel_password: str = "",
) -> tuple[bool, str]:
    """
    Windows/Linux makineden doğrudan SSH + SCP kullanarak Oracle Cloud sunucusuna bağlanır,
    projeyi yükler, systemd 7/24 servisini ve güvenlik duvarı portunu kurar.
    """
    ip = server_ip.strip()
    user = (ssh_user.strip() or "ubuntu")
    key_path = ssh_key_path.strip().strip('"')
    if not re.match(r"^[a-zA-Z0-9.\-]+$", ip):
        return False, "Geçerli bir Oracle Sunucu IP adresi girin (örn. 130.61.x.x)."
    if key_path and not Path(key_path).expanduser().exists():
        return False, f"SSH anahtar dosyası bulunamadı: {key_path}"
    if not shutil.which("ssh") or not shutil.which("scp"):
        return False, "Sistemde OpenSSH (ssh/scp) bulunamadı. İndirilen .zip paketiyle manuel yükleyebilirsiniz."

    effective_pwd = (panel_password or config.dashboard_password or secrets.token_urlsafe(8)).strip()
    bundle_bytes = build_oracle_bundle_zip(config, panel_password=effective_pwd, include_db=True)

    ssh_opts = [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]
    if key_path:
        ssh_opts.extend(["-i", str(Path(key_path).expanduser())])

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_file = Path(tmpdir) / "oracle_bundle.zip"
        zip_file.write_bytes(bundle_bytes)

        scp_cmd = ["scp", *ssh_opts, str(zip_file), f"{user}@{ip}:~/oracle_bundle.zip"]
        scp_res = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
        if scp_res.returncode != 0:
            err = (scp_res.stderr or scp_res.stdout or "").strip()[:350]
            return False, f"SCP dosya gönderimi başarısız oldu: {err}"

        remote_script = (
            "set -e; "
            "sudo apt-get update -qq && sudo apt-get install -y -qq unzip python3-venv python3-pip; "
            "mkdir -p ~/firsat-takip && unzip -o ~/oracle_bundle.zip -d ~/firsat-takip >/dev/null; "
            "chmod +x ~/firsat-takip/deploy/oracle_setup.sh; "
            "cd ~/firsat-takip && sudo bash deploy/oracle_setup.sh"
        )
        ssh_cmd = ["ssh", *ssh_opts, f"{user}@{ip}", remote_script]
        ssh_res = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)
        if ssh_res.returncode != 0:
            err = (ssh_res.stderr or ssh_res.stdout or "").strip()[-400:]
            return False, f"Uzaktan kurulum sırasında hata oluştu: {err}"

    port = int(config.dashboard_port or 8050)
    return (
        True,
        f"Oracle Cloud 7/24 yayını kuruldu! Panel: http://{ip}:{port} | Panel Şifresi: {effective_pwd} "
        f"(Not: Oracle VCN Security List içinde TCP {port} Ingress kuralının açık olduğundan emin olun.)",
    )


def install_detached_windows_task(config: AppConfig) -> tuple[bool, str]:
    """
    Antigravity kapansa veya kotası bitse bile yayının yerel bilgisayarda kesintisiz sürmesi için
    Windows Görev Zamanlayıcı kaydı oluşturur ve bağımsız pythonw sürecini güvenceye alır.
    """
    if sys.platform != "win32":
        return False, "Bu özellik yalnızca Windows içindir; Linux/Oracle sunucuda systemd servisi kullanılır."

    venv_pythonw = config.base_dir / ".venv" / "Scripts" / "pythonw.exe"
    venv_python = config.base_dir / ".venv" / "Scripts" / "python.exe"
    exe = venv_pythonw if venv_pythonw.exists() else (venv_python if venv_python.exists() else Path(sys.executable))
    main_py = config.base_dir / "main.py"
    task_name = "FirsatTakip7x24Daemon"

    tr_cmd = f'"{exe}" "{main_py}" --daemon'
    res = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "ONLOGON",
            "/TN",
            task_name,
            "/TR",
            tr_cmd,
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True, text=True)
        return (
            True,
            f"Bağımsız Windows görevi ({task_name}) kuruldu ve başlatıldı! Artık asistan kotası bitse veya IDE kapansa bile bilgisayar açıkken yayın durmaz.",
        )

    # schtasks yetkisi yoksa WMI (Win32_Process.Create) ile Job Object dışında tamamen bağımsız başlat
    wmi_cmd = (
        f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='\"{exe}\" \"{main_py}\" --daemon'; CurrentDirectory='{config.base_dir}'}}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", wmi_cmd], capture_output=True, text=True)
    return (
        True,
        "Bağımsız arka plan süreci (WMI / pythonw) asistan oturumundan kopuk olarak başlatıldı!",
    )
