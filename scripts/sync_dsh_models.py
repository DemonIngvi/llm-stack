"""Turn LiteLLM /model/info (stdin) into a dsh provider route that points at LiteLLM.

The file is rewritten only while it still holds what this script wrote last time:
once dsh or a person edits it, the script leaves it alone.

Exit codes: 0 written or unchanged, 3 no models in LiteLLM, 4 edited in dsh.
"""

import hashlib
import json
import pathlib
import sys

HEADER = "# Managed by llm-stack: models from LiteLLM. Edit in dsh to take it over.\n"


def model_entry(item):
    name = item["model_name"]
    target = item.get("litellm_params", {}).get("model", "")
    info = item.get("model_info") or {}
    entry = {"id": name}
    if info.get("max_input_tokens"):
        entry["contextWindow"] = info["max_input_tokens"]
    if info.get("max_output_tokens"):
        entry["maxTokens"] = info["max_output_tokens"]
    if info.get("supports_vision"):
        entry["input"] = ["text", "image"]
    # DeepSeek V4 thinks unless told not to; dsh needs this switch behind a gateway.
    if target.startswith("deepseek/"):
        entry["compat"] = {"thinkingFormat": "deepseek"}
        entry["reasoningEfforts"] = {"off": None, "high": "high"}
    return entry


def build_patch(models):
    return [{
        "id": "llm-pi-ai",
        "config": {
            "providers": {
                "litellm": {
                    "api": "openai-completions",
                    "baseURL": "http://litellm:4000/v1",
                    "apiKeyEnv": "LITELLM_API_KEY",
                    "models": models,
                },
            },
        },
    }]


def main():
    target = pathlib.Path(sys.argv[1])
    marker = target.with_name(target.name + ".llm-stack.sha256")

    seen, models = set(), []
    for item in json.load(sys.stdin).get("data", []):
        if item["model_name"] not in seen:
            seen.add(item["model_name"])
            models.append(model_entry(item))
    if not models:
        print("  dsh: в LiteLLM пока нет моделей, настройки dsh не тронуты")
        return 3

    if target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if not marker.exists() or marker.read_text().strip() != current:
            print("  dsh: настройки изменены в dsh, модели не обновлены. "
                  "Обновите их в dsh: Settings → Models → litellm → Fetch available models")
            return 4

    text = HEADER + json.dumps(build_patch(models), ensure_ascii=False, indent=2) + "\n"
    target.write_text(text)
    marker.write_text(hashlib.sha256(text.encode()).hexdigest() + "\n")
    print(f"  dsh: моделей из LiteLLM: {len(models)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
