# MVP 需求对照清单

依据 `docs/mvp-requirements.md`。

| 需求项 | 状态 | 实现位置 |
|--------|------|----------|
| PPT / Excel 批量抽图 + 来源（文件名、页码/Sheet） | ✅ | `src/extract/office.py`（另支持 PDF） |
| 自动打标（品类/风格/季节/版型/颜色/细节） | ✅ | `src/tag/openai_tagger.py`（GPT-4o-mini） |
| 严格 JSON 标签输出 | ✅ | `prompts/tagging_prompt.md` |
| 标签体系按第 4 节草稿 | ✅ | `src/schema.py` |
| 文字语义搜索 | ✅ | CLIP text→image + 标签加权 |
| 以图搜图 | ✅ | CLIP + FAISS |
| 基础筛选 | ✅ | Vue 图库 / 搜索页 |
| 可用界面 | ✅ | Vue 3（`frontend/`）；Streamlit 备选 `app/streamlit_app.py` |
| 一级人工审核 | ✅ | `/review`：直接通过 / 修改后通过 / 驳回；通过后入正式图库 |
| 审核记录与持久化 | ✅ | SQLite；AI 建议、人工结果、审核人、时间、意见与历史快照 |
| 优先复用开源、不自训模型 | ✅ | python-pptx / openpyxl / pymupdf / CLIP / FAISS / OpenAI |
| 项目目录结构 | ✅ | 与文档第 5 节一致，额外含 `api/`、`frontend/` |

## 本期明确不做

- ERP 打通
- 外部爬虫
- 以图生款
- 复杂多用户权限

## 推荐使用方式

```bash
# 抽图 → GPT-4o-mini 打标 → CLIP 索引
python scripts/run_pipeline.py --input data/raw --backend openai

# 前端
uvicorn api.main:app --host 0.0.0.0 --port 8520
cd frontend && npm run dev
```
