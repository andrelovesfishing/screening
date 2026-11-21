"""Malformed-data smoke test: damage a copy of a data folder deterministically, run the engine, and report
every hit that had to fall back to REVIEW (each one is a crash point).

Usage: python tests/mutate_smoke.py <data folder> [rate]

Damage per record field, chosen by a seeded RNG: delete it, set it to null, make it "", turn a string into a
list or a number, or turn a list into a string. The run must still write one valid row per hit.
"""

import io
import json
import os
import random
import shutil
import sys
import tempfile
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system"))
from screening.engine import Engine  # noqa: E402

KEEP = {"hit_id", "alert_id", "record_id", "customer_id", "note_id", "check_id", "result_id"}  # keys stay intact


def damage(v, rng):
    kind = rng.choice(["del", "null", "empty", "type"])
    if kind == "del":
        return KeyError
    if kind == "null":
        return None
    if kind == "empty":
        return [] if isinstance(v, list) else ""
    if isinstance(v, list):
        return "; ".join(map(str, v))
    if isinstance(v, str):
        return rng.choice([[v], 12345])
    return str(v)


def mutate_folder(src, dst, rate, seed=7):
    rng = random.Random(seed)
    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if not name.endswith(".jsonl") or name == "practice_labels.jsonl":
            if os.path.isfile(s):
                shutil.copy(s, d)
            continue
        with open(s, encoding="utf-8") as f, open(d, "w", encoding="utf-8") as g:
            for line in f:
                row = json.loads(line)
                for k in sorted(row):
                    if k not in KEEP and rng.random() < rate:
                        v = damage(row[k], rng)
                        if v is KeyError:
                            del row[k]
                        else:
                            row[k] = v
                g.write(json.dumps(row) + "\n")


def main(argv):
    src, rate = argv[1], float(argv[2]) if len(argv) > 2 else 0.1
    with tempfile.TemporaryDirectory() as tmp:
        mutate_folder(src, tmp, rate)
        err = io.StringIO()
        with redirect_stderr(err):
            rows = list(Engine(tmp).run())
        with open(os.path.join(tmp, "hits.jsonl"), encoding="utf-8") as f:
            n_hits = sum(1 for _ in f)
    warnings = [w for w in err.getvalue().splitlines() if w.startswith("warning")]
    kinds = {}
    for w in warnings:
        k = w.split(": ", 2)[2].split(" -> ")[0][:90]
        kinds[k] = kinds.get(k, 0) + 1
    print(f"rate {rate}: {len(rows)} rows for {n_hits} hits, {len(warnings)} fallbacks")
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {v:4}  {k}")
    return 0 if len(rows) == n_hits else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
