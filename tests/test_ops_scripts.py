from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_ops_scripts_do_not_accept_or_print_tokens() -> None:
    for name in ("translation-audit.ps1", "production-config.ps1", "production-operator.ps1"):
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
    assert 'from app.ops_config import append_audit, validate_values, write_values' in config
    assert 'curl_config -X PUT' not in config
    assert '. "$SECRETS_FILE"' not in audit
    assert '. "$SECRETS_FILE"' not in config
    assert 'TRANSLATION_AUDIT_API_TOKEN"' in audit
    assert 'TRANSLATION_CONFIG_API_TOKEN"' in config


def test_update_script_excludes_local_env_and_checks_native_commands() -> None:
    text = (ROOT / "youtube-subtitles-update.ps1").read_text(encoding="utf-8")
    root_wrapper = (ROOT / "scripts" / "youtube-proxy-update").read_text(encoding="utf-8")
    assert "archive --format=tar" in text
    assert ":(exclude).env" in text
    assert "scpに失敗しました" in text
    assert "本番更新に失敗しました" in text
    assert '"$STAGE/scripts/youtube-proxy-config" /usr/local/sbin/youtube-proxy-config' in root_wrapper
    assert '"$STAGE/scripts/youtube-proxy-operator" /usr/local/sbin/youtube-proxy-operator' in root_wrapper


def test_storage_operator_uses_only_the_configured_mount_point() -> None:
    operator = (ROOT / "scripts" / "youtube-proxy-operator").read_text(encoding="utf-8")
    assert "CACHE_ARCHIVE_MOUNT_POINT が未設定" in operator
    assert 'umount "$ARCHIVE_MOUNT"' in operator
    assert "/prepare/storage-status" in operator
    assert "eval " not in operator


def test_provisioning_removes_legacy_app_env_only_after_backup() -> None:
    text = (ROOT / "scripts" / "provision-production-ops.sh").read_text(encoding="utf-8")
    assert 'install -o root -g root -m 600 "$APP_DIR/.env" "$BACKUP/app.env"' in text
    assert 'rm -f -- "$APP_DIR/.env"' in text
