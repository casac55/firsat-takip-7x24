param(
    [Parameter(Mandatory=$true)]
    [string]$ServerIP,

    [Parameter(Mandatory=$false)]
    [string]$SshUser = "ubuntu",

    [Parameter(Mandatory=$false)]
    [string]$SshKeyPath = "",

    [Parameter(Mandatory=$false)]
    [string]$PanelPassword = ""
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectDir

$PyExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $PyExe)) {
    $PyExe = "python"
}

Write-Host "Oracle Cloud sunucusuna ($SshUser@$ServerIP) 7/24 yayin paketi gonderiliyor..." -ForegroundColor Cyan

& $PyExe -c "
import sys
from src.config import load_config
from src.oracle_deployer import deploy_to_oracle_ssh

cfg = load_config()
ok, msg = deploy_to_oracle_ssh(
    cfg,
    server_ip=r'''$ServerIP''',
    ssh_user=r'''$SshUser''',
    ssh_key_path=r'''$SshKeyPath''',
    panel_password=r'''$PanelPassword''',
)
print(msg)
sys.exit(0 if ok else 1)
"
