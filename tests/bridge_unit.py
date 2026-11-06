"""Bridge logic that the end-to-end test cannot reach: approvals and user questions.

dsh asks them only under some permission policies, so a fake runtime stands in.
Run: python3 tests/bridge_unit.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "dsh" / "bridge"))
import bridge  # noqa: E402

FAILED = []


def check(title, condition, detail=""):
    print(("ok    " if condition else "FAIL  ") + title + ("" if condition else f"\n      {detail}"))
    if not condition:
        FAILED.append(title)


class FakeRuntime:
    def __init__(self):
        self.responses, self.prompts = [], []

    def alive(self):
        return True

    def respond(self, request_id, result):
        self.responses.append((request_id, result))

    def request(self, method, params=None, timeout=60):
        self.prompts.append(params["contentBlocks"][0]["text"])


def conversation(interactive=True):
    conv = bridge.Conversation("c-1", interactive)
    conv.runtime, conv.model, conv.running = FakeRuntime(), "m", True
    return conv


def drain(conv):
    text = "".join(conv.out)
    conv.out.clear()
    return text


conv = conversation()
conv.on_message("approval/request", {"sessionId": conv.session, "toolName": "bash", "reason": "rm -rf build"}, 7)
shown = drain(conv)
check("подтверждение показано в чате", "Подтвердите действие" in shown and "rm -rf build" in shown, shown)
check("поток останавливается на вопросе", conv.question is not None)
conv.answer("Да", "m", None)
check("«да» → allowed-once", conv.runtime.responses[-1] == (7, {"outcome": "allowed-once"}))

conv.on_message("approval/request", {"sessionId": conv.session, "toolName": "bash"}, 8)
conv.answer("нет", "m", None)
check("«нет» → rejected без новой задачи",
      conv.runtime.responses[-1] == (8, {"outcome": "rejected"}) and not conv.runtime.prompts)

conv.on_message("approval/request", {"sessionId": conv.session, "toolName": "bash"}, 9)
conv.answer("лучше удали только dist", "m", None)
check("другой ответ → отказ и новая инструкция агенту",
      conv.runtime.responses[-1] == (9, {"outcome": "rejected"}) and conv.runtime.prompts == ["лучше удали только dist"])

questions = [
    {"id": "q1", "question": "Какой фреймворк?", "options": [{"label": "pytest"}, {"label": "unittest"}]},
    {"id": "q2", "question": "Можно править CI?",
     "intent": {"kind": "approval", "approve": "Разрешить", "reject": "Запретить"}},
]
conv.on_message("user-question/ask", {"sessionId": conv.session, "requestId": "r1", "questions": questions}, 10)
shown = drain(conv)
check("варианты ответа пронумерованы", "1) pytest" in shown and "2) unittest" in shown, shown)
conv.answer("2\nда", "m", None)
check("номер → вариант, «да» → согласие", conv.runtime.responses[-1] == (10, {"answers": [
    {"id": "q1", "selected": ["unittest"]}, {"id": "q2", "selected": ["Разрешить"]}]}), str(conv.runtime.responses[-1]))

conv.on_message("user-question/ask", {"sessionId": conv.session, "requestId": "r2", "questions": questions[:1]}, 11)
conv.answer("pytest, но с плагином xdist", "m", None)
check("свой текст → custom", conv.runtime.responses[-1] == (11, {"answers": [
    {"id": "q1", "selected": [], "custom": "pytest, но с плагином xdist"}]}), str(conv.runtime.responses[-1]))

conv.on_message("user-question/ask", {"sessionId": conv.session, "requestId": "r3", "questions": questions[:1]}, 12)
conv.on_message("user-question.cancelled", {"sessionId": conv.session, "requestId": "r3"}, None)
check("снятый агентом вопрос больше не ждёт ответа", conv.question is None)

quiet = conversation(interactive=False)
quiet.on_message("approval/request", {"sessionId": quiet.session, "toolName": "bash"}, 20)
quiet.on_message("user-question/ask", {"sessionId": quiet.session, "requestId": "r", "questions": questions}, 21)
check("MCP-задачи: подтверждения отклоняются, вопросы снимаются",
      quiet.runtime.responses == [(20, {"outcome": "rejected"}), (21, {"cancelled": True})], str(quiet.runtime.responses))
check("MCP-задачи: статус говорит об отказе", "вопрос пользователю отклонён" in quiet.status(), quiet.status())

other = conversation()
other.on_message("session.event", {"sessionId": "чужая", "event": {"type": "tool/call", "data": {"name": "bash"}}}, None)
check("события подагентов не смешиваются с основной сессией", not other.out)

old = other.runtime
other.runtime = FakeRuntime()
other.on_exit(old)
check("завершение старого процесса не отвязывает новый", other.runtime is not None)

print("Все проверки пройдены" if not FAILED else f"Ошибок: {len(FAILED)}")
sys.exit(1 if FAILED else 0)
