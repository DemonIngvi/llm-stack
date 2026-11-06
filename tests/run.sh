#!/usr/bin/env bash
# Checks add-model.sh and the dsh model sync against a mocked LiteLLM.
# Needs bash, curl and python3; no Docker and no real models.
#
#   bash tests/run.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
PORT=$((20000 + RANDOM % 20000))
LOG="$WORK/requests.jsonl"
FAILED=0

export STACK_HOME="$WORK/stack" LITELLM_URL="http://127.0.0.1:$PORT" LITELLM_MASTER_KEY=test-master
mkdir -p "$STACK_HOME"
printf 'LITELLM_MASTER_KEY=test-master\nWINDOWS_HOST_IP=172.28.224.1\n' > "$STACK_HOME/.env"
PATCH="$STACK_HOME/dsh-home/profiles/web/cordis.patch.yml"

python3 "$ROOT/tests/mock_litellm.py" "$PORT" test-master "$LOG" &
MOCK=$!
trap 'kill $MOCK 2>/dev/null; rm -rf "$WORK"' EXIT
until curl -fs -o /dev/null "$LITELLM_URL/health/liveliness"; do sleep 0.1; done

check() {
  local title="$1"; shift
  if "$@"; then echo "ok    $title"; else echo "FAIL  $title"; FAILED=1; fi
}

# Asserts on the last request sent to PATH: last_body PATH PYTHON_EXPR
last_body() {
  python3 - "$LOG" "$1" "$2" <<'EOF'
import json, sys
log, path, expr = sys.argv[1:]
bodies = [r["body"] for r in map(json.loads, open(log)) if r["path"] == path]
sys.exit(0 if bodies and eval(expr, {"b": bodies[-1]}) else 1)
EOF
}

add() { bash "$ROOT/add-model.sh" "$@" >/dev/null; }

add ollama qwen3:8b
check "ollama: адрес Windows-хоста и без ключа" last_body /model/new \
  'b["litellm_params"] == {"model": "ollama_chat/qwen3:8b", "api_base": "http://windows-host:11434"}'

add ollama llama3.2 --api-base http://host.docker.internal:11434 --name local-llama
check "ollama: свой адрес и имя" last_body /model/new \
  'b["model_name"] == "local-llama" and b["litellm_params"]["api_base"] == "http://host.docker.internal:11434"'

MODEL_API_KEY=sk-test-deepseek add deepseek deepseek-v4-flash
check "deepseek: ключ передан в запросе" last_body /model/new \
  'b["litellm_params"] == {"model": "deepseek/deepseek-v4-flash", "api_key": "sk-test-deepseek"}'

MODEL_API_KEY=sk-or add openrouter qwen/qwen3-coder --name coder
check "openrouter: префикс провайдера" last_body /model/new 'b["litellm_params"]["model"] == "openrouter/qwen/qwen3-coder"'

MODEL_API_KEY=k add compatible my-model --api-base https://gw.example/v1
check "compatible: OpenAI-протокол и адрес" last_body /model/new \
  'b["litellm_params"] == {"model": "openai/my-model", "api_base": "https://gw.example/v1", "api_key": "k"}'

check "compatible без --api-base отклоняется" bash -c '! MODEL_API_KEY=k bash "$0/add-model.sh" compatible x >/dev/null 2>&1' "$ROOT"

add chatgpt gpt-5.5 --no-login
check "chatgpt: подписка без ключа" last_body /model/new 'b["litellm_params"] == {"model": "chatgpt/gpt-5.5"}'

add mock
check "mock: готовый ответ без провайдера" last_body /model/new 'b["litellm_params"].get("mock_response")'

check "ключ не сохранён в файлах стека" bash -c '! grep -rq sk-test-deepseek "$0"' "$STACK_HOME"

check "dsh: файл настроек создан" test -f "$PATCH"
check "dsh: настройки для моста (профиль sdk) тоже" cmp -s "$PATCH" "${PATCH%/web/*}/sdk/cordis.patch.yml"
check "dsh: все модели и маршрут на LiteLLM" python3 - "$PATCH" <<'EOF'
import json, sys
text = open(sys.argv[1]).read()
route = json.loads(text[text.index("["):])[0]["config"]["providers"]["litellm"]
ids = [m["id"] for m in route["models"]]
assert route["baseURL"] == "http://litellm:4000/v1" and route["apiKeyEnv"] == "LITELLM_API_KEY"
assert ids == ["qwen3:8b", "local-llama", "deepseek-v4-flash", "coder", "my-model", "gpt-5.5", "mock"], ids
ds = route["models"][2]
assert ds["compat"] == {"thinkingFormat": "deepseek"} and ds["contextWindow"] == 1000000
EOF

bash "$ROOT/add-model.sh" remove coder >/dev/null
check "remove: модель исчезла из dsh" bash -c '! grep -q "\"coder\"" "$0"' "$PATCH"

echo "# правка в dsh" >> "$PATCH"
add mock --name mock-2
check "mock: имя без модели" last_body /model/new 'b["model_name"] == "mock-2"'
check "dsh: правки в dsh не затираются" bash -c 'grep -q "правка в dsh" "$0" && ! grep -q mock-2 "$0"' "$PATCH"

check "list показывает модели" bash -c 'bash "$0/add-model.sh" list | grep -q "deepseek/deepseek-v4-flash"' "$ROOT"

[ "$FAILED" = 0 ] && echo "Все проверки пройдены" || { echo "Есть ошибки"; exit 1; }
