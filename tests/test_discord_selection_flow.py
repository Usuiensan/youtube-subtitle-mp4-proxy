"""Discord component callbacks must acknowledge clicks and submit prepare jobs only once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import main


class FakeProgressMessage:
    def __init__(self) -> None:
        self.content = ""
        self.view = None
        self.edits: list[dict] = []

    async def edit(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = str(kwargs["content"])
        if "view" in kwargs:
            self.view = kwargs["view"]


class FakeResponse:
    def __init__(self, message: FakeProgressMessage) -> None:
        self.message = message
        self.edits: list[dict] = []
        self.deferred: list[dict] = []
        self.sent: list[dict] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def edit_message(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        await self.message.edit(**kwargs)

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.sent.append({"content": content, **kwargs})


def interaction(message: FakeProgressMessage) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=42),
        message=message,
        response=FakeResponse(message),
        channel=None,
    )


def test_subtitle_select_confirmed_only_once(monkeypatch) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def prepare(*_args: object, **_kwargs: object):
            entered.set()
            await finish.wait()
            return 200, {
                "status": "ready",
                "mode": "mp4",
                "video_id": "dQw4w9WgXcQ",
                "url": "https://example.test/video.mp4",
            }
        mocked = AsyncMock(side_effect=prepare)
        monkeypatch.setattr(main, "prepare_video", mocked)
        view = main.SubtitleChoiceView(
            requester_id=42,
            video_id="dQw4w9WgXcQ",
            lang="ja",
            mode="mp4",
            options_body={"title": "sample", "candidates": [{"language": "en", "name": "English"}]},
        )
        message = FakeProgressMessage()
        first = interaction(message)
        second = interaction(message)
        task = asyncio.create_task(view.start_prepare.callback(first))
        await entered.wait()
        assert len(first.response.deferred) == 1
        assert message.edits[0]["view"] is None
        assert "確定" in message.content
        await view.start_prepare.callback(second)
        assert len(second.response.sent) == 1
        assert second.response.sent[0]["ephemeral"] is True
        assert mocked.await_count == 1
        finish.set()
        await task
        assert mocked.await_count == 1
        assert "準備できました" in message.content
        assert message.view is None

    asyncio.run(scenario())


def test_intake_next_opens_single_subtitle_menu(monkeypatch) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def fetch(*_args: object):
            entered.set()
            await finish.wait()
            return {
                "title": "sample",
                "candidates": [{"language": "en", "name": "English"}],
            }
        mocked = AsyncMock(side_effect=fetch)
        monkeypatch.setattr(main, "fetch_subtitle_options", mocked)
        view = main.IntakePrepareView(requester_id=42, video_id="dQw4w9WgXcQ")
        message = FakeProgressMessage()
        first = interaction(message)
        second = interaction(message)
        task = asyncio.create_task(view.continue_prepare.callback(first))
        await entered.wait()
        assert len(first.response.deferred) == 1
        assert message.edits[0]["view"] is None
        assert "取得しています" in message.content
        await view.continue_prepare.callback(second)
        assert len(second.response.sent) == 1
        assert second.response.sent[0]["ephemeral"] is True
        assert mocked.await_count == 1
        finish.set()
        await task
        assert mocked.await_count == 1
        assert isinstance(message.view, main.SubtitleChoiceView)
        assert "翻訳元" in message.content

    asyncio.run(scenario())


def test_selection_change_acknowledges_and_displays_current_choice() -> None:
    async def scenario() -> None:
        view = main.SubtitleChoiceView(
            requester_id=42,
            video_id="dQw4w9WgXcQ",
            lang="ja",
            mode="mp4",
            options_body={
                "title": "Original video title",
                "candidates": [
                    {"language": "en", "name": "English"},
                    {"language": "es", "name": "Español"},
                ],
            },
        )
        view.source_select._values = ["es"]
        message = FakeProgressMessage()
        request = interaction(message)
        await view.on_source_selected(request)
        assert view.source_lang == "es"
        assert "Original video title" in message.content
        assert "スペイン語" in message.content
        assert view.source_select.options[1].default is True
        assert len(request.response.deferred) == 1

    asyncio.run(scenario())


def test_late_select_response_cannot_restore_menu_after_prepare(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            main, "prepare_video",
            AsyncMock(return_value=(200, {"status": "ready", "mode": "mp4", "video_id": "dQw4w9WgXcQ"})),
        )
        view = main.SubtitleChoiceView(
            requester_id=42,
            video_id="dQw4w9WgXcQ",
            lang="ja",
            mode="mp4",
            options_body={
                "title": "sample",
                "candidates": [
                    {"language": "en", "name": "English"},
                    {"language": "es", "name": "Español"},
                ],
            },
        )
        message = FakeProgressMessage()
        # Delay the selection edit until after the execute callback has claimed the view.
        await view._ui_lock.acquire()
        try:
            view.source_select._values = ["es"]
            selection = asyncio.create_task(view.on_source_selected(interaction(message)))
            await asyncio.sleep(0)
            execution = asyncio.create_task(view.start_prepare.callback(interaction(message)))
            await asyncio.sleep(0)
            assert view._busy is True
        finally:
            view._ui_lock.release()
        await asyncio.gather(selection, execution)
        assert message.view is None
        assert "準備できました" in message.content
        assert not any(item.get("view") is view for item in message.edits)

    asyncio.run(scenario())
