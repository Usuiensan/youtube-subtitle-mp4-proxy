# Hetzner Cloud デプロイ手順

Hetzner Cloud に YouTube 字幕焼き込み MP4/HLS プロキシを置く手順です。

## 結論の構成

安く始めるなら:

```text
Provider: Hetzner Cloud
Location: Singapore, Germany, Finland, US のどれか
Server type: CAX11 or CAX21
OS: Ubuntu 24.04
Public IPv4: 有効
Firewall: 22, 80, 443のみ許可
App: FastAPI + yt-dlp + ffmpeg + Nginx
```

おすすめ:

```text
まず試す: CAX11  2 vCPU / 4 GB RAM / 40 GB SSD
余裕重視: CAX21  4 vCPU / 8 GB RAM / 80 GB SSD
```

このアプリは同時変換1本なので、まず `CAX11` で始めて、変換が遅い・メモリが苦しい場合に `CAX21` へ Rescale するのが安いです。

## 1. Hetzner Cloud Projectを作る

Hetzner Cloud Console で新しい Project を作ります。

```text
https://console.hetzner.cloud/
```

例:

```text
Project name: youtube-mp4-proxy
```

## 2. SSH鍵を登録する

左メニューの `Security > SSH Keys` で公開鍵を登録します。

Windows PowerShellで鍵がまだない場合:

```powershell
ssh-keygen -t ed25519 -C "youtube-mp4-proxy"
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

表示された公開鍵をHetznerに貼ります。

注意: Hetznerでは、サーバー作成後にConsoleからSSH鍵を追加できません。作成前に必ず登録します。

## 3. Cloud Firewallを作る

左メニューの `Firewalls > Create Firewall` で作ります。

推奨 inbound rules:

```text
TCP 22   自分のIPだけ
TCP 80   0.0.0.0/0, ::/0
TCP 443  0.0.0.0/0, ::/0
```

最初だけSSH元IPが変わりそうなら:

```text
TCP 22  0.0.0.0/0
```

で作って、ログイン確認後に自分のIPへ絞ります。

Outbound rules は空でOKです。Hetzner Cloud Firewallは、outbound rulesを空にすると外向き通信は許可されます。

## 4. Serverを作る

左メニューの `Servers > Add server` を開きます。

推奨設定:

```text
Location:
  Singapore: 日本から近い。ただしプラン在庫や価格を確認
  Falkenstein / Nuremberg / Helsinki: 安定、安価、欧州

Image:
  Ubuntu 24.04

Type:
  CAX11 から開始
  きつければ CAX21

Networking:
  IPv4: 有効
  IPv6: 有効でOK

SSH key:
  さきほど登録した鍵

Firewall:
  さきほど作ったFirewall

Backups:
  まずは無効でOK

Name:
  youtube-mp4-proxy
```

ARMの `CAX` は安くて今回向きです。ffmpegもUbuntuのパッケージでARM版が入ります。

## 5. SSH接続

サーバー作成後、表示されたIPv4に接続します。

```bash
ssh root@YOUR_SERVER_IP
```

HetznerのUbuntuイメージは最初は `root` で入るのが基本です。

## 6. 初期セットアップ

```bash
apt update
apt upgrade -y
apt install -y \
  git \
  nginx \
  ffmpeg \
  python3 \
  python3-venv \
  python3-pip \
  fonts-noto-cjk \
  ufw
```

一般ユーザーを作る場合:

```bash
adduser app
usermod -aG sudo app
rsync --archive --chown=app:app ~/.ssh /home/app
```

以降は `app` ユーザーで作業します。

```bash
su - app
```

簡単に済ませたい検証段階なら `root` のままでも動きますが、常用するなら `app` ユーザー推奨です。

## 7. アプリを配置

```bash
sudo mkdir -p /opt/youtube-mp4-proxy
sudo chown app:app /opt/youtube-mp4-proxy
cd /opt/youtube-mp4-proxy
```

GitHubに置いた場合:

```bash
git clone YOUR_REPO_URL .
```

手元から送る場合:

```bash
rsync -av --exclude .deps --exclude .venv ./ app@YOUR_SERVER_IP:/opt/youtube-mp4-proxy/
```

## 8. Python環境を作る

```bash
cd /opt/youtube-mp4-proxy
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

確認:

```bash
.venv/bin/yt-dlp --version
ffmpeg -version
```

## 9. キャッシュディレクトリを作る

```bash
sudo mkdir -p /var/cache/youtube-mp4
sudo chown app:app /var/cache/youtube-mp4
```

## 10. 環境変数を作る

```bash
sudo tee /etc/youtube-mp4-proxy.env >/dev/null <<'EOF'
CACHE_DIR=/var/cache/youtube-mp4
DEFAULT_LANG=ja
MAX_DURATION_SECONDS=1800
MAX_HEIGHT=720
CACHE_TTL_SECONDS=86400
JOB_TIMEOUT_SECONDS=7200
HLS_SEGMENT_SECONDS=6
HLS_READY_TIMEOUT_SECONDS=1800
# YouTubeがbot確認を出す場合だけ有効化
# YTDLP_COOKIES_FILE=/etc/youtube-mp4-cookies.txt
SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT_SIZE=20
SUBTITLE_MARGIN_V=34
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H99000000
# API_KEY=change-this
EOF
```

公開URLを知っている人に使われたくない場合は `API_KEY` を有効にします。

## 11. systemdサービスを作る

```bash
sudo tee /etc/systemd/system/youtube-mp4-proxy.service >/dev/null <<'EOF'
[Unit]
Description=YouTube subtitle burned MP4/HLS proxy
After=network-online.target
Wants=network-online.target

[Service]
User=app
Group=app
WorkingDirectory=/opt/youtube-mp4-proxy
EnvironmentFile=/etc/youtube-mp4-proxy.env
ExecStart=/opt/youtube-mp4-proxy/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

起動:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now youtube-mp4-proxy
sudo systemctl status youtube-mp4-proxy
```

確認:

```bash
curl http://127.0.0.1:8000/healthz
```

## 12. Nginxを設定

```bash
sudo tee /etc/nginx/sites-available/youtube-mp4-proxy >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name _;

    client_max_body_size 1m;

    location /youtube/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Range $http_range;
        proxy_set_header If-Range $http_if_range;
        proxy_read_timeout 7200s;
        proxy_send_timeout 7200s;
    }

    location /youtube-hls/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 7200s;
        proxy_send_timeout 7200s;
    }

    location /hls/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /healthz {
        proxy_pass http://127.0.0.1:8000;
    }
}
EOF
```

有効化:

```bash
sudo ln -sf /etc/nginx/sites-available/youtube-mp4-proxy /etc/nginx/sites-enabled/youtube-mp4-proxy
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

## 13. UFWを設定

Hetzner Cloud Firewallだけでも守れますが、OS側も絞っておくと安心です。

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw --force enable
sudo ufw status
```

## 14. 外から確認

```bash
curl http://YOUR_SERVER_IP/healthz
```

HLS:

```bash
curl -L http://YOUR_SERVER_IP/youtube-hls/SpQZyPBAtA0/ja
```

MP4:

```bash
curl -L -o out.mp4 http://YOUR_SERVER_IP/youtube/SpQZyPBAtA0/ja
```

## 15. ドメインとHTTPS

IP直打ちで動作確認できたら、任意でドメインを向けます。

DNS:

```text
A     your-domain.example  YOUR_SERVER_IP
AAAA  your-domain.example  YOUR_SERVER_IPV6
```

Certbot:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

HTTPS化後:

```bash
curl https://your-domain.example/healthz
```

VRChatで使うならHTTPSのほうが無難です。

## 16. 運用コマンド

ログ:

```bash
sudo journalctl -u youtube-mp4-proxy -f
```

再起動:

```bash
sudo systemctl restart youtube-mp4-proxy
```

yt-dlp更新:

```bash
cd /opt/youtube-mp4-proxy
. .venv/bin/activate
pip install -U "yt-dlp[default,curl-cffi]"
sudo systemctl restart youtube-mp4-proxy
```

キャッシュ削除:

```bash
sudo rm -rf /var/cache/youtube-mp4/*
```

## 17. Rescaleの目安

CAX11で始めて、次の症状が出たらCAX21へ上げます。

```text
変換が遅すぎる
ffmpeg中にメモリ不足になる
HLS生成中にCPUが張り付きすぎる
複数動画を短時間に変換したい
```

このアプリは同時変換1本なので、CAX11でも実用テストはできます。快適さを取るならCAX21です。

## 参考

- Hetzner: Creating a Server: https://docs.hetzner.com/cloud/servers/getting-started/creating-a-server
- Hetzner: Connecting to your Server: https://docs.hetzner.com/cloud/servers/getting-started/connecting-to-the-server
- Hetzner: Creating a Firewall: https://docs.hetzner.com/cloud/firewalls/getting-started/creating-a-firewall
- Hetzner: Firewall FAQ: https://docs.hetzner.com/cloud/firewalls/faq/
