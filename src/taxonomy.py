"""Fixed two-level garment categories, sleeve length, fabric, and synonyms.

Import never creates a category. Unknown phrases resolve to 待归类 until someone
assigns one and the original wording is stored here.
"""

from __future__ import annotations

from dataclasses import dataclass

UNSORTED = "待归类"
SLEEVE_LENGTHS = ("无袖", "短袖", "中袖", "长袖")
FABRICS = ("针织", "梭织")
_SLEEVE_PREFIXES = ("长袖", "短袖", "中袖", "无袖")

# level1, child names. A row with an empty child is the level itself.
TREE = (
    ("上衣", ("套衫", "开衫", "背心/马甲", "T恤", "衬衫", "卫衣")),
    ("外套", ("针织外套", "大衣", "夹克", "西装", "羽绒/棉服")),
    ("连衣裙", ()),
    ("半身裙", ()),
    ("裤装", ("长裤", "短裤", "针织裤")),
    ("套装", ()),
    ("配饰", ()),
)

# phrase, level1, level2, sleeve, fabric. Covers archive wording and common aliases.
SEED_SYNONYMS = (
    ("长袖套衫", "上衣", "套衫", "长袖", "针织"),
    ("短袖套衫", "上衣", "套衫", "短袖", "针织"),
    ("套衫", "上衣", "套衫", "", "针织"),
    ("套头衫", "上衣", "套衫", "", "针织"),
    ("毛衣", "上衣", "套衫", "", "针织"),
    ("针织衫", "上衣", "套衫", "", "针织"),
    ("长袖开衫", "上衣", "开衫", "长袖", "针织"),
    ("短袖开衫", "上衣", "开衫", "短袖", "针织"),
    ("开衫", "上衣", "开衫", "", "针织"),
    ("针织开衫", "上衣", "开衫", "", "针织"),
    ("背心", "上衣", "背心/马甲", "无袖", "针织"),
    ("马甲", "上衣", "背心/马甲", "无袖", "针织"),
    ("背心/马甲", "上衣", "背心/马甲", "无袖", ""),
    ("吊带", "上衣", "背心/马甲", "无袖", ""),
    ("T恤", "上衣", "T恤", "短袖", ""),
    ("短T", "上衣", "T恤", "短袖", ""),
    ("衬衫", "上衣", "衬衫", "", "梭织"),
    ("卫衣", "上衣", "卫衣", "长袖", "针织"),
    ("针织外套", "外套", "针织外套", "", "针织"),
    ("大衣", "外套", "大衣", "", ""),
    ("风衣", "外套", "大衣", "", ""),
    ("夹克", "外套", "夹克", "", ""),
    ("西装", "外套", "西装", "", "梭织"),
    ("羽绒/棉服", "外套", "羽绒/棉服", "", ""),
    ("羽绒服", "外套", "羽绒/棉服", "", ""),
    ("棉服", "外套", "羽绒/棉服", "", ""),
    ("外套", "外套", "", "", ""),
    ("连衣裙", "连衣裙", "", "", ""),
    ("裙子", "半身裙", "", "", ""),
    ("半裙", "半身裙", "", "", ""),
    ("半身裙", "半身裙", "", "", ""),
    ("长裤", "裤装", "长裤", "", ""),
    ("短裤", "裤装", "短裤", "", ""),
    ("针织裤", "裤装", "针织裤", "", "针织"),
    ("阔腿裤", "裤装", "长裤", "", ""),
    ("打底裤", "裤装", "长裤", "", ""),
    ("裤装", "裤装", "", "", ""),
    ("套装", "套装", "", "", ""),
    ("配饰", "配饰", "", "", ""),
    ("其他", UNSORTED, "", "", ""),
    ("待归类", UNSORTED, "", "", ""),
    ("上衣", "上衣", "", "", ""),
)

_CACHE: dict[str, "Taxonomy"] = {}


class TaxonomyError(ValueError):
    """A category edit the page can show as-is."""


@dataclass(frozen=True)
class Placement:
    level1: str
    level2: str
    sleeve: str
    fabric: str
    raw: str
    unmapped: bool = False

    @property
    def label(self) -> str:
        if not self.level1 or self.level1 == UNSORTED:
            return UNSORTED
        if self.level2:
            return f"{self.level1}>{self.level2}"
        return self.level1


@dataclass(frozen=True)
class Synonym:
    phrase: str
    level1: str
    level2: str
    sleeve: str
    fabric: str
    learned: bool = False


@dataclass
class Taxonomy:
    nodes: list[tuple[str, str, int]]
    synonyms: dict[str, Synonym]

    def labels(self) -> list[str]:
        ordered = []
        parents = []
        children: dict[str, list[str]] = {}
        for level1, level2, _pos in self.nodes:
            if level1 == UNSORTED:
                continue
            if not level2:
                parents.append(level1)
            else:
                children.setdefault(level1, []).append(level2)
        for level1 in parents:
            kids = children.get(level1) or []
            if not kids:
                ordered.append(level1)
            else:
                ordered.append(level1)
                ordered.extend(f"{level1}>{name}" for name in kids)
        ordered.append(UNSORTED)
        return ordered

    def tree(self) -> list[dict]:
        grouped: dict[str, list[str]] = {}
        order: list[str] = []
        for level1, level2, _pos in self.nodes:
            if level1 == UNSORTED:
                continue
            if level1 not in grouped:
                grouped[level1] = []
                order.append(level1)
            if level2 and level2 not in grouped[level1]:
                grouped[level1].append(level2)
        return [{"level1": name, "children": grouped[name]} for name in order]

    def has(self, level1: str, level2: str = "") -> bool:
        return any(a == level1 and b == level2 for a, b, _pos in self.nodes)


def phrase_key(value: str) -> str:
    return (value or "").strip().replace(" ", "").replace("\u3000", "").casefold()


def label_of(level1: str, level2: str = "") -> str:
    if not level1 or level1 == UNSORTED:
        return UNSORTED
    return f"{level1}>{level2}" if level2 else level1


def default_labels() -> list[str]:
    return _seed_taxonomy().labels()


def _seed_taxonomy() -> Taxonomy:
    nodes = []
    position = 0
    for level1, children in TREE:
        nodes.append((level1, "", position))
        position += 1
        for child in children:
            nodes.append((level1, child, position))
            position += 1
    nodes.append((UNSORTED, "", position))
    synonyms = {}
    for phrase, level1, level2, sleeve, fabric in SEED_SYNONYMS:
        synonyms[phrase_key(phrase)] = Synonym(phrase, level1, level2, sleeve, fabric, False)
    return Taxonomy(nodes, synonyms)


def _db_key(conn) -> str:
    from src.db import is_mysql

    if is_mysql(conn):
        row = conn.execute("SELECT DATABASE()").fetchone()
        return "mysql:" + str(row[0] if row else "")
    row = conn.execute("PRAGMA database_list").fetchone()
    return str(row[2] if row else "")


def _remember(conn, taxonomy: Taxonomy) -> Taxonomy:
    from src.db import is_mysql

    _CACHE[_db_key(conn)] = taxonomy
    if is_mysql(conn):
        return taxonomy
    try:
        from pathlib import Path

        from src import catalog

        _CACHE[str(catalog.DATABASE_PATH)] = taxonomy
        _CACHE[str(Path(catalog.DATABASE_PATH).resolve())] = taxonomy
    except Exception:
        pass
    return taxonomy


def active() -> Taxonomy:
    """Seed table until a database connection has loaded the saved one."""
    from pathlib import Path

    from src import catalog
    from src.db import mysql_config

    try:
        settings = mysql_config()
    except RuntimeError:
        settings = None
    if settings:
        cached = _CACHE.get("mysql:" + settings["database"])
        return cached if cached is not None else _seed_taxonomy()

    for key in (str(catalog.DATABASE_PATH), str(Path(catalog.DATABASE_PATH).resolve())):
        if key in _CACHE:
            return _CACHE[key]
    return _seed_taxonomy()


def ensure_taxonomy(conn) -> Taxonomy:
    from src.db import MYSQL_TAXONOMY_SCHEMA, exec_script, is_mysql

    if is_mysql(conn):
        exec_script(conn, MYSQL_TAXONOMY_SCHEMA)
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS garment_categories (
                level1 TEXT NOT NULL,
                level2 TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                PRIMARY KEY (level1, level2)
            );
            CREATE TABLE IF NOT EXISTS category_synonyms (
                phrase_key TEXT PRIMARY KEY,
                phrase TEXT NOT NULL,
                level1 TEXT NOT NULL,
                level2 TEXT NOT NULL DEFAULT '',
                sleeve_length TEXT NOT NULL DEFAULT '',
                fabric TEXT NOT NULL DEFAULT '',
                learned INTEGER NOT NULL DEFAULT 0
            );
        """)
    if conn.execute("SELECT 1 FROM garment_categories LIMIT 1").fetchone() is None:
        conn.executemany(
            "INSERT INTO garment_categories (level1, level2, position) VALUES (?, ?, ?)",
            _seed_taxonomy().nodes,
        )
    if conn.execute("SELECT 1 FROM category_synonyms LIMIT 1").fetchone() is None:
        conn.executemany(
            "INSERT INTO category_synonyms (phrase_key, phrase, level1, level2, sleeve_length, fabric, learned) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            [
                (phrase_key(phrase), phrase, level1, level2, sleeve, fabric)
                for phrase, level1, level2, sleeve, fabric in SEED_SYNONYMS
            ],
        )
    # Seeding runs before the catalog transaction starts. Close it so BEGIN IMMEDIATE can open.
    if conn.in_transaction:
        conn.commit()
    return _remember(conn, read_taxonomy(conn))


def read_taxonomy(conn) -> Taxonomy:
    nodes = [
        (row["level1"], row["level2"], row["position"])
        for row in conn.execute("SELECT level1, level2, position FROM garment_categories ORDER BY position, level1, level2")
    ]
    synonyms = {}
    for row in conn.execute(
        "SELECT phrase_key, phrase, level1, level2, sleeve_length, fabric, learned FROM category_synonyms"
    ):
        synonyms[row["phrase_key"]] = Synonym(
            row["phrase"], row["level1"], row["level2"], row["sleeve_length"], row["fabric"], bool(row["learned"]),
        )
    return Taxonomy(nodes, synonyms)


def resolve(phrase: str, *, factory: bool = False) -> Placement:
    raw = (phrase or "").strip()
    key = phrase_key(raw)
    taxonomy = active()
    if not key:
        return Placement(UNSORTED, "", "", "针织" if factory else "", raw, True)

    split = _split_known(raw, taxonomy)
    if split is not None:
        level1, level2 = split
        sleeve, fabric = _attrs_for(key, taxonomy)
        if factory and not fabric:
            fabric = "针织"
        return Placement(level1, level2, sleeve, fabric, raw, False)

    found = taxonomy.synonyms.get(key)
    if found is None:
        for prefix in _SLEEVE_PREFIXES:
            if key.startswith(phrase_key(prefix)) and len(key) > len(phrase_key(prefix)):
                rest = taxonomy.synonyms.get(key[len(phrase_key(prefix)):])
                if rest is not None:
                    sleeve = rest.sleeve or prefix
                    fabric = rest.fabric or ("针织" if factory else "")
                    return Placement(rest.level1, rest.level2, sleeve, fabric, raw, False)
        return Placement(UNSORTED, "", "", "针织" if factory else "", raw, True)

    fabric = found.fabric or ("针织" if factory else "")
    return Placement(found.level1, found.level2, found.sleeve, fabric, raw, False)


def _split_known(raw: str, taxonomy: Taxonomy) -> tuple[str, str] | None:
    text = raw.strip()
    if text == UNSORTED and taxonomy.has(UNSORTED, ""):
        return UNSORTED, ""
    if ">" in text:
        level1, level2 = [part.strip() for part in text.split(">", 1)]
        if taxonomy.has(level1, level2):
            return level1, level2
        return None
    if taxonomy.has(text, ""):
        return text, ""
    return None


def _attrs_for(key: str, taxonomy: Taxonomy) -> tuple[str, str]:
    found = taxonomy.synonyms.get(key)
    if found is None:
        return "", ""
    return found.sleeve, found.fabric


def snapshot(conn=None) -> dict:
    taxonomy = read_taxonomy(conn) if conn is not None else active()
    return {
        "unsorted": UNSORTED,
        "sleeves": list(SLEEVE_LENGTHS),
        "fabrics": list(FABRICS),
        "tree": taxonomy.tree(),
        "labels": taxonomy.labels(),
        "synonyms": [
            {
                "phrase": item.phrase,
                "level1": item.level1,
                "level2": item.level2,
                "label": label_of(item.level1, item.level2),
                "sleeve_length": item.sleeve,
                "fabric": item.fabric,
                "learned": item.learned,
            }
            for item in sorted(taxonomy.synonyms.values(), key=lambda item: (item.learned, item.phrase))
        ],
    }


def _require_node(taxonomy: Taxonomy, level1: str, level2: str) -> None:
    if level1 == UNSORTED:
        raise TaxonomyError("待归类是系统品类，不能当作新的归类目标")
    if not taxonomy.has(level1, ""):
        raise TaxonomyError("一级品类不在固定品类表里")
    if level2 and not taxonomy.has(level1, level2):
        raise TaxonomyError("二级品类不在固定品类表里")
    kids = [child for parent, child, _pos in taxonomy.nodes if parent == level1 and child]
    if kids and not level2:
        raise TaxonomyError("这个一级品类还有二级，请选到二级")


def add_category(conn, level1: str, level2: str = "") -> Taxonomy:
    level1, level2 = (level1 or "").strip(), (level2 or "").strip()
    if not level1 or level1 == UNSORTED or ">" in level1 or ">" in level2:
        raise TaxonomyError("品类名称不能为空，也不能包含 >")
    taxonomy = read_taxonomy(conn)
    if level2 and not taxonomy.has(level1, ""):
        raise TaxonomyError("请先添加一级品类")
    if taxonomy.has(level1, level2):
        raise TaxonomyError("这个品类已经在表里")
    position = max((item[2] for item in taxonomy.nodes), default=0) + 1
    conn.execute(
        "INSERT INTO garment_categories (level1, level2, position) VALUES (?, ?, ?)",
        (level1, level2, position),
    )
    return _remember(conn, read_taxonomy(conn))


def rename_category(conn, level1: str, level2: str, new_level1: str, new_level2: str) -> Taxonomy:
    level2 = level2 or ""
    new_level1, new_level2 = (new_level1 or "").strip(), (new_level2 or "").strip()
    if (level1, level2) == (UNSORTED, "") or new_level1 == UNSORTED:
        raise TaxonomyError("待归类是系统品类，不能改名")
    if not new_level1 or ">" in new_level1 or ">" in new_level2:
        raise TaxonomyError("新品类名称不能为空，也不能包含 >")
    taxonomy = read_taxonomy(conn)
    if not taxonomy.has(level1, level2):
        raise TaxonomyError("原来的品类不在表里")
    if (new_level1, new_level2) != (level1, level2) and taxonomy.has(new_level1, new_level2):
        raise TaxonomyError("改名后的品类已经存在")
    if not level2:
        children = [child for parent, child, _pos in taxonomy.nodes if parent == level1 and child]
        if children and new_level1 != level1:
            conn.execute("UPDATE garment_categories SET level1=? WHERE level1=?", (new_level1, level1))
            conn.execute("UPDATE category_synonyms SET level1=? WHERE level1=?", (new_level1, level1))
        elif new_level2:
            raise TaxonomyError("一级品类不能直接改成某一个二级")
        else:
            conn.execute(
                "UPDATE garment_categories SET level1=? WHERE level1=? AND level2=''",
                (new_level1, level1),
            )
    else:
        if new_level1 != level1 and not taxonomy.has(new_level1, ""):
            raise TaxonomyError("目标一级品类不在表里")
        conn.execute(
            "UPDATE garment_categories SET level1=?, level2=? WHERE level1=? AND level2=?",
            (new_level1, new_level2, level1, level2),
        )
        conn.execute(
            "UPDATE category_synonyms SET level1=?, level2=? WHERE level1=? AND level2=?",
            (new_level1, new_level2, level1, level2),
        )
    old, new = label_of(level1, level2), label_of(new_level1 if not level2 else new_level1, new_level2 if level2 else "")
    if not level2 and new_level1 != level1:
        _rewrite_prefix(conn, level1, new_level1)
    elif level2 and old != label_of(new_level1, new_level2):
        _rewrite_label(conn, old, label_of(new_level1, new_level2))
    return _remember(conn, read_taxonomy(conn))


def delete_category(conn, level1: str, level2: str = "") -> Taxonomy:
    level2 = level2 or ""
    if (level1, level2) == (UNSORTED, ""):
        raise TaxonomyError("待归类是系统品类，不能删除")
    taxonomy = read_taxonomy(conn)
    if not taxonomy.has(level1, level2):
        raise TaxonomyError("这个品类不在表里")
    if not level2:
        children = [child for parent, child, _pos in taxonomy.nodes if parent == level1 and child]
        if children:
            raise TaxonomyError("请先删除这个一级品类下面的二级品类")
    used = usage_count(conn, level1, level2)
    if used:
        raise TaxonomyError(f"还有 {used} 条资料在用「{label_of(level1, level2)}」，请先把这些资料改归到其他品类")
    conn.execute("DELETE FROM garment_categories WHERE level1=? AND level2=?", (level1, level2))
    conn.execute("DELETE FROM category_synonyms WHERE level1=? AND level2=?", (level1, level2))
    return _remember(conn, read_taxonomy(conn))


def usage_count(conn, level1: str, level2: str = "") -> int:
    label = label_of(level1, level2)
    total = 0
    for row in conn.execute("SELECT payload FROM records"):
        import json
        data = json.loads(row["payload"] if isinstance(row, dict) or hasattr(row, "keys") else row[0])
        phrases = []
        for key in ("tags", "proposed_tags", "ai_tags"):
            block = data.get(key) or {}
            if isinstance(block, dict) and block.get("品类"):
                phrases.append(str(block["品类"]))
        if any(resolve(phrase).label == label for phrase in phrases):
            total += 1
    for row in conn.execute("SELECT category FROM style_variants"):
        raw = row["category"] if hasattr(row, "keys") else row[0]
        if raw and resolve(str(raw), factory=True).label == label:
            total += 1
    return total


def save_synonym(conn, phrase: str, level1: str, level2: str = "", sleeve: str = "", fabric: str = "", *, learned: bool = False) -> Taxonomy:
    phrase = (phrase or "").strip()
    level2 = level2 or ""
    sleeve = (sleeve or "").strip()
    fabric = (fabric or "").strip()
    if not phrase_key(phrase):
        raise TaxonomyError("请填写原始叫法")
    if sleeve and sleeve not in SLEEVE_LENGTHS:
        raise TaxonomyError("袖长只能是无袖、短袖、中袖或长袖")
    if fabric and fabric not in FABRICS:
        raise TaxonomyError("面料只能是针织或梭织")
    taxonomy = read_taxonomy(conn)
    if level1 != UNSORTED:
        _require_node(taxonomy, level1, level2)
    elif not taxonomy.has(UNSORTED, ""):
        raise TaxonomyError("待归类不在品类表里")
    from src.db import on_conflict_update

    conn.execute(
        """
        INSERT INTO category_synonyms (phrase_key, phrase, level1, level2, sleeve_length, fabric, learned)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """ + on_conflict_update(
            "phrase_key",
            ["phrase", "level1", "level2", "sleeve_length", "fabric", "learned"],
        ),
        (phrase_key(phrase), phrase, level1, level2, sleeve, fabric, 1 if learned else 0),
    )
    return _remember(conn, read_taxonomy(conn))


def delete_synonym(conn, phrase: str) -> Taxonomy:
    conn.execute("DELETE FROM category_synonyms WHERE phrase_key=?", (phrase_key(phrase),))
    return _remember(conn, read_taxonomy(conn))


def learn_phrase(conn, phrase: str, level1: str, level2: str = "", sleeve: str = "", fabric: str = "") -> None:
    """Remember an unsorted wording. Canonical labels and seeded specifics stay put."""
    key = phrase_key(phrase)
    if not key or key in {phrase_key(label_of(level1, level2)), phrase_key(UNSORTED)}:
        return
    current = active().synonyms.get(key)
    if current is not None and not current.learned and current.level1 != UNSORTED:
        return
    placement = resolve(phrase)
    if current is None and not placement.unmapped and placement.label != UNSORTED:
        return
    try:
        _require_node(active(), level1, level2)
    except TaxonomyError:
        return
    save_synonym(
        conn, phrase, level1, level2,
        sleeve or (current.sleeve if current else ""),
        fabric or (current.fabric if current else ""),
        learned=True,
    )


def _rewrite_label(conn, old: str, new: str) -> None:
    import json
    for row in conn.execute("SELECT id, payload FROM records"):
        data = json.loads(row["payload"])
        changed = False
        for key in ("tags", "proposed_tags", "ai_tags"):
            block = data.get(key)
            if isinstance(block, dict) and block.get("品类") == old:
                block["品类"] = new
                changed = True
        if changed:
            conn.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))


def _rewrite_prefix(conn, old_level1: str, new_level1: str) -> None:
    import json
    prefix = old_level1 + ">"
    replacement = new_level1 + ">"
    for row in conn.execute("SELECT id, payload FROM records"):
        data = json.loads(row["payload"])
        changed = False
        for key in ("tags", "proposed_tags", "ai_tags"):
            block = data.get(key)
            if not isinstance(block, dict) or "品类" not in block:
                continue
            label = str(block.get("品类") or "")
            if label == old_level1:
                block["品类"] = new_level1
                changed = True
            elif label.startswith(prefix):
                block["品类"] = replacement + label[len(prefix):]
                changed = True
        if changed:
            conn.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))


def migrate_record_labels(conn) -> None:
    """Remap stored categories once. Revision stays put so open reviews do not 409."""
    import json
    if conn.execute("SELECT 1 FROM migrations WHERE name='taxonomy_v1'").fetchone():
        return
    for row in conn.execute("SELECT id, payload FROM records"):
        data = json.loads(row["payload"])
        changed = False
        raw_phrase = ""
        sleeve = ""
        fabric = ""
        for key in ("tags", "proposed_tags", "ai_tags"):
            block = data.get(key)
            if not isinstance(block, dict) or "品类" not in block:
                continue
            phrase = str(block.get("品类") or "")
            placement = resolve(phrase)
            if phrase and phrase != placement.label:
                if not raw_phrase:
                    raw_phrase = phrase
                block["品类"] = placement.label
                changed = True
            if placement.sleeve and not block.get("袖长"):
                block["袖长"] = placement.sleeve
                sleeve = sleeve or placement.sleeve
                changed = True
            if placement.fabric and not block.get("面料"):
                block["面料"] = placement.fabric
                fabric = fabric or placement.fabric
                changed = True
        if raw_phrase and not data.get("category_raw"):
            data["category_raw"] = raw_phrase
            changed = True
        if sleeve and not data.get("sleeve_length"):
            data["sleeve_length"] = sleeve
            changed = True
        if fabric and not data.get("fabric"):
            data["fabric"] = fabric
            changed = True
        for field, default in (("category_raw", ""), ("sleeve_length", ""), ("fabric", "")):
            if field not in data:
                data[field] = default
                changed = True
        if changed:
            conn.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
    conn.execute("INSERT INTO migrations VALUES ('taxonomy_v1')")
