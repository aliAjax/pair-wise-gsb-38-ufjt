"""影视字幕本地化质检与交付服务入口。

数据、判定和页面分开维护：
- subtitle_qc/store.py     数据：SQLite 表结构与审计
- subtitle_qc/services.py  判定：术语维护、遗留检查、状态机、交付修订
- subtitle_qc/server.py    接口：HTTP 路由
- static/index.html        页面
"""
from subtitle_qc import Database, DomainError, QCService, seed_demo
from subtitle_qc.server import Handler, main

__all__ = ["Database", "DomainError", "QCService", "seed_demo", "Handler", "main"]

if __name__ == "__main__":
    main()
