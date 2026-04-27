# Oracle Always Free VM デプロイ手順

Oracle Cloud Infrastructure の Ampere A1 Always Free VM に、この YouTube 字幕焼き込み MP4/HLS プロキシを置く手順です。

## 前提

- OS: Ubuntu 22.04 または 24.04
- Shape: `VM.Standard.A1.Flex`
- 推奨: 2-4 OCPU / 12-24 GB RAM
- 公開ポート: `80` または `443`
- アプリ内部ポート: `127.0.0.1:8000`

Oracle 公式ドキュメントでは、Always Free の Ampere A1 は月 3,000 OCPU 時間と 18,000 GB 時間まで無料、Always Free 専用アカウントでは 4 OCPU / 24 GB 相当です。

## 1. VMを作る

OCI Console で次のように作成します。

```text
Compute > Instances > Create instance
```

推奨設定:

```text
Image: Ubuntu
Shape: VM.Standard.A1.Flex
OCPU: 2-4
Memory: 12-24 GB
Boot volume: 47 GB以上
Public IPv4: 有効
SSH key: 自分の公開鍵
```

無料枠を維持したい場合は、必ず `Always Free-eligible` 表示のあるリソースを選びます。

## 2. OCI側のネットワークを開ける

VCN の Security List または Network Security Group で、最低限これを許可します。

```text
Ingress TCP 22   from 自分のIP
Ingress TCP 80   from 0.0.0.0/0
Ingress TCP 443  from 0.0.0.0/0
```

自分専用なら、`80/443` も可能なら自宅IPなどに絞ると安全です。

## 3. SSH接続

Ubuntu イメージなら通常ユーザーは `ubuntu` です。

```bash
ssh ubuntu@YOUR_PUBLIC_IP
```

## 4. OSパッケージを入れる

```bash
sudo apt update
sudo apt install -y \
  git \
  nginx \
  ffmpeg \
  python3 \
  python3-venv \
  python3-pip \
  fonts-noto-cjk
```

`yt-dlp` は apt 版が古いことがあるため、Python 仮想環境内に入れます。

## 5. アプリを配置

例では `/opt/youtube-mp4-proxy` に置きます。

```bash
sudo mkdir -p /opt/youtube-mp4-proxy
sudo chown ubuntu:ubuntu /opt/youtube-mp4-proxy
cd /opt/youtube-mp4-proxy
```

GitHub 等に置いた場合:

```bash
git clone YOUR_REPO_URL .
```

手元から転送する場合:

```bash
rsync -av --exclude .deps --exclude .venv ./ ubuntu@YOUR_PUBLIC_IP:/opt/youtube-mp4-proxy/
```

## 6. Python依存関係を入れる

```bash
cd /opt/youtube-mp4-proxy
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt yt-dlp
```

確認:

```bash
.venv/bin/yt-dlp --version
ffmpeg -version
```

## 7. キャッシュディレクトリを作る

```bash
sudo mkdir -p /var/cache/youtube-mp4
sudo chown ubuntu:ubuntu /var/cache/youtube-mp4
```

## 8. 環境変数ファイルを作る

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

公開範囲を絞れない場合は `API_KEY` を有効にしてください。

## 9. systemdサービスを作る

```bash
sudo tee /etc/systemd/system/youtube-mp4-proxy.service >/dev/null <<'EOF'
[Unit]
Description=YouTube subtitle burned MP4/HLS proxy
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Group=ubuntu
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

ローカル確認:

```bash
curl http://127.0.0.1:8000/healthz
```

## 10. Nginxを設定

```bash
sudo tee /etc/nginx/sites-available/youtube-mp4-proxy >/dev/null <<'EOF'
server {
    listen 80;
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

## 11. Ubuntuファイアウォールを開ける

`ufw` を有効化している場合:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw status
```

## 12. 動作確認

Health check:

```bash
curl http://YOUR_PUBLIC_IP/healthz
```

MP4完成待ち:

```bash
curl -L -o out.mp4 http://YOUR_PUBLIC_IP/youtube/SpQZyPBAtA0/ja
```

HLS逐次配信:

```bash
curl -L http://YOUR_PUBLIC_IP/youtube-hls/SpQZyPBAtA0/ja
```

`API_KEY` を設定した場合:

```bash
curl -H 'X-Api-Key: change-this' -L http://YOUR_PUBLIC_IP/youtube-hls/SpQZyPBAtA0/ja
```

## 13. ログを見る

```bash
sudo journalctl -u youtube-mp4-proxy -f
```

Nginx:

```bash
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

## 14. 更新手順

```bash
cd /opt/youtube-mp4-proxy
git pull
. .venv/bin/activate
pip install -r requirements.txt yt-dlp
sudo systemctl restart youtube-mp4-proxy
```

`yt-dlp` だけ更新したい場合:

```bash
cd /opt/youtube-mp4-proxy
. .venv/bin/activate
pip install -U yt-dlp
sudo systemctl restart youtube-mp4-proxy
```

## 15. よくある詰まり

### 502になる

ログを見ます。

```bash
sudo journalctl -u youtube-mp4-proxy -n 100 --no-pager
```

`yt-dlp` がYouTube側変更に追従できていない場合は更新します。

```bash
cd /opt/youtube-mp4-proxy
. .venv/bin/activate
pip install -U yt-dlp
sudo systemctl restart youtube-mp4-proxy
```

### 字幕がない

指定言語の字幕がない動画は `422` になります。別言語を指定してください。

```text
/youtube-hls/VIDEO_ID/en
/youtube/VIDEO_ID/en
```

### HLSが途中までしか出ない

変換中にサービスを再起動した可能性があります。該当キャッシュを消して再変換します。

```bash
sudo rm -rf /var/cache/youtube-mp4/VIDEOID_ja_*
```

### ディスクが心配

キャッシュTTLは初期値24時間です。すぐ消したい場合:

```bash
sudo rm -rf /var/cache/youtube-mp4/*
```

## 参考

- Oracle Always Free Resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Oracle Ampere A1 pricing: https://www.oracle.com/cloud/compute/arm/pricing/
