#!/bin/zsh
set -euo pipefail
umask 077

profile="${1:?profile required}"
case "$profile" in
  general|assistant|researcher|coder|writer|producer|marketing|health|finance) ;;
  *) print -u2 -- "Unsupported Hermes profile: $profile"; exit 64 ;;
esac

hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-def2cf0ca4e941cb9799a74786f1c8c4a41065d8/venv/bin/hermes"
if [[ "$profile" == "assistant" ]]; then
  hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-d25b0bc3d9e884ddde6550c6d1fd250e98b97a09-baseline-deps/venv/bin/hermes"
fi
if [[ "$profile" == "general" ]]; then
  hermes_executable="/Users/mutlupolatcan/.hermes/runtime/releases/hermes-agent-fd5d65e8b272480176f66d52907135500bdb5302/venv/bin/hermes"
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
