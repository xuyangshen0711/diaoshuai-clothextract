# 服装内部图片库（MVP）

按 [`docs/mvp-requirements.md`](docs/mvp-requirements.md) 落地：把 PPT / Excel / PDF 里的女装款式图抽取、打标、归类，AI 分类后即在主页归类展示并标记「未审核」，人工确认后标记「已审核」；灵感搜索与以图找款可以选择是否包含未审核图片。

对照清单见 [`docs/mvp-checklist.md`](docs/mvp-checklist.md)。

## 技术组合（对齐方案）

| 模块 | 实现 |
|------|------|
| 抽图 | `python-pptx` + `openpyxl` + `pymupdf` |
| 打标 | GPT-4o-mini 或 DeepSeek Flash + JSON Output |
| 检索 | CLIP（多语言文本塔）+ FAISS |
| 存储 | 本地图片 + SQLite（含审核状态与操作历史） |
| 界面 | Vue 3 + FastAPI |

## 快速开始

需要 Python 3.10 及以上。Linux 上如果 `python3 -m venv` 提示没有 ensurepip，先安装 `python3-venv`。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入真实的 OPENAI_API_KEY 或 DEEPSEEK_API_KEY；示例值不算已配置，页面会改用模拟分类
```

`data/raw` 里还没有资料时，不必跑 `run_pipeline`，直接启动网页，在「导入」里上传即可：

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8520
cd frontend && npm install && npm run dev
```

已有一批 PDF / PPT / Excel 时，可以先批量处理再打开网页：

```bash
python scripts/run_pipeline.py --input data/raw --backend openai
```

没有密钥时把 `--backend openai` 改成 `--backend mock`，或在导入页选择「模拟分类（测试）」。`--backend` 留空时：只配置了 OpenAI 或 DeepSeek 其中一家就用那一家；两家都配置好时用 DeepSeek；都没有（或仍是示例密钥）时用模拟分类。

DeepSeek 密钥在 <https://platform.deepseek.com> 创建，写入 `.env`：

```bash
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_THINKING=true
```

`DEEPSEEK_THINKING` 不写或写成 `true` 时开启思考（官方默认，请求里是 `thinking.type=enabled`）。设成 `false` 则发送 `thinking.type=disabled`。两种都配合 `response_format` 的 `json_object`。开启思考时接口还会返回 `reasoning_content`，打标只解析 `content` 里的 JSON，思维链不参与校验。返回内容仍会去掉 markdown 代码块，并按 `src/schema.py` 的标签表校验。某一张图解析失败只标记这张失败，不会卡住整批。图片只放在用户消息里，支持 JPEG、PNG、GIF、WebP，单张不超过 32 MiB。单次请求读超时 180 秒。密钥无效、余额不足、限流和超时会显示中文说明；限流会退避重试。命令行：

```bash
python scripts/run_pipeline.py --input data/raw --backend deepseek
```

启用 CLIP 语义检索前，在本机准备权重并建索引：

```bash
python scripts/prepare_local_search.py --index
```

- 前端：<http://127.0.0.1:8518>
- API：<http://127.0.0.1:8520/docs>

第一板块建议按这个顺序操作：先上传图片文件夹，再导入款式档案，最后导入销量。三个步骤的顺序可以颠倒，款号会自动对上。图片文件名用基础款号（`516083.jpg`），空格会去掉；不要把客户名写在文件名后面。

## 自动处理

- **PDF 抽图**：按页面区域截图，自动拼回被切成横条的模特图，过滤横幅、整页背景、小图标和重复图。
- **设计说明**：PDF 里印在图片旁边的文字自动抓取。款式明细表按列取款号、价格、成分、材质说明，并把款式说明拆成灵感价值 / 版型特点 / 工艺细节；模特图页取图下说明、分组标题和页面主题。设计说明会一并发给 AI 打标作参考，可被文字搜索，审核时可修改。（Excel / PPT 暂未抓取文字。）
- **非款式图**：AI 打标时判断不是服装的图（面料特写、品牌标、文字卡片等）导入时直接丢弃；对已入库的图重新打标时自动驳回并保留记录。

## 人工审核

导入 → AI 分类并展示在主页（未审核）→ 直接通过 / 修改后通过 / 驳回。主页卡片显示款号、品类和审核状态，来源文件与导入时间在详情页；可筛选审核状态；驳回图片从主页移除。

打开前端「人工审核」或 `/review`。保留 AI 建议、人工最终分类、审核人、时间和操作历史；重新打标不会覆盖上次人工分类。旧 JSON 数据会自动迁入 SQLite，并进入待审核，原文件保留。

导入页支持后台任务进度和失败续跑；审核页支持批次筛选、快捷操作及位置恢复；点击图库图片可查看款式详情、来源、审核记录并下载原图。

详细流程、迁移和测试说明见 [人工审核与 SQLite 存储](docs/review-workflow.md)。本期审核人手动填写，尚未接入登录和角色权限。

## 本厂产品

导入时为每个文件选择资料大类：本厂产品、网图趋势、品牌资料包、市调拍照。也可以直接上传图片或整个文件夹。本厂产品图片的文件名（不含扩展名）就是大货款号基码，例如 `516083.jpg`。同一款号再次上传会进入审核，确认后才替换原图，旧图留在详情页。

款式档案和销量表在导入页单独上传，按表头列名读取，重复导入会覆盖同一款号而不会把销量加两遍。有档案时，图库品类以档案里的一级品类为准，经同义词表归入固定品类，并拆出袖长和面料；AI 标签仍保留作参考。例如档案写「长袖套衫」、AI 说开衫，图库显示「上衣>套衫」、袖长长袖、面料针织。先传图片、再传档案、最后传销量即可；顺序换一下也会按款号接上。说明和取舍见 [本厂产品](docs/own-factory.md)。

## 固定品类

品类是一张固定的两级表，保存在数据库里。导入、AI 和档案都只能归入这张表，不能在导入时新建品类。对不上的归入系统品类「待归类」。袖长（无袖 / 短袖 / 中袖 / 长袖）和面料（针织 / 梭织）单独存放，不写进品类名称。本厂产品没有面料信息时默认针织。

一级和二级如下。没有列出二级的，一级本身就是可归入的品类：

| 一级 | 二级 |
|------|------|
| 上衣 | 套衫、开衫、背心/马甲、T恤、衬衫、卫衣 |
| 外套 | 针织外套、大衣、夹克、西装、羽绒/棉服 |
| 连衣裙 | — |
| 半身裙 | — |
| 裤装 | 长裤、短裤、针织裤 |
| 套装 | — |
| 配饰 | — |
| 待归类 | 系统品类，不能改名或删除 |

同义词表把档案一级品类、AI 输出和资料里的叫法（针织开衫、毛衣、马甲、阔腿裤等）映射成「固定品类 + 袖长 + 面料」。审核页可以筛出待归类，选一个固定品类后，原始叫法会写入同义词表，下次导入自动归入。新增、改名、删除品类只在导航「品类设置」里进行；还有资料在用的品类要先改归，才能删除。已有资料会按同义词表迁移一次，不改审核版本号，原始叫法仍保留。

风格、季节、版型、颜色、领型、袖型和细节元素仍在 [`src/schema.py`](src/schema.py)。品类表和同义词在 [`src/taxonomy.py`](src/taxonomy.py)。细节见 [固定品类与同义词](docs/taxonomy.md)。

```yaml
风格: [极简, 复古, 甜美, 通勤, 工装, 法式, 休闲, 其他]
季节: [春, 夏, 秋, 冬, 四季]
版型: [修身, 宽松, 直筒, 廓形, 其他]
颜色主色调: [黑色, 白色, 灰色, 米色, 蓝色, 红色, 其他]
领型: [圆领, V领, 高领, 翻领, 无领, 其他]
袖型: [长袖, 短袖, 无袖, 泡泡袖, 其他]
细节元素: [开襟, 拉链, 口袋, 刺绣, 印花, 拼接, 其他]
袖长: [无袖, 短袖, 中袖, 长袖]
面料: [针织, 梭织]
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
