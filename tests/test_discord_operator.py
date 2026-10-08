import asyncio

from bot import main


def test_discord_operator_requires_an_exact_configured_user_id(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "363466015683903488")
    assert main.is_discord_operator(363466015683903488) is True
    assert main.is_discord_operator(1) is False
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "")
    assert main.is_discord_operator(363466015683903488) is False


def test_intake_channel_requires_explicit_prepare_choices(monkeypatch) -> None:
    class Author:
        bot = False
        id = 42

    class ProgressMessage:
        def __init__(self, view=None) -> None:
            self.content = ""
            self.view = view

        async def edit(self, *, content: str) -> None:
            self.content = content

    class Channel:
        id = 123

        def __init__(self) -> None:
            self.messages = []

        async def send(self, content: str, **_kwargs):
            message = ProgressMessage(_kwargs.get("view"))
            message.content = content
            self.messages.append(message)
            return message

    class Message:
        guild = object()
        content = "https://youtu.be/dQw4w9WgXcQ"
        author = Author()

        def __init__(self) -> None:
            self.channel = Channel()

    monkeypatch.setattr(main.settings, "url_intake_channel_id", "123")
    monkeypatch.setattr(main.settings, "discord_prepare_token", "token")

    message = Message()
    asyncio.run(main.YoutubeProxyBot.__new__(main.YoutubeProxyBot).on_message(message))

    sent = message.channel.messages[0]
    assert sent.content == "配信方法を選択してください。字幕付きの場合は元言語と翻訳先も選択します。"
    assert isinstance(sent.view, main.IntakePrepareView)
    assert sent.view.mode == "direct"
    assert sent.view.mode_select.options[1].default is True


def test_direct_message_url_opens_prepare_choices(monkeypatch) -> None:
    class Author:
        bot = False
        id = 42

        def __init__(self) -> None:
            self.messages = []

        async def send(self, content: str, **kwargs):
            self.messages.append((content, kwargs.get("view")))

    class Channel:
        def __init__(self) -> None:
            self.messages = []

        async def send(self, content: str, **kwargs):
            self.messages.append((content, kwargs.get("view")))

    class Message:
        guild = None
        content = "https://youtu.be/dQw4w9WgXcQ"
        author = Author()

        def __init__(self) -> None:
            self.channel = Channel()

    monkeypatch.setattr(main.settings, "discord_prepare_token", "token")
    message = Message()

    asyncio.run(main.YoutubeProxyBot.__new__(main.YoutubeProxyBot).on_message(message))

    content, view = message.author.messages[0]
    assert content == "配信方法を選択してください。字幕付きの場合は元言語と翻訳先も選択します。"
    assert isinstance(view, main.IntakePrepareView)
    assert view.mode == "direct"


def test_subtitle_intake_uses_available_source_languages(monkeypatch) -> None:
    view = main.IntakePrepareView(requester_id=42, video_id="dQw4w9WgXcQ")
    view.mode = "mp4"

    class ProgressMessage:
        def __init__(self) -> None:
            self.content = ""
            self.view = None

        async def edit(self, *, content: str, view=None) -> None:
            self.content = content
            self.view = view

    class Response:
        async def defer(self, **_kwargs) -> None:
            pass

    class Interaction:
        user = type("User", (), {"id": 42})()
        response = Response()
        message = ProgressMessage()

    async def fetch_options(_video_id: str, _lang: str, _mode: str):
        return {
            "title": "video",
            "candidates": [
                {"language": "es", "name": "スペイン語"},
                {"language": "en", "name": "英語"},
            ],
        }

    monkeypatch.setattr(main, "fetch_subtitle_options", fetch_options)
    interaction = Interaction()

    asyncio.run(view.continue_prepare.callback(interaction))

    assert isinstance(interaction.message.view, main.SubtitleChoiceView)
    assert [option.value for option in interaction.message.view.source_select.options] == ["es", "en"]


def test_scan_days_result_does_not_call_count_cumulative() -> None:
    message = main.scan_result_message(30, 12, 3, 100, 0, False)

    assert "直近30日の動画: 12件" in message
    assert "累計" not in message
    assert "初回から部分走査" in message
    assert "注意" not in main.scan_result_message(30, 12, 3, 100, 0, True)


def test_extract_video_ids_from_text_deduplicates_links() -> None:
    content = (
        "https://youtu.be/dQw4w9WgXcQ "
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ and "
        "https://youtube.com/shorts/9bZkp7q19f0"
    )

    assert main.extract_video_ids_from_text(content) == {"dQw4w9WgXcQ", "9bZkp7q19f0"}
