# 服装内部图片库（MVP）

按 [`docs/mvp-requirements.md`](docs/mvp-requirements.md) 落地：把 PPT / Excel / PDF 里的女装款式图抽取、打标、归类，AI 分类后即在主页归类展示并标记「未审核」，人工确认后标记「已审核」；灵感搜索与以图找款可以选择是否包含未审核图片。

对照清单见 [`docs/mvp-checklist.md`](docs/mvp-checklist.md)。

## 技术组合（对齐方案）

| 模块 | 实现 |
|------|------|
| 抽图 | `python-pptx` + `openpyxl` + `pymupdf` |
| 打标 | GPT-4o-mini + 受约束 JSON（等价于文档中的多模态 API 路径） |
| 检索 | CLIP（多语言文本塔）+ FAISS |
| 存储 | 本地图片 + SQLite（含审核状态与操作历史） |
| 界面 | Vue 3 + FastAPI |

## 快速开始

需要 Python 3.10 及以上。

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

## 自动处理

- **PDF 抽图**：按页面区域截图，自动拼回被切成横条的模特图，过滤横幅、整页背景、小图标和重复图。
- **设计说明**：PDF 里印在图片旁边的文字自动抓取。款式明细表按列取款号、价格、成分、材质说明，并把款式说明拆成灵感价值 / 版型特点 / 工艺细节；模特图页取图下说明、分组标题和页面主题。设计说明会一并发给 AI 打标作参考，可被文字搜索，审核时可修改。（Excel / PPT 暂未抓取文字。）
- **非款式图**：AI 打标时判断不是服装的图（面料特写、品牌标、文字卡片等）导入时直接丢弃；对已入库的图重新打标时自动驳回并保留记录。

## 人工审核

导入 → AI 分类并展示在主页（未审核）→ 直接通过 / 修改后通过 / 驳回。主页卡片显示来源 PDF / Excel / PPT 文件、来源位置和导入时间，可筛选审核状态；驳回图片从主页移除。

打开前端「人工审核」或 `/review`。保留 AI 建议、人工最终分类、审核人、时间和操作历史；重新打标不会覆盖上次人工分类。旧 JSON 数据会自动迁入 SQLite，并进入待审核，原文件保留。

导入页支持后台任务进度和失败续跑；审核页支持批次筛选、快捷操作及位置恢复；点击图库图片可查看款式详情、来源、审核记录并下载原图。

详细流程、迁移和测试说明见 [人工审核与 SQLite 存储](docs/review-workflow.md)。本期审核人手动填写，尚未接入登录和角色权限。

## 本厂产品

导入时为每个文件选择资料大类：本厂产品、网图趋势、品牌资料包、市调拍照。也可以直接上传图片或整个文件夹。本厂产品图片的文件名（不含扩展名）就是大货款号基码，例如 `516083.jpg`。同一款号再次上传会进入审核，确认后才替换原图，旧图留在详情页。

款式档案和销量表在导入页单独上传，按表头列名读取，重复导入会覆盖同一款号而不会把销量加两遍。有档案时，图库里的品类用一级品类，AI 标签仍保留作参考。说明和取舍见 [本厂产品](docs/own-factory.md)。

## 标签体系（需求草稿）

唯一来源是 [`src/schema.py`](src/schema.py)：打标提示词、筛选项、搜索词表都从那里生成，改标签只改这一处。

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
├── data/{raw,extracted,processed}
├── docs/mvp-requirements.md
├── prompts/tagging_prompt.md
├── scripts/run_pipeline.py
└── src/{extract,tag,index,search,factory}
```

## 本期不做

ERP、外部爬虫、以图生款、复杂多用户权限。
