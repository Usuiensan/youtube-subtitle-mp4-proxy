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

## sudoers

接続ユーザーに許可するrootコマンドは次の3つだけです。

```text
furukawa ALL=(root) NOPASSWD: /usr/local/sbin/youtube-proxy-update, /usr/local/sbin/youtube-proxy-audit, /usr/local/sbin/youtube-proxy-config
```

`/usr/local/sbin/youtube-proxy-audit` はlocalhostの `/translation-audit` だけ、`youtube-proxy-config` はlocalhostの `/ops/config` と固定2サービスのrestart/statusだけを扱います。任意URL、任意shell、任意systemctlは受け付けません。
