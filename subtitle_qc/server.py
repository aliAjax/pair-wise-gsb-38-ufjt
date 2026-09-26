"""HTTP 接口层：只负责路由、请求解析和响应，业务判定全在 services。"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .errors import DomainError
from .services import QCService, seed_demo
from .store import DEFAULT_DB, ROOT, Database


class Handler(BaseHTTPRequestHandler):
    service: QCService
    server_version = "SubtitleQC/2.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.service.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.service.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.service.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.service.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "versions"]:
                return self._send(self.service.version_detail(int(parts[2])))
            if len(parts) == 3 and parts[:2] == ["api", "deliveries"]:
                return self._send(self.service.delivery_detail(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.service.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.service.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "conflicts":
                return self._send(self.service.version_conflicts(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.service.glossary_current(int(parts[2])))
            if len(parts) == 5 and parts[:2] == ["api", "projects"] and parts[3:] == ["glossary", "history"]:
                return self._send({"history": self.service.glossary_history(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "conflicts":
                return self._send(self.service.project_conflicts(int(parts[2])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.service.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.service.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.service.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.service.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.service.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.service.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.service.review(int(parts[2]), actor, body, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "revisions":
                result = self.service.create_revision(int(parts[2]), actor, role)
                return self._send(result, 200 if result.get("existing") else 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "deliver", "reopen"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.service.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.service.lock(version_id, actor, role))
                if parts[3] == "reopen":
                    return self._send(self.service.reopen(version_id, actor, role))
                return self._send(self.service.deliver(version_id, actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    service = QCService(Database(args.db))
    if args.init:
        seed = seed_demo(service)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed.get('version', '-')}")
        return
    Handler.service = service
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()
