"""Usage: python run.py <data_folder> <output.jsonl> [--trace trace.jsonl]

Deterministic: same input folder -> byte-identical output. No network, no randomness.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screening.engine import Engine  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    folder, out = argv[1], argv[2]
    trace_path = argv[argv.index("--trace") + 1] if "--trace" in argv else None
    rows = list(Engine(folder).run())
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    if trace_path:
        with open(trace_path, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps({"hit_id": r["hit_id"], **r["_trace"]}, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} hit decisions to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
