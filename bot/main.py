from __future__ import annotations

import asyncio
import json
import os
import re
import base64
import hashlib
import hmac
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env.local"
LANG_NAMES_JA = {
    "ar": "アラビア語",
    "de": "ドイツ語",
    "en": "英語",
    "en-gb": "英語(英国)",
    "es": "スペイン語",
    "es-419": "スペイン語(ラテンアメリカ)",
    "fr": "フランス語",
    "fr-ca": "フランス語(カナダ)",
    "hi": "ヒンディー語",
    "id": "インドネシア語",
    "it": "イタリア語",
    "ja": "日本語",
    "ko": "韓国語",
    "nl": "オランダ語",
    "pt": "ポルトガル語",
    "pt-br": "ポルトガル語(ブラジル)",
    "ru": "ロシア語",
    "th": "タイ語",
    "tr": "トルコ語",
    "uk": "ウクライナ語",
    "vi": "ベトナム語",
    "zh": "中国語",
    "zh-cn": "中国語(簡体字)",
    "zh-hans": "中国語(簡体字)",
    "zh-hant": "中国語(繁体字)",
    "zh-tw": "中国語(繁体字)",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_FILE)


class Settings:
    discord_bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
    discord_prepare_token = os.getenv("DISCORD_PREPARE_TOKEN", "")
    youtube_proxy_base_url = os.getenv("YOUTUBE_PROXY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    youtube_proxy_internal_base_url = os.getenv(
        "YOUTUBE_PROXY_INTERNAL_BASE_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")
    poll_seconds = int(os.getenv("DISCORD_PREPARE_POLL_SECONDS", "10"))
    poll_timeout_seconds = int(os.getenv("DISCORD_PREPARE_POLL_TIMEOUT_SECONDS", "7200"))
    prepare_batch_max_items = int(os.getenv("DISCORD_PREPARE_BATCH_MAX_ITEMS", "5000"))
    webui_temp_key_secret = os.getenv("WEBUI_TEMP_KEY_SECRET", os.getenv("DISCORD_PREPARE_TOKEN", ""))


settings = Settings()
JST = timezone(timedelta(hours=9))


class PrepareApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def hmac_sha256(message: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_webui_temp_key(days: int) -> dict[str, Any]:
    if days < 1 or days > 30:
        raise ValueError("days は 1 から 30 の範囲で指定してください。")
    if not settings.webui_temp_key_secret:
        raise RuntimeError("WEBUI_TEMP_KEY_SECRET が設定されていません。")
    today = datetime.now(JST).date()
    expires_date = today + timedelta(days=days - 1)
    expires_on = expires_date.isoformat()
    signature = hmac_sha256(f"webui-temp-key:{expires_on}", settings.webui_temp_key_secret)[:32]
    expires_at = datetime.combine(expires_date + timedelta(days=1), datetime.min.time(), tzinfo=JST)
    return {
        "key": f"{expires_on}-{signature}",
        "expires_on": expires_on,
        "expires_at": int(expires_at.timestamp()),
    }


def extract_video_id(value: str) -> str:
    value = value.strip()
    if VIDEO_ID_RE.fullmatch(value):
        return value

    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        parsed = urllib.parse.urlparse("https://" + value)
    else:
        parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        candidate = parsed.path.strip("/").split("/")[0]
        if VIDEO_ID_RE.fullmatch(candidate):
            return candidate

    query = urllib.parse.parse_qs(parsed.query)
    candidate = query.get("v", [""])[0]
    if VIDEO_ID_RE.fullmatch(candidate):
        return candidate

    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and VIDEO_ID_RE.fullmatch(parts[index + 1]):
                return parts[index + 1]

    raise ValueError("YouTube URLまたは動画IDを認識できませんでした。")


def looks_like_playlist_or_channel(value: str) -> bool:
    value = value.strip()
    if value.startswith("@"):
        return True
    if value.startswith("UC") and not VIDEO_ID_RE.fullmatch(value):
        return True
    if not value.startswith(("http://", "https://")) and ("." in value or "/" in value):
        parsed = urllib.parse.urlparse("https://" + value)
    else:
        parsed = urllib.parse.urlparse(value)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("list"):
        return True
    parts = [part for part in parsed.path.split("/") if part]
    return bool(parts and (parts[0] in {"channel", "c", "user"} or parts[0].startswith("@")))


def http_json(method: str, url: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {settings.discord_prepare_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
            detail = body.get("detail") or body.get("error") or raw
        except json.JSONDecodeError:
            detail = raw
        raise PrepareApiError(error.code, str(detail)) from error
    except urllib.error.URLError as error:
        raise PrepareApiError(502, str(error.reason)) from error


async def prepare_video(
    video_id: str,
    lang: str,
    mode: str,
    discord_user_id: int,
    subtitle_source_lang: str | None = None,
    translation_engine: str | None = None,
) -> tuple[int, dict[str, Any]]:
    params = {
        "mode": mode,
        "discordUserId": str(discord_user_id),
    }
    query = urllib.parse.urlencode(params)
    variant_path = ""
    if subtitle_source_lang and translation_engine:
        variant_path = f"/{urllib.parse.quote(subtitle_source_lang, safe='')}/{urllib.parse.quote(translation_engine, safe='')}"
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/youtube/{video_id}/{lang}{variant_path}?{query}"
    return await asyncio.to_thread(http_json, "POST", url)


async def fetch_subtitle_options(video_id: str, lang: str, mode: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"mode": mode})
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/youtube/{video_id}/{lang}/subtitles?{query}"
    _status, body = await asyncio.to_thread(http_json, "GET", url)
    return body


async def prepare_batch(source: str, lang: str, mode: str, discord_user_id: int, max_items: int) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "source": source,
            "sourceType": "auto",
            "mode": mode,
            "maxItems": str(max_items),
            "discordUserId": str(discord_user_id),
        }
    )
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/youtube-batch/{lang}?{query}"
    return await asyncio.to_thread(http_json, "POST", url)


async def clear_video(video_id: str, lang: str) -> tuple[int, dict[str, Any]]:
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/youtube/{video_id}/{lang}/clear"
    return await asyncio.to_thread(http_json, "POST", url)


async def clear_all_videos() -> tuple[int, dict[str, Any]]:
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/youtube/clear-all"
    return await asyncio.to_thread(http_json, "POST", url)


async def reset_eta_metrics() -> tuple[int, dict[str, Any]]:
    url = f"{settings.youtube_proxy_internal_base_url}/prepare/eta/reset"
    return await asyncio.to_thread(http_json, "POST", url)


async def fetch_job(status_url: str) -> dict[str, Any]:
    public_base = settings.youtube_proxy_base_url
    internal_base = settings.youtube_proxy_internal_base_url
    if public_base and status_url.startswith(public_base):
        status_url = internal_base + status_url[len(public_base):]
    _status, body = await asyncio.to_thread(http_json, "GET", status_url)
    return body


def eta_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    eta_seconds = body.get("eta_seconds")
    if isinstance(eta_seconds, (int, float)) and eta_seconds > 0:
        seconds = max(1, int(round(eta_seconds)))
        minutes = seconds // 60
        remaining_seconds = seconds % 60
        if minutes:
            parts.append(f"予想{minutes}分{remaining_seconds}秒")
        else:
            parts.append(f"予想{remaining_seconds}秒")

    estimated_ready_at = body.get("estimated_ready_at")
    if isinstance(estimated_ready_at, (int, float)) and estimated_ready_at > 0:
        parts.append(f"終了予想 <t:{int(estimated_ready_at)}:t>")

    return " / ".join(parts) if parts else "終了予想を計算中"


def public_url(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        public_base = urllib.parse.urlparse(settings.youtube_proxy_base_url)
        return urllib.parse.urlunparse(
            (
                public_base.scheme,
                public_base.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )
    return url


def mention_text(body: dict[str, Any], fallback_user_id: int | None = None) -> str:
    mentions = body.get("mentions")
    if isinstance(mentions, list) and mentions:
        return " ".join(str(mention) for mention in mentions)
    if fallback_user_id is not None:
        return f"<@{fallback_user_id}>"
    return ""


def lang_name_ja(code: Any) -> str:
    if not isinstance(code, str) or not code:
        return ""
    normalized = code.lower().strip()
    base = normalized.split("-")[0]
    name = LANG_NAMES_JA.get(normalized) or LANG_NAMES_JA.get(base)
    if name:
        return name
    return code


def status_message(body: dict[str, Any], fallback_user_id: int | None = None) -> str:
    if isinstance(body.get("counts"), dict):
        return batch_status_message(body, fallback_user_id)

    status = body.get("status", "unknown")
    mode = body.get("mode", "mp4")
    title = body.get("title")
    title_part = f"\n{title}" if title else ""
    subtitle_part = subtitle_status_text(body.get("subtitle"))

    if status == "ready":
        mention = mention_text(body, fallback_user_id)
        prefix = f"{mention} " if mention else ""
        return f"{prefix}準備できました。{title_part}{subtitle_part}\n{public_url(body.get('url'))}"
    if status == "failed":
        mention = mention_text(body, fallback_user_id)
        prefix = f"{mention} " if mention else ""
        return f"{prefix}準備に失敗しました。{title_part}\n{body.get('error', 'unknown error')}"

    progress = body.get("progress")
    if isinstance(progress, dict) and progress:
        phase = progress.get("phase", "")
        percent = progress.get("percent", 0.0)
        eta_sec = progress.get("eta_seconds")
        details = progress.get("details", "")

        phase_names = {
            "download": "動画・字幕のダウンロード",
            "translate": "字幕の翻訳 (LLM)",
            "encode": "動画の再エンコード (字幕焼き込み)",
            "hls": "HLS配信用の書き出し"
        }
        phase_ja = phase_names.get(phase, phase)

        bar_width = 15
        filled = int(round(percent / 100 * bar_width))
        bar = "█" * filled + "░" * (bar_width - filled)
        
        eta_part = ""
        if isinstance(eta_sec, (int, float)) and eta_sec > 0:
            seconds = max(1, int(round(eta_sec)))
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            if minutes:
                eta_part = f" (残り {minutes}分{remaining_seconds}秒)"
            else:
                eta_part = f" (残り {remaining_seconds}秒)"

        progress_bar = f"`[{bar}] {percent:5.1f}%`{eta_part}"
        details_part = f"\n{details}" if details else ""
        
        return (
            f"{mode.upper()}を準備しています...\n"
            f"**進捗**: {phase_ja}\n"
            f"{progress_bar}{details_part}"
            f"{title_part}{subtitle_part}"
        )

    return f"{mode.upper()}を準備しています。{eta_text(body)}{title_part}{subtitle_part}"


def batch_status_message(body: dict[str, Any], fallback_user_id: int | None = None) -> str:
    status = body.get("status", "unknown")
    mode = body.get("mode", "mp4")
    name = body.get("playlist_name") or body.get("playlist_id") or body.get("source") or "playlist/channel"
    counts = body.get("counts") if isinstance(body.get("counts"), dict) else {}
    total = counts.get("total", 0)
    ready = counts.get("ready", 0)
    failed = counts.get("failed", 0)
    running = counts.get("running", 0)
    queued = counts.get("queued", 0)
    mention = mention_text(body, fallback_user_id)
    prefix = f"{mention} " if mention and status in {"ready", "failed"} else ""

    if status in {"ready", "failed"}:
        label = "一括準備が完了しました。" if status == "ready" else "一括準備が終了しました。"
        lines = [
            f"{prefix}{label}",
            str(name),
            f"ready {ready}/{total} / failed {failed}",
        ]
        items = body.get("items") if isinstance(body.get("items"), list) else []
        urls = [public_url(item.get("url")) for item in items if isinstance(item, dict) and item.get("url")]
        if urls:
            lines.extend(urls[:10])
            if len(urls) > 10:
                lines.append(f"...ほか {len(urls) - 10} 件")
        return "\n".join(lines)

    return (
        f"{mode.upper()}を一括準備しています。{eta_text(body)}\n"
        f"{name}\n"
        f"ready {ready}/{total} / running {running} / queued {queued} / failed {failed}"
    )


def subtitle_status_text(meta: Any) -> str:
    if not isinstance(meta, dict) or not meta:
        return ""
    source = meta.get("source_language")
    requested = meta.get("requested_language")
    translated = meta.get("translated")
    kind = meta.get("source_kind")
    engine = meta.get("translation_engine")
    fallback = meta.get("translation_fallback_used")
    if translated:
        engine_text = "Google翻訳フォールバック" if fallback else (engine or "local_llm")
        kind_text = "手動" if kind == "manual" else str(kind or "")
        return f"\n字幕: {lang_name_ja(source)}（{kind_text}）→{lang_name_ja(requested)}（{engine_text}）"
    if source:
        return f"\n字幕: {lang_name_ja(source)}"
    return ""


def option_label(value: str, limit: int = 100) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class SubtitleChoiceView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        video_id: str,
        lang: str,
        mode: str,
        options_body: dict[str, Any],
    ) -> None:
        super().__init__(timeout=300)
        self.requester_id = requester_id
        self.video_id = video_id
        self.lang = lang
        self.mode = mode
        self.source_lang: str | None = None
        self.translation_engine = "local_llm"

        candidates = options_body.get("candidates") if isinstance(options_body.get("candidates"), list) else []
        source_options = []
        for candidate in candidates[:25]:
            if not isinstance(candidate, dict):
                continue
            language = str(candidate.get("language") or "")
            if not language:
                continue
            name = str(candidate.get("name") or language)
            name_en = str(candidate.get("name_en") or "")
            source_options.append(
                discord.SelectOption(
                    label=option_label(f"{language} / {name}", 100),
                    value=language,
                    description=option_label(name_en, 100) if name_en else None,
                )
            )

        engine_options = [
            discord.SelectOption(label="LLM翻訳", value="local_llm", default=True),
            discord.SelectOption(label="Google翻訳", value="google_cloud"),
        ]
        self.source_select = discord.ui.Select(
            placeholder="翻訳元字幕を選択",
            min_values=1,
            max_values=1,
            options=source_options,
        )
        self.engine_select = discord.ui.Select(
            placeholder="翻訳エンジンを選択",
            min_values=1,
            max_values=1,
            options=engine_options,
        )
        self.source_select.callback = self.on_source_selected
        self.engine_select.callback = self.on_engine_selected
        self.add_item(self.source_select)
        self.add_item(self.engine_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("この選択はコマンド実行者だけが操作できます。", ephemeral=True)
        return False

    async def on_source_selected(self, interaction: discord.Interaction) -> None:
        self.source_lang = self.source_select.values[0]
        await interaction.response.defer()

    async def on_engine_selected(self, interaction: discord.Interaction) -> None:
        self.translation_engine = self.engine_select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="この設定で準備", style=discord.ButtonStyle.primary)
    async def start_prepare(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not self.source_lang:
            await interaction.response.send_message("先に翻訳元字幕を選択してください。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        for child in self.children:
            child.disabled = True
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

        try:
            _status, body = await prepare_video(
                self.video_id,
                self.lang,
                self.mode,
                interaction.user.id,
                subtitle_source_lang=self.source_lang,
                translation_engine=self.translation_engine,
            )
        except PrepareApiError as error:
            await interaction.followup.send(f"準備APIエラー ({error.status_code}): {error.detail}")
            return

        await interaction.followup.send(
            status_message(body, interaction.user.id),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        status_url = body.get("status_url")
        if body.get("status") in {"queued", "running"} and isinstance(status_url, str):
            asyncio.create_task(notify_when_done(interaction, status_url))

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="準備をキャンセルしました。", view=self)
        self.stop()


async def notify_when_done(
    interaction: discord.Interaction,
    status_url: str,
) -> None:
    deadline = time.monotonic() + settings.poll_timeout_seconds
    latest: dict[str, Any] | None = None

    async def send_notification(content: str) -> None:
        channel = interaction.channel
        if channel is None and interaction.channel_id is not None:
            try:
                fetched = await interaction.client.fetch_channel(interaction.channel_id)
                if isinstance(fetched, discord.abc.Messageable):
                    channel = fetched
            except Exception:
                pass
        if channel is not None:
            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return
            except discord.HTTPException:
                pass
        try:
            await interaction.followup.send(
                content,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            try:
                await interaction.user.send(
                    content,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass

    is_first_poll = True
    last_content = None
    last_edit_time = 0.0
    while time.monotonic() < deadline:
        sleep_time = 2 if is_first_poll else settings.poll_seconds
        await asyncio.sleep(sleep_time)
        is_first_poll = False
        try:
            latest = await fetch_job(status_url)
        except Exception:
            continue
        if latest.get("status") in {"ready", "failed"}:
            notification = latest.get("notification") or {}
            content = notification.get("content") or status_message(latest, interaction.user.id)
            subtitle_part = subtitle_status_text(latest.get("subtitle"))
            if subtitle_part and subtitle_part not in content:
                content += subtitle_part
            content = content.replace("http://127.0.0.1:8000", settings.youtube_proxy_base_url)
            content = content.replace("http://localhost:8000", settings.youtube_proxy_base_url)
            await send_notification(content)
            return

        if latest:
            content = status_message(latest, interaction.user.id)
            if content != last_content:
                now = time.monotonic()
                elapsed = now - last_edit_time
                if elapsed < 4.0:
                    await asyncio.sleep(4.0 - elapsed)
                try:
                    await interaction.edit_original_response(content=content)
                    last_content = content
                    last_edit_time = time.monotonic()
                except discord.HTTPException:
                    pass

    await send_notification(
        f"<@{interaction.user.id}> 準備ジョブの確認がタイムアウトしました。status_url={status_url}",
    )


class YoutubeProxyBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()


client = YoutubeProxyBot()


@client.tree.command(name="prepare", description="YouTube動画を変換またはSSDへ準備します")
@app_commands.describe(
    url="YouTube URL、動画ID、playlist URL、channel URL",
    lang="字幕言語",
    mode="出力形式",
    max_items="playlist/channel URL の最大準備件数",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="MP4", value="mp4"),
        app_commands.Choice(name="HLS", value="hls"),
    ]
)
async def prepare_command(
    interaction: discord.Interaction,
    url: str,
    lang: str = "ja",
    mode: app_commands.Choice[str] | None = None,
    max_items: int | None = None,
) -> None:
    await interaction.response.defer(thinking=True)

    if not settings.discord_prepare_token:
        await interaction.followup.send("DISCORD_PREPARE_TOKEN が設定されていません。")
        return

    selected_mode = mode.value if mode else "mp4"
    selected_max_items = max_items if max_items is not None else settings.prepare_batch_max_items
    if selected_max_items < 1 or selected_max_items > 5000:
        await interaction.followup.send("max_items は 1 から 5000 の範囲で指定してください。")
        return

    try:
        if looks_like_playlist_or_channel(url):
            _status, body = await prepare_batch(
                url,
                lang,
                selected_mode,
                interaction.user.id,
                selected_max_items,
            )
        else:
            video_id = extract_video_id(url)
            if lang == "ja":
                try:
                    options_body = await fetch_subtitle_options(video_id, lang, selected_mode)
                except PrepareApiError as error:
                    await interaction.followup.send(f"字幕候補取得APIエラー ({error.status_code}): {error.detail}")
                    return
                if options_body.get("requires_choice"):
                    candidates = options_body.get("candidates") if isinstance(options_body.get("candidates"), list) else []
                    if not candidates:
                        await interaction.followup.send(str(options_body.get("error") or "翻訳可能な手動字幕がありません。"))
                        return
                    title = options_body.get("title") or video_id
                    view = SubtitleChoiceView(
                        requester_id=interaction.user.id,
                        video_id=video_id,
                        lang=lang,
                        mode=selected_mode,
                        options_body=options_body,
                    )
                    await interaction.followup.send(
                        f"日本語字幕が見つかりませんでした。\n{title}\n翻訳元字幕と翻訳エンジンを選択してください。既定は LLM 翻訳です。",
                        view=view,
                    )
                    return
                if options_body.get("error"):
                    await interaction.followup.send(str(options_body["error"]))
                    return
            _status, body = await prepare_video(video_id, lang, selected_mode, interaction.user.id)
    except ValueError:
        try:
            _status, body = await prepare_batch(
                url,
                lang,
                selected_mode,
                interaction.user.id,
                selected_max_items,
            )
        except PrepareApiError as error:
            await interaction.followup.send(f"準備APIエラー ({error.status_code}): {error.detail}")
            return
    except PrepareApiError as error:
        await interaction.followup.send(f"準備APIエラー ({error.status_code}): {error.detail}")
        return

    await interaction.followup.send(
        status_message(body, interaction.user.id),
        allowed_mentions=discord.AllowedMentions(users=True),
    )

    status_url = body.get("status_url")
    if body.get("status") in {"queued", "running"} and isinstance(status_url, str):
        asyncio.create_task(notify_when_done(interaction, status_url))


@client.tree.command(name="clear", description="既存の再エンコードされた動画及び翻訳済みテキストファイルを削除して初期化します")
@app_commands.describe(
    url="YouTube URLまたは動画ID",
    lang="字幕言語",
)
async def clear_command(
    interaction: discord.Interaction,
    url: str,
    lang: str = "ja",
) -> None:
    await interaction.response.defer(thinking=True)

    if not settings.discord_prepare_token:
        await interaction.followup.send("DISCORD_PREPARE_TOKEN が設定されていません。")
        return

    try:
        video_id = extract_video_id(url)
    except ValueError as error:
        await interaction.followup.send(str(error))
        return

    try:
        _status, body = await clear_video(video_id, lang)
    except PrepareApiError as error:
        await interaction.followup.send(f"初期化APIエラー ({error.status_code}): {error.detail}")
        return

    await interaction.followup.send(
        body.get("message", "初期化しました。"),
    )


@client.tree.command(name="clear-all", description="すべての動画の再エンコードされた動画及び翻訳済みテキストファイルを削除して初期化します")
async def clear_all_command(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(thinking=True)

    if not settings.discord_prepare_token:
        await interaction.followup.send("DISCORD_PREPARE_TOKEN が設定されていません。")
        return

    try:
        _status, body = await clear_all_videos()
    except PrepareApiError as error:
        await interaction.followup.send(f"初期化APIエラー ({error.status_code}): {error.detail}")
        return

    await interaction.followup.send(
        body.get("message", "すべての動画を初期化しました。"),
    )


@client.tree.command(name="reset-eta", description="変換時間予想の学習データをリセットします")
async def reset_eta_command(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer(thinking=True)

    if not settings.discord_prepare_token:
        await interaction.followup.send("DISCORD_PREPARE_TOKEN が設定されていません。")
        return

    try:
        _status, body = await reset_eta_metrics()
    except PrepareApiError as error:
        await interaction.followup.send(f"予想時間リセットAPIエラー ({error.status_code}): {error.detail}")
        return

    await interaction.followup.send(
        body.get("message", "予想時間の学習データをリセットしました。"),
    )


@client.tree.command(name="webui-key", description="Web UI 一時利用者向けの期限付き準備キーを発行します")
@app_commands.describe(
    days="有効日数。JSTで期限日の終わりまで有効です",
)
async def webui_key_command(
    interaction: discord.Interaction,
    days: int = 1,
) -> None:
    await interaction.response.defer(thinking=True, ephemeral=True)

    try:
        body = make_webui_temp_key(days)
    except (RuntimeError, ValueError) as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

    await interaction.followup.send(
        (
            "Web UI用の一時キーを発行しました。\n"
            f"有効期限: <t:{body['expires_at']}:F> まで\n"
            "```text\n"
            f"{body['key']}\n"
            "```"
        ),
        ephemeral=True,
    )


def main() -> None:
    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    client.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
