# Ubuntu 26.04 LTS + GTX 1050 Ti 運用手順

Ubuntu 26.04 LTS の x86_64 サーバーで、GTX 1050 Ti の NVENC を使って YouTube 字幕焼き込み MP4/HLS プロキシと Discord bot を動かす手順です。

この構成では FastAPI サーバーと Discord bot を別プロセスで起動します。配信 URL を直接叩いても変換は始まりません。MP4 は SSD 側にあれば SSD から返し、HDD アーカイブにだけある場合も昇格せずそのまま返します。Discord bot が準備 API を呼んだときだけ、変換または HDD から SSD への昇格を行います。

## 前提

```text
OS: Ubuntu 26.04 LTS
Kernel: 7.0.0-27-generic x86_64
GPU: NVIDIA GeForce GTX 1050 Ti
App: /opt/youtube-mp4-proxy
User: app
API: http://127.0.0.1:8000
SSD hot cache: /mnt/ssd/youtube-mp4-hot
HDD archive: /mnt/hdd/youtube-mp4-archive
```

GTX 1050 Ti は NVIDIA の対応表上、Pascal 世代の NVENC を持ち、H.264 と HEVC のエンコードに対応しています。AV1 エンコードは非対応です。このアプリでは `h264_nvenc` を使います。

## 1. OS と基本パッケージ

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y \
  git \
  curl \
  nginx \
  ffmpeg \
  python3 \
  python3-venv \
  python3-pip \
  fonts-noto-cjk \
  ufw \
  ubuntu-drivers-common
```

`ffmpeg` に NVENC が入っているか確認します。

```bash
ffmpeg -hide_banner -encoders | grep nvenc
```

`h264_nvenc` が出れば、このアプリのGPU設定で使えます。

## 2. NVIDIA ドライバー

Ubuntuでは `ubuntu-drivers` でハードウェアに合うドライバーを入れるのが基本です。サーバー用途ではまず `--gpgpu` の候補を確認します。

```bash
sudo ubuntu-drivers list --gpgpu
```

自動選択でよければ:

```bash
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

再起動後に確認します。

```bash
nvidia-smi
cat /proc/driver/nvidia/version
```

`nvidia-smi` で GTX 1050 Ti が見えない場合は、Secure Boot、PCIe認識、ドライバー系列、カーネルヘッダー不足を確認してください。

## 3. ユーザーと配置先

```bash
sudo adduser app
sudo usermod -aG sudo app

sudo mkdir -p /opt/youtube-mp4-proxy
sudo chown app:app /opt/youtube-mp4-proxy
```

リポジトリを配置します。

```bash
sudo -iu app
cd /opt/youtube-mp4-proxy
git clone YOUR_REPO_URL .
```

Python環境を作ります。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

翻訳エンジンを複数選べるようにする場合は、`TRANSLATION_DEFAULT_PROFILE` と `LOCAL_LLM_MODEL_*` を `.env.local` に入れて、RTX 3060 側で公開しているモデル名に合わせてください。

確認:

```bash
.venv/bin/python -m py_compile app/main.py bot/main.py
.venv/bin/yt-dlp --version
ffmpeg -hide_banner -encoders | grep h264_nvenc
```

## 4. SSD/HDD キャッシュ

SSDを変換作業と直近配信用、HDDを古い成果物のアーカイブにします。

```bash
sudo mkdir -p /mnt/ssd/youtube-mp4-hot
sudo mkdir -p /mnt/hdd/youtube-mp4-archive
sudo chown -R app:app /mnt/ssd/youtube-mp4-hot /mnt/hdd/youtube-mp4-archive
```

HDDがexFATの場合も読み書きはできます。ただし常時運用では、突然の電源断やUSB切断に弱い点に注意してください。Linux専用の固定ディスクとして使えるなら ext4、Windowsとの共有を優先するなら exFAT のまま、という判断で構いません。

## 5. 環境変数

Discord bot token と prepare token は実値に置き換えてください。`DISCORD_PREPARE_TOKEN` はFastAPIとbotで同じ値にします。

```bash
sudo tee /etc/youtube-mp4-proxy.env >/dev/null <<'EOF'
CACHE_HOT_DIR=/mnt/ssd/youtube-mp4-hot
CACHE_ARCHIVE_DIR=/mnt/hdd/youtube-mp4-archive
CACHE_ARCHIVE_AFTER_SECONDS=604800
CACHE_HOT_MIN_FREE_BYTES=50000000000
CACHE_PROMOTE_ARCHIVE_ON_ACCESS=1

DEFAULT_LANG=ja
MAX_DURATION_SECONDS=3600
MAX_HEIGHT=720
CACHE_TTL_SECONDS=604800
JOB_TIMEOUT_SECONDS=7200
HLS_SEGMENT_SECONDS=6
HLS_READY_TIMEOUT_SECONDS=1800

FFMPEG_VIDEO_ENCODER=h264_nvenc
FFMPEG_VIDEO_PRESET=fast
FFMPEG_VIDEO_CQ=23

YTDLP_EXTRA_ARGS=--js-runtimes deno --remote-components ejs:npm
# YTDLP_COOKIES_FILE=/etc/youtube-mp4-cookies.txt

SUBTITLE_FONT=Noto Sans CJK JP
SUBTITLE_FONT_SIZE=20
SUBTITLE_MARGIN_V=34
SUBTITLE_MARGIN_L=24
SUBTITLE_MARGIN_R=24
SUBTITLE_PRIMARY_COLOUR=&H00FFFFFF
SUBTITLE_BACK_COLOUR=&H40000000

DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN
DISCORD_PREPARE_TOKEN=CHANGE_THIS_RANDOM_TOKEN
WEBUI_TEMP_KEY_SECRET=CHANGE_THIS_RANDOM_WEBUI_TEMP_KEY_SECRET
YOUTUBE_PROXY_BASE_URL=https://lab.usuiensan.dev
YOUTUBE_PROXY_INTERNAL_BASE_URL=http://127.0.0.1:8000
DISCORD_PREPARE_POLL_SECONDS=10
DISCORD_PREPARE_POLL_TIMEOUT_SECONDS=7200
DISCORD_PREPARE_BATCH_MAX_ITEMS=5000

TRANSLATION_ENABLED=1
TRANSLATION_SOURCE_LANGS=en,ko,zh-Hans,zh-Hant,zh,zh-CN,zh-TW
TRANSLATION_FALLBACK_ENGINE=google_cloud
LOCAL_LLM_ENGINE=openai_compatible
LOCAL_LLM_ENDPOINT=http://192.168.68.115:11434/v1/chat/completions
LOCAL_LLM_MODEL=qwen3:4b-instruct
LOCAL_LLM_TIMEOUT_SECONDS=300
LOCAL_LLM_TARGET_WINDOW_SECONDS=120
LOCAL_LLM_TARGET_MAX_EVENTS=10
LOCAL_LLM_CONTEXT_BEFORE_SECONDS=120
LOCAL_LLM_CONTEXT_BEFORE_MAX_EVENTS=25
LOCAL_LLM_CONTEXT_AFTER_SECONDS=120
LOCAL_LLM_CONTEXT_AFTER_MAX_EVENTS=25
LOCAL_LLM_TEMPERATURE=0
# GOOGLE_APPLICATION_CREDENTIALS=/etc/youtube-mp4-google-credentials.json
# GOOGLE_CLOUD_PROJECT=your-google-cloud-project-id

# Required for playlist/channel batch prepare and YamaPlayer JSON export
# YOUTUBE_DATA_API_KEY=AIza...
EOF

sudo chmod 600 /etc/youtube-mp4-proxy.env
sudo chown app:app /etc/youtube-mp4-proxy.env
```

GTX 1050 TiでNVENCが使えない場合でも、アプリはドライバー/API不一致を検出すると `libx264` にフォールバックします。GPU運用を必須にしたい場合は、ログに `NVENC is unavailable` が出ていないか確認してください。

## 6. systemd サービス

FastAPIサーバー:

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
ExecStart=/opt/youtube-mp4-proxy/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Discord bot:

```bash
sudo tee /etc/systemd/system/youtube-mp4-discord-bot.service >/dev/null <<'EOF'
[Unit]
Description=YouTube MP4 proxy Discord bot
After=network-online.target youtube-mp4-proxy.service
Wants=network-online.target

[Service]
User=app
Group=app
WorkingDirectory=/opt/youtube-mp4-proxy
EnvironmentFile=/etc/youtube-mp4-proxy.env
ExecStart=/opt/youtube-mp4-proxy/.venv/bin/python -m bot.main
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
sudo systemctl enable --now youtube-mp4-discord-bot
```

確認:

```bash
systemctl status youtube-mp4-proxy --no-pager
systemctl status youtube-mp4-discord-bot --no-pager
journalctl -u youtube-mp4-proxy -n 100 --no-pager
journalctl -u youtube-mp4-discord-bot -n 100 --no-pager
curl http://127.0.0.1:8000/healthz
```

## 7. Nginx

配信URLを外部に出す場合だけ設定します。準備APIはDiscord botがローカルで叩くため、外部公開しない構成を推奨します。

```bash
sudo tee /etc/nginx/sites-available/youtube-mp4-proxy >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name YOUR_DOMAIN_OR_IP;

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

    location /prepare/ {
        return 404;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/youtube-mp4-proxy /etc/nginx/sites-enabled/youtube-mp4-proxy
sudo nginx -t
sudo systemctl reload nginx
```

外部URLをDiscordに投稿したい場合は、`YOUTUBE_PROXY_BASE_URL` を公開URLにします。botが準備APIを叩く先は `YOUTUBE_PROXY_INTERNAL_BASE_URL` のままローカルにします。

```bash
sudo sed -i 's#^YOUTUBE_PROXY_BASE_URL=.*#YOUTUBE_PROXY_BASE_URL=https://YOUR_DOMAIN#' /etc/youtube-mp4-proxy.env
sudo grep -q '^YOUTUBE_PROXY_INTERNAL_BASE_URL=' /etc/youtube-mp4-proxy.env || echo 'YOUTUBE_PROXY_INTERNAL_BASE_URL=http://127.0.0.1:8000' | sudo tee -a /etc/youtube-mp4-proxy.env
sudo systemctl restart youtube-mp4-proxy youtube-mp4-discord-bot
```

## 8. Discord bot

Discord Developer Portal で bot を作成し、サーバーへ招待します。最低限、次の権限が必要です。

- `applications.commands`
- メッセージ送信
- ユーザーへのメンションを含むメッセージ送信

botが起動すると `/prepare` スラッシュコマンドを同期します。

```text
/prepare url:https://www.youtube.com/watch?v=dQw4w9WgXcQ lang:ja mode:MP4
/prepare url:https://www.youtube.com/@ikeaireland lang:ja mode:MP4 max_items:5000
```

`url` にプレイリスト URL、プレイリスト ID、`@handle`、`https://www.youtube.com/@handle`、`https://www.youtube.com/channel/...` を渡すと、YouTube Data API v3 で一覧を展開して一括準備します。この機能には `YOUTUBE_DATA_API_KEY` が必要です。

単体動画で `lang:ja` を指定し、日本語字幕が存在しない場合は、準備開始前に Discord の選択 UI で翻訳元字幕と翻訳エンジンを選びます。既定は LLM 翻訳で、必要なら Google 翻訳を選べます。一括準備では動画ごとの UI は出さず、従来どおり自動選択します。

翻訳元や翻訳方式を明示した版は query string ではなく path に含めます。例: `https://YOUR_DOMAIN/youtube/dQw4w9WgXcQ/ja/en/google_cloud`。従来の `/youtube/VIDEO_ID/ja` は細かい版を指定しない既定版です。

準備開始時は次のような返信をします。

```text
MP4を準備しています。予想8分 / 終了予想 <t:1783619520:t>
```

完了時はコマンド実行ユーザーにメンションします。一括準備では先頭 10 件の配信 URL と残り件数を投稿します。

```text
<@123456789012345678> 準備できました: https://YOUR_DOMAIN/youtube/dQw4w9WgXcQ/ja
```

## 9. ローカルLLM翻訳

日本語字幕がない動画では、外国語の手動字幕を選んで日本語へ翻訳できます。翻訳workerはジョブごとに起動・終了するため、FastAPI本体へモデルをロードしません。翻訳が終わってworkerが終了してからNVENCを開始します。

GTX 1050 Ti 4GBでは、小型の量子化モデルをOpenAI互換HTTP APIやOllama互換エンドポイントで動かす想定です。ローカルLLMが失敗した字幕windowだけ、Google Cloud Translation APIへフォールバックします。初期実装ではGoogle翻訳を字幕イベントごとに1回呼びます。

Google fallbackを使う場合は、認証JSONをリポジトリ外へ置きます。

```bash
sudo install -o app -g app -m 600 google-credentials.json /etc/youtube-mp4-google-credentials.json
sudo systemctl restart youtube-mp4-proxy
```

## 10. 動作確認

NVENC:

```bash
ffmpeg -hide_banner -f lavfi -i testsrc2=size=1280x720:rate=30 -t 5 \
  -c:v h264_nvenc -preset fast -cq 23 -f null -
```

変換準備API:

```bash
source /etc/youtube-mp4-proxy.env
curl -X POST \
  -H "Authorization: Bearer $DISCORD_PREPARE_TOKEN" \
  "http://127.0.0.1:8000/prepare/youtube/dQw4w9WgXcQ/ja?mode=mp4&discordUserId=123456789012345678"

curl -X POST \
  -H "Authorization: Bearer $DISCORD_PREPARE_TOKEN" \
  "http://127.0.0.1:8000/prepare/youtube-batch/ja?source=https%3A%2F%2Fwww.youtube.com%2F%40ikeaireland&mode=mp4&maxItems=5000&discordUserId=123456789012345678"
```

MP4配信URLは、SSDまたはHDDアーカイブに `output.mp4` があれば `200`、Range リクエストなら `206` を返します。どちらにもなければ `404` です。HLS配信URLはSSD側に準備済みでない場合 `404` です。

```bash
curl -I http://127.0.0.1:8000/youtube/dQw4w9WgXcQ/ja
```

## 11. トラブルシュート

### `No NVENC capable devices found`

```bash
nvidia-smi
ffmpeg -hide_banner -encoders | grep nvenc
```

GPUが見えない場合はドライバー、Secure Boot、PCIe認識を確認します。

### `Driver does not support the required nvenc API version`

FFmpegが要求するNVENC APIに対してNVIDIAドライバーが古い状態です。Ubuntuの推奨ドライバーを更新してください。

```bash
sudo ubuntu-drivers list --gpgpu
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

### Discord botが起動しない

```bash
journalctl -u youtube-mp4-discord-bot -n 100 --no-pager
```

`DISCORD_BOT_TOKEN`、bot招待、slash command権限を確認してください。

### `/prepare` は動くが配信URLが外部から見えない

`YOUTUBE_PROXY_BASE_URL` が外部公開URLになっているか確認します。Nginxを使う場合は `/youtube/`、`/youtube-hls/`、`/hls/` だけ公開し、`/prepare/` は外部から404にする構成を推奨します。

## 参考

- Ubuntu NVIDIA driver installation: https://help.ubuntu.com/community/NvidiaDriversInstallation
- NVIDIA Video Encode and Decode Support Matrix: https://developer.nvidia.com/video-encode-decode-support-matrix
今後の更新は次の3行で統一できます。
```bash
cd /opt/youtube-mp4-proxy
sudo -u app git pull
sudo systemctl restart youtube-mp4-proxy
```
