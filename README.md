# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表（含历史）、复核意见和交付快照。

术语维护、遗留检查和交付修订接成一条流程：术语每次修改都会推进术语版本并立即检查未交付版本；存在冲突的版本会被挡住提交、复核和交付；交付固定当时术语快照；交付后的修正从原版本另起修订。

## 结构（数据、判定、页面分开维护）

- `subtitle_qc/store.py` —— 数据层：SQLite 表结构、连接、审计写入。
- `subtitle_qc/services.py` —— 判定层：术语维护、遗留检查、复核交付状态机、交付修订。
- `subtitle_qc/server.py` —— 接口层：HTTP 路由。
- `static/index.html` —— 页面。
- `app.py` —— 入口，装配以上各层。

## 运行

```bash
python3 app.py --init
python3 app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。保存时按当前术语表校验，禁用译法直接阻止保存。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 术语每次修改都会写入 `glossary_history` 并推进项目的 `glossary_revision`，同时对所有未交付版本做遗留检查，返回受影响的具体句子。
6. 存在术语冲突的未交付版本会被挡住：不能提交复核、不能复核通过、不能交付，错误信息列出具体句子。退回（`reject` / `reopen`）不受限，改完才能继续。
7. 负责人锁定已批准版本，再执行交付。交付把当时的术语版本号和术语内容固定进 SHA-256 快照，后续术语改动不影响旧快照；同语言的新交付把旧版本标记为 `superseded`，旧快照不会删除或覆盖。
8. 交付后的修正：`POST /api/versions/{id}/revisions` 从原交付版本另起修订，自动复制字幕和人员分配；同一来源只保留一条未完成修订，重复发起返回已有记录。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本（同一来源只留一条未完成记录）。
- `POST /api/projects/{id}/glossary`：修改术语并推进术语版本，响应携带受影响版本和具体句子（`impacts`）。
- `GET /api/projects/{id}/glossary`：当前术语和术语版本号。
- `GET /api/projects/{id}/glossary/history`：术语修改历史。
- `GET /api/projects/{id}/conflicts`：全项目未交付版本的术语冲突。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`，响应携带该版本剩余冲突。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver|reopen`：复核交付状态机；冲突版本在提交、复核通过、交付处被挡。
- `POST /api/versions/{id}/revisions`：从已交付版本另起修订（复制字幕和人员；重复发起返回已有记录）。
- `GET /api/versions/{id}`：版本详情（含冲突、交付记录、下游修订）。
- `GET /api/versions/{id}/cues|comments|conflicts`：查看字幕、评论和具体冲突句子。
- `GET /api/deliveries`、`GET /api/deliveries/{id}`：交付列表和快照详情（含固定的术语版本）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用、人员权限、交付固定术语快照、遗留检查列出具体句子并挡住提交/复核/交付、交付后修订链和同一来源唯一未完成记录。
