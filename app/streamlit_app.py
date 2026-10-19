"""Fashion Image Library — Streamlit MVP."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import (  # noqa: E402
    EXTRACTED_DIR,
    RAW_DIR,
    ensure_data_dirs,
    load_catalog,
    save_catalog,
    upsert_records,
)
from src.extract.office import extract_from_directory, extract_from_file  # noqa: E402
from src.index.embeddings import build_index  # noqa: E402
from src.schema import FILTER_FIELDS, FashionTags, ImageRecord  # noqa: E402
from src.search.query import filter_records, search_by_image, search_by_text  # noqa: E402
from src.tag.tagger import tag_records  # noqa: E402

st.set_page_config(
    page_title="服装内部图片库",
    layout="wide",
)

ensure_data_dirs()


def reload_catalog() -> list[ImageRecord]:
    return load_catalog()


def render_filters(prefix: str = "") -> dict:
    cols = st.columns(5)
    keys = ["品类", "风格", "季节", "颜色主色调", "版型"]
    selected = {}
    for col, key in zip(cols, keys):
        options = ["全部", *FILTER_FIELDS[key]]
        selected[key] = col.selectbox(key, options, key=f"{prefix}{key}")
    return selected


def apply_ui_filters(records: list[ImageRecord], selected: dict) -> list[ImageRecord]:
    return filter_records(
        records,
        category=selected.get("品类"),
        style=selected.get("风格"),
        season=selected.get("季节"),
        color=selected.get("颜色主色调"),
        fit=selected.get("版型"),
    )


def show_gallery(items: list[tuple[ImageRecord, float | None]], empty_msg: str) -> None:
    if not items:
        st.info(empty_msg)
        return
    cols = st.columns(4)
    for idx, (record, score) in enumerate(items):
        col = cols[idx % 4]
        with col:
            path = Path(record.image_path)
            if path.exists():
                st.image(str(path), use_container_width=True)
            else:
                st.warning("图片缺失")
            caption = record.tags.一句话描述 or "（无描述）"
            meta = f"{record.tags.品类} · {record.tags.风格} · {record.tags.颜色主色调}"
            if score is not None:
                st.caption(f"{caption}\n\n{meta}\n相似分 {score:.3f}")
            else:
                st.caption(f"{caption}\n\n{meta}")
            with st.expander("来源 / 标签"):
                st.write(
                    {
                        "来源": f"{record.source.source_file} / {record.source.location}",
                        "打标": record.tags_source,
                        "人工修正": record.manually_edited,
                        **record.tags.model_dump(),
                    }
                )


def page_library() -> None:
    st.subheader("图片库")
    records = reload_catalog()
    st.caption(f"当前共 {len(records)} 条记录")
    selected = render_filters("lib_")
    filtered = apply_ui_filters(records, selected)
    show_gallery([(r, None) for r in filtered], "暂无图片。请先在「导入」页上传 PPT/Excel，或运行样本脚本。")


def page_search() -> None:
    st.subheader("检索")
    records = reload_catalog()
    if not records:
        st.warning("图库为空，请先导入并重建索引。")
        return

    selected = render_filters("search_")
    filtered = apply_ui_filters(records, selected)
    mode = st.radio("检索方式", ["文字搜索", "以图搜图"], horizontal=True)

    if mode == "文字搜索":
        query = st.text_input("描述你想找的款", placeholder="例如：宽松高领毛衣 复古感")
        if st.button("搜索", type="primary") and query.strip():
            try:
                results = search_by_text(records, query, filtered=filtered)
                show_gallery(results, "没有匹配结果，试试更短的关键词或放宽筛选。")
            except FileNotFoundError:
                st.error("尚未建立向量索引。请到「导入」页点击「重建索引」。")
    else:
        upload = st.file_uploader("上传一张参考图", type=["png", "jpg", "jpeg", "webp"])
        if upload and st.button("以图搜图", type="primary"):
            temp_dir = EXTRACTED_DIR / "_query"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / upload.name
            temp_path.write_bytes(upload.getvalue())
            st.image(str(temp_path), caption="查询图", width=220)
            try:
                results = search_by_image(records, temp_path, filtered=filtered)
                show_gallery(results, "没有相似结果。")
            except FileNotFoundError:
                st.error("尚未建立向量索引。请到「导入」页点击「重建索引」。")


def page_import() -> None:
    st.subheader("导入与处理")
    st.markdown(
        "支持 `.pptx` / `.xlsx`。抽取后会用 **Mock 打标**（本地启发式，无需 API），"
        "再写入目录并可选重建 FAISS 索引。"
    )

    uploaded = st.file_uploader(
        "上传 PPT / Excel",
        type=["pptx", "xlsx", "xlsm"],
        accept_multiple_files=True,
    )
    col_a, col_b, col_c = st.columns(3)

    with col_a:
        run_extract = st.button("抽取图片", type="primary", use_container_width=True)
    with col_b:
        run_tag = st.button("对未打标项执行 Mock 打标", use_container_width=True)
    with col_c:
        run_index = st.button("重建索引", use_container_width=True)

    if run_extract:
        if not uploaded and not list(RAW_DIR.glob("*.pptx")) and not list(RAW_DIR.glob("*.xlsx")):
            st.warning("请先上传文件，或把文件放到 `data/raw/`。")
        else:
            new_records: list[ImageRecord] = []
            for file in uploaded or []:
                dest = RAW_DIR / file.name
                dest.write_bytes(file.getvalue())
                new_records.extend(extract_from_file(dest))
            if not uploaded:
                new_records.extend(extract_from_directory(RAW_DIR))
            if not new_records:
                st.warning("未抽到图片。请确认文件内含嵌入图。")
            else:
                tagged = tag_records(new_records, backend="mock")
                merged = upsert_records(load_catalog(), tagged)
                save_catalog(merged)
                build_index(merged)
                st.success(f"抽取并打标 {len(tagged)} 张，目录现有 {len(merged)} 条，索引已更新。")

    if run_tag:
        catalog = load_catalog()
        if not catalog:
            st.warning("目录为空。")
        else:
            tagged = tag_records(catalog, backend="mock", overwrite=False)
            save_catalog(tagged)
            st.success("Mock 打标完成。")

    if run_index:
        catalog = load_catalog()
        build_index(catalog)
        st.success(f"索引已重建（{len(catalog)} 条）。")

    st.divider()
    st.markdown("#### 当前目录预览")
    catalog = load_catalog()
    if catalog:
        st.dataframe(pd.DataFrame([r.to_catalog_row() for r in catalog]), use_container_width=True)
    else:
        st.info("尚无记录。可运行 `python scripts/seed_sample_catalog.py` 生成演示样本。")


def page_edit() -> None:
    st.subheader("人工修正标签")
    records = reload_catalog()
    if not records:
        st.info("暂无记录可修正。")
        return

    options = {
        f"{r.id} · {r.tags.一句话描述 or Path(r.image_path).name}": r.id for r in records
    }
    label = st.selectbox("选择图片", list(options.keys()))
    record = next(r for r in records if r.id == options[label])

    cols = st.columns([1, 2])
    with cols[0]:
        if Path(record.image_path).exists():
            st.image(record.image_path, use_container_width=True)
        st.caption(f"{record.source.source_file} / {record.source.location}")

    with cols[1]:
        tags = record.tags
        品类 = st.selectbox("品类", FILTER_FIELDS["品类"], index=FILTER_FIELDS["品类"].index(tags.品类) if tags.品类 in FILTER_FIELDS["品类"] else len(FILTER_FIELDS["品类"]) - 1)
        风格 = st.selectbox("风格", FILTER_FIELDS["风格"], index=FILTER_FIELDS["风格"].index(tags.风格) if tags.风格 in FILTER_FIELDS["风格"] else len(FILTER_FIELDS["风格"]) - 1)
        季节 = st.selectbox("季节", FILTER_FIELDS["季节"], index=FILTER_FIELDS["季节"].index(tags.季节) if tags.季节 in FILTER_FIELDS["季节"] else len(FILTER_FIELDS["季节"]) - 1)
        版型 = st.selectbox("版型", FILTER_FIELDS["版型"], index=FILTER_FIELDS["版型"].index(tags.版型) if tags.版型 in FILTER_FIELDS["版型"] else len(FILTER_FIELDS["版型"]) - 1)
        颜色 = st.selectbox("颜色主色调", FILTER_FIELDS["颜色主色调"], index=FILTER_FIELDS["颜色主色调"].index(tags.颜色主色调) if tags.颜色主色调 in FILTER_FIELDS["颜色主色调"] else len(FILTER_FIELDS["颜色主色调"]) - 1)
        领型 = st.selectbox("领型", FILTER_FIELDS["领型"], index=FILTER_FIELDS["领型"].index(tags.领型) if tags.领型 in FILTER_FIELDS["领型"] else len(FILTER_FIELDS["领型"]) - 1)
        袖型 = st.selectbox("袖型", FILTER_FIELDS["袖型"], index=FILTER_FIELDS["袖型"].index(tags.袖型) if tags.袖型 in FILTER_FIELDS["袖型"] else len(FILTER_FIELDS["袖型"]) - 1)
        细节 = st.text_input("细节元素（逗号分隔）", value=",".join(tags.细节元素))
        描述 = st.text_area("一句话描述", value=tags.一句话描述)

        if st.button("保存修正", type="primary"):
            record.tags = FashionTags(
                品类=品类,
                风格=风格,
                季节=季节,
                版型=版型,
                颜色主色调=颜色,
                领型=领型,
                袖型=袖型,
                细节元素=[x.strip() for x in 细节.split(",") if x.strip()],
                一句话描述=描述.strip(),
            )
            record.tags_source = "manual"
            record.manually_edited = True
            record.touch()
            save_catalog(records)
            build_index(records)
            st.success("已保存，并重建索引。")


def main() -> None:
    st.title("服装内部图片库")
    st.caption("MVP：PPT/Excel 抽图 · Mock 打标 · 文字搜 / 以图搜图 · 人工修正")

    page = st.sidebar.radio(
        "功能",
        ["图片库", "检索", "导入与处理", "人工修正"],
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown(
        "**打标后端**：Mock（本地）  \n"
        "**向量后端**：local-lite（可换成 CLIP）  \n"
        "API 接入后续再做。"
    )

    if page == "图片库":
        page_library()
    elif page == "检索":
        page_search()
    elif page == "导入与处理":
        page_import()
    else:
        page_edit()


if __name__ == "__main__":
    main()
