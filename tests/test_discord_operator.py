from bot import main


def test_discord_operator_requires_an_exact_configured_user_id(monkeypatch) -> None:
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "363466015683903488")
    assert main.is_discord_operator(363466015683903488) is True
    assert main.is_discord_operator(1) is False
    monkeypatch.setattr(main.settings, "discord_operator_user_id", "")
    assert main.is_discord_operator(363466015683903488) is False
