"""HTTP layer for subtitle localization quality-control and delivery.

Routing and serialization only. Data lives in ``data.py``; terminology and
state-machine judgments live in ``judgment.py``; pages live under ``static/``.
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from data import DEFAULT_DB, Database, DomainError, utcnow
from judgment import Service

ROOT = Path(__file__).resolve().parent


def seed_demo(db: Database) -> dict[str, int]:
    service = Service(db)
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = service.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en",
                                               "media_name": "polar.mp4", "media_sha256": "b" * 64,
                                               "duration_ms": 120000}, "owner")
    service.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹",
                                                  "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    version = service.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    service: Service
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: DomainError) -> None:
        payload: dict[str, Any] = {"error": str(exc)}
        if exc.details:
            payload.update(exc.details)
        self._send(payload, exc.status)

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
            svc = self.service
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            # /api/versions/{id}/cues|comments|conflicts|lineage
            if len(parts) == 4 and parts[:2] == ["api", "versions"]:
                version_id = int(parts[2])
                if parts[3] == "cues":
                    return self._send({"cues": self.db.list_cues(version_id)})
                if parts[3] == "comments":
                    return self._send({"comments": self.db.list_comments(version_id)})
                if parts[3] == "conflicts":
                    return self._send(svc.conflicts(version_id))
                if parts[3] == "lineage":
                    return self._send(svc.lineage(version_id))
            # /api/projects/{id}/glossary
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(svc.glossary(int(parts[2])))
            # /api/deliveries/{id}
            if len(parts) == 3 and parts[:2] == ["api", "deliveries"]:
                row = self.db.get_delivery(int(parts[2]))
                if not row:
                    raise DomainError("交付不存在", 404)
                payload = dict(row)
                payload["manifest"] = json.loads(row["manifest"])
                return self._send(payload)
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._error(exc if isinstance(exc, DomainError) else DomainError(str(exc), 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            svc = self.service
            status = 200
            if parts == ["api", "projects"]:
                result, status = svc.create_project(actor, body, role), 201
            elif len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                result, status = svc.create_version(int(parts[2]), actor, body, role), 201
            elif len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                result, status = svc.set_glossary(int(parts[2]), actor, body, role), 201
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                result = svc.assign(int(parts[2]), actor, body, role)
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                result = svc.save_cue(int(parts[2]), actor, body, role)
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                result = svc.add_comment(int(parts[2]), actor, body, role)
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {
                "submit", "lock", "deliver", "reopen"
            }:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    result = svc.submit(version_id, actor, role)
                elif parts[3] == "lock":
                    result = svc.lock(version_id, actor, role)
                elif parts[3] == "deliver":
                    result = svc.deliver(version_id, actor, role)
                else:
                    result = svc.reopen(version_id, actor, role)
            elif len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                result = svc.review(int(parts[2]), actor, body, role)
            else:
                raise DomainError("接口不存在", 404)
            self._send(result, status)
        except (ValueError, TypeError, DomainError) as exc:
            self._error(exc if isinstance(exc, DomainError) else DomainError(str(exc), 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args} at {utcnow()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    Handler.service = Service(db)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
