"""字幕本地化质检与交付服务。

分层维护：
- store.py    数据：SQLite 表结构、连接、审计写入
- services.py 判定：术语维护、遗留检查、复核交付状态机、交付修订
- server.py   接口：HTTP 路由
- static/     页面
"""
from .errors import DomainError
from .services import QCService, seed_demo
from .store import Database

__all__ = ["Database", "DomainError", "QCService", "seed_demo"]
