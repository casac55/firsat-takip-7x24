# Samsung Galaxy S25 Ultra Fiyat Takip - Windows Kurulum Scripti
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Find-Python {
    $candidates = @(
        "C:\Users\$env:USERNAME\AppData\Local\Python\bin\python.exe",
        (Get-Command python -ErrorAction SilentlyContinue).Source,
        (Get-Command py -ErrorAction SilentlyContinue).Source
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and ($c -notlike "*WindowsApps*")) {
            return $c
        }
    }
    return $null
}

function Ensure-Uv {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) { return $uvCmd.Source }
    $localUv = Join-Path $HOME ".local\bin\uv.exe"
    if (Test-Path $localUv) { return $localUv }

    Write-Host "Python bulunamadı; hızlı Python ortam yöneticisi 'uv' kuruluyor..." -ForegroundColor Yellow
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host
    if (Test-Path $localUv) { return $localUv }
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) { return $uvCmd.Source }
    throw "uv kurulumu başarısız oldu."
}

$py = Find-Python
if (-not (Test-Path ".venv")) {
    if ($py) {
        Write-Host "Kullanılan Python: $py" -ForegroundColor Cyan
        & $py -m venv .venv
    } else {
        $uv = Ensure-Uv
        Write-Host "uv ile Python 3.12 sanal ortamı (.venv) oluşturuluyor..." -ForegroundColor Cyan
        & $uv venv .venv --python 3.12 --seed
    }
}

Write-Host "Bağımlılıklar kuruluyor..." -ForegroundColor Yellow
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env dosyası oluşturuldu! Lütfen TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID bilgilerinizi .env içine yazın." -ForegroundColor Green
}

Write-Host "`nKurulum tamamlandı!" -ForegroundColor Green
Write-Host "1) .env dosyasını doldurun."
Write-Host "2) Bağlantı testi: .\.venv\Scripts\python.exe main.py --test-telegram"
Write-Host "3) Başlatma: .\baslat.bat"
