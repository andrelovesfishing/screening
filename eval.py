"""Score output against the practice labels and print every miss with its trace.

Usage: python eval.py <data_folder> <output.jsonl> [trace.jsonl]
"""

import json
import sys

sys.path.insert(0, "system")
from screening.data import Store  # noqa: E402
from screening.text import as_list  # noqa: E402

folder, out = sys.argv[1], sys.argv[2]
traces = {}
if len(sys.argv) > 3:
    traces = {json.loads(l)["hit_id"]: json.loads(l) for l in open(sys.argv[3], encoding="utf-8")}
pred = {json.loads(l)["hit_id"]: json.loads(l) for l in open(out, encoding="utf-8")}
labels = Store(folder).labels

n = dec_ok = code_ok = rec_ok = 0
worst = 0
from collections import Counter
conf = Counter()
for lab in labels:
    p = pred.get(lab["hit_id"])
    n += 1
    if not p:
        print("MISSING", lab["hit_id"])
        continue
    conf[(lab["decision"], p["decision"])] += 1
    ok = p["decision"] == lab["decision"]
    codes = as_list(lab.get("reason_codes"))
    cok = ok and p["reason_code"] in codes
    recs = as_list(lab.get("customer_record_ids"))
    rok = (p["customer_record_id"] in recs) or (not recs and p["customer_record_id"] in (None, "AMBIGUOUS"))
    dec_ok += ok
    code_ok += cok
    rec_ok += rok
    if lab["decision"] == "SAME_ENTITY" and p["decision"] == "DIFFERENT_ENTITY":
        worst += 1
    if not (ok and cok and rok):
        print(f"\n### {lab['hit_id']} want {lab['decision']} {codes} rec {recs} | got {p['decision']} {p['reason_code']} rec {p['customer_record_id']}")
        print("  REF PATH:", lab["path"][:700])
        print("  OUR REASON:", p["reason"])
        if lab["hit_id"] in traces:
            t = traces[lab["hit_id"]]
            print("  TRACE:", json.dumps({k: t[k] for k in ("customer", "persons", "hit", "rules")}, ensure_ascii=False)[:1500])

print(f"\n== {n} labels: decision {dec_ok}/{n}, decision+code {code_ok}/{n}, customer record {rec_ok}/{n}, "
      f"WRONGFUL CLEARS (same->different) {worst}")
print("confusion (truth, ours):", dict(conf))
