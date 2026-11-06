"""Bridge between LibreChat and DeepSeek Harness (dsh).

LibreChat reaches dsh two ways:
- an OpenAI-compatible endpoint: every chat message goes straight into a dsh
  session, so no LibreChat-side model spends tokens on the handoff;
- an MCP server with three compact tools for LibreChat agents.

Each conversation owns one `dsh --profile sdk` process speaking JSON-RPC over
stdio. Work runs detached from HTTP requests: a request only watches it, so a
long task survives client timeouts and is picked up by the next message.
Only the standard library is used.
"""

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", "")
WORKSPACE = os.environ.get("DSH_WORKSPACE_DIR", "/workspace")
DSH_COMMAND = os.environ.get("DSH_COMMAND", "dsh --profile sdk").split()
PROVIDER = os.environ.get("DSH_PROVIDER", "litellm")
DSH_UI_URL = os.environ.get("DSH_UI_URL", "http://localhost:3081/")
DEFAULT_MODEL = os.environ.get("DSH_DEFAULT_MODEL", "")
PORT = int(os.environ.get("BRIDGE_PORT", "8000"))
IDLE_SECONDS = int(os.environ.get("BRIDGE_IDLE_SECONDS", "900"))
MAX_RUNTIMES = int(os.environ.get("BRIDGE_MAX_RUNTIMES", "4"))
KEEPALIVE_SECONDS = 10

YES = {"да", "д", "yes", "y", "ок", "ok", "+", "разрешаю", "можно", "approve"}
NO = {"нет", "н", "no", "n", "-", "отклонить", "нельзя", "reject"}
STOP = {"/stop", "стоп"}
STATUS = {"/status", "статус"}
HELP = (
    "Команды: **/status** — что делает агент, **/stop** — остановить работу.\n"
    "Пока агент работает, новое сообщение встаёт к нему в очередь.\n"
    f"Подробный вид сессий: [интерфейс dsh]({DSH_UI_URL})."
)
# Argument keys that best describe a tool call, in order of preference.
SUMMARY_KEYS = ("command", "path", "file_path", "pattern", "query", "url", "description")


def log(*parts):
    print("bridge:", *parts, file=sys.stderr, flush=True)


def list_models():
    request = urllib.request.Request(f"{LITELLM_URL}/v1/models",
                                     headers={"Authorization": f"Bearer {LITELLM_KEY}"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return [item["id"] for item in json.load(response).get("data", [])]


def default_model():
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    models = list_models()
    if not models:
        raise RuntimeError("в LiteLLM нет моделей: добавьте модель через add-model.sh")
    return models[0]


def clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_call(arguments):
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return clip(arguments, 100)
    if not isinstance(args, dict):
        return clip(args, 100)
    for key in SUMMARY_KEYS:
        if args.get(key):
            return clip(args[key], 100)
    return clip(json.dumps(args, ensure_ascii=False), 100)


def block_text(content, kind="text"):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if b.get("type") == kind)


class Runtime:
    """One dsh SDK process: requests go out, notifications and host requests come in."""

    def __init__(self, model, on_message, on_exit):
        self.model = model
        self.on_message = on_message
        self.on_exit = on_exit
        self.ids = itertools.count(1)
        self.waiters = {}
        self.write_lock = threading.Lock()
        self.proc = subprocess.Popen(
            DSH_COMMAND, cwd=WORKSPACE, text=True, bufsize=1,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request("initialize", {
            "cwd": WORKSPACE, "provider": PROVIDER, "model": model,
            "approvalRequests": True, "capabilities": {"userQuestions": True},
        }, timeout=180)

    def alive(self):
        return self.proc.poll() is None

    def _send(self, message):
        with self.write_lock:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def request(self, method, params=None, timeout=60):
        request_id = next(self.ids)
        waiter = {"done": threading.Event()}
        self.waiters[request_id] = waiter
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        if not waiter["done"].wait(timeout):
            self.waiters.pop(request_id, None)
            raise RuntimeError(f"dsh не ответил на {method}")
        if "error" in waiter:
            raise RuntimeError(waiter["error"].get("message", str(waiter["error"])))
        return waiter.get("result")

    def respond(self, request_id, result):
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _read(self):
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "method" in message:
                try:
                    self.on_message(message["method"], message.get("params") or {}, message.get("id"))
                except Exception as error:  # a rendering bug must not kill the reader
                    log("ошибка обработки", message.get("method"), error)
            elif message.get("id") in self.waiters:
                waiter = self.waiters.pop(message["id"])
                waiter.update({k: message[k] for k in ("result", "error") if k in message})
                waiter["done"].set()
        for waiter in list(self.waiters.values()):
            waiter["error"] = {"message": "процесс dsh завершился"}
            waiter["done"].set()
        self.on_exit(self)

    def _drain_stderr(self):
        for line in self.proc.stderr:
            log("dsh:", line.rstrip())

    def close(self):
        if self.alive():
            try:
                self.request("shutdown", timeout=10)
            except Exception:
                pass
        if self.alive():
            self.proc.kill()


class Conversation:
    """A LibreChat conversation (or MCP task) bound to one dsh session."""

    def __init__(self, conversation_id, interactive=True):
        self.id = conversation_id
        self.session = "lc-" + re.sub(r"[^a-zA-Z0-9-]", "", conversation_id)[:60]
        self.interactive = interactive
        self.runtime = None
        self.model = None
        self.cond = threading.Condition()
        self.out = []
        self.running = False
        self.question = None
        self.started = False
        self.steps = 0
        self.last_action = ""
        self.final = ""
        self.error = ""
        self.usage = {}
        self.touched = time.time()

    # Output -----------------------------------------------------------------

    def emit(self, text):
        with self.cond:
            if self.interactive:
                self.out.append(text)
            self.cond.notify_all()

    def watch(self, write):
        """Deliver output until the turn ends or waits for the user. False means the client left."""
        while True:
            with self.cond:
                if not self.out and self.running and self.question is None:
                    self.cond.wait(KEEPALIVE_SECONDS)
                chunk = "".join(self.out)
                self.out.clear()
                finished = not self.running or self.question is not None
            try:
                write(chunk)
            except OSError:
                with self.cond:
                    self.out.insert(0, chunk)
                return False
            if finished and not chunk:
                return True

    # dsh messages -----------------------------------------------------------

    def on_message(self, method, params, request_id):
        if method == "session.event":
            if params.get("sessionId") == self.session:
                self.render(params.get("event") or {})
        elif method == "session.status":
            if params.get("sessionId") == self.session:
                self.set_running(params.get("status") == "running")
        elif method == "approval/request":
            self.ask_approval(params, request_id)
        elif method == "user-question/ask":
            self.ask_questions(params, request_id)
        elif method == "user-question.cancelled":
            with self.cond:
                if self.question and self.question.get("request") == params.get("requestId"):
                    self.question = None
            self.emit("\n\n_Вопрос снят агентом._\n")
        elif method == "subagent.started" and params.get("parentSessionId") == self.session:
            self.emit("\n- ↳ запущен подагент")
        elif method == "subagent.finished" and params.get("parentSessionId") == self.session:
            self.emit(f"\n- ↳ подагент завершён: {params.get('status', '')}")

    def on_exit(self, runtime):
        with self.cond:
            if self.runtime is not runtime:
                return  # an old process replaced by a model switch or /stop
            was_running, self.running, self.question = self.running, False, None
            self.runtime = None
            self.cond.notify_all()
        if was_running:
            self.emit("\n\n⚠️ Процесс dsh завершился.\n")

    def render(self, event):
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "step/start":
            self.steps += 1
        elif kind == "tool/call":
            line = f"`{data.get('name', '?')}` {summarize_call(data.get('arguments'))}".strip()
            self.last_action = line
            self.emit(f"\n- ▸ {line}")
        elif kind == "tool/result":
            message = data.get("message") or {}
            if data.get("error") or message.get("isError"):
                reason = (data.get("error") or {}).get("reason") or block_text(message.get("content"))
                self.emit(f"\n  ✗ {clip(reason, 200)}")
        elif kind == "assistant/message":
            for key, value in (data.get("usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.usage[key] = self.usage.get(key, 0) + value
            text = block_text((data.get("message") or {}).get("content")).strip()
            if text:
                self.final = text
                self.emit(f"\n\n{text}\n")
        elif kind == "turn/end":
            reason = data.get("reason") or {}
            if reason.get("kind") not in (None, "completed"):
                detail = clip(json.dumps(reason.get("error") or reason, ensure_ascii=False), 300)
                self.error = f"{reason.get('kind')}: {detail}"
                self.emit(f"\n\n⚠️ Ход завершён: {self.error}\n")

    def set_running(self, running):
        with self.cond:
            was_running, self.running = self.running, running
            self.touched = time.time()
            self.cond.notify_all()
        if was_running and not running:
            used = ", ".join(f"{k} {int(v)}" for k, v in sorted(self.usage.items())) or "нет данных"
            self.emit(f"\n\n---\n_Готово · шагов: {self.steps} · токены dsh: {used}_\n")

    def ask_approval(self, params, request_id):
        tool = params.get("toolName", "?")
        if not self.interactive:
            self.runtime.respond(request_id, {"outcome": "rejected"})
            self.last_action = f"отклонено без подтверждения: {tool}"
            return
        with self.cond:
            self.question = {"kind": "approval", "id": request_id}
        reason = f": {params['reason']}" if params.get("reason") else ""
        self.emit(f"\n\n⚠️ **Подтвердите действие** `{tool}`{reason}\n\nОтветьте **да** или **нет**.\n")

    def ask_questions(self, params, request_id):
        items = params.get("questions") or []
        if not self.interactive:
            self.runtime.respond(request_id, {"cancelled": True})
            self.last_action = "вопрос пользователю отклонён"
            return
        with self.cond:
            self.question = {"kind": "questions", "id": request_id,
                             "request": params.get("requestId"), "items": items}
        lines = []
        for number, item in enumerate(items, 1):
            prefix = f"{number}. " if len(items) > 1 else ""
            lines.append(f"\n\n❓ {prefix}**{item.get('header') or item.get('question', '')}**")
            if item.get("header") and item.get("question"):
                lines.append(f"\n{item['question']}")
            if item.get("detail"):
                lines.append(f"\n\n{clip(item['detail'], 1500)}")
            intent = item.get("intent") or {}
            if intent.get("kind") == "approval":
                lines.append("\n\nОтветьте **да** или **нет**.")
            else:
                for index, option in enumerate(item.get("options") or [], 1):
                    note = f" — {option['description']}" if option.get("description") else ""
                    lines.append(f"\n{index}) {option['label']}{note}")
                lines.append("\n\nОтветьте номером варианта или своим текстом.")
        if len(items) > 1:
            lines.append("\nНа несколько вопросов отвечайте по строке на каждый.")
        self.emit("".join(lines) + "\n")

    # User actions -----------------------------------------------------------

    def ensure_runtime(self, model, bridge):
        if self.runtime and self.runtime.alive() and self.model == model:
            return
        if self.runtime and self.runtime.alive():
            if self.running:
                raise RuntimeError("агент ещё работает: дождитесь конца или отправьте /stop")
            self.runtime.close()
        bridge.make_room(self)
        self.runtime = Runtime(model, self.on_message, self.on_exit)
        self.model = model
        try:
            self.runtime.request("session/resume", {"sessionId": self.session}, timeout=60)
        except RuntimeError:
            pass  # a new session: the first prompt creates it

    def prompt(self, text, model, bridge):
        self.touched = time.time()
        self.ensure_runtime(model, bridge)
        if not self.started:
            self.started = True
            self.emit(f"**DeepSeek Harness** · модель `{model}` · [интерфейс dsh]({DSH_UI_URL})\n")
        with self.cond:
            self.running = True
            self.error = ""
        try:
            self.runtime.request("session/prompt", {
                "sessionId": self.session, "contentBlocks": [{"type": "text", "text": text}]})
        except RuntimeError as error:
            with self.cond:
                self.running = False
            self.emit(f"\n\n⚠️ dsh не принял задачу: {error}\n")

    def queue(self, text):
        self.runtime.request("session/prompt", {
            "sessionId": self.session, "contentBlocks": [{"type": "text", "text": text}]})
        self.emit("\n\n↪ _Сообщение добавлено в очередь агента._\n")

    def answer(self, text, model, bridge):
        with self.cond:
            question, self.question = self.question, None
        word = text.strip().lower().rstrip(".!")
        if question["kind"] == "approval":
            allowed = word in YES
            self.runtime.respond(question["id"], {"outcome": "allowed-once" if allowed else "rejected"})
            self.emit("✓ Разрешено.\n" if allowed else "✗ Отклонено.\n")
            if not allowed and word not in NO:
                self.queue(text)
            return
        answers = []
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()] or [""]
        for item, line in zip(question["items"], lines + [lines[-1]] * len(question["items"])):
            intent = item.get("intent") or {}
            options = [o["label"] for o in item.get("options") or []]
            low = line.lower().rstrip(".!")
            if intent.get("kind") == "approval":
                answers.append({"id": item["id"], "selected": [intent["approve"] if low in YES else intent["reject"]]})
            elif intent.get("kind") == "plan-review" and low in YES:
                answers.append({"id": item["id"], "selected": [intent["approve"]]})
            else:
                numbers = [int(n) for n in re.findall(r"\d+", line) if 0 < int(n) <= len(options)]
                if numbers and re.fullmatch(r"[\d\s,;]+", line):
                    answers.append({"id": item["id"], "selected": [options[n - 1] for n in numbers]})
                else:
                    answers.append({"id": item["id"], "selected": [], "custom": line})
        self.runtime.respond(question["id"], {"answers": answers})
        self.emit("✓ Ответ передан агенту.\n")

    def stop(self):
        # The protocol has no cancel: closing the process stops the turn, the session log stays.
        with self.cond:
            runtime, self.runtime = self.runtime, None
            self.running, self.question = False, None
            self.cond.notify_all()
        if runtime:
            runtime.proc.kill()
        self.emit("\n⏹ Остановлено. Сессия сохранена: следующее сообщение продолжит её.\n")

    def status(self):
        if self.question:
            state = "ждёт вашего ответа"
        elif self.running:
            state = "работает"
        else:
            state = "свободен"
        parts = [state, f"шагов: {self.steps}"]
        if self.model:
            parts.append(f"модель: {self.model}")
        if self.last_action:
            parts.append(f"последнее: {self.last_action}")
        if self.error:
            parts.append(f"ошибка: {self.error}")
        return " · ".join(parts)


class Bridge:
    def __init__(self):
        self.conversations = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reap, daemon=True).start()

    def get(self, conversation_id, interactive=True):
        with self.lock:
            if conversation_id not in self.conversations:
                self.conversations[conversation_id] = Conversation(conversation_id, interactive)
            return self.conversations[conversation_id]

    def make_room(self, wanted):
        with self.lock:
            live = [c for c in self.conversations.values()
                    if c is not wanted and c.runtime and c.runtime.alive()]
        if len(live) < MAX_RUNTIMES:
            return
        idle = sorted((c for c in live if not c.running and c.question is None), key=lambda c: c.touched)
        if not idle:
            raise RuntimeError(f"уже работают {len(live)} задачи dsh: дождитесь окончания одной из них")
        idle[0].runtime.close()

    def _reap(self):
        while True:
            time.sleep(60)
            with self.lock:
                conversations = list(self.conversations.values())
            for conversation in conversations:
                runtime = conversation.runtime
                if (runtime and runtime.alive() and not conversation.running and conversation.question is None
                        and time.time() - conversation.touched > IDLE_SECONDS):
                    runtime.close()


BRIDGE = Bridge()

MCP_TOOLS = [
    {
        "name": "dsh_task",
        "description": "Передать задачу агенту DeepSeek Harness: код, файлы и команды в его рабочей папке. "
                       "Сразу возвращает id задачи; ход проверяйте через dsh_status.",
        "inputSchema": {"type": "object", "required": ["task"], "properties": {
            "task": {"type": "string", "description": "Что сделать, с критерием готовности"},
            "model": {"type": "string", "description": "Модель из LiteLLM; по умолчанию первая"}}},
    },
    {
        "name": "dsh_status",
        "description": "Короткий статус задачи dsh одной строкой.",
        "inputSchema": {"type": "object", "required": ["task_id"],
                        "properties": {"task_id": {"type": "string"}}},
    },
    {
        "name": "dsh_result",
        "description": "Итоговый ответ агента dsh по задаче.",
        "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
            "task_id": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Предел длины ответа, по умолчанию 1500"}}},
    },
]


def call_tool(name, args):
    if name == "dsh_task":
        task_id = "mcp-" + uuid.uuid4().hex[:8]
        model = args.get("model") or default_model()
        BRIDGE.get(task_id, interactive=False).prompt(args["task"], model, BRIDGE)
        return f"{task_id} запущена, модель {model}. Действия, требующие подтверждения, будут отклонены."
    with BRIDGE.lock:
        conversation = BRIDGE.conversations.get(args.get("task_id", ""))
    if not conversation:
        raise RuntimeError("нет такой задачи")
    if name == "dsh_status":
        return conversation.status()
    if name == "dsh_result":
        if conversation.running:
            return "ещё выполняется: " + conversation.status()
        return clip(conversation.final or conversation.error or "ответа нет", int(args.get("max_chars") or 1500))
    raise RuntimeError(f"нет инструмента {name}")


def mcp_reply(message):
    if "id" not in message:
        return None
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "dsh-bridge", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": MCP_TOOLS}
    elif method == "tools/call":
        try:
            text, failed = call_tool(params.get("name"), params.get("arguments") or {}), False
        except Exception as error:
            text, failed = f"Ошибка: {error}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": f"нет метода {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def last_user_text(messages):
    for message in reversed(messages or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                return "".join(part.get("text", "") for part in content if part.get("type") == "text")
            return content or ""
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"status": "ok"})
        elif self.path == "/v1/models":
            try:
                models = list_models()
            except Exception as error:
                log("нет списка моделей", error)
                models = []
            self.send_json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "dsh"} for m in models]})
        else:
            self.send_json(405 if self.path == "/mcp" else 404, {"error": "not supported"})

    def do_POST(self):
        if self.path == "/mcp":
            body = self.read_json()
            replies = [r for r in map(mcp_reply, body if isinstance(body, list) else [body]) if r]
            if not replies:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_json(200, replies if isinstance(body, list) else replies[0])
        elif self.path == "/v1/chat/completions":
            self.chat(self.read_json())
        else:
            self.send_json(404, {"error": self.path})

    def conversation_id(self, body):
        given = (self.headers.get("X-Conversation-Id") or "").strip()
        if given and given not in ("new", "null", "undefined"):
            return given
        # Without an id every message would start a new session; pin it to the user and first message.
        first = next((m for m in body.get("messages", []) if m.get("role") == "user"), {})
        seed = (self.headers.get("X-User-Id") or "") + json.dumps(first.get("content"), ensure_ascii=False)
        return "anon-" + hashlib.sha256(seed.encode()).hexdigest()[:16]

    def chat(self, body):
        conversation = BRIDGE.get(self.conversation_id(body))
        text = last_user_text(body.get("messages")).strip()
        command = text.lower()
        notice = ""
        try:
            if command in STOP:
                conversation.stop()
            elif command in STATUS:
                notice = conversation.status()
            elif command in ("/help", "помощь"):
                notice = HELP
            elif conversation.question is not None:
                conversation.answer(text, body.get("model"), BRIDGE)
            elif conversation.running and conversation.runtime:
                conversation.queue(text)
            else:
                conversation.prompt(text, body.get("model") or default_model(), BRIDGE)
        except Exception as error:
            notice = f"⚠️ {error}"
        if body.get("stream"):
            self.stream(conversation, notice, body.get("model", "dsh"))
        else:
            parts = [notice] if notice else []
            conversation.watch(parts.append)
            self.send_json(200, {
                "id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
                "created": int(time.time()), "model": body.get("model", "dsh"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "".join(parts)}}]})

    def stream(self, conversation, notice, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk_id, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())

        def send(delta, finish=None):
            payload = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        try:
            send({"role": "assistant", "content": notice})
            # An empty delta doubles as a keepalive while the agent works.
            if conversation.watch(lambda text: send({"content": text})):
                send({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


def main():
    os.makedirs(WORKSPACE, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(f"слушаю :{PORT}, dsh: {' '.join(DSH_COMMAND)}, провайдер {PROVIDER}")
    server.serve_forever()


if __name__ == "__main__":
    main()
