#!/bin/zsh
set -euo pipefail
umask 077

profile="${1:?profile required}"
case "$profile" in
  general|assistant|researcher|coder|writer|producer|marketing|health|finance) ;;
  *) print -u2 -- "Unsupported Hermes profile: $profile"; exit 64 ;;
esac

hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-6f80745da16cc84d3f2ee125e14c00e1d775d61d-coder/venv/bin/hermes"
if [[ "$profile" == "general" ]]; then
  hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-97c26fd50d7ebc644c7ee70a1bb9dcf9d59848bd-general/venv/bin/hermes"
elif [[ "$profile" == "assistant" || "$profile" == "researcher" || "$profile" == "writer" || "$profile" == "producer" || "$profile" == "marketing" || "$profile" == "health" || "$profile" == "finance" ]]; then
  hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-665e8a0206bae1d5b5ce19646eb024f05241dfeb-fleet-youtube/venv/bin/hermes"
elif [[ "$profile" == "coder" ]]; then
  hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-6f80745da16cc84d3f2ee125e14c00e1d775d61d-coder/venv/bin/hermes"
fi

service="com.polatcangames.hermes.op-service-account"
token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)
[[ -n "$token" ]] || { print -u2 -- "Missing 1Password service-account token for $profile"; exit 65; }

export OP_SERVICE_ACCOUNT_TOKEN="$token"
unset token VIRTUAL_ENV
export PATH="/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HERMES_HOME="/Users/mutlupolatcan/.hermes/profiles/$profile"

bootstrap_root="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap"
bootstrap_python="$bootstrap_root/venv/bin/python"
bootstrap_script="$bootstrap_root/hermes_gateway_sdk_bootstrap.py"
[[ -x "$bootstrap_python" && -x "$bootstrap_script" ]] || {
  print -u2 -- "Missing Hermes gateway SDK bootstrap runtime"
  exit 66
}

exec "$bootstrap_python" "$bootstrap_script" "$profile" \
  --hermes-executable "$hermes_executable"
