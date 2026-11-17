# 服装内部图片库（MVP）

把 PPT / Excel / PDF 里的女装款式图抽取、打标、归类。有效 AI 分类完成后自动入库，可直接浏览和检索；需要纠正时在图库或详情点击「修改标签」。AI 建议和逐字段人工修改分别保留，人工修改优先。

对照清单见 [`docs/mvp-checklist.md`](docs/mvp-checklist.md)。

## 技术组合（对齐方案）

| 模块 | 实现 |
|------|------|
| 抽图 | `python-pptx` + `openpyxl` + `pymupdf` |
| 打标 | GPT-4o-mini 或 DeepSeek Flash + JSON Output |
| 检索 | CLIP（多语言文本塔）+ FAISS |
| 存储 | 默认本地图片 + SQLite。配置 `DATABASE_URL`（或 `MYSQL_*`）后改用阿里云 RDS MySQL；配置 `OSS_*` 后图片改存阿里云 OSS，可选 CDN |
| 检索索引 | FAISS 文件放在运行服务的那台机器上（ECS 本地磁盘），需要时重建 |
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

启用 CLIP 语义检索前，在本机准备权重并建索引。索引文件留在这台机器的 `data/processed/index`，不进数据库，也不进 OSS：

```bash
python scripts/prepare_local_search.py --index
```

服务已经在跑时，也可以只重建索引：

```bash
curl -X POST http://127.0.0.1:8520/api/index/rebuild
```

## 数据库和图片放哪里

什么都不填时，和以前一样：SQLite 文件在 `data/processed/catalog.sqlite3`，图片在 `data/` 下面。

要接到阿里云时，在 `.env` 里填写（示例都是占位符，见 [`.env.example`](.env.example)）：

- `DATABASE_URL` 或 `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE`：改用 RDS MySQL。只填了一半会直接报错，不会悄悄退回 SQLite。
- `OSS_ENDPOINT` / `OSS_BUCKET` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET`：新图片上传到 OSS。可选 `OSS_CDN_BASE_URL`，配置后页面上的图片地址走 CDN。

已有本机数据可以一次性搬过去：

```bash
python scripts/init_mysql.py
python scripts/migrate_sqlite_to_mysql.py
python scripts/upload_images_to_oss.py
```

确认 OSS 上的图能打开后，再加 `--delete-local` 删掉已经上传的本地副本。公司服务器的安装步骤、Nginx、systemd 和备份写在 [部署指南](docs/部署指南.md)。

- 前端：<http://127.0.0.1:8518>
- API：<http://127.0.0.1:8520/docs>

第一板块建议按这个顺序操作：先上传图片文件夹，再导入款式档案，最后导入销量。三个步骤的顺序可以颠倒，款号会自动对上。图片文件名用基础款号（`516083.jpg`），空格会去掉；不要把客户名写在文件名后面。

## 自动处理

- **PDF 抽图**：按页面区域截图，自动拼回被切成横条的模特图，过滤横幅、整页背景、小图标和重复图。
- **设计说明**：PDF 里印在图片旁边的文字自动抓取。款式明细表按列取款号、价格、成分、材质说明，并把款式说明拆成灵感价值 / 版型特点 / 工艺细节；模特图页取图下说明、分组标题和页面主题。设计说明会一并发给 AI 打标作参考，保留在详情中，也可被文字搜索。（Excel / PPT 暂未抓取文字。）
- **非款式图**：AI 打标时判断不是服装的图（面料特写、品牌标、文字卡片等）导入时直接丢弃；对已入库的图重新打标时自动驳回并保留记录。

## 自动入库与人工修改

导入 → AI 返回有效建议 → 自动进入图库和搜索，无需逐张审核。失败或未获得有效建议的图片留在任务中，补好服务配置后重试。模拟分类是显式测试选项，卡片会注明「模拟分类」。

图库卡片和款式详情都有「修改标签」。改过的字段使用人工值，其余字段沿用 AI；手动清空也会保留。重新识别可更新 AI 建议，但不会覆盖人工修改。可逐项或全部恢复 AI 建议，详情显示 AI、人工及当前生效标签与修改历史。

图库支持「全部 / AI 分类 / 人工已修改」筛选。图库筛选、文字搜索、以图搜索的标签条件和数据助手均使用生效标签；以图搜索仍比较原图外观，以已有款式找相似款时同时参考生效标签。旧数据中的有效 AI 分类自动可用，人工修改与旧操作历史保留，原本隐藏的图片不自动恢复。

详细流程、迁移和测试说明见 [自动入库、标签修改与存储](docs/review-workflow.md)。网站登录和数据分析密码沿用现有配置；本次没有新增账号或角色权限。

## 本厂产品

图库卡片显示款号、季节、品类、色系、版本数和销售摘要。资料页图片可在款式详情页点击「调整封面」，框选服装区域，也可输入选区边界百分比。选区保存在数据库中，只影响图库封面；详情原图和下载文件保持完整，随时可以恢复。新图识别成功并替换原图时，会清除旧封面选区，避免把原来的裁法套到新图片上。

导入时为每个文件选择资料大类：本厂产品、网图趋势、品牌资料包、市调拍照。也可以直接上传图片或整个文件夹。本厂产品图片的文件名（不含扩展名）就是大货款号基码，例如 `516083.jpg`。同一款号上传新图，识别成功后自动替换，旧图留在详情页；识别失败时保留原图，人工标签始终保留。

款式档案和销量表在导入页单独上传，按表头列名读取，重复导入不会把销量加两遍。有档案时，图库品类经同义词表归入固定品类，并拆出袖长和面料；若该字段有人工修改，人工值优先，原档案和真实销售明细不被改写。先传图片、再传档案、最后传销量即可；顺序换一下也会按款号接上。说明和取舍见 [本厂产品](docs/own-factory.md)。

## 数据分析与问数据

从图库的「本厂产品」进入「数据分析」，选择下单日期、订单品类和归档季节，预览后下载 PDF。报告按有日期的订单明细计算，补单已包含在总件数。历史汇总和无日期明细另外提示。

「问数据」可直接聊天，日期和品类都可以在消息里说。首次提问未指定日期时，订单查询使用已有明细的日期覆盖。启用项目现有的 `DEEPSEEK_API_KEY` 后，DeepSeek 读取经检查的消息和最近四轮提问，并选择只读查询工具。服务端在发出消息前拦截常见敏感内容，并用代号替换款号。数据库内容、查询结果和密钥留在应用侧。DeepSeek 不可用时使用本地规则。

- 在运行环境的 `.env` 设置独立的 `ANALYTICS_PASSWORD`（10 至 256 字符，且与网站密码不同），然后重启服务。数据分析默认在 HTTPS 上使用，SQLite 本机预览可用回环地址。
- SQLite 自动用只读模式连接。RDS 需设置 `ANALYTICS_DATABASE_URL`，使用当前业务库的独立只读账号，至少具有 `records`、`style_variants`、`style_order_lines`、`style_sales` 的 SELECT 权限；也可在 RDS 控制台创建普通账号，对现有业务库选择“只读”授权。
- 配套 ECS 部署脚本支持 `--configure-analytics`：交互输入分析密码和 RDS 只读账号密码，沿用服务器现有 RDS 地址、端口和数据库，检查授权及四张业务表可读后，只更新 `.env` 中的 `ANALYTICS_PASSWORD`、`ANALYTICS_DATABASE_URL`。先备份配置，重启检查失败会恢复原配置；不修改网站密码、主数据库连接、OSS 配置或业务数据。正常部署步骤仍保留 `.env`。
- 分析密码保护本页及其接口。图库和款式详情的访问权限由原有站点登录配置管理。

暂时需要通过 HTTP 的 IP 地址使用报告和聊天时，可显式设置 `ANALYTICS_ALLOW_HTTP=true`。
也可以由部署脚本在项目根目录创建 `.analytics-access.json`，内容为 `{"allow_http": true}`，无需修改现有 `.env`。
环境变量优先于该文件，未设置、文件损坏或值无效时默认要求 HTTPS。
HTTP 兼容访问必须已配置 `SITE_PASSWORD`，仍需网站登录、独立分析解锁和 RDS 只读账号；不会授予 Agent 写入或删除权限。
页面会标明当前连接未加密。密码并不能加密 HTTP 中传输的登录信息、对话和报告。
配置好 HTTPS 后将该文件改为 `{"allow_http": false}` 即可关闭兼容访问；若 `.env` 中显式开启过该开关，也需改为 false 并重启服务。

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

品类使用固定的两级分类表；图库可以筛出待归类并通过「修改标签」归入现有品类。独立的品类管理页面已移除。已有资料会按同义词表迁移一次，原始叫法仍保留。

风格、季节、版型、颜色、领型、袖型、图案和分组细节元素在 [`src/schema.py`](src/schema.py)。品类表和同义词在 [`src/taxonomy.py`](src/taxonomy.py)。细节见 [固定品类与同义词](docs/taxonomy.md)。

## 界面字体

字体文件放在 [`frontend/public/fonts`](frontend/public/fonts)，由页面本地加载，不引用 Google 或其他外部 CDN（工厂网络可能访问不到）。四套字体都是 SIL Open Font License 1.1，许可全文在同目录：

| 字体 | 用途 | 文件 | 许可 |
|------|------|------|------|
| 朱雀仿宋 Zhuque Fangsong（TrionesType/zhuque，璇玑造字） | 除品牌名以外的界面文字 | `ZhuqueFangsong-Regular.woff2` | `OFL-ZhuqueFangsong.txt` |
| 思源宋体 Noto Serif SC | 页头品牌名「貂帅服装」，只保留这四个字 | `NotoSerifSC-SemiBold.woff2` | `OFL-NotoSerifSC.txt` |
| 思源黑体 Noto Sans SC | 仿宋缺字时的回退 | `NotoSansSC-Regular.woff2` | `OFL-NotoSansSC.txt` |
| Cormorant Garamond | 页头英文「DIAOSHUAI KNIT」和斜体副标题 | `CormorantGaramond-SemiBold.woff2`、`CormorantGaramond-SemiBoldItalic.woff2` | `OFL-CormorantGaramond.txt` |

仿宋已转成 woff2，并按 GB2312 与界面用字做了子集；缺字回退到 Noto Sans SC。`font-display` 为 `swap`。这些字体的版权行没有声明 Reserved Font Name，因此保留原家族名。

```yaml
风格: [极简, 复古, 甜美, 通勤, 工装, 法式, 休闲, 其他]
季节: [春, 夏, 秋, 冬, 四季]
版型: [修身, 宽松, 直筒, 廓形, 其他]
颜色主色调: [黑色, 白色, 灰色, 米色, 蓝色, 红色, 其他]
领型: [圆领, V领, 高领, 翻领, 无领, 其他]
袖型: [长袖, 短袖, 无袖, 泡泡袖, 其他]
图案: [纯色/无明显图案, 条纹, 格纹, 波点, 花卉, 动物纹, 几何图案, 字母/图形, 迷彩, 抽象/泼墨, 混合图案, 其他图案, 未判断]
细节元素分组: [结构与闭合, 装饰工艺, 针织纹理]
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
