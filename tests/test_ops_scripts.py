from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_ops_scripts_do_not_accept_or_print_tokens() -> None:
    for name in ("translation-audit.ps1", "production-config.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "TRANSLATION_AUDIT_API_TOKEN" not in text
        assert "TRANSLATION_CONFIG_API_TOKEN" not in text
        assert "?token" not in text.lower()


def test_root_wrappers_have_fixed_local_endpoints_and_services() -> None:
    audit = (ROOT / "scripts" / "youtube-proxy-audit").read_text(encoding="utf-8")
    config = (ROOT / "scripts" / "youtube-proxy-config").read_text(encoding="utf-8")
    assert "127.0.0.1:8000/translation-audit" in audit
    assert "127.0.0.1:8000/ops/config" in config
    assert "youtube-mp4-proxy" in config and "youtube-mp4-discord-bot" in config
    assert "--config -" in audit and "--config -" in config
