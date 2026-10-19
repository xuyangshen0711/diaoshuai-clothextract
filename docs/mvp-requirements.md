# 服装内部图片库（场景二）最小可行原型需求与技术方案

> 目标：把散落在 PPT、Excel 里的女装款式图片，自动抽取、打标、归类，建成可检索的图片库（支持文字搜 + 以图搜图），供设计师和业务员调用。  
> 原则：**优先复用 GitHub 开源项目，能不从零搭建就不从零搭建**。

---

## 1. 项目背景与范围

### 已对齐的颗粒度
- **品类范围**：女装全品类（先做全品类，后续可根据实际效果再聚焦）。
- **标签体系**：先出一版草稿，后续找设计师确认和细分。
- **图片来源**：主要是 PPT 和 Excel（含嵌入图片）。
- **检索方式**：同时支持「文字搜索」和「以图搜图」。
- **数据量**：暂不明确，后续再定（先按几百到几千张的量级设计）。
- **准确率要求**：第一版尽量高，但接受人工抽查修正。
- **技术约束**：可以使用商业 API（企业承担 token 费用），也可以本地模型。

### 本期不做
- ERP 数据打通
- 外部爬虫（时装周、电商销量等）
- 款式生成（以图生款）
- 复杂权限与多用户协作（先做单用户/内部小团队可用版本）

---

## 2. 核心目标（MVP）

1. 从 PPT / Excel 批量抽取图片，保留来源信息（文件名、页码/Sheet 名）。
2. 对抽取的图片自动打标签（品类、风格、季节、版型、颜色、细节等）。
3. 建立可检索的图片库：
   - 文字语义搜索（例如：“宽松高领毛衣 复古感”）
   - 以图搜图（上传一张图找相似款）
   - 基础筛选（品类、季节、风格等）
4. 提供一个简单可用的界面（优先 Streamlit 或轻量 Web），方便设计师试用。

---

## 3. 推荐技术路径（优先复用开源）

### 3.1 图片抽取（PPT / Excel）
优先使用现成工具，而不是自己写复杂解析：

- **推荐**：
  - [`SlideSpeak/image-extractor-cli`](https://github.com/SlideSpeak/image-extractor-cli)（支持 PPTX、PDF 等）
  - [`SWHL/ExtractOfficeContent`](https://github.com/SWHL/ExtractOfficeContent)
  - `python-pptx` + `openpyxl`（轻量，可控性强，推荐作为主力）
- **策略**：先用 `python-pptx` / `openpyxl` 抽图，保留元数据。如果遇到复杂格式再参考上面的 CLI 工具。

### 3.2 图片理解与打标（核心）
**强烈建议优先复用以下开源项目，而不是从零训练模型：**

| 项目 | 用途 | 推荐优先级 | 说明 |
|------|------|------------|------|
| [`si88har1h/product-tagger`](https://github.com/si88har1h/product-tagger) | 结构化打标 | ★★★★★ | 支持多种后端（本地 Ollama/Qwen-VL、CLIP、OpenAI、Claude），输出受约束 JSON，非常适合改造 |
| [`open-mmlab/mmfashion`](https://github.com/open-mmlab/mmfashion) | 属性预测 + 检索 | ★★★★☆ | 最完整的服装视觉工具箱，有属性预测和以图搜图模块 |
| CLIP / SigLIP + FAISS | 以图搜图 & 语义检索 | ★★★★★ | 成熟方案，几乎所有开源视觉检索项目都在用 |

**推荐组合方案**：
- 打标：优先改造 `product-tagger`，或直接用多模态模型（Qwen2-VL / InternVL / GPT-4o / Claude）+ 精心设计的提示词输出 JSON。
- 检索：用 CLIP 类模型提取 embedding，存入 FAISS 或 Chroma，实现文字搜和以图搜图。

### 3.3 存储与检索
- 图片文件：本地目录或对象存储
- 元数据 + 标签：SQLite / PostgreSQL（或直接 JSON + 简单数据库）
- 向量：FAISS 或 Chroma（轻量，适合 MVP）

### 3.4 界面
- 优先 **Streamlit**（最快出可用界面）
- 或 FastAPI + 简单前端

---

## 4. 标签体系草稿（待设计师确认）

先按以下维度出一版，后续细化：

```yaml
品类: [毛衣, 外套, 连衣裙, 衬衫, 裤装, 半裙, 套装, 其他]
风格: [极简, 复古, 甜美, 通勤, 工装, 法式, 休闲, 其他]
季节: [春, 夏, 秋, 冬, 四季]
版型: [修身, 宽松, 直筒, 廓形, 其他]
颜色主色调: [黑色, 白色, 灰色, 米色, 蓝色, 红色, 其他]
领型: [圆领, V领, 高领, 翻领, 无领, 其他]
袖型: [长袖, 短袖, 无袖, 泡泡袖, 其他]
细节元素: [开襟, 拉链, 口袋, 刺绣, 印花, 拼接, 其他]
一句话描述: "自由文本"
来源: "PPT文件名 + 页码 / Excel文件名 + Sheet"
```

---

## 5. 推荐的项目结构（给 Cursor 参考）

```
fashion-image-library/
├── data/
│   ├── raw/                  # 原始 PPT / Excel
│   ├── extracted/            # 抽出来的图片
│   └── processed/            # 打标后的结果
├── src/
│   ├── extract/              # 抽图模块（基于 python-pptx / openpyxl）
│   ├── tag/                  # 打标模块（优先改造 product-tagger 或调用多模态 API）
│   ├── index/                # 向量索引（CLIP + FAISS）
│   └── search/               # 检索逻辑
├── app/
│   └── streamlit_app.py      # 简易界面
├── prompts/
│   └── tagging_prompt.md     # 打标提示词
├── requirements.txt
└── README.md
```

---

## 6. 开发步骤建议（给 Cursor）

1. **搭建基础结构**，安装依赖（`python-pptx`、`openpyxl`、`Pillow`、`faiss-cpu`、`sentence-transformers` 或 `open-clip-torch`、`streamlit` 等）。
2. **实现抽图模块**：支持批量处理 PPT 和 Excel，输出图片 + 来源元数据。
3. **实现打标模块**：
   - 优先参考/改造 `product-tagger`。
   - 或直接写一个调用多模态 API 的脚本（先支持 OpenAI / Claude / 通义千问等）。
   - 提示词要求输出严格 JSON。
4. **实现向量索引与检索**：用 CLIP 提取 embedding，支持文字搜和以图搜图。
5. **做 Streamlit 界面**：上传/浏览、筛选、文字搜、以图搜图。
6. **用你提供的几份企划 PDF / 商品资料做第一轮测试**。

---

## 7. 重要原则（务必遵守）

- **优先复用 GitHub 开源项目**，尤其是：
  - 打标：`product-tagger`、`mmfashion`
  - 抽图：现成 CLI 或 `python-pptx` / `openpyxl`
  - 检索：CLIP + FAISS 生态
- 能不自己从零训练模型就不训练。
- 第一版追求“能用 + 可迭代”，而不是完美。
- 所有自动打标结果都要保留人工修正入口。

---

## 8. 下一步可执行动作

1. 把本文件丢给 Cursor，让它按上面结构开始搭项目骨架。
2. 准备 50–100 张真实图片样本（从现有 PPT/Excel 里抽）。
3. 确认第一版要支持的多模态 API（或本地模型）。
4. 设计师确认标签体系草稿后，再细化提示词。

---

**备注**：本方案基于与 AI 的多轮讨论整理，重点是“内部图片资源沉淀与可检索”，优先落地场景二。
