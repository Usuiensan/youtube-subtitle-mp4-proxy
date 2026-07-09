from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import discord
from discord import app_commands


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
ENV_FILE = Path(__file__).resolve().parent.parent / ".env.local"


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
    poll_seconds = int(os.getenv("DISCORD_PREPARE_POLL_SECONDS", "10"))
    poll_timeout_seconds = int(os.getenv("DISCORD_PREPARE_POLL_TIMEOUT_SECONDS", "7200"))


settings = Settings()


class PrepareApiError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


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


async def prepare_video(video_id: str, lang: str, mode: str, discord_user_id: int) -> tuple[int, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "mode": mode,
            "discordUserId": str(discord_user_id),
        }
    )
    url = f"{settings.youtube_proxy_base_url}/prepare/youtube/{video_id}/{lang}?{query}"
    return await asyncio.to_thread(http_json, "POST", url)


async def fetch_job(status_url: str) -> dict[str, Any]:
    _status, body = await asyncio.to_thread(http_json, "GET", status_url)
    return body


def eta_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    eta_seconds = body.get("eta_seconds")
    if isinstance(eta_seconds, (int, float)) and eta_seconds > 0:
        minutes = max(1, round(eta_seconds / 60))
        parts.append(f"予想{minutes}分")

    estimated_ready_at = body.get("estimated_ready_at")
    if isinstance(estimated_ready_at, (int, float)) and estimated_ready_at > 0:
        parts.append(f"終了予想 <t:{int(estimated_ready_at)}:t>")

    return " / ".join(parts) if parts else "終了予想を計算中"


def status_message(body: dict[str, Any]) -> str:
    status = body.get("status", "unknown")
    mode = body.get("mode", "mp4")
    title = body.get("title")
    title_part = f"\n{title}" if title else ""

    if status == "ready":
        return f"準備できました。{title_part}\n{body.get('url')}"
    if status == "failed":
        return f"準備に失敗しました。{title_part}\n{body.get('error', 'unknown error')}"
    return f"{mode.upper()}を準備しています。{eta_text(body)}{title_part}"


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

    while time.monotonic() < deadline:
        await asyncio.sleep(settings.poll_seconds)
        try:
            latest = await fetch_job(status_url)
        except Exception:
            continue
        if latest.get("status") in {"ready", "failed"}:
            notification = latest.get("notification") or {}
            content = notification.get("content") or status_message(latest)
            await send_notification(content)
            return

        if latest:
            try:
                await interaction.edit_original_response(content=status_message(latest))
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
    url="YouTube URLまたは動画ID",
    lang="字幕言語",
    mode="出力形式",
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

    selected_mode = mode.value if mode else "mp4"
    try:
        _status, body = await prepare_video(video_id, lang, selected_mode, interaction.user.id)
    except PrepareApiError as error:
        await interaction.followup.send(f"準備APIエラー ({error.status_code}): {error.detail}")
        return

    await interaction.followup.send(status_message(body))

    status_url = body.get("status_url")
    if body.get("status") in {"queued", "running"} and isinstance(status_url, str):
        asyncio.create_task(notify_when_done(interaction, status_url))


def main() -> None:
    if not settings.discord_bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured")
    client.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
