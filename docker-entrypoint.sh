#!/bin/sh
# Self-seed configuration when /config is ephemeral.
#
# If $LLM_SUMMARY_CONFIG does not exist, render it from the bundled template using
# environment variables. The app also runs fine with no config file at all
# (env > toml > defaults), so a failure to seed is non-fatal.
set -eu

CONFIG_PATH="${LLM_SUMMARY_CONFIG:-/config/config.toml}"
TEMPLATE_PATH="${LLM_SUMMARY_CONFIG_TEMPLATE:-/app/config/config.example.toml}"

if [ ! -f "$CONFIG_PATH" ] && [ -f "$TEMPLATE_PATH" ]; then
    CONFIG_DIR="$(dirname "$CONFIG_PATH")"
    if mkdir -p "$CONFIG_DIR" 2>/dev/null && touch "$CONFIG_PATH" 2>/dev/null; then
        echo "llm-summary: seeding config at $CONFIG_PATH from template"
        # Substitute ${VAR} placeholders from the environment; secrets are passed
        # through env and never echoed here.
        envsubst < "$TEMPLATE_PATH" > "$CONFIG_PATH" 2>/dev/null \
            || cp "$TEMPLATE_PATH" "$CONFIG_PATH"
    else
        echo "llm-summary: /config is read-only; relying on env + defaults" >&2
    fi
fi

exec python -m llm_summary.main "$@"
