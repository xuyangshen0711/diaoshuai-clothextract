# 服装内部图片库（MVP）

按 [`docs/mvp-requirements.md`](docs/mvp-requirements.md) 落地：把 PPT / Excel / PDF 里的女装款式图抽取、打标、归类，AI 分类后需人工审核通过，才进入正式图库，支持文字搜与以图搜图。

对照清单见 [`docs/mvp-checklist.md`](docs/mvp-checklist.md)。

## 技术组合（对齐方案）

| 模块 | 实现 |
|------|------|
| 抽图 | `python-pptx` + `openpyxl` + `pymupdf` |
| 打标 | GPT-4o-mini + 受约束 JSON（等价于文档中的多模态 API 路径） |
| 检索 | CLIP（多语言文本塔）+ FAISS |
| 存储 | 本地图片 + SQLite（含审核状态与操作历史） |
| 界面 | Vue 3 + FastAPI（文档允许轻量 Web；Streamlit 仍保留） |

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入 OPENAI_API_KEY

python scripts/run_pipeline.py --input data/raw --backend openai

uvicorn api.main:app --host 0.0.0.0 --port 8520
cd frontend && npm install && npm run dev
```

- 前端：<http://127.0.0.1:8518>
- API：<http://127.0.0.1:8520/docs>

## 人工审核

导入 → AI 分类 → 待人工审核 → 直接通过 / 修改后通过 / 驳回。

打开前端「人工审核」或 `/review`。保留 AI 建议、人工最终分类、审核人、时间和操作历史；重新打标不会覆盖上次人工分类。旧 JSON 数据会自动迁入 SQLite，并进入待审核，原文件保留。

详细流程、迁移和测试说明见 [人工审核与 SQLite 存储](docs/review-workflow.md)。本期审核人手动填写，尚未接入登录和角色权限。

## 标签体系（需求草稿）

```yaml
品类: [毛衣, 外套, 连衣裙, 衬衫, 裤装, 半裙, 套装, 其他]
风格: [极简, 复古, 甜美, 通勤, 工装, 法式, 休闲, 其他]
季节: [春, 夏, 秋, 冬, 四季]
版型: [修身, 宽松, 直筒, 廓形, 其他]
颜色主色调: [黑色, 白色, 灰色, 米色, 蓝色, 红色, 其他]
领型: [圆领, V领, 高领, 翻领, 无领, 其他]
袖型: [长袖, 短袖, 无袖, 泡泡袖, 其他]
细节元素: [开襟, 拉链, 口袋, 刺绣, 印花, 拼接, 其他]
```

## 目录结构

```
├── api/                      # FastAPI
├── frontend/                 # Vue 3
├── app/streamlit_app.py      # 可选 Streamlit
├── data/{raw,extracted,processed}
├── docs/mvp-requirements.md
├── prompts/tagging_prompt.md
├── scripts/run_pipeline.py
└── src/{extract,tag,index,search}
```

## 本期不做

ERP、外部爬虫、以图生款、复杂多用户权限。
