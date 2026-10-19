# 服装内部图片库（MVP）

把散落在 PPT / Excel / PDF 里的女装款式图抽取、打标、入库，支持文字搜索与以图搜图。

当前界面：**Vue 3 + Vite**；后端：**FastAPI**（复用 `src/` 抽图、打标、检索逻辑）。Streamlit 版仍保留在 `app/streamlit_app.py` 作为备选。

## 功能

- 从 `.pptx` / `.xlsx` / `.pdf` 批量抽图，保留来源
- 自动打标：优先 **GPT-4o-mini**（需 `OPENAI_API_KEY`）；无 key 时回退 Mock
- 目录浏览 + 筛选
- 文字搜索 / 以图搜图
- 人工修正标签

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY=sk-...

# 终端 1：API
uvicorn api.main:app --host 0.0.0.0 --port 8520 --reload

# 终端 2：Vue 前端
cd frontend
npm install
npm run dev
```

打开前端：<http://127.0.0.1:8518>  
API 文档：<http://127.0.0.1:8520/docs>

可选：对已入库 PDF 图重打标

```bash
python scripts/retag_with_openai.py --pdf-only
```

## 目录结构

```
├── api/main.py               # FastAPI
├── frontend/                 # Vue 3 + Vite
├── app/streamlit_app.py      # 旧 Streamlit 界面（可选）
├── data/
│   ├── raw/
│   ├── extracted/
│   └── processed/
├── prompts/tagging_prompt.md
├── scripts/
└── src/                      # extract / tag / index / search
```

## 技术说明

| 模块 | 当前实现 |
|------|----------|
| 前端 | Vue 3 + Vue Router + Vite |
| API | FastAPI |
| 抽图 | python-pptx / openpyxl / pymupdf |
| 打标 | GPT-4o-mini |
| 检索 | local-lite 向量 + FAISS；文字检索含词表匹配 |

## 本期不做

ERP 打通、外部爬虫、以图生款、复杂多用户权限。
