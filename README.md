# YouTube subtitle burned MP4 proxy

YouTube の動画 ID を受け取り、字幕を焼き込んだ MP4 を返す自分用プロキシです。

```text
GET /youtube/:videoId
GET /youtube/:videoId/:lang
GET /youtube-hls/:videoId
GET /youtube-hls/:videoId/:lang
GET /yamaplayer/playlist?list=:playlistIdOrUrl
GET /yamaplayer/channel?channel=:channelIdOrHandleOrUrl
GET /yamaplayer/batch?sources=:newlineSeparatedSources
```

例:

```bash
curl -L -o out.mp4 http://127.0.0.1:8000/youtube/dQw4w9WgXcQ/ja
curl -L http://127.0.0.1:8000/youtube-hls/dQw4w9WgXcQ/ja
curl -L -o playlist.json "http://127.0.0.1:8000/yamaplayer/playlist?list=PLxxxxxxxx"
curl -L -o channel.json "http://127.0.0.1:8000/yamaplayer/channel?channel=@GoogleDevelopers"
curl -L -o batch.json "http://127.0.0.1:8000/yamaplayer/batch?sourceType=auto&sources=@GoogleDevelopers%0APLxxxxxxxx"
curl -L -o batch-mp4.json "http://127.0.0.1:8000/yamaplayer/batch?sourceType=auto&sources=@GoogleDevelopers%0APLxxxxxxxx&urlMode=mp4&lang=ja"
```

## モード

### MP4完成待ち

```text
/youtube/:videoId/:lang
```

字幕焼き込み済み MP4 が完成するまで待ってから返します。HLS 側で不具合が出たときの安定フォールバックです。

### HLS逐次配信

```text
/youtube-hls/:videoId/:lang
```

動画と字幕を取得後、ffmpeg が最初の HLS セグメントを生成した時点で `m3u8` を返します。MP4 の完成待ちよりレスポンス開始を早くできます。

内部的には次の URL でセグメントを配信します。

```text
/hls/:videoId_:lang_:styleId/index.m3u8
/hls/:videoId_:lang_:styleId/segment_00000.ts
```

## 制限

- 最大長: 30 分
- 最大画質: 720p
- 同時変換: 1 件
- キャッシュ TTL: 24 時間
- 同一 `videoId + lang + 字幕スタイル + エンコード設定` は変換ジョブを共有
- `Accept-Ranges: bytes` 対応
- `yt-dlp` のユーザー設定は `--ignore-config` で無視
- HLS は最初の `segment_*.ts` が生成されたら `m3u8` を返す

## セットアップ

Ubuntu ARM での最小構成:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt yt-dlp
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

必要なら環境変数で設定します。

```bash
export CACHE_DIR=/var/cache/youtube-mp4
export DEFAULT_LANG=ja
export SUBTITLE_FONT='BIZ UDGothic'
```

### Discord bot からの準備ジョブ

`/youtube/...` と `/youtube-hls/...` は配信専用です。URL を叩いただけでは変換や HDD から SSD への移動を開始しません。MP4 は SSD 側にあれば SSD から返し、HDD アーカイブにだけある場合も昇格せずそのまま返します。HLS は SSD 側に準備済みでない場合 `404` を返します。

変換または HDD から SSD への昇格は、Bearer token 付きの準備 API から開始します。

```bash
export DISCORD_PREPARE_TOKEN='change-this-token'

curl -X POST \
  -H "Authorization: Bearer $DISCORD_PREPARE_TOKEN" \
  "http://127.0.0.1:8000/prepare/youtube/dQw4w9WgXcQ/ja?mode=mp4&discordUserId=123456789012345678"

curl -H "Authorization: Bearer $DISCORD_PREPARE_TOKEN" \
  "http://127.0.0.1:8000/prepare/jobs/JOB_ID"
```

`POST /prepare/youtube/:videoId/:lang?mode=mp4|hls` は、準備済みなら `200 {"status":"ready","url":"..."}` を返します。準備が必要なら `202` と `job_id` / `status_url` を返すので、Discord bot 側で `GET /prepare/jobs/:jobId` をポーリングし、`ready` になってから `url` を投稿します。

`discordUserId` を渡すと、ジョブ状態に `mentions` と `notification.content` が含まれます。bot は `ready` または `failed` になったときに `notification.content` を投稿すれば、変換コマンドを実行したユーザーへメンションできます。同じ動画の準備ジョブが既に動いている場合、後から来た `discordUserId` も同じジョブの通知対象に追加されます。

準備中のレスポンスには、分かる範囲で `eta_seconds` と Unix 秒の `estimated_ready_at` を含めます。HDD から SSD への昇格はアーカイブサイズから概算し、新規変換は動画長を取得できた後に概算を更新します。

Discord bot は FastAPI サーバーとは別プロセスで起動します。同じ `.env.local` を読み、準備 API を HTTP 経由で呼びます。

```powershell
.\scripts\reset-local-env.ps1 `
  -DiscordBotToken "YOUR_DISCORD_BOT_TOKEN" `
  -DiscordPrepareToken "change-this-token"

.\start-local-server.bat
.\start-discord-bot.bat
```

bot はスラッシュコマンド `/prepare` を提供します。

```text
/prepare url:https://www.youtube.com/watch?v=dQw4w9WgXcQ lang:ja mode:MP4
```

準備開始時は `予想N分 / 終了予想 <t:1783619520:t>` の形式で返信します。ジョブが完了または失敗すると、コマンドを実行したユーザーにメンションして結果を投稿します。

### SSD/HDD アーカイブキャッシュ

SSD を変換作業と直近キャッシュ、HDD を古い成果物の保管先に分ける場合は、`CACHE_HOT_DIR` と `CACHE_ARCHIVE_DIR` を指定します。`CACHE_HOT_DIR` が未指定なら従来どおり `CACHE_DIR` を使います。

```bash
export CACHE_HOT_DIR=/mnt/ssd/youtube-mp4-hot
export CACHE_ARCHIVE_DIR=/mnt/hdd/youtube-mp4-archive
export CACHE_ARCHIVE_AFTER_SECONDS=604800
export CACHE_HOT_MIN_FREE_BYTES=50000000000
export CACHE_PROMOTE_ARCHIVE_ON_ACCESS=1
```

7 日以上前のエントリは削除せず HDD へ移動します。SSD の空き容量が `CACHE_HOT_MIN_FREE_BYTES` を下回る場合は、7 日未満でも古い順に HDD へ移動します。各エントリには変換済み `output.mp4` / HLS に加えて、元動画、字幕、取得内容をまとめた `source.json` を保存します。

準備 API が HDD 側のエントリを見つけた場合は SSD へ昇格コピーします。通常の MP4 配信 URL は HDD からも直接返せるため、一人で観る用途では再準備なしで再生できます。HDD 直配信は初回スピンアップやシークで待ちが出やすいため、複数人に共有する前は Discord bot から準備して SSD へ戻す運用を推奨します。

Google の API キーが必要なのは、YouTube Data API v3 を使う `/yamaplayer/playlist`、`/yamaplayer/channel`、`/yamaplayer/batch` だけです。

## YamaPlayer JSON 書き出し

YouTube Data API v3 を使って、YouTube のプレイリストまたはチャンネルの投稿一覧を YamaPlayer の JSON インポート形式で返します。環境変数 `YOUTUBE_DATA_API_KEY` が必要です。

```bash
export YOUTUBE_DATA_API_KEY=your-youtube-data-api-key
```

プレイリスト:

```bash
curl -L -o yamaplayer.json \
  "http://127.0.0.1:8000/yamaplayer/playlist?list=https%3A%2F%2Fwww.youtube.com%2Fplaylist%3Flist%3DPLxxxxxxxx&mode=0&maxItems=500"
```

チャンネル投稿一覧:

```bash
curl -L -o yamaplayer.json \
  "http://127.0.0.1:8000/yamaplayer/channel?channel=@GoogleDevelopers&mode=0&maxItems=500"
```

複数の投稿者・プレイリストを一括:

```bash
curl -L -o yamaplayer.json \
  "http://127.0.0.1:8000/yamaplayer/batch?sourceType=auto&sources=@GoogleDevelopers%0Ahttps%3A%2F%2Fwww.youtube.com%2Fplaylist%3Flist%3DPLxxxxxxxx&mode=0&maxItems=500"
```

トップページの JSON タブでも、`Channel or Playlist URLs` に 1 行 1 件で複数のチャンネル URL、`@handle`、チャンネル ID、プレイリスト URL、プレイリスト ID を貼り付けると、ひとつの JSON にまとめて書き出せます。

JSON 内の動画 URL は `urlMode` で切り替えられます。初期値の `original` は通常の YouTube URL、`mp4` はこのサーバーの字幕焼き込み MP4 URL、`hls` は HLS URL にします。`mp4` / `hls` では `lang=ja` のように字幕言語も指定できます。

```bash
curl -L -o yamaplayer-mp4.json \
  "http://127.0.0.1:8000/yamaplayer/batch?sourceType=auto&sources=@GoogleDevelopers%0APLxxxxxxxx&urlMode=mp4&lang=ja&mode=0&maxItems=500"
```

出力形式:

```json
{
  "playlists": [
    {
      "active": true,
      "name": "Playlist name",
      "youtubeListId": "PLxxxxxxxx",
      "tracks": [
        {
          "mode": 0,
          "title": "動画タイトル",
          "url": "https://www.youtube.com/watch?v=VIDEO_ID"
        }
      ]
    }
  ]
}
```

`mode` は `0` が UnityVideoPlayer、`1` が AVProVideoPlayer、`2` が ImageViewer です。チャンネル指定は `UC...` のチャンネル ID、`@handle`、`https://www.youtube.com/channel/...`、`https://www.youtube.com/@handle` に対応しています。YouTube Data API のクォータは `channels.list`、`playlists.list`、`playlistItems.list` が各 1 unit です。50 件を超える一覧はページごとに `playlistItems.list` を追加で呼びます。

### YouTube Data API キー準備

1. Google Cloud Console でプロジェクトを作成または選択します。
2. APIs & Services で `YouTube Data API v3` を有効化します。
3. APIs & Services の Credentials で `API key` を作成します。
4. 可能なら API key の制限で、利用 API を `YouTube Data API v3` に絞ります。
5. ローカル起動時に指定します。

```powershell
.\scripts\reset-local-env.ps1 -YoutubeDataApiKey "AIza..."
```

または `.env.local` に直接書きます。

```text
YOUTUBE_DATA_API_KEY=AIza...
```

## GPU エンコード

NVIDIA GTX 1050 Ti など NVENC 対応 GPU がある Windows 環境では、ローカル起動時に GPU エンコードを有効化できます。

```powershell
.\start-local-server.bat -GpuEncode
```

手動で指定する場合:

```powershell
$env:FFMPEG_VIDEO_ENCODER="h264_nvenc"
$env:FFMPEG_VIDEO_PRESET="fast"
$env:FFMPEG_VIDEO_CQ="23"
```

CPU エンコードに戻す場合は `FFMPEG_VIDEO_ENCODER=libx264` を指定します。NVENC を使うには、NVIDIA ドライバーと `h264_nvenc` 対応の FFmpeg が必要です。

`Driver does not support the required nvenc API version` が出る場合は、FFmpeg が要求する NVENC API に対して NVIDIA ドライバーが古い状態です。この場合、アプリは CPU エンコードに自動フォールバックします。GPU を使い切りたい場合は NVIDIA ドライバーを更新してください。

## Nginx 例

```nginx
server {
    listen 80;
    server_name example.com;

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
```

## systemd 例

```ini
[Unit]
Description=YouTube MP4 proxy
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/youtube-mp4-proxy
Environment=CACHE_DIR=/var/cache/youtube-mp4
Environment=DEFAULT_LANG=ja
Environment=MAX_DURATION_SECONDS=1800
Environment=MAX_HEIGHT=720
Environment=CACHE_TTL_SECONDS=86400
Environment=SUBTITLE_FONT=BIZ UDGothic
ExecStart=/opt/youtube-mp4-proxy/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 注意

字幕が存在しない動画や指定言語の字幕が取得できない動画は `422` を返します。YouTube 側の仕様変更や制限で `yt-dlp` の更新が必要になることがあります。

## 手順書

- [Oracle Always Free VM デプロイ手順](docs/oracle-always-free-deploy.md)
- [Hetzner Cloud デプロイ手順](docs/hetzner-cloud-deploy.md)
- [Ubuntu 26.04 LTS + GTX 1050 Ti 運用手順](docs/ubuntu-26.04-gtx1050ti-deploy.md)
- [字幕デザイン変更手順](docs/subtitle-style-guide.md)

## 字幕デザイン

初期値は白文字、半透明黒背景、下寄せ中央です。720pで自動改行が入りにくいよう、文字サイズは控えめにしています。

```bash
export SUBTITLE_FONT='BIZ UDGothic'
export SUBTITLE_FONT_SIZE=20
export SUBTITLE_MARGIN_V=34
export SUBTITLE_MARGIN_L=24
export SUBTITLE_MARGIN_R=24
export SUBTITLE_PRIMARY_COLOUR='&H00FFFFFF'
export SUBTITLE_BACK_COLOUR='&H99000000'
```

`SUBTITLE_FONT` はサーバーにインストール済みのフォント名を指定してください。候補は `BIZ UDGothic`、`Noto Sans CJK JP`、`Rounded M+ 1c` あたりです。

字幕スタイルを変えると内部キャッシュキーも変わるため、古い見た目のキャッシュとは混ざりません。
