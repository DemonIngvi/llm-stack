"""Scripted OpenAI-compatible model for bridge tests.

A user message makes it call the `bash` tool; a tool result makes it answer
with text. "долго" in the task asks for a slow command, to test /stop.
Usage: python3 mock_llm.py PORT REQUEST_LOG
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, REQUEST_LOG = int(sys.argv[1]), sys.argv[2]


def text_of(message):
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "mock-dsh", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = request.get("messages", [])
        with open(REQUEST_LOG, "a") as log:
            log.write(json.dumps({"count": len(messages), "last_role": messages[-1]["role"]}) + "\n")
        last = messages[-1]
        if last["role"] == "tool":
            delta = [{"content": "Готово: " + text_of(last).strip()[:80]}]
            finish = "stop"
        else:
            task = text_of(last)
            command = "sleep 60" if "долго" in task else "echo bridge-ok > bridge.txt && cat bridge.txt"
            arguments = json.dumps({"description": "проверка моста", "command": command})
            delta = [{"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                      "function": {"name": "bash", "arguments": arguments}}]}]
            finish = "tool_calls"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = [{"role": "assistant"}] + delta
        for index, piece in enumerate(chunks):
            done = finish if index == len(chunks) - 1 else None
            self.send_chunk({"choices": [{"index": 0, "delta": piece, "finish_reason": done}]})
        self.send_chunk({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                                  "total_tokens": 110}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def send_chunk(self, payload):
        payload.update(id="mock", object="chat.completion.chunk", created=0, model="mock-dsh")
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
