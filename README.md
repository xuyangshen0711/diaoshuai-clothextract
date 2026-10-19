# 服装内部图片库（MVP）

把散落在 PPT / Excel 里的女装款式图抽取、打标、入库，支持文字搜索与以图搜图。第一版面向内部设计师/业务员试用。

## 功能

- 从 `.pptx` / `.xlsx` 批量抽图，保留来源（文件名、页码 / media 名）
- Mock 自动打标（品类、风格、季节、版型、颜色等；**暂不调用商业 API**）
- 目录浏览 + 筛选
- 文字语义搜索 / 以图搜图（本地轻量向量 + FAISS；后续可换 CLIP）
- 人工修正标签入口

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：生成 6 张演示样本并建索引
python scripts/seed_sample_catalog.py

# 启动界面（默认示例端口）
streamlit run app/streamlit_app.py --server.port 8517 --server.address 0.0.0.0
```

浏览器打开 <http://127.0.0.1:8517>。

## 目录结构

```
├── app/streamlit_app.py      # 界面
├── data/
│   ├── raw/                  # 原始 PPT / Excel
│   ├── extracted/            # 抽出的图片
│   └── processed/            # catalog.json + FAISS 索引
├── prompts/tagging_prompt.md # 打标提示词草稿（给未来 API 用）
├── scripts/seed_sample_catalog.py
├── src/
│   ├── extract/              # python-pptx / openpyxl 抽图
│   ├── tag/                  # Mock 打标（API 占位已留）
│   ├── index/                # 向量索引
│   └── search/               # 文字搜 / 以图搜图
└── docs/mvp-requirements.md  # 原始需求与技术方案
```

## 使用流程

1. 在「导入与处理」上传 PPT/Excel，或把文件放到 `data/raw/`
2. 点击「抽取图片」（会自动 Mock 打标并建索引）
3. 在「图片库」浏览筛选；在「检索」做文字搜或以图搜图
4. 在「人工修正」改标签（会标记为 manual，重建索引）

## 技术说明

| 模块 | 当前实现 | 后续可替换 |
|------|----------|------------|
| 抽图 | `python-pptx` + `openpyxl` / zip media | SlideSpeak CLI 等 |
| 打标 | `MockTagger` 本地启发式 | OpenAI / Claude / Qwen-VL / product-tagger |
| 向量 | local-lite（颜色+纹理哈希）+ FAISS | CLIP / SigLIP |
| 界面 | Streamlit | FastAPI + 前端 |

## 本期不做

ERP 打通、外部爬虫、以图生款、复杂多用户权限。
