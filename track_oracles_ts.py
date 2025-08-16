#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
track_oracles_ts.py — Tree-sitter TypeScript oracle change detector (build grammar locally)

- Parses true TypeScript syntax (Tree-sitter) for DefiLlama protocol files
- Scans data.ts, data1.ts, data2.ts, data3.ts, data4.ts
- Extracts id, name, oracles[], oraclesBreakdown[] (name/type only)
- Stores snapshot (oracle_state.json) next to this script
- CLI:
    --repo <path>        (required) path to defi/src/protocols
    --out human|json     (default human)
    --dry-run            do not write oracle_state.json
    --dump-id <ID>       print parsed entry for ID and exit
    --dump-all           list all parsed IDs and exit
    --debug-ast          print per-file object counts
"""

import argparse
import json
import subprocess
from html import escape 
from pathlib import Path
from typing import Dict, List, Optional

from tree_sitter import Language, Parser  # requires tree_sitter==0.20.4

# ---------------- Config ----------------
TARGET_FILES = ["data.ts", "data1.ts", "data2.ts", "data3.ts", "data4.ts"]

SCRIPT_DIR = Path(__file__).parent.resolve()
SNAPSHOT_FILE = SCRIPT_DIR / "oracle_state.json"

VENDOR_DIR = SCRIPT_DIR / "vendor"
TS_REPO_DIR = VENDOR_DIR / "tree-sitter-typescript"
TS_LANG_DIR = TS_REPO_DIR / "typescript"
BUILD_DIR = SCRIPT_DIR / "build"
LANG_SO = BUILD_DIR / "my-languages.so"
LANG_NAME = "typescript"

# ---------------- Grammar setup ----------------
def ensure_ts_language() -> Language:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    if not TS_REPO_DIR.exists():
        print("ℹ️  Cloning tree-sitter-typescript grammar …")
        subprocess.run(
            ["git", "clone", "--depth=1", "https://github.com/tree-sitter/tree-sitter-typescript.git", str(TS_REPO_DIR)],
            check=True
        )

    if not LANG_SO.exists():
        print("ℹ️  Building TypeScript parser library …")
        Language.build_library(
            str(LANG_SO),
            [str(TS_LANG_DIR)],
        )

    return Language(str(LANG_SO), LANG_NAME)

# ---------------- Tree-sitter helpers ----------------
def get_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

def unquote(s: str) -> str:
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s

def node_is_string(node) -> bool:
    return node.type == "string"

def node_is_array(node) -> bool:
    return node.type == "array"

def node_is_object(node) -> bool:
    return node.type == "object"

def iter_object_pairs(obj_node):
    for child in obj_node.children:
        if child.type == "pair":
            key = child.child_by_field_name("key")
            val = child.child_by_field_name("value")
            if key is None or val is None:
                continue
            yield key, val

def key_name(key_node, src: bytes) -> Optional[str]:
    if key_node.type == "property_identifier":
        return get_text(key_node, src)
    if key_node.type == "string":
        return unquote(get_text(key_node, src))
    return None

def array_string_values(arr_node, src: bytes) -> List[str]:
    out = []
    for el in arr_node.named_children:
        if node_is_string(el):
            out.append(unquote(get_text(el, src)))
    return out

def oracles_breakdown_items(arr_node, src: bytes) -> List[dict]:
    items = []
    for el in arr_node.named_children:
        if not node_is_object(el):
            continue
        name_val = ""
        type_val = ""
        for k_node, v_node in iter_object_pairs(el):
            k = key_name(k_node, src)
            if k == "name" and node_is_string(v_node):
                name_val = unquote(get_text(v_node, src))
            elif k == "type" and node_is_string(v_node):
                type_val = unquote(get_text(v_node, src))
        if name_val or type_val:
            items.append({"name": name_val, "type": type_val})
    items.sort(key=lambda x: (x["name"], x["type"]))
    return items

def object_to_protocol_min(obj_node, src: bytes, file_name: str) -> Optional[Dict]:
    pid = None
    name_val = ""
    oracles = []
    breakdown = []
    for k_node, v_node in iter_object_pairs(obj_node):
        k = key_name(k_node, src)
        if not k:
            continue
        if k == "id" and node_is_string(v_node):
            pid = unquote(get_text(v_node, src))
        elif k == "name" and node_is_string(v_node):
            name_val = unquote(get_text(v_node, src))
        elif k == "oracles" and node_is_array(v_node):
            oracles = sorted(set(array_string_values(v_node, src)))
        elif k == "oraclesBreakdown" and node_is_array(v_node):
            breakdown = oracles_breakdown_items(v_node, src)
    if not pid:
        return None
    return {
        "id": pid,
        "name": name_val,
        "file": file_name,
        "oracles": oracles,
        "oraclesBreakdown": breakdown,
    }

def parse_file_ts(parser: Parser, path: Path) -> Dict[str, dict]:
    src = path.read_bytes()
    tree = parser.parse(src)
    root = tree.root_node
    by_id: Dict[str, dict] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node_is_object(node):
            mini = object_to_protocol_min(node, src, path.name)
            if mini and mini["id"]:
                by_id[mini["id"]] = mini
        for child in node.children:
            stack.append(child)
    return by_id

# ---------------- Diffing ----------------
def breakdown_name_to_type(lst: List[dict]) -> Dict[str, str]:
    return { (x.get("name") or "").strip(): (x.get("type") or "").strip()
             for x in lst if (x.get("name") or "").strip() }

def diff_states(prev: Dict[str, dict], nxt: Dict[str, dict]) -> List[dict]:
    changes = []
    for pid in sorted(set(prev) | set(nxt)):
        a, b = prev.get(pid), nxt.get(pid)
        if not a or not b:
            continue
        a_or, b_or = set(a.get("oracles", [])), set(b.get("oracles", []))
        or_added, or_removed = b_or - a_or, a_or - b_or
        a_bt, b_bt = breakdown_name_to_type(a.get("oraclesBreakdown", [])), breakdown_name_to_type(b.get("oraclesBreakdown", []))
        names_prev, names_next = set(a_bt), set(b_bt)
        type_changes = [(n, a_bt[n], b_bt[n]) for n in sorted(names_prev & names_next) if a_bt[n] != b_bt[n]]
        added_names = sorted(names_next - names_prev)
        removed_names = sorted(names_prev - names_next)
        plus = sorted(set(added_names) | or_added)
        minus = sorted(set(removed_names) | or_removed)
        if plus or minus or type_changes:
            changes.append({
                "id": pid,
                "name": b.get("name") or a.get("name"),
                "file": b.get("file"),
                "plus": plus,
                "minus": minus,
                "types": type_changes,
            })
    return changes

# ---------------- Snapshot ----------------
def load_snapshot() -> Optional[Dict[str, dict]]:
    if SNAPSHOT_FILE.exists():
        return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return None

def save_snapshot(state: Dict[str, dict]):
    SNAPSHOT_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def print_human(changes: List[dict]):
    """
    HTML-ready output for Telegram (set TG_PARSE_MODE=HTML in your .env).
    Uses <b>, <i>, <code>, and keeps emojis tasteful.
    """
    if not changes:
        # no need to escape here—static text
        print("✨ No oracle changes today!")
        return

    lines = []
    for c in changes:
        name = escape(c.get("name") or "")
        pid  = escape(c.get("id") or "")
        file = escape(c.get("file") or "")

        # Header line (the runner will optionally append a (Commit) link)
        lines.append(f"🛠️ <b>Protocol {name}</b> (id <code>{pid}</code>) on <i>{file}</i> has the following changes:")

        # Additions / removals (from `oracles` and/or new/removed entries in `oraclesBreakdown`)
        for n in c.get("plus", []):
            lines.append(f"  ➕ <b>{escape(n)}</b>")
        for n in c.get("minus", []):
            lines.append(f"  ➖ <b>{escape(n)}</b>")

        # Type changes within oraclesBreakdown
        for name_old_new in c.get("types", []):
            oname, old, new = name_old_new
            oname = escape(oname or "")
            old   = escape(old or "(none)")
            new   = escape(new or "(none)")
            lines.append(f"  🔄 <b>{oname}</b> (type: <code>{old}</code> → <code>{new}</code>)")

        # blank line between protocols
        lines.append("")

    lines.append(f"📌 Total protocols with oracle changes: {len(changes)}")
    print("\n".join(lines))

# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="Path to data folder (defi/src/protocols)")
    ap.add_argument("--out", choices=["human", "json"], default="human")
    ap.add_argument("--dry-run", action="store_true", help="Do not write oracle_state.json")
    ap.add_argument("--dump-id", help="Print the parsed entry for the given protocol id and exit")
    ap.add_argument("--dump-all", action="store_true", help="List all parsed IDs and exit")
    ap.add_argument("--debug-ast", action="store_true", help="Per-file object counts while parsing")
    args = ap.parse_args()

    ts_lang = ensure_ts_language()
    parser = Parser()
    parser.set_language(ts_lang)

    repo = Path(args.repo).resolve()
    dataset: Dict[str, dict] = {}

    for fname in TARGET_FILES:
        path = repo / fname
        if not path.exists():
            continue
        per_file = parse_file_ts(parser, path)
        if args.debug_ast:
            print(f"(ast) {fname}: objects_as_protocols={len(per_file)}")
        dataset.update(per_file)

    if args.dump_all:
        print(json.dumps(sorted(dataset.keys()), indent=2))
        return
    if args.dump_id:
        entry = dataset.get(args.dump_id)
        if not entry:
            print(f"(debug) id {args.dump_id} not found in parsed dataset")
        else:
            print(json.dumps(entry, indent=2, ensure_ascii=False))
        return

    prev = load_snapshot()
    if prev is None:
        if args.dry_run:
            print("DRY-RUN: no snapshot found; would initialize oracle_state.json.")
            return
        save_snapshot(dataset)
        print("Initialized snapshot at oracle_state.json. Next run will show changes.")
        return

    changes = diff_states(prev, dataset)
    if args.out == "json":
        print(json.dumps({"changed_count": len(changes), "changes": changes}, indent=2, ensure_ascii=False))
    else:
        print_human(changes)

    if not args.dry_run:
        save_snapshot(dataset)

if __name__ == "__main__":
    main()
