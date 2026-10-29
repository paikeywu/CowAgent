"""P1-阶段一 (M0/M3): 迁移判据脚本骨架。

判据，不是迁移器本身 —— 文本外置的「重建器/迁移回填」在阶段二/三。
本脚本回答两件事，且**两端必须一致**才算地基成立：

  M0  每个被寻址的库都有四列，且 schema 指纹 = 本 build 期望的指纹
      （两端指纹一致 → 迁移到位；不一致 → NOTRUN，不静默 PASS）
  M3  所有库 text_state 无 NULL（阶段一默认 'projection'，NULL 即缺口）

用法：
    python scripts/memory/migrate_check.py --cow-root C:\\Users\\User\\cow

退出码：0 = 全部通过；1 = 至少一条判据失败；2 = 前置不成立（NOTRUN）。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.memory.schema_drift import (  # noqa: E402
    ADDABLE_COLUMNS,
    schema_fingerprint,
)

FOUR_COLS = ("body_path", "body_anchor", "body_hash", "text_state")


def _index_dbs(cow_root: Path):
    agents = cow_root / "agents"
    if not agents.is_dir():
        return
    for ws in sorted(agents.iterdir()):
        db = ws / "memory" / "long-term" / "index.db"
        if db.is_file():
            yield db
    root_db = cow_root / "memory" / "long-term" / "index.db"
    if root_db.is_file():
        yield root_db


def _reference_fingerprint(dbs):
    """本 build 期望的指纹 = 已迁移参考库的真实指纹。

    一个 ALTER 追加的库列序与干净 CREATE 不同，但**彼此一致**（同一迁移
    路径）。所以参考指纹取「四列齐全 + text_state 无 NULL」的第一个库的
    真实指纹，而不是在内存里重放 DDL —— 内存重放给出的是另一种列序，
    永远对不上真实库。返回 (指纹, 参考库路径)；无可参考库时 (None, None)。
    """
    for db in dbs:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
            if all(c in cols for c in FOUR_COLS):
                null_state = conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE text_state IS NULL"
                ).fetchone()[0]
                if null_state == 0:
                    return schema_fingerprint(db), db
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cow-root", required=True)
    args = ap.parse_args()
    cow_root = Path(args.cow_root)

    dbs = list(_index_dbs(cow_root))
    if not dbs:
        print("NOTRUN: no index.db found under", cow_root)
        return 2

    expected_fp, ref_db = _reference_fingerprint(dbs)
    if expected_fp is None:
        # 没有任何一个库四列齐全 —— 全部待迁移，这是「失败」不是「无库」。
        print("RESULT: FAIL — no index carries the four externalization columns yet")
        for db in dbs:
            print(f"  FAIL {db}  (four cols missing)")
        return 1
    print(f"reference fingerprint (from {ref_db}): {expected_fp}")
    print()

    failures = []
    for db in dbs:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
            missing = [c for c in FOUR_COLS if c not in cols]
            fp = schema_fingerprint(db)
            null_state = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE text_state IS NULL"
            ).fetchone()[0] if "text_state" in cols else -1
        except sqlite3.Error as e:
            print(f"NOTRUN: {db}: cannot read schema: {e}")
            return 2
        finally:
            conn.close()

        ok = (not missing) and fp == expected_fp and null_state == 0
        mark = "PASS" if ok else "FAIL"
        print(f"{mark} {db}")
        print(f"     fingerprint match: {fp == expected_fp} (fp={fp})")
        print(f"     four cols missing: {missing or 'none'}")
        print(f"     text_state NULL rows: {null_state}")
        if not ok:
            failures.append(str(db))

    print()
    if failures:
        print(f"RESULT: FAIL — {len(failures)}/{len(dbs)} index(es) not migrated")
        return 1
    print(f"RESULT: PASS — {len(dbs)}/{len(dbs)} indexes match the expected schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
