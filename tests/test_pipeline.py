"""解析管线自测：100 单全量入库，统计拦截情况与脏数据类型覆盖。"""
import json
from collections import Counter

from common.db import get_conn
from datagen.generator import generate
from parser_service.ingest import ingest_file


def main():
    from bootstrap import bootstrap
    bootstrap(reset=True)
    manifest = json.loads(open("data/manifest.json", encoding="utf-8").read())
    n_bad = 0
    for rec in manifest:
        r = ingest_file(rec["excel"], rec["pdf"], defect_type=rec.get("defect_type", ""))
        if not r["valid"]:
            n_bad += 1
    conn = get_conn()
    stats = Counter(r["status"] for r in conn.execute("SELECT status FROM tasks"))
    caught = Counter()
    for rec in manifest:
        if not rec["dirty"]:
            continue
        row = conn.execute("SELECT status, validation_errors FROM tasks WHERE ref_no=?",
                           (rec["ref_no"],)).fetchone()
        caught[(rec["defect_type"], row["status"])] += 1
    conn.close()
    print("task status:", dict(stats))
    print(f"dirty intercepted: {n_bad}/20")
    for k, v in sorted(caught.items()):
        print(" ", k, v)
    assert stats.get("invalid") == 20 and stats.get("queued") == 80
    print("PIPELINE OK")


if __name__ == "__main__":
    main()
