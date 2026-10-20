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
    approved_records,
    library_records,
    review_record,
    review_history,
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
from src.tag.tagger import has_openai_key, tag_records  # noqa: E402

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
            st.caption(f"{'已审核' if record.review_status == 'approved' else '未审核'} · {record.source.source_type.upper()}\n\n{record.source.source_file} / {record.source.location}\n\n导入时间：{record.source.uploaded_at or record.source.extracted_at}")
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
    records = [r.model_copy(update={"tags": r.display_tags}) for r in library_records(reload_catalog())]
    st.caption(f"当前共 {len(records)} 条记录（含未审核）")
    selected = render_filters("lib_")
    filtered = apply_ui_filters(records, selected)
    show_gallery([(r, None) for r in filtered], "暂无图片。请先在「导入」页上传 PPT/Excel，或运行样本脚本。")


def page_search() -> None:
    st.subheader("检索")
    records = approved_records(reload_catalog())
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
    api_ready = has_openai_key()
    default_backend = "openai" if api_ready else "mock"
    st.markdown(
        "支持 `.pptx` / `.xlsx` / `.pdf`。"
        f"当前默认打标：**{'GPT-4o-mini' if api_ready else 'Mock（未检测到 OPENAI_API_KEY）'}**。"
    )
    if not api_ready:
        st.warning(
            "尚未配置 `OPENAI_API_KEY`。请在项目根目录 `.env` 写入 key 后重启应用，"
            "或先用 Mock / 人工修正。"
        )

    backend = st.selectbox(
        "打标后端",
        ["openai", "mock"],
        index=0 if default_backend == "openai" else 1,
        format_func=lambda x: "GPT-4o-mini（推荐）" if x == "openai" else "Mock 本地启发式",
    )

    uploaded = st.file_uploader(
        "上传 PPT / Excel / PDF",
        type=["pptx", "xlsx", "xlsm", "pdf"],
        accept_multiple_files=True,
    )
    col_a, col_b, col_c, col_d = st.columns(4)

    with col_a:
        run_extract = st.button("抽取并打标", type="primary", use_container_width=True)
    with col_b:
        run_tag = st.button("API 重打未确认项", use_container_width=True)
    with col_c:
        run_retag_all = st.button("API 全量重打标", use_container_width=True)
    with col_d:
        run_index = st.button("重建索引", use_container_width=True)

    if run_extract:
        if (
            not uploaded
            and not list(RAW_DIR.glob("*.pptx"))
            and not list(RAW_DIR.glob("*.xlsx"))
            and not list(RAW_DIR.glob("*.pdf"))
        ):
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
                if backend == "openai" and not has_openai_key():
                    st.error("缺少 OPENAI_API_KEY。")
                else:
                    progress = st.progress(0.0, text="打标中…")

                    def on_progress(idx, total, record, skipped=False):
                        progress.progress(idx / max(total, 1), text=f"打标 {idx}/{total}")

                    tagged = tag_records(
                        new_records,
                        backend=backend,
                        overwrite=True,
                        progress_callback=on_progress,
                    )
                    merged = upsert_records(load_catalog(), tagged)
                    save_catalog(merged)
                    build_index(merged)
                    st.success(
                        f"抽取并打标 {len(tagged)} 张（{backend}），目录现有 {len(merged)} 条。"
                    )

    if run_tag:
        catalog = load_catalog()
        if not catalog:
            st.warning("目录为空。")
        elif backend == "openai" and not has_openai_key():
            st.error("缺少 OPENAI_API_KEY。")
        else:
            progress = st.progress(0.0, text="打标中…")

            def on_progress(idx, total, record, skipped=False):
                progress.progress(idx / max(total, 1), text=f"打标 {idx}/{total}")

            tagged = tag_records(
                catalog,
                backend=backend,
                overwrite=False,
                progress_callback=on_progress,
            )
            save_catalog(tagged)
            build_index(tagged)
            st.success("已对未确认项完成打标。")

    if run_retag_all:
        catalog = load_catalog()
        if not catalog:
            st.warning("目录为空。")
        elif backend == "openai" and not has_openai_key():
            st.error("缺少 OPENAI_API_KEY。")
        else:
            progress = st.progress(0.0, text="全量重打标…")

            def on_progress(idx, total, record, skipped=False):
                progress.progress(idx / max(total, 1), text=f"重打标 {idx}/{total}")

            tagged = tag_records(
                catalog,
                backend=backend,
                overwrite=True,
                progress_callback=on_progress,
            )
            save_catalog(tagged)
            build_index(tagged)
            st.success("全量重打标完成，等待人工审核。上次人工分类已保留。")

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
    st.subheader("人工审核")
    st.caption("未审核图片已在主页展示；审核通过后确认分类，驳回后从主页移除。")
    statuses = {"待审核": "pending", "已通过": "approved", "已驳回": "rejected"}
    selected_status = st.radio("审核状态", list(statuses), horizontal=True)
    records = [r for r in reload_catalog() if r.review_status == statuses[selected_status]]
    if not records:
        st.info("当前状态下没有图片。新导入的图片会进入待审核。")
        return
    selected_id = st.selectbox("选择图片", [r.id for r in records],
        format_func=lambda rid: next(f"{r.source.source_file} / {r.source.location}" for r in records if r.id == rid))
    record = next(r for r in records if r.id == selected_id)
    left, right = st.columns([1, 2])
    with left:
        if Path(record.image_path).exists():
            st.image(record.image_path, use_container_width=True)
        st.caption(f"{record.source.source_file} / {record.source.location}")
        if record.ai_tags:
            st.write("AI 建议", record.ai_tags.model_dump())
    with right:
        reviewer = st.text_input("审核人（手动填写）", key="reviewer")
        note = st.text_area("审核意见 / 驳回原因（驳回必填）", key=f"note_{record.id}_{record.revision}")
        try:
            if record.review_status != "pending":
                st.write("当前分类", (record.proposed_tags or record.tags).model_dump())
                st.caption(f"上次审核：{record.reviewer or ''}，{record.reviewed_at or ''}，{record.review_note}")
                if st.button("重新提交审核"):
                    review_record(record.id, action="resubmit", reviewer=reviewer,
                        note=note, expected_revision=record.revision)
                    st.rerun()
            else:
                base = record.proposed_tags or record.tags
                values = {}
                for key, options in FILTER_FIELDS.items():
                    current = getattr(base, key)
                    values[key] = st.selectbox(key, options,
                        index=options.index(current) if current in options else 0,
                        key=f"{record.id}_{record.revision}_{key}")
                values["细节元素"] = [x.strip() for x in st.text_input("细节元素（逗号分隔）",
                    value=",".join(base.细节元素), key=f"details_{record.id}_{record.revision}").split(",") if x.strip()]
                values["一句话描述"] = st.text_area("一句话描述", value=base.一句话描述,
                    key=f"description_{record.id}_{record.revision}")
                accept, reject = st.columns(2)
                if accept.button("通过分类", type="primary"):
                    review_record(record.id, action="approve", reviewer=reviewer, note=note,
                        tags=FashionTags(**values), expected_revision=record.revision)
                    st.rerun()
                if reject.button("驳回"):
                    review_record(record.id, action="reject", reviewer=reviewer, note=note,
                        expected_revision=record.revision)
                    st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        with st.expander("分类与审核历史"):
            for event in review_history(record.id):
                action_label = {"approve": "通过", "reject": "驳回", "resubmit": "重新提交审核",
                    "record_created": "导入分类", "classification_proposed": "提交新的分类",
                    "legacy_imported": "迁移历史数据"}.get(event["action"], event["action"])
                st.write(f"{action_label} / {event['actor'] or '系统'} / {event['created_at']}")
                if event["note"]:
                    st.caption(event["note"])
                st.write(event["after"]["proposed_tags"] or event["after"]["tags"])


def main() -> None:
    st.title("服装内部图片库")
    st.caption("MVP：PPT/Excel/PDF 抽图 · GPT-4o-mini 打标 · 文字搜 / 以图搜图 · 人工审核")

    page = st.sidebar.radio(
        "功能",
        ["图片库", "检索", "导入与处理", "人工审核"],
    )
    st.sidebar.markdown("---")
    api_ready = has_openai_key()
    st.sidebar.markdown(
        f"**打标后端**：{'GPT-4o-mini ✅' if api_ready else '未配置 API Key'}  \n"
        "**向量后端**：local-lite  \n"
        "Key 放在项目根目录 `.env`。"
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
