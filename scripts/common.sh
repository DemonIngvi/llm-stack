# Shared helpers for deploy.sh and add-model.sh. Source it, do not run it.

# Docker lives in WSL: on Windows rerun the calling script there with the same arguments.
handover_to_wsl() {
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) return 0 ;;
  esac
  if [ -z "${GH_TOKEN:-}" ] && command -v gh >/dev/null; then
    GH_TOKEN="$(gh auth token 2>/dev/null || true)"
  fi
  export GH_TOKEN="${GH_TOKEN:-}"
  export WSLENV="GH_TOKEN:STACK_HOME:REPO_URL:MODEL_API_KEY"
  export MSYS_NO_PATHCONV=1
  local distro="${WSL_DISTRO:-Ubuntu}" script
  script="$(wsl.exe -d "$distro" --exec wslpath -a "$(cygpath -am "$1")" | tr -d '\r')"
  shift
  exec wsl.exe -d "$distro" --exec bash "$script" "$@"
}

STACK_HOME="${STACK_HOME:-$HOME/llm-stack}"
ENV_FILE="$STACK_HOME/.env"
LITELLM_URL="${LITELLM_URL:-http://127.0.0.1:4000}"
DSH_PATCH="$STACK_HOME/dsh-home/profiles/web/cordis.patch.yml"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

env_value() { [ -f "$ENV_FILE" ] && grep "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

set_env_value() {
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

# Calls the LiteLLM admin API with the master key: litellm_admin PATH [curl args...]
litellm_admin() {
  local path="$1"; shift
  curl -fsS -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-$(env_value LITELLM_MASTER_KEY)}" \
    -H 'Content-Type: application/json' "$@" "$LITELLM_URL$path"
}

# Gives a service its own LiteLLM key once: ensure_virtual_key ENV_NAME ALIAS
ensure_virtual_key() {
  [ -n "$(env_value "$1")" ] && return 0
  local key
  key="$(litellm_admin /key/generate -X POST -d "{\"key_alias\":\"$2\"}" \
    | grep -o '"key":"sk-[^"]*"' | head -1 | cut -d'"' -f4)"
  [ -n "$key" ] || die "LiteLLM не выдал ключ для $2"
  set_env_value "$1" "$key"
}

# Writes LiteLLM's model list into dsh settings, unless they were edited in dsh.
sync_dsh_models() {
  local scripts_dir="$1"
  mkdir -p "$(dirname "$DSH_PATCH")"
  litellm_admin /model/info | python3 "$scripts_dir/sync_dsh_models.py" "$DSH_PATCH"
}
