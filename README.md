# 影视字幕本地化质检

仅使用 Python 标准库的字幕翻译、时间轴审核和交付服务。数据、判定和页面分开维护：

- `data.py`：SQLite 表结构、迁移与行级读写（数据层）。
- `judgment.py`：术语判定、遗留冲突检查、复核/交付状态机与修订谱系规则（判定层）。
- `app.py`：HTTP 路由与鉴权头解析（接口层）。
- `static/index.html`：页面，只通过 API 读写。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和术语修订 1。数据库默认 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改；旧结构数据库会在启动时自动迁移（旧术语表冻结为不可变修订 1）。

## 流程：术语维护 → 遗留检查 → 交付修订

### 1. 术语维护：每次保存都是一条不可变修订

- 术语表不再原地覆盖。`POST /api/projects/{id}/glossary` 每次保存都会复制当前全部条目并生成新的 `revision_no`，历史修订可随时查阅。
- 未交付版本（草稿/复核中/已批准/已锁定）一律按当前术语校验字幕。

### 2. 交付固定术语，后续改动不影响旧快照

- 交付时把当时的术语修订连同字幕一起写进确定性 SHA-256 快照（manifest 中的 `glossary_revision`），之后术语表再怎么改，旧快照字节不变，旧内容通过 `GET /api/deliveries/{id}` 查询。
- 已交付/已取代版本豁免术语冲突，也不能被覆盖或再编辑。

### 3. 遗留检查：冲突列出具体句子并挡住流转

- 术语改动后，所有未交付版本立即与新术语表比对；保存术语的响应里带 `affected_versions`，页面"遗留术语冲突"区逐条列出受影响的字幕序号、句子原文和原因。
- 只要存在冲突，`提交复核`、`复核通过`、`锁定`、`交付` 全部返回 409（错误体含 `conflicts` 明细）。复核中被术语变更追上的版本，复核通过会被自动退回草稿。
- 逐条改完冲突字幕才能继续。已批准/已锁定版本可用 `reopen` 退回草稿后修改。

### 4. 交付后的修正：从原版本另起修订

- 修订只能从 `delivered`/`superseded` 版本创建（`parent_id`），系统自动把旧字幕复制进新草稿，谱系根 `root_version_id` 不变。
- 同一来源（同一谱系根）只允许一条未完成记录（草稿/复核/已批准/已锁定）；存在未完成修订时不能再开新分支。
- 谱系通过 `GET /api/versions/{id}/lineage` 查询，每个节点标出状态、术语基线和对应交付快照。

字幕保存仍会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表（禁用译法直接阻止保存）。

## API

身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目。
- `POST /api/projects/{id}/versions`：创建版本；带 `parent_id` 即从已交付版本另起修订。
- `POST /api/projects/{id}/glossary`：新增一条不可变术语修订，响应含受影响的未交付版本。
- `GET /api/projects/{id}/glossary`：术语修订历史。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver|reopen`：状态流转；冲突时 409 并返回具体句子。
- `GET /api/versions/{id}/conflicts`：遗留术语冲突明细。
- `GET /api/versions/{id}/lineage`：修订谱系与交付快照。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`、`GET /api/deliveries/{id}`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：完整复核锁定交付、旧快照在术语修订后字节不变、遗留冲突列出句子并挡住提交/复核/交付、改完放行、交付后修订复制旧字幕、同一来源仅一条未完成记录、旧库迁移、时间轴重叠和权限。
