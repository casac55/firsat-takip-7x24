# 🎛️ 7/24 Fırsat Takip, Telegram Kanal & Bulut Yönetim Merkezi

Asistan kotasından veya bilgisayarın açık kalmasından **tamamen bağımsız** çalışacak şekilde tasarlanmış, tüm işlemleri web arayüzünden yönetilebilen fiyat radarı ve otomatik Telegram kanal (`@firsat_takibi`) yayıncısı.

## ☁️ Oracle Cloud Always Free ile 7/24 Kesintisiz Yayın

### Yöntem 1 — Web Panelinden Tek Tıkla SSH Kurulumu (En Kolay)
1. Yerel panelden (`http://127.0.0.1:8050`) **☁️ Oracle Cloud 7/24 Yayın & Loglar** sekmesini açın.
2. Oracle Ubuntu VM **Public IP**, **SSH kullanıcı adı** (`ubuntu`), indirdiğiniz **SSH Private Key (`.key`)** yolunu ve belirlemek istediğiniz **Panel Şifresini** girip **🚀 SSH ile Oracle Sunucuya Bağlan ve 7/24 Servisi Kur** butonuna tıklayın.
3. Oracle Cloud Console → **Networking → Virtual Cloud Networks → VCN → Security Lists → Ingress Rules** altında `0.0.0.0/0` için **TCP `8050`** portuna izin verin.
4. Artık dünyanın her yerinden `http://ORACLE_SUNUCU_IP:8050` adresinden şifrenizle tüm sistemi yönetebilirsiniz!

### Yöntem 2 — PowerShell Komutuyla Gönderim
```powershell
.\deploy\oracle_deploy.ps1 -ServerIP "ORACLE_PUBLIC_IP" -SshUser "ubuntu" -SshKeyPath "C:\path\to\ssh-key.key" -PanelPassword "GucluPanelSifresi2026"
```

### Yöntem 3 — ZIP Paketi veya Docker ile Kurulum
- Panelden **📦 Oracle Yayın Paketini İndir (.zip)** butonuna basıp sunucuya yükleyin ve çalıştırın:
  ```bash
  unzip -o oracle_7x24_yayin_paketi.zip -d ~/firsat-takip
  sudo bash ~/firsat-takip/deploy/oracle_setup.sh
  ```
- Veya Docker ile:
  ```bash
  docker compose up -d --build
  ```

## 💻 Windows'ta Asistan Kotasından Bağımsız Çalıştırma
Oracle sunucuya geçene kadar kendi bilgisayarınızda Antigravity kapansa bile yayının durmaması için:
- Paneldeki **💻 Bağımsız Windows Arka Plan Görevini Kur & Başlat** butonuna tıklayın veya `gorev_ekle.ps1` betiğini çalıştırın.
