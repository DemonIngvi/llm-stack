#!/usr/bin/env bash
# Adds a model to LiteLLM. LibreChat and dsh pick it up from there.
#
#   bash add-model.sh ollama     qwen3:8b                 local Ollama on this machine
#   bash add-model.sh deepseek   deepseek-v4-flash        API key, paid by tokens
#   bash add-model.sh openai     gpt-5.5                  same for openai, anthropic, gemini, openrouter
#   bash add-model.sh compatible my-model --api-base URL  any OpenAI-compatible API
#   bash add-model.sh chatgpt    gpt-5.5                  ChatGPT subscription, one-time sign-in
#   bash add-model.sh mock                                test model without keys
#   bash add-model.sh list
#   bash add-model.sh remove NAME
#
# Options: --name NAME (name shown in chats), --api-base URL, --no-login.
# The API key is asked without echo, or taken from MODEL_API_KEY.
set -euo pipefail

source "$(dirname "$0")/scripts/common.sh"
handover_to_wsl "$0" "$@"
SCRIPTS_DIR="$(cd "$(dirname "$0")/scripts" && pwd)"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

[ $# -ge 1 ] || usage 1
provider="$1"; shift
model=""
case "${1:-}" in
  ""|--*) ;;
  *) model="$1"; shift ;;
esac
name=""; api_base=""; login=1
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --api-base) api_base="$2"; shift 2 ;;
    --no-login) login=0; shift ;;
    -h|--help) usage ;;
    *) die "неизвестный параметр: $1" ;;
  esac
done

command -v python3 >/dev/null || die "не найден python3"
[ -n "${LITELLM_MASTER_KEY:-$(env_value LITELLM_MASTER_KEY)}" ] || die "нет $ENV_FILE: сначала запустите deploy.sh"

list_models() {
  litellm_admin /model/info | python3 -c '
import json, sys
for m in json.load(sys.stdin).get("data", []):
    p = m.get("litellm_params", {})
    print("  {:<28} {:<36} {}".format(m["model_name"], p.get("model", ""), p.get("api_base") or ""))'
}

remove_model() {
  local ids
  ids="$(litellm_admin /model/info | python3 -c '
import json, sys
for m in json.load(sys.stdin).get("data", []):
    if m["model_name"] == sys.argv[1]:
        print(m["model_info"]["id"])' "$1")"
  [ -n "$ids" ] || die "модели $1 нет в LiteLLM"
  for id in $ids; do
    litellm_admin /model/delete -X POST -d "{\"id\":\"$id\"}" >/dev/null
  done
  echo "Удалена: $1"
}

ask_key() {
  if [ -z "${MODEL_API_KEY:-}" ]; then
    read -r -s -p "API-ключ $provider: " MODEL_API_KEY; echo
  fi
  [ -n "$MODEL_API_KEY" ] || die "нужен API-ключ"
  export MODEL_API_KEY
}

# Default Ollama address: the Windows host when the stack runs in WSL, else the Docker host.
ollama_base() {
  if [ -n "$(env_value WINDOWS_HOST_IP)" ]; then
    echo "http://windows-host:11434"
  else
    echo "http://host.docker.internal:11434"
  fi
}

chatgpt_login() {
  [ -f "$STACK_HOME/repo/docker-compose.yml" ] || die "стек не развёрнут: сначала deploy.sh"
  echo "Вход в ChatGPT: откройте ссылку и введите код, который появится ниже."
  docker compose -f "$STACK_HOME/repo/docker-compose.yml" --env-file "$ENV_FILE" exec litellm \
    python3 -c 'from litellm.llms.chatgpt.authenticator import Authenticator; Authenticator().get_access_token(); print("Вход выполнен")'
}

case "$provider" in
  list) list_models; exit 0 ;;
  remove) [ -n "$model" ] || usage 1; remove_model "$model"; sync_dsh_models "$SCRIPTS_DIR" || true; exit 0 ;;
  -h|--help|help) usage ;;
esac

target=""; needs_key=0
case "$provider" in
  ollama)     target="ollama_chat/$model"; api_base="${api_base:-$(ollama_base)}" ;;
  deepseek)   target="deepseek/$model"; needs_key=1 ;;
  openai|anthropic|gemini|openrouter) target="$provider/$model"; needs_key=1 ;;
  compatible) target="openai/$model"; needs_key=1
              [ -n "$api_base" ] || die "для compatible нужен --api-base" ;;
  chatgpt)    target="chatgpt/$model" ;;
  mock)       model="${model:-mock}"; target="openai/$model" ;;
  *) usage 1 ;;
esac
[ -n "$model" ] || usage 1
name="${name:-$model}"
[ "$needs_key" = 1 ] && ask_key

# The key travels in the request body through stdin, never in arguments or files.
NAME="$name" TARGET="$target" API_BASE="$api_base" PROVIDER="$provider" python3 -c '
import json, os
params = {"model": os.environ["TARGET"]}
if os.environ.get("API_BASE"):
    params["api_base"] = os.environ["API_BASE"]
if os.environ.get("MODEL_API_KEY"):
    params["api_key"] = os.environ["MODEL_API_KEY"]
if os.environ["PROVIDER"] == "mock":
    params.update(api_key="mock", mock_response="Ответ тестовой модели: цепочка работает.")
print(json.dumps({"model_name": os.environ["NAME"], "litellm_params": params}))' \
  | litellm_admin /model/new -X POST --data @- >/dev/null

echo "Добавлена: $name → $target${api_base:+ ($api_base)}"
sync_dsh_models "$SCRIPTS_DIR" || true

if [ "$provider" = chatgpt ] && [ "$login" = 1 ]; then
  chatgpt_login
fi
echo "LibreChat покажет модель после обновления страницы."
