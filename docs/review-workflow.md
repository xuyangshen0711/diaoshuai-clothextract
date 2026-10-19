# 人工审核与 SQLite 存储

## 使用流程

1. 在「导入」上传 PDF、PPTX 或 Excel。抽图、AI 分类完成后进入「待审核」。
2. 打开「人工审核」：左侧选图，中间查看完整图片和来源，右侧对照 AI 建议调整标签。
3. 填写审核人姓名，选择「直接通过」「修改后通过」或「驳回」。驳回必须填写原因。
4. 通过后进入正式图库，可以文字搜索和以图搜索。待审核、已驳回的图片都不会进入图库或搜索结果。
5. 在「已通过」「已驳回」中查看处理记录，必要时「重新提交审核」。重新审核期间图片暂时移出图库。

AI 全量重打标和重复导入都会生成新的待审核建议，保留此前人工确认的标签。再次通过才会替换最终分类。每次新建议、通过、驳回、重新提交都会保存前后快照，历史面板可追溯标签、审核人、时间和意见。

## 存储与迁移

首次切换前先停止旧应用，备份 `data/`，再启动新版本，避免旧 JSON 写入程序与 SQLite 版本同时处理同一份资料。

- `data/processed/catalog.sqlite3`：图片记录、审核状态和追加保存的操作历史。SQLite 来自 Python 标准库，无需另装数据库服务。
- `data/raw/`、`data/extracted/`：原始资料和图片。
- `data/processed/index/`：FAISS 检索索引，只含已审核通过的图片；图片搜索时会自动校正索引成员，无需每次审批后手动重建。
- 首次访问数据库时，自动、事务性地导入现有 `catalog.json`，保留原 JSON 文件且只导入一次。历史记录全部标记为待审核，即使以前做过人工修改，也不会自动视为审核通过。
- `tags` 保留当前或上次人工结果；`ai_tags` 是最新模型建议；`proposed_tags` 是本轮待审核分类。历史快照保存历轮建议和决定。
- 每条记录都有版本号。审核或写入旧版本返回 HTTP 409，刷新后重新确认，避免并发操作静默覆盖。
- API 通过记录 ID 提供图片，不再公开整个 `data/` 目录，因此不会公开数据库和原始办公文档。

备份时停止应用，再复制整个 `data/` 目录。SQLite WAL 模式下不要在运行期间单独复制数据库主文件；可用 SQLite backup API 制作在线备份。

本期是一级审核，审核人姓名由操作人员填写，尚未接登录账号和角色权限。适合受信任的内部环境，不提供身份认证的审计保证。

## 启动与验证

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn api.main:app --host 127.0.0.1 --port 8520
# 另开终端
cd frontend
npm ci
npm run dev
```

打开 http://127.0.0.1:8518/review。无 API Key 时可选择 Mock 验证导入和审核流程；Mock 不代表真实 AI 分类效果。CLIP 不可用时会降级，文字搜索仅使用标签匹配。

```bash
.venv/bin/python -m pytest -q
cd frontend
npm run build
```

可用 `CLOTHEXTRACT_DATA_DIR=/absolute/path` 指定独立数据目录。测试通过临时目录运行，不修改真实图库，也不调用付费 AI 接口。

Streamlit 备选界面也使用相同数据库、审核服务和审核隔离逻辑。旧 `/edit` 地址自动跳转到 `/review`。

默认端口被占用时，可指定独立端口（不要把测试资料导入已有服务）：

```bash
CLOTHEXTRACT_DATA_DIR=/tmp/clothextract-demo EMBEDDING_BACKEND=local .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 18520
# 在 frontend 目录另开终端
API_PROXY_TARGET=http://127.0.0.1:18520 npm run dev -- --host 127.0.0.1 --port 18518 --strictPort
```
