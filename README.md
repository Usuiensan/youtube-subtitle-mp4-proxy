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

翻訳プロンプトを調整したい場合は `TRANSLATION_PROMPT_TEMPLATE` か `TRANSLATION_PROMPT_TEMPLATE_FILE` を設定します。長文は `.txt` を `TRANSLATION_PROMPT_TEMPLATE_FILE` で読むほうが扱いやすいです。既定のリポジトリ管理版は [prompts/translation-prompt.txt](/C:/private/youtube-subtitle-mp4-proxy/prompts/translation-prompt.txt) に置いてあります。`{source_language}` `{target_language}` `{video_title}` `{channel_name}` `{topic}` `{glossary}` `{previous_subtitles}` `{current_subtitle}` `{next_subtitles}` を埋め込めます。

```bash
export TRANSLATION_PROMPT_TEMPLATE='You are a subtitle translator.
Translate from {source_language} to {target_language}.
Video title: {video_title}
Topic: {topic}
Glossary: {glossary}

Previous:
{previous_subtitles}

Current:
{current_subtitle}

Next:
{next_subtitles}

Rules:
- Output only the translation.
- Keep names, numbers, URLs, and line breaks.'
```

```bash
export TRANSLATION_PROMPT_TEMPLATE_FILE=/home/masato/youtube-subtitle-mp4-proxy/prompts/translation-prompt.txt
```

設定確認は次でできます。

```bash
python scripts/check_config.py
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

準備済みの原語 SRT と翻訳 SRT はそれぞれ以下からダウンロードできます。

```text
/prepared/:key/source.srt
/prepared/:key/translated.srt
```

翻訳元や翻訳方式を明示する版は、VRChat の動画プレーヤーで query string が落ちる可能性を避けるため path でも指定できます。

```text
/youtube/:videoId/:targetLang/:sourceLang/:translationEngine
/youtube-hls/:videoId/:targetLang/:sourceLang/:translationEngine
POST /prepare/youtube/:videoId/:targetLang/:sourceLang/:translationEngine
```

例: `/youtube/dQw4w9WgXcQ/ja/en/google_cloud`。従来の `/youtube/:videoId/:lang` は「細かい版を指定しない既定版」を返します。複数版を並行保持したい場合は、Discord bot の字幕選択 UI から明示版を準備すると、その明示パスの URL が返ります。

`POST /prepare/youtube-batch/:lang?source=:playlistOrChannelUrl&sourceType=auto&mode=mp4|hls&maxItems=5000` は、YouTube Data API v3 でプレイリストまたはチャンネル投稿一覧を展開し、含まれる動画をすべて準備ジョブへ投入します。返却される `batch_id` / `status_url` は `GET /prepare/batches/:batchId` でポーリングできます。`source` はプレイリスト URL/ID、`@handle`、`https://www.youtube.com/@handle`、`https://www.youtube.com/channel/...` に対応します。

`POST /prepare/archive-all` は、SSDホットキャッシュ上の完了済みエントリをすべて `CACHE_ARCHIVE_DIR` へ退避します。実行中または待機中の準備ジョブに関係するエントリと、一時作業ディレクトリはスキップします。削除系と同じく `DISCORD_PREPARE_TOKEN` が必要で、Web UI一時キーでは実行できません。

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
/archive-all
/webui-key days:3
/reset-eta
```

`url` にプレイリスト URL やチャンネル URL を渡した場合は、YouTube Data API v3 で一覧を展開して一括準備します。`max_items` の既定値は `DISCORD_PREPARE_BATCH_MAX_ITEMS`、未設定時は 5000 件です。
`url` に動画URLまたは動画IDを複数入れた場合も、手動動画リストとして一括準備できます。区切りは改行、空白、カンマに対応します。Web UI の動画準備欄も同じ形式を受け付け、複数件の場合は `/prepare/youtube-batch` に `sourceType=videos` で送信します。

大量一括準備では `PREPARE_JOB_CONCURRENCY` で同時に実行する準備ジョブ数を制限します。既定は `3` です。DL・翻訳はエンコード待ち中でも先行して実行され、エンコードだけを `FFMPEG_ENCODE_CONCURRENCY` で制限します。既定は `1` です。GPU/NVENC やCPUに余裕がある場合は `FFMPEG_ENCODE_CONCURRENCY=2` 以上へ上げられます。各ジョブは一時的な `429` / `5xx` / `502` / `504` / タイムアウト系の失敗を `PREPARE_JOB_MAX_ATTEMPTS` 回まで再試行し、待ち時間は `PREPARE_JOB_RETRY_BASE_SECONDS` から指数バックオフします。さらに、YouTube 取得に使う yt-dlp は `YTDLP_CONCURRENCY=1`、`YTDLP_MIN_INTERVAL_SECONDS=8` を既定にして、メタデータ取得・動画DL・字幕DLの開始頻度を制限します。チャンネル全件などで失敗が多い場合は、まず `PREPARE_JOB_CONCURRENCY=1` または `2`、`YTDLP_MIN_INTERVAL_SECONDS=10` 以上に下げ、cookies と yt-dlp の更新状況を確認してください。一括完了通知と `/prepare/batches/:batchId` には代表的な失敗理由が最大5件含まれます。

大量処理で作業領域を使い切りそうな場合は `CACHE_HOT_MIN_FREE_BYTES` を SSD 容量に合わせて大きめに設定してください。準備開始時に不足があれば、完了済みの古いエントリを先に HDD へ退避します。単発の準備は必要容量を満たしていれば続行し、閾値未満でも余裕があれば警告だけ出します。必要容量そのものが足りなければ `507 Insufficient Storage` で止まります。

Discord bot の完了待ちは単体ジョブでは `DISCORD_PREPARE_POLL_TIMEOUT_SECONDS`、一括ジョブでは `DISCORD_PREPARE_BATCH_POLL_TIMEOUT_SECONDS` を使います。既定では一括ジョブを24時間まで追跡し、サーバー側の `estimated_ready_at` が後ろへ伸びた場合は `DISCORD_PREPARE_POLL_TIMEOUT_GRACE_SECONDS` 分の余裕を足して待機期限も延長します。タイムアウトしても準備ジョブ自体はサーバー側で継続している可能性があります。`/prepare/batches/:batchId` は認証付きAPIなので、Discord上のリンクとして直接開く用途には使いません。

一括準備の進捗表示は `ready + failed` を全体完了数として進捗バーに反映します。各動画が ready になった時点で、bot はコマンド実行者だけに見える控えめな「1件準備できました」通知を出します。チャンネル全体の公開通知は一括ジョブ全体が完了した時だけです。

字幕スタイルやエンコード設定だけを変えて焼き込み直したい場合は、Discord の `/reburn` を使います。入力形式は `/prepare` と同じで、既存の `source/` 配下にある動画・元字幕・翻訳済み字幕を再利用し、YouTubeからの再ダウンロードやLLM/Google再翻訳を行わずにMP4/HLS出力だけを作り直します。
再利用可能な準備済み動画をまとめて作り直す場合は `/reburn-all` を使います。`lang:all` で全言語、`lang:ja` などで対象言語を絞れます。対象はSSD/HDDに残っていて、かつ `source/` 配下の動画・字幕が残っているものです。
SSD容量を空けたい場合は `/archive-all` を使います。SSD上の完了済み動画・字幕・メタデータをHDDアーカイブへ退避し、実行中の準備ジョブに関係するものはスキップします。通常のMP4配信URLはHDDからも直接返せますが、複数人に共有する前は再度 `/prepare` でSSDへ準備する運用を推奨します。

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

LLM 翻訳は GTX 1050 Ti サーバー上では実行せず、RTX 3060 を搭載した別PCの OpenAI 互換 API に送ります。翻訳エンジン選択では `Qwen 3 4B Instruct`、`Qwen 3 8B`、`Aya Expanse 8B`、`Gemini Flash` を切り替えられます。準備前に `REMOTE_LLM_HEALTH_URL` を確認し、応答がない、または余裕なしとして失敗する場合は、Discord bot がユーザーに Google 翻訳で進めてよいか確認します。LLM 失敗時に Google Cloud Translation API へ自動フォールバックする動作は行いません。

```bash
export TRANSLATION_ENABLED=1
export TRANSLATION_SOURCE_LANGS=en,ko,zh-Hans,zh-Hant,zh,zh-CN,zh-TW
export TRANSLATION_DEFAULT_PROFILE=qwen3_4b_instruct
export TRANSLATION_PROVIDER=qwen3_4b_instruct
export REMOTE_LLM_ENDPOINT=http://192.168.68.115:11434/v1/chat/completions
export REMOTE_LLM_HEALTH_URL=http://192.168.68.115:11434/v1/models
export REMOTE_LLM_MODEL=qwen3:4b-instruct
export TRANSLATION_AUDIT_DIR=/var/lib/youtube-proxy/translation-audit
export LOCAL_LLM_MODEL_QWEN3_4B_INSTRUCT=qwen3:4b-instruct
export LOCAL_LLM_MODEL_QWEN3_8B=qwen3:8b
export LOCAL_LLM_MODEL_AYA_EXPANSE_8B=aya-expanse:8b
export LOCAL_LLM_MODEL_GEMINI_2_5_FLASH=gemini-2.5-flash
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
`FFMPEG_VIDEO_PRESET=veryfast` は `libx264` 用で、`h264_nvenc` では使えません。NVENC では `fast`、`medium`、`slow`、`p1` から `p7` などを使ってください。無効な値が設定されている場合、アプリは `fast` にフォールバックします。

`Driver does not support the required nvenc API version` が出る場合は、FFmpeg が要求する NVENC API に対して NVIDIA ドライバーが古い状態です。この場合、アプリは CPU エンコードに自動フォールバックします。GPU を使い切りたい場合は NVIDIA ドライバーを更新してください。

Gemini API は無料枠の RPD が小さいため、字幕1件ごとの大量リクエストに使う場合は枯渇しやすいです。`GEMINI_MAX_REQUESTS_PER_JOB` は1ジョブでGeminiへ投げる最大字幕件数です。既定は `3` で、超過時は `GEMINI_FALLBACK_PROFILE`、既定 `qwen3_4b_instruct`、へ自動で切り替えます。spending cap / RPD 系の 429 が返った場合も再試行せずローカルLLMへ退避します。

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
