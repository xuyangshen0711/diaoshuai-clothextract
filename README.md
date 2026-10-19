# 服装内部图片库（MVP）

按 `docs/mvp-requirements.md` 落地的女装内部图片库：从 PPT / Excel / PDF 抽图 → GPT-4o-mini 打标 → CLIP + FAISS 检索。

界面：**Vue 3 + Vite**；后端：**FastAPI**。Streamlit 仍保留在 `app/streamlit_app.py`。

## 已实现（对照需求）

| 需求 | 状态 |
|------|------|
| PPT / Excel 抽图 + 来源元数据 | ✅（额外支持 PDF） |
| 自动打标（品类/风格/季节等） | ✅ GPT-4o-mini，严格 JSON |
| 文字搜索 + 以图搜图 + 筛选 | ✅ CLIP 向量 + 中文标签加权 |
| 可用界面 | ✅ Vue 3（文档也允许轻量 Web） |
| 人工修正入口 | ✅ |
| 优先复用开源 / 不自训模型 | ✅ python-pptx / openpyxl / pymupdf / CLIP / FAISS / OpenAI |

标签体系以需求草稿为准，试用期补充了 `T恤`、`马甲`，便于区分上装。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 填入 OPENAI_API_KEY=sk-...

# 一键流水线：抽图 → 打标 → 建 CLIP 索引
python scripts/run_pipeline.py --input data/raw --backend openai

# API
uvicorn api.main:app --host 0.0.0.0 --port 8520

# Vue 前端
cd frontend && npm install && npm run dev
```

- 前端：<http://127.0.0.1:8518>
- API 文档：<http://127.0.0.1:8520/docs>

## 目录结构

```
├── api/main.py
├── frontend/                 # Vue 3
├── app/streamlit_app.py      # 可选旧界面
├── data/{raw,extracted,processed}
├── docs/mvp-requirements.md
├── prompts/tagging_prompt.md
├── scripts/{run_pipeline,retag_with_openai,seed_sample_catalog}.py
└── src/{extract,tag,index,search}
```

## 费用说明（GPT-4o-mini）

当前配置下，约 **$0.0006 / 张**（≈ ¥0.004）。检索本身走本地 CLIP，不额外计费。

## 本期不做

ERP、外部爬虫、以图生款、复杂多用户权限。
