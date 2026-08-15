#!/bin/bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OLD_ENV=/etc/youtube-mp4-proxy.env
OPS_DIR=/etc/youtube-mp4-proxy
BACKUP_DIR=/var/backups/youtube-mp4-proxy
[[ $(id -u) -eq 0 ]] || { echo 'root権限で実行してください' >&2; exit 1; }
[[ -f "$OLD_ENV" ]] || { echo '既存のenvファイルが見つかりません' >&2; exit 1; }
getent group app >/dev/null || { echo 'appグループが見つかりません' >&2; exit 1; }

TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_DIR/env-$TS"
TMP=$(mktemp -d /tmp/youtube-proxy-provision.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
install -d -o root -g root -m 700 "$BACKUP" "$TMP"
install -o root -g root -m 600 "$OLD_ENV" "$BACKUP/legacy.env"
install -d -o root -g app -m 750 "$OPS_DIR"

awk -F= '
BEGIN {
  secret["DISCORD_BOT_TOKEN"]=1
  secret["DISCORD_PREPARE_TOKEN"]=1
  secret["WEBUI_TEMP_KEY_SECRET"]=1
  secret["TRANSLATION_AUDIT_API_TOKEN"]=1
  secret["TRANSLATION_CONFIG_API_TOKEN"]=1
  secret["GEMINI_API_KEY"]=1
  secret["OPENAI_API_KEY"]=1
  secret["GROQ_API_KEY"]=1
  secret["REMOTE_LLM_API_KEY"]=1
  secret["YOUTUBE_DATA_API_KEY"]=1
  secret["YTDLP_COOKIES_FILE"]=1
  secret["GOOGLE_APPLICATION_CREDENTIALS"]=1
}
{
  key=$1
  if (key ~ /^[[:space:]]*#/ || key !~ /^[A-Za-z_][A-Za-z0-9_]*$/) { print >> cfg; next }
  if (secret[key]) print >> sec
  else print >> cfg
}' cfg="$TMP/config.env" sec="$TMP/secrets.env" "$OLD_ENV"

if ! grep -q '^TRANSLATION_AUDIT_API_TOKEN=' "$TMP/secrets.env"; then
    printf 'TRANSLATION_AUDIT_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$TMP/secrets.env"
fi
if ! grep -q '^TRANSLATION_CONFIG_API_TOKEN=' "$TMP/secrets.env"; then
    printf 'TRANSLATION_CONFIG_API_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$TMP/secrets.env"
fi

install -o root -g app -m 660 "$TMP/config.env" "$OPS_DIR/config.env"
install -o root -g app -m 640 "$TMP/secrets.env" "$OPS_DIR/secrets.env"
install -d -o app -g app -m 755 /var/lib/youtube-mp4-proxy
install -o root -g root -m 755 "$ROOT_DIR/scripts/youtube-proxy-update" /usr/local/sbin/youtube-proxy-update
install -o root -g root -m 755 "$ROOT_DIR/scripts/youtube-proxy-audit" /usr/local/sbin/youtube-proxy-audit
install -o root -g root -m 755 "$ROOT_DIR/scripts/youtube-proxy-config" /usr/local/sbin/youtube-proxy-config
install -o root -g root -m 644 "$ROOT_DIR/deploy/youtube-mp4-proxy.service" /etc/systemd/system/youtube-mp4-proxy.service
install -o root -g root -m 644 "$ROOT_DIR/deploy/youtube-mp4-discord-bot.service" /etc/systemd/system/youtube-mp4-discord-bot.service

install -o root -g root -m 440 "$ROOT_DIR/deploy/youtube-proxy-sudoers" /etc/sudoers.d/youtube-proxy-deploy.new
visudo -cf /etc/sudoers.d/youtube-proxy-deploy.new >/dev/null
install -o root -g root -m 440 /etc/sudoers.d/youtube-proxy-deploy.new /etc/sudoers.d/youtube-proxy-deploy
rm -f /etc/sudoers.d/youtube-proxy-deploy.new

systemctl daemon-reload
systemctl restart youtube-mp4-proxy youtube-mp4-discord-bot
sleep 3
systemctl is-active --quiet youtube-mp4-proxy
systemctl is-active --quiet youtube-mp4-discord-bot
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
rm -f "$OLD_ENV"
if id -nG furukawa | tr ' ' '\n' | grep -qx sudo; then
    gpasswd -d furukawa sudo >/dev/null
fi
printf '本番運用基盤を配置しました。旧envバックアップ: %s\n' "$BACKUP/legacy.env"
