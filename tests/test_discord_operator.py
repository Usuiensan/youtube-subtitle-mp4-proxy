import asyncio

from bot import main


def test_discord_operator_requires_an_exact_configured_user_id(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "363466015683903488")
    assert main.is_discord_operator(363466015683903488) is True
    assert main.is_discord_operator(1) is False
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "")
    assert main.is_discord_operator(363466015683903488) is False


def test_intake_channel_starts_prepare_without_a_command(monkeypatch) -> None:
    class Author:
        bot = False
        id = 42

    class ProgressMessage:
        def __init__(self) -> None:
            self.content = ""

        async def edit(self, *, content: str) -> None:
            self.content = content

    class Channel:
        id = 123

        def __init__(self) -> None:
            self.messages = []

        async def send(self, content: str, **_kwargs):
            message = ProgressMessage()
            message.content = content
            self.messages.append(message)
            return message

    class Message:
        guild = object()
        content = "https://youtu.be/dQw4w9WgXcQ"
        author = Author()

        def __init__(self) -> None:
            self.channel = Channel()

    prepared = {}

    async def fetch_options(*_args):
        return {
            "title": "動画",
            "requires_choice": True,
            "candidates": [{"language": "ko"}],
        }

    async def prepare(*args, **kwargs):
        prepared["args"] = args
        prepared["kwargs"] = kwargs
        return 202, {"status": "queued"}

    monkeypatch.setattr(main.settings, "url_intake_channel_id", "123")
    monkeypatch.setattr(main.settings, "discord_prepare_token", "token")
    monkeypatch.setattr(main, "fetch_subtitle_options", fetch_options)
    monkeypatch.setattr(main, "prepare_video", prepare)
    monkeypatch.setattr(main, "status_message", lambda *_args: "準備開始")

    message = Message()
    asyncio.run(main.YoutubeProxyBot.__new__(main.YoutubeProxyBot).on_message(message))

    assert prepared["args"] == ("dQw4w9WgXcQ", "ja", "mp4", 42)
    assert prepared["kwargs"]["subtitle_source_lang"] == "ko"
    assert message.channel.messages[0].content == "準備開始"
