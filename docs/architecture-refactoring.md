# 保守性向上リファクタリングの構成

## 方針

既存 API の URL、キャッシュ形式、環境変数名、ジョブ状態の JSON 形式を互換性の基準とする。新しい責務は、まず純粋関数または小さなアダプターとして切り出し、既存の `app.main` から互換インポートする。

## モジュール境界

| モジュール | 責務 | 外部 I/O |
| --- | --- | --- |
| `app.main` | FastAPI のルーティング、ジョブ orchestration、既存互換 API | あり |
| `app.translation` | SRT の翻訳処理 | 翻訳 API／worker |
| `app.translation_profiles` | 翻訳モデルのプロファイル定義 | なし |
| `app.validation` | リクエスト値の検証 | なし |
| `app.input_patterns` | ID・言語の正規表現契約 | なし |
| `app.youtube_inputs` | YouTube URL／ID の解析 | なし |
| `app.yamaplayer_helpers` | YamaPlayer の入出力整形 | なし |
| `app.cache_layout` | ホット／アーカイブのパス構造 | なし |
| `app.json_files` | 防御的な JSON オブジェクト読み込み | ファイル |
| `app.metrics` | ETA 用メトリクスの保存・集計 | ファイル |
| `app.http_range` | HTTP byte range の解析 | なし |
| `app.media_stream` | ローカルメディアの非同期チャンク読み出し | ファイル |
| `app.hls_playlist` | HLS プレイリストの URL 書き換え | なし |
| `app.config_files` | `.env` とプロンプトファイルの読み込み | ファイル |
| `app.command_errors` | 外部コマンドのエラー型・分類 | なし |
| `app.command_runner` | 非同期 subprocess の起動・timeout・終了処理 | プロセス |
| `app.ytdlp_args` | yt-dlp の引数生成・cookies 引数除去 | なし |

## 拡張時のルール

1. URL や JSON の既存契約を変更する前に、対応する既存テストを確認する。
2. 純粋な変換・検証は `app.main` に追加せず、責務に対応する小さなモジュールへ追加する。
3. 外部 API、`yt-dlp`、`ffmpeg`、ファイルシステムを直接呼ぶ処理はアダプター境界を越えて混ぜない。
4. キャッシュの保存先やファイル名を追加・変更する場合は `CacheLayout` とメタデータのテストを同時に更新する。
5. `app.main` の互換ラッパーを削除する場合は、テスト・bot・外部利用箇所を検索してから行う。
6. リファクタリング後は `pytest`、`compileall`、import 検証、`git diff --check` を実行する。

## 未分離の大きな責務

`app.main` には、準備ジョブの orchestration、`yt-dlp`／`ffmpeg` の呼び出し制御、キャッシュ昇格・退避、HTTP ルートが残っている。外部プロセスの基本実行は `app.command_runner` に分離済みだが、yt-dlp の semaphore／rate limit と ffmpeg の進捗管理は共有状態を持つため、次に分離する場合は、まずジョブ状態と外部コマンドのインターフェースをテストで固定してから段階的に移動する。
