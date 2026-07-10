# YouTube subtitle burned MP4 proxy

YouTube の動画 ID を受け取り、字幕を焼き込んだ MP4 を返す自分用プロキシです。

```text
GET /youtube/:videoId
GET /youtube/:videoId/:lang
GET /youtube/:videoId/:targetLang/:sourceLang/:translationEngine
GET /youtube-hls/:videoId
GET /youtube-hls/:videoId/:lang
GET /youtube-hls/:videoId/:targetLang/:sourceLang/:translationEngine
POST /prepare/youtube-batch/:lang?source=:playlistOrChannelUrl
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
export SUBTITLE_FONT='Noto Sans JP'
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

curl -X POST \
  -H "Authorization: Bearer $DISCORD_PREPARE_TOKEN" \
  "http://127.0.0.1:8000/prepare/youtube-batch/ja?source=https%3A%2F%2Fwww.youtube.com%2F%40ikeaireland&mode=mp4&maxItems=5000&discordUserId=123456789012345678"
```

`POST /prepare/youtube/:videoId/:lang?mode=mp4|hls` は、準備済みなら `200 {"status":"ready","url":"..."}` を返します。準備が必要なら `202` と `job_id` / `status_url` を返すので、Discord bot 側で `GET /prepare/jobs/:jobId` をポーリングし、`ready` になってから `url` を投稿します。

`GET /prepare/youtube/:videoId/:lang/subtitles?mode=mp4|hls` は、準備前に字幕候補を確認するための API です。`lang=ja` で日本語字幕がなく、翻訳可能な手動字幕がある場合は `requires_choice: true` と `candidates` を返します。翻訳エンジンは一時的に `google_cloud` のみ受け付けます。

翻訳元や翻訳方式を明示する版は、VRChat の動画プレーヤーで query string が落ちる可能性を避けるため path でも指定できます。

```text
/youtube/:videoId/:targetLang/:sourceLang/:translationEngine
/youtube-hls/:videoId/:targetLang/:sourceLang/:translationEngine
POST /prepare/youtube/:videoId/:targetLang/:sourceLang/:translationEngine
```

例: `/youtube/dQw4w9WgXcQ/ja/en/google_cloud`。従来の `/youtube/:videoId/:lang` は「細かい版を指定しない既定版」を返します。複数版を並行保持したい場合は、Discord bot の字幕選択 UI から明示版を準備すると、その明示パスの URL が返ります。

`POST /prepare/youtube-batch/:lang?source=:playlistOrChannelUrl&sourceType=auto&mode=mp4|hls&maxItems=5000` は、YouTube Data API v3 でプレイリストまたはチャンネル投稿一覧を展開し、含まれる動画をすべて準備ジョブへ投入します。返却される `batch_id` / `status_url` は `GET /prepare/batches/:batchId` でポーリングできます。`source` はプレイリスト URL/ID、`@handle`、`https://www.youtube.com/@handle`、`https://www.youtube.com/channel/...` に対応します。

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
/prepare url:https://www.youtube.com/@ikeaireland lang:ja mode:MP4 max_items:5000
/webui-key days:3
/reset-eta
```

`url` にプレイリスト URL やチャンネル URL を渡した場合は、YouTube Data API v3 で一覧を展開して一括準備します。`max_items` の既定値は `DISCORD_PREPARE_BATCH_MAX_ITEMS`、未設定時は 5000 件です。
`url` に動画URLまたは動画IDを複数入れた場合も、手動動画リストとして一括準備できます。区切りは改行、空白、カンマに対応します。Web UI の動画準備欄も同じ形式を受け付け、複数件の場合は `/prepare/youtube-batch` に `sourceType=videos` で送信します。

単体動画で `lang:ja` を指定し、日本語字幕が存在しない場合は、準備を始める前に翻訳元字幕と言語エンジンを選ぶ UI を表示します。翻訳エンジンは一時的に Google 翻訳のみ使用できます。翻訳元字幕は `TRANSLATION_SOURCE_LANGS` の優先順で初期選択され、未設定時は英語系字幕を優先します。

準備開始時は `予想N分N秒 / 終了予想 <t:1783619520:t>` の形式で返信します。ジョブが完了または失敗すると、コマンドを実行したユーザーにメンションして結果を投稿します。一括準備では完了時に先頭 10 件の配信 URL と残り件数を投稿します。予想時間の学習データは `/reset-eta` でリセットできます。

トップページの Video タブからも単体動画の準備を開始できます。`Prepare token` に `DISCORD_PREPARE_TOKEN` または `/webui-key` で発行した一時キーを入力して `Prepare` を押します。`Enable Notifications` を押してブラウザ通知を許可しておくと、ページを開いている間は準備完了または失敗時に通知します。削除系操作はブラウザ UI には置いていません。

`/webui-key days:N` は Web UI 一次利用者向けの一時キーを ephemeral で返します。キー形式は `YYYY-MM-DD-署名` で、先頭の日付を見ると有効期限が分かります。有効期限はその日付の終わりまで、タイムゾーンは JST です。一時キーは準備・状態確認には使えますが、削除系 API と `/reset-eta` には使えません。`WEBUI_TEMP_KEY_SECRET` を FastAPI と Discord bot の両方で同じ値にしてください。未設定時は `DISCORD_PREPARE_TOKEN` を使いますが、運用では別値を推奨します。

トップページの Monitor タブでは、CPU、メモリ、NVIDIA GPU、SSD/HDD空き容量、実行中の準備ジョブ進捗を確認できます。履歴は `SYSTEM_METRICS_FILE` に JSONL で保存され、ブラウザは `GET /monitor/system?seconds=21600` を5秒ごとに読み直してグラフを更新します。Linux では CPU/メモリを `/proc` から、GPUを `nvidia-smi` から取得します。

トップページの「準備済み」タブでは、SSD/HDDに残っている準備済み動画を一覧表示します。各行に動画タイトル、動画ID、字幕言語、翻訳元/翻訳エンジン、保存先、MP4/HLS URL、元YouTube URLを表示し、ボタンでURLをクリップボードへコピーできます。一覧取得には準備キーまたはWeb UI一時キーが必要です。

```bash
export SYSTEM_METRICS_ENABLED=1
export SYSTEM_METRICS_INTERVAL_SECONDS=5
export SYSTEM_METRICS_HISTORY_SECONDS=86400
export SYSTEM_METRICS_FILE=/var/lib/youtube-mp4-proxy/system-metrics.jsonl
```

### SSD/HDD アーカイブキャッシュ

SSD を変換作業と直近キャッシュ、HDD を古い成果物の保管先に分ける場合は、`CACHE_HOT_DIR` と `CACHE_ARCHIVE_DIR` を指定します。`CACHE_HOT_DIR` が未指定なら従来どおり `CACHE_DIR` を使います。

```bash
export CACHE_HOT_DIR=/mnt/ssd/youtube-mp4-hot
export CACHE_ARCHIVE_DIR=/mnt/hdd/youtube-mp4-archive
export CACHE_ARCHIVE_AFTER_SECONDS=604800
export CACHE_HOT_MIN_FREE_BYTES=50000000000
export CACHE_PROMOTE_ARCHIVE_ON_ACCESS=1
export YOUTUBE_PROXY_BASE_URL=https://lab.usuiensan.dev
export YOUTUBE_PROXY_INTERNAL_BASE_URL=http://127.0.0.1:8000
```

7 日以上前のエントリは削除せず HDD へ移動します。SSD の空き容量が `CACHE_HOT_MIN_FREE_BYTES` を下回る場合は、7 日未満でも古い順に HDD へ移動します。各エントリには変換済み `output.mp4` / HLS に加えて、元動画、字幕、取得内容をまとめた `source.json` を保存します。

準備 API が HDD 側のエントリを見つけた場合は SSD へ昇格コピーします。通常の MP4 配信 URL は HDD からも直接返せるため、一人で観る用途では再準備なしで再生できます。HDD 直配信は初回スピンアップやシークで待ちが出やすいため、複数人に共有する前は Discord bot から準備して SSD へ戻す運用を推奨します。

Google の API キーが必要なのは、YouTube Data API v3 を使う `/prepare/youtube-batch`、`/yamaplayer/playlist`、`/yamaplayer/channel`、`/yamaplayer/batch` です。

### 日本語字幕がない動画の翻訳

`TRANSLATION_ENABLED=1` の場合、要求言語が `ja` で日本語の手動字幕がない動画は、Discord bot の単体 `/prepare` では翻訳元字幕をユーザーが選び、日本語へ翻訳してから焼き込みます。API から `subtitleSourceLang` を指定しない場合や一括準備では、動画の原言語、英語、韓国語、中国語、`TRANSLATION_SOURCE_LANGS` の順で自動選択します。

LLM 翻訳は GTX 1050 Ti サーバー上では実行せず、RTX 3060 を搭載した別PCの OpenAI 互換 API に送ります。準備前に `REMOTE_LLM_HEALTH_URL` を確認し、応答がない、または余裕なしとして失敗する場合は、Discord bot がユーザーに Google 翻訳で進めてよいか確認します。LLM 失敗時に Google Cloud Translation API へ自動フォールバックする動作は行いません。

```bash
export TRANSLATION_ENABLED=1
export TRANSLATION_SOURCE_LANGS=en,ko,zh-Hans,zh-Hant,zh,zh-CN,zh-TW
export TRANSLATION_PROVIDER=remote_llm
export REMOTE_LLM_ENDPOINT=http://rtx3060-pc:11434/v1/chat/completions
export REMOTE_LLM_HEALTH_URL=http://rtx3060-pc:11434/v1/models
export REMOTE_LLM_MODEL=qwen2.5:3b-instruct-q4_K_M
export REMOTE_LLM_API_KEY=
export LOCAL_LLM_TIMEOUT_SECONDS=300
export LOCAL_LLM_TARGET_WINDOW_SECONDS=120
export LOCAL_LLM_TARGET_MAX_EVENTS=10
export LOCAL_LLM_CONTEXT_BEFORE_SECONDS=120
export LOCAL_LLM_CONTEXT_BEFORE_MAX_EVENTS=25
export LOCAL_LLM_CONTEXT_AFTER_SECONDS=120
export LOCAL_LLM_CONTEXT_AFTER_MAX_EVENTS=25
export LOCAL_LLM_TEMPERATURE=0
export TRANSLATION_FALLBACK_ENGINE=
export GOOGLE_APPLICATION_CREDENTIALS=/etc/youtube-mp4-google-credentials.json
export GOOGLE_CLOUD_PROJECT=your-google-cloud-project-id
```

翻訳済み字幕は `source/subtitle.ja.translated.srt`、元字幕は `source/subtitle.SOURCE.original.srt`、翻訳メタデータは `source/translation.json` に保存します。翻訳設定とモデル名はキャッシュキーへ含まれるため、モデルやwindow設定を変えた場合に古いMP4を誤再利用しません。

通常の選択肢は `remote_llm` と `google_cloud` です。`remote_llm` は RTX 3060 側が利用可能な場合だけ使い、利用不可なら Discord で Google 翻訳の確認を出します。GTX 1050 Ti サーバーでローカル LLM / NLLB / Opus MT を走らせる運用は行いません。

### NLLB-200 distilled 600M

NLLB は LLM ではなく、Transformers で動かす翻訳専用モデルです。Ollama API には送りません。`TRANSLATION_PROVIDER=nllb` にすると、設定画面や準備 API の既定エンジンとして NLLB を選びやすくなります。Google 翻訳と Ollama 翻訳はそのまま使えます。

対応する言語コードは次の通りです。

```text
ja en ko zh zh-CN zh-Hans zh-TW zh-Hant es pt fr de it ru uk pl tr ar fa hi bn id vi th
```

NLLB 用の設定例です。

```bash
export TRANSLATION_PROVIDER=nllb
export NLLB_MODEL=facebook/nllb-200-distilled-600M
export NLLB_DEVICE=auto
export NLLB_BATCH_SIZE=16
export NLLB_MAX_INPUT_TOKENS=512
export NLLB_MAX_NEW_TOKENS=128
export NLLB_NUM_BEAMS=1
export NLLB_KEEP_LOADED=1
export HF_HOME=/mnt/youtube-archive/ai-cache/huggingface
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

`NLLB_DEVICE` は `auto` / `cuda` / `cpu` を受け付けます。`auto` は CUDA が使えれば GPU、そうでなければ CPU です。`cuda` を指定したのに GPU が使えない場合は明確にエラーにします。GTX 1050 Ti 4GB では `NLLB_BATCH_SIZE=16` から始め、OOM が出るなら 8, 4, 2 と下げてください。実装側は CUDA OOM 時にバッチを半分ずつ再試行します。

GTX 1050 Ti の運用では、CUDA 12.6 対応の PyTorch を使ってください。CUDA 12.8 系はこの構成では使わない前提です。

モデルを手動で事前ダウンロードする場合は、Hugging Face キャッシュ先を固定してから `from_pretrained()` してください。

```bash
sudo -u youtubeproxy env \
  HF_HOME=/mnt/youtube-archive/ai-cache/huggingface \
  HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 \
  /opt/youtube-mp4-proxy/.venv/bin/python \
  - <<'PY'
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
model = "facebook/nllb-200-distilled-600M"
AutoTokenizer.from_pretrained(model)
AutoModelForSeq2SeqLM.from_pretrained(model)
print("cached")
PY
```

NLLB 単体の確認には、3 文以上の英日翻訳を小さなスクリプトで流し、使用モデル、使用デバイス、dtype、件数、処理時間、結果を確認します。GPU メモリは `nvidia-smi` で別途見ます。`NLLB_KEEP_LOADED=0` にすると、ジョブ終了後にモデルを解放できます。

設定の切り替えは次の通りです。

```bash
export TRANSLATION_PROVIDER=google_cloud   # Google 翻訳を既定にする
export TRANSLATION_PROVIDER=local_llm      # Ollama / OpenAI 互換 LLM を既定にする
export TRANSLATION_PROVIDER=nllb          # NLLB を既定にする
```

systemd で動かしている場合は、設定変更後に FastAPI と Discord bot を再起動します。

```bash
sudo systemctl restart youtube-mp4-proxy
sudo systemctl restart youtube-mp4-proxy-discord
```

NLLB の単体確認スクリプトは、`app/nllb_provider.py` を使って英語から日本語へ 3 文以上翻訳する小さな Python を用意し、`HF_HOME` とオフライン設定を付けて実行してください。`NLLB_MODEL`、`NLLB_DEVICE`、`NLLB_BATCH_SIZE` を変えたときは、同じコマンドで再確認できます。

#### 進め方

1. 既存コードと README を調査します。
2. 修正方針を簡潔に整理します。
3. 実装します。
4. 単体テストを追加します。
5. 既存テストと新規テストを実行します。
6. import・型・構文エラーを確認します。
7. 変更ファイル一覧を提示します。
8. 設定追加内容を提示します。
9. 実行したテストと結果を提示します。
10. 残る制約や未確認事項を明記します。

#### 禁止事項

- Google 翻訳機能を削除しない
- Ollama 翻訳機能を削除しない
- API キーや認証ファイルをコードへ直書きしない
- Hugging Face トークンをログへ出さない
- 字幕ごとにモデルをロードしない
- リクエストごとに Tokenizer を生成しない
- NLLB を Ollama モデルとして扱わない
- GPU OOM を握りつぶさない
- 既存 API 仕様を理由なく破壊しない
- 動作確認せずに完了扱いにしない

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
Environment=SUBTITLE_FONT=Noto Sans JP
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

初期値は白文字、約25%透明の黒背景、下寄せ中央です。720pで自動改行が入りにくいよう、文字サイズは控えめにしています。

```bash
export SUBTITLE_FONT='Noto Sans JP'
export SUBTITLE_FONT_SIZE=20
export SUBTITLE_MARGIN_V=34
export SUBTITLE_MARGIN_L=24
export SUBTITLE_MARGIN_R=24
export SUBTITLE_PRIMARY_COLOUR='&H00FFFFFF'
export SUBTITLE_BACK_COLOUR='&H40000000'
```

`SUBTITLE_FONT` はサーバーにインストール済みのフォント名を指定してください。候補は `Noto Sans JP`、`Noto Sans CJK JP`、`BIZ UDPGothic`、`Rounded M+ 1c` あたりです。
Windows の `start-local-server.bat` は起動時に `.env.local` を再生成します。`SUBTITLE_FONT_SIZE` などの字幕設定は、既存の `.env.local` に値があれば保持されます。コマンドラインで `-SubtitleFontSize 16` のように指定した場合は、その指定値で上書きされます。起動済みプロセスには環境変数ファイルの変更は自動反映されないため、変更後はサーバーを再起動してください。
既に作成済みのMP4は字幕サイズを変えても書き換わりません。新しい字幕設定で作り直すには、対象動画のキャッシュを削除してから再準備してください。

字幕スタイルを変えると内部キャッシュキーも変わるため、古い見た目のキャッシュとは混ざりません。
