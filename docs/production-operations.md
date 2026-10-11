# 本番運用操作基盤

本番サービスは、設定を次の2ファイルへ分離して読み込みます。

- `/etc/youtube-mp4-proxy/config.env`: 非機密設定。APIから変更できるキーはallowlistのみ。
- `/etc/youtube-mp4-proxy/secrets.env`: APIキー、Discord token、準備token、監査token、コンフィグtoken。値をAIエージェントへ返さない。

systemdの対象サービスは `youtube-mp4-proxy` と `youtube-mp4-discord-bot` です。デプロイは従来どおり `youtube-subtitles-update.ps1` を使い、アーカイブはrootラッパーが専用ディレクトリへ移して検証します。

## 監査確認

```powershell
.scripts	ranslation-audit.ps1 -List -Limit 20 -VideoId dQw4w9WgXcQ
.scripts	ranslation-audit.ps1 -DetailName 20260815-dQw4w9WgXcQ-ja-model.jsonl -Limit 200
```

PowerShellはトークンを受け取らず、SSH先の `/usr/local/sbin/youtube-proxy-audit` がroot専用の `secrets.env` から監査tokenを読みます。許可される操作はlist/detailだけです。

## 非機密コンフィグ

```powershell
$state = .\scripts\production-config.ps1 -Get | ConvertFrom-Json
.\scripts\production-config.ps1 -Set -Key TRANSLATION_API_RETRY_MAX_ATTEMPTS -Value 3 -ExpectedRevision $state.revision
```

変更APIは `expected_revision` が一致する場合だけatomic renameし、変更後に2サービスを再起動して `/healthz` を確認します。

allowlist:

- `LOCAL_LLM_MAX_OUTPUT_TOKENS` (1..1000000)
- `TRANSLATION_API_RETRY_MAX_ATTEMPTS` (1..10)
- `TRANSLATION_API_RETRY_BASE_SECONDS` (0..300)
- `GEMINI_THINKING_LEVEL` (`minimal`, `low`, `medium`, `high`)
- `SYSTEM_METRICS_INTERVAL_SECONDS` (1..3600)
- `SYSTEM_METRICS_HISTORY_SECONDS` (60..31536000)
- `DISCORD_OPERATOR_USER_ID` (Discordの運用者ID)
- `DISCORD_URL_INTAKE_CHANNEL_ID` (URL貼り付け受付チャンネルID)
- `CACHE_ARCHIVE_MOUNT_POINT` (HDDの固定マウント点)
- `MAX_HEIGHT` (360..2160、字幕描画前の映像高上限)
- `FFMPEG_VIDEO_CQ` (0..51、NVENC品質。大きいほど低画質・小容量)
- `FFMPEG_VIDEO_PRESET` (`p1`..`p7`、NVENC速度。`p1`が最速)

## sudoers

接続ユーザーに許可するrootコマンドは次の3つだけです。

```text
furukawa ALL=(root) NOPASSWD: /usr/local/sbin/youtube-proxy-update, /usr/local/sbin/youtube-proxy-audit, /usr/local/sbin/youtube-proxy-config, /usr/local/sbin/youtube-proxy-operator
```

`/usr/local/sbin/youtube-proxy-audit` はlocalhostの `/translation-audit` だけ、`youtube-proxy-config` はlocalhostの `/ops/config` と固定2サービスのrestart/statusだけを扱います。任意URL、任意shell、任意systemctlは受け付けません。

## HDD とDiscordの運用

最初に、HDDを実際にマウントしている固定パスとDiscord運用者IDを設定します。本番のHDDは `/mnt/video` です。

```powershell
$state = .\scripts\production-config.ps1 -Get | ConvertFrom-Json
.\scripts\production-config.ps1 -Set -Key CACHE_ARCHIVE_MOUNT_POINT -Value /mnt/video -ExpectedRevision $state.revision
$state = .\scripts\production-config.ps1 -Get | ConvertFrom-Json
.\scripts\production-config.ps1 -Set -Key DISCORD_OPERATOR_USER_ID -Value 363466015683903488 -ExpectedRevision $state.revision
```

以後、Windowsからは次だけです。

```powershell
.\scripts\production-operator.ps1 -Action Status
.\scripts\production-operator.ps1 -Action Detach
.\scripts\production-operator.ps1 -Action Attach
.\scripts\production-operator.ps1 -Action SetProfile -Profile gpt_5_nano
```

`Detach` は退避完了かつ実行中ジョブなしの場合だけproxyを停止してアンマウントします。成功表示の後にHDDを抜いてください。常駐watcherが5秒ごとに再接続を確認し、HDDが戻るとマウントしてproxyを自動再開します。Discordでは同じ操作を `/server-storage` と `/translation-model` から実行できます。いずれも設定したユーザーIDだけが実行でき、返答はephemeralです。

## 自動保守

- `youtube-proxy-ytdlp-update.timer` は毎日04:10 JST以降に新版を確認する。変換中は延期し、更新後にYouTubeメタデータ、字幕一覧、API、Discord Gatewayを検証する。失敗時は更新前の `yt-dlp`、`curl-cffi`、`yt-dlp-ejs` へ戻す。
- `youtube-proxy-maintenance-check.timer` は毎日03:45 JST以降に `reboot-required`、NVIDIAのロード済み版とライブラリ版、`nvidia-smi`、1フレームNVENCを確認する。異常と復旧だけをDiscord運用者へDMする。
- `youtube-proxy-maintenance-reboot.timer` は毎日04:30 JST以降、再起動が必要な場合だけ動く。実行中ジョブ、`ffmpeg`、`yt-dlp`、apt/dpkg、対話ログインがある場合と、`/etc/youtube-mp4-proxy/no-auto-reboot` がある場合は翌日へ延期する。
- 再起動後は `youtube-proxy-maintenance-boot.service` がAPI、Discord Bot、HDD、NVIDIA、NVENCを確認する。失敗しても再起動を繰り返さず、CPUフォールバックを維持してDiscordへ通知する。

保守状態は `/var/lib/youtube-mp4-proxy/maintenance.json` の一箇所だけに保存する。正常時の定期通知は行わない。
