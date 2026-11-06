"""End-to-end check of the LibreChat ↔ dsh bridge with a real dsh runtime and a scripted model.

Runs inside the llm-stack/dsh image (see tests/bridge.sh): starts the mock
model, writes the dsh provider route, starts the bridge and drives it the way
LibreChat does — over the OpenAI streaming endpoint and over MCP.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = pathlib.Path(tempfile.mkdtemp())
MOCK_PORT, BRIDGE_PORT = 9100, 9200
BRIDGE = f"http://127.0.0.1:{BRIDGE_PORT}"
MOCK_LOG = WORK / "mock.jsonl"
WORKSPACE = WORK / "workspace"
FAILED = []


def check(title, condition, detail=""):
    print(("ok    " if condition else "FAIL  ") + title + ("" if condition else f"\n      {detail[:1500]}"))
    if not condition:
        FAILED.append(title)


def post(path, body, headers=None, timeout=240):
    request = urllib.request.Request(BRIDGE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout)


def chat(conversation, text, model="mock-dsh"):
    """Sends one LibreChat-style message and returns the streamed text and whether it finished."""
    body = {"model": model, "stream": True,
            "messages": [{"role": "system", "content": "LibreChat instructions"},
                         {"role": "user", "content": text}]}
    parts, finished = [], False
    with post("/v1/chat/completions", body, {"X-Conversation-Id": conversation}) as response:
        for line in response:
            line = line.decode().strip()
            if line == "data: [DONE]":
                finished = True
            elif line.startswith("data: "):
                for choice in json.loads(line[6:])["choices"]:
                    parts.append(choice["delta"].get("content", ""))
    return "".join(parts), finished


def mcp(method, params=None, request_id=1):
    with post("/mcp", {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) as r:
        return json.load(r)["result"]


def tool(name, **arguments):
    return mcp("tools/call", {"name": name, "arguments": arguments})["content"][0]["text"]


def wait_http(url, seconds=60):
    for _ in range(seconds * 5):
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.2)
    raise SystemExit(f"не поднялся {url}")


def main():
    home = WORK / "dsh-home"
    WORKSPACE.mkdir()
    env = dict(os.environ, DSH_HOME=str(home), LITELLM_API_KEY="mock-key",
               LITELLM_URL=f"http://127.0.0.1:{MOCK_PORT}", DSH_WORKSPACE_DIR=str(WORKSPACE),
               BRIDGE_PORT=str(BRIDGE_PORT), DSH_ROUTE_BASE_URL=f"http://127.0.0.1:{MOCK_PORT}/v1")
    model_info = json.dumps({"data": [{"model_name": "mock-dsh", "litellm_params": {"model": "openai/mock"}}]})
    subprocess.run([sys.executable, ROOT / "scripts/sync_dsh_models.py",
                    home / "profiles/sdk/cordis.patch.yml"], input=model_info, text=True, env=env, check=True)

    processes = [
        subprocess.Popen([sys.executable, ROOT / "tests/mock_llm.py", str(MOCK_PORT), MOCK_LOG]),
        subprocess.Popen([sys.executable, ROOT / "dsh/bridge/bridge.py"], env=env,
                         stderr=open(WORK / "bridge.log", "w")),
    ]
    try:
        wait_http(f"http://127.0.0.1:{MOCK_PORT}/v1/models")
        wait_http(BRIDGE + "/health")
        run_checks()
    finally:
        for process in processes:
            process.kill()
        if FAILED:
            print("---- bridge.log (хвост)")
            print("".join((WORK / "bridge.log").read_text().splitlines(True)[-40:]))
    print("Все проверки пройдены" if not FAILED else f"Ошибок: {len(FAILED)}")
    sys.exit(1 if FAILED else 0)


def run_checks():
    with urllib.request.urlopen(BRIDGE + "/v1/models") as r:
        check("список моделей берётся из LiteLLM", [m["id"] for m in json.load(r)["data"]] == ["mock-dsh"])

    text, finished = chat("conv-1", "Создай файл bridge.txt")
    approvals = 0
    while "Подтвердите" in text and approvals < 3:
        approvals += 1
        more, finished = chat("conv-1", "да")
        text += more
    check("задача дошла до dsh и вызвала bash", "▸ `bash`" in text, text)
    check("ответ модели пришёл в чат", "Готово: bridge-ok" in text, text)
    check("поток закрылся по окончании работы", finished, text)
    check("команда выполнена в рабочей папке", (WORKSPACE / "bridge.txt").read_text().strip() == "bridge-ok")
    check("итог с шагами и токенами", "шагов:" in text and "токены dsh" in text, text)
    print(f"      подтверждений запрошено: {approvals}")

    status, _ = chat("conv-1", "/status")
    check("/status отвечает без запуска модели", status.startswith("свободен"), status)

    calls_before = len(MOCK_LOG.read_text().splitlines())
    text, _ = chat("conv-1", "Ещё раз")
    while "Подтвердите" in text:
        more, _ = chat("conv-1", "да")
        text += more
    history = [json.loads(line) for line in MOCK_LOG.read_text().splitlines()][calls_before:]
    check("вторая задача продолжает ту же сессию dsh", history and history[0]["count"] > 3, str(history))
    check("в dsh ушло только новое сообщение, без системной подсказки LibreChat",
          "LibreChat instructions" not in json.dumps(history), str(history))

    result = {}
    worker = threading.Thread(target=lambda: result.update(zip(("text", "finished"), chat("conv-2", "долго"))))
    worker.start()
    for _ in range(100):
        stopped, _ = chat("conv-2", "/status")
        if "sleep 60" in stopped or "Подтвердите" in stopped:
            break
        time.sleep(0.5)
    stopped, _ = chat("conv-2", "/stop")
    worker.join(30)
    check("/stop останавливает работу", "Остановлено" in stopped and not worker.is_alive(), stopped)
    text, _ = chat("conv-2", "/status")
    check("после /stop агент свободен", text.startswith("свободен"), text)

    info = mcp("initialize", {"protocolVersion": "2025-06-18"})
    check("MCP: сервер отвечает", info["serverInfo"]["name"] == "dsh-bridge")
    names = [t["name"] for t in mcp("tools/list")["tools"]]
    check("MCP: три инструмента", names == ["dsh_task", "dsh_status", "dsh_result"], str(names))
    started = tool("dsh_task", task="Создай файл bridge.txt")
    task_id = started.split()[0]
    check("MCP: задача запущена коротким ответом", task_id.startswith("mcp-") and len(started) < 150, started)
    for _ in range(240):
        status = tool("dsh_status", task_id=task_id)
        if status.startswith("свободен"):
            break
        time.sleep(0.5)
    check("MCP: статус одной строкой", "\n" not in status and len(status) < 300, status)
    answer = tool("dsh_result", task_id=task_id)
    check("MCP: итог задачи", answer.startswith("Готово") or "отклонено" in status, f"{answer} | {status}")


if __name__ == "__main__":
    main()
