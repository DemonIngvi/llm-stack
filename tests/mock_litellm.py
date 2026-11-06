"""In-memory stand-in for the LiteLLM admin API used by add-model.sh and deploy.sh.

Usage: python3 mock_litellm.py PORT MASTER_KEY REQUEST_LOG
Every accepted request body is appended to REQUEST_LOG as one JSON line.
"""

import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, MASTER_KEY, REQUEST_LOG = int(sys.argv[1]), sys.argv[2], sys.argv[3]
MODELS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Authorization") != f"Bearer {MASTER_KEY}":
            self.reply(401, {"error": "bad key"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health/liveliness":
            return self.reply(200, "ok")
        if not self.authorized():
            return
        if self.path == "/model/info":
            return self.reply(200, {"data": MODELS})
        self.reply(404, {"error": self.path})

    def do_POST(self):
        if not self.authorized():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with open(REQUEST_LOG, "a") as log:
            log.write(json.dumps({"path": self.path, "body": body}) + "\n")
        if self.path == "/model/new":
            info = {"id": str(uuid.uuid4())}
            if body["litellm_params"]["model"].startswith("deepseek/"):
                info.update(max_input_tokens=1000000, max_output_tokens=384000)
            MODELS.append({**body, "model_info": info})
            return self.reply(200, {"model_id": info["id"]})
        if self.path == "/model/delete":
            MODELS[:] = [m for m in MODELS if m["model_info"]["id"] != body["id"]]
            return self.reply(200, {"message": "deleted"})
        if self.path == "/key/generate":
            return self.reply(200, {"key": "sk-mock-" + body.get("key_alias", "key")})
        self.reply(404, {"error": self.path})


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
