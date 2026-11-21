"""Stage 2 of the Grok audit: the flip test. Does new wording make our engine clear a hit it should not?

  python audit/stage2.py <data folder> [--system <path to system/>] [--limit N] [--workers 16]

1. Take every hit our engine decides as REVIEW or SAME_ENTITY (decisions that do not clear).
2. Grok rewrites the free text behind each one (hit profile notes, search-result snippets and titles,
   customer notes) in new wording, keeping every fact. Structured fields are left alone.
3. Write a reworded copy of the data folder to audit/out/reworded/ (git-ignored: it is dev data) and run
   our unchanged engine on it.
4. Any target hit that becomes DIFFERENT_ENTITY is a would-be unsafe clear caused only by wording: the
   facts are the same, so the decision should be too. Report it with the original and reworded text.

The reworded folder is also a reusable second test set: run system/run.py on it after any change.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_VERSION = "reword-v1"
MAX_CHARS = 3000  # longer texts: only the first MAX_CHARS are reworded, the rest is kept as is

SYSTEM = """You rewrite short texts for a robustness test of a sanctions-screening system. The system \
reads facts out of free text with hand-written patterns; we want to know whether it still reads the \
same facts when the same information is worded differently.

Rewrite each text so that it states EXACTLY the same facts in different words. Rules:
1. Keep every fact: every person's name (copied exactly as written, same spelling and order), every \
date, age, year, country, document, role and event. Add nothing, drop nothing, never change a value.
2. Change the wording freely: sentence structure and order, synonyms ("born" -> "date of birth", \
"birth date", "DOB", "came into the world"; "aged 36" -> "36 years old", "then 36"), active/passive, \
punctuation.
3. Change date formats: e.g. "10 April 1960" -> "April 10th, 1960", "10/04/1960" (day first), \
"1960-04-10", "the 10th of April 1960". A numeric date must be day-first and unambiguous to a careful \
reader of UK documents. Keep partial dates partial (a year stays only a year).
4. Countries may be written as the country name, a common alternative name, or a nationality adjective \
("Nigerian", "a Kenyan national"), but never replaced by a city or region.
5. If a line starts with a fixed prefix like "According to <source>; <date>:", keep that prefix but \
write the date in another format.
6. Keep roughly the same length. Return every input text, with its id."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rewrites"],
          "properties": {"rewrites": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["id", "text"],
              "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--system", default=os.path.join(HERE, "..", "system"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args(argv)
    system = os.path.abspath(a.system)
    sys.path.insert(0, system)
    sys.path.insert(0, HERE)
    from screening.engine import Engine  # noqa: E402
    from screening.text import fold  # noqa: E402
    from grok import Grok  # noqa: E402

    out_dir = os.path.join(HERE, "out")
    os.makedirs(out_dir, exist_ok=True)
    base_out = os.path.join(out_dir, "stage2_base.jsonl")
    subprocess.run([sys.executable, os.path.join(system, "run.py"), a.data, base_out], check=True)
    base = {r["hit_id"]: r for r in map(json.loads, open(base_out, encoding="utf-8"))}
    targets = sorted(h for h, r in base.items() if r["decision"] != "DIFFERENT_ENTITY")
    if a.limit:
        targets = targets[: a.limit]

    eng = Engine(a.data)
    st = eng.store
    hit_alert = {h: al for al in st.alerts for h in st.hit_ids(al)}
    # free texts behind each target, each text owned by the first target that needs it
    owned, jobs = set(), []
    for hid in targets:
        alert = hit_alert[hid]
        hit = st.hits[hid]
        texts = []
        if (hit.get("profile_notes") or "").strip():
            texts.append((f"hit:{hid}", hit["profile_notes"]))
        q = " ".join(sorted(fold(hit.get("name") or "").split()))
        for sr in st.search_by_query.get(q, []):
            texts.append((f"sr:{sr['result_id']}", f"{sr.get('title', '')}\n{sr.get('snippet', '')}"))
        persons, _ = eng.resolver.resolve(alert)
        emails = {(st.customers[r].get("email") or "").lower() for p in persons for r in p} - {""}
        for e in sorted(emails):
            for n in st.notes_by_email.get(e, []):
                texts.append((f"note:{n['note_id']}", n.get("text") or ""))
        texts = [(i, t) for i, t in texts if i not in owned and t.strip()]
        owned |= {i for i, _ in texts}
        if texts:
            jobs.append((hid, texts))
    print(f"{len(targets)} non-clear hits, {len(jobs)} with free text, {len(owned)} texts to reword", file=sys.stderr)

    grok = Grok(PROMPT_VERSION)

    def reword(job):
        hid, texts = job
        heads = [(i, t[:MAX_CHARS]) for i, t in texts]
        user = "\n\n".join(f"[TEXT {i}]\n{t}" for i, t in heads)
        ans = grok.ask(SYSTEM, user, SCHEMA)
        got = {r["id"]: r["text"] for r in ans["rewrites"]}
        return {i: got[i] + t[MAX_CHARS:] for i, t in texts if got.get(i, "").strip()}

    res = grok.map(reword, jobs, workers=a.workers)
    print(grok.summary(), file=sys.stderr)
    new_text, errors = {}, []
    for (hid, _), r in zip(jobs, res):
        if isinstance(r, tuple) and r and r[0] == "ERROR":
            errors.append((hid, r[1]))
        else:
            new_text.update(r)

    # ---- reworded copy of the data folder (raw JSON lines, only the free-text fields change)
    rdir = os.path.join(out_dir, "reworded")
    shutil.rmtree(rdir, ignore_errors=True)
    shutil.copytree(a.data, rdir)
    old_text = {}

    def rewrite_file(name, fn):
        p = os.path.join(rdir, name)
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        for r in rows:
            fn(r)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def hits_fn(r):
        k = f"hit:{r['hit_id']}"
        if k in new_text:
            old_text[k] = r.get("profile_notes")
            r["profile_notes"] = new_text[k]

    def sr_fn(r):
        k = f"sr:{r['result_id']}"
        if k in new_text:
            old_text[k] = f"{r.get('title', '')}\n{r.get('snippet', '')}"
            title, _, snip = new_text[k].partition("\n")
            r["title"], r["snippet"] = (title, snip) if snip.strip() else (r.get("title"), new_text[k])

    def note_fn(r):
        k = f"note:{r['note_id']}"
        if k in new_text:
            old_text[k] = r.get("text")
            r["text"] = new_text[k]

    rewrite_file("hits.jsonl", hits_fn)
    rewrite_file("search_results.jsonl", sr_fn)
    rewrite_file("notes.jsonl", note_fn)

    new_out = os.path.join(out_dir, "stage2_reworded.jsonl")
    subprocess.run([sys.executable, os.path.join(system, "run.py"), rdir, new_out], check=True)
    new = {r["hit_id"]: r for r in map(json.loads, open(new_out, encoding="utf-8"))}

    job_texts = dict(jobs)
    flips, other = [], []
    for hid in targets:
        o, n = base[hid], new.get(hid)
        if not n or n["decision"] == o["decision"]:
            continue
        item = {"hit_id": hid, "before": {k: o[k] for k in ("decision", "reason_code", "reason")},
                "after": {k: n[k] for k in ("decision", "reason_code", "reason")},
                "texts": [{"id": i, "original": old_text.get(i, t)[:1500], "reworded": new_text.get(i, "")[:1500]}
                          for i, t in job_texts.get(hid, [])]}
        (flips if n["decision"] == "DIFFERENT_ENTITY" else other).append(item)
    lost_clears = sum(1 for h, r in base.items() if r["decision"] == "DIFFERENT_ENTITY"
                      and new.get(h, {}).get("decision") != "DIFFERENT_ENTITY")

    with open(os.path.join(out_dir, "stage2.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for x in flips + other:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    L = ["# Stage 2: flip test (same facts, new wording)", "",
         f"- Non-clear hits tested: {len(targets)}; texts reworded: {len(new_text)}; Grok errors: {len(errors)}",
         f"- Grok: {grok.summary()}",
         f"- **Became a clear (would-be unsafe clears): {len(flips)}**",
         f"- Changed between REVIEW and SAME: {len(other)}",
         f"- Clears lost under new wording (costs ~3 each, not 60): {lost_clears}", "",
         "## Became a clear", ""]
    for x in flips:
        L += [f"### {x['hit_id']}: {x['before']['decision']} {x['before']['reason_code']} -> "
              f"{x['after']['decision']} {x['after']['reason_code']}",
              f"- Before: {x['before']['reason']}", f"- After: {x['after']['reason']}"]
        for t in x["texts"]:
            if t["original"] != t["reworded"]:
                L += [f"- {t['id']} original: {t['original'][:500]!r}", f"- {t['id']} reworded: {t['reworded'][:500]!r}"]
        L.append("")
    L += ["## REVIEW <-> SAME changes", ""] + [
        f"- {x['hit_id']}: {x['before']['decision']} {x['before']['reason_code']} -> {x['after']['decision']} {x['after']['reason_code']}"
        for x in other]
    with open(os.path.join(out_dir, "stage2.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"done: {len(flips)} became clears, {len(other)} REVIEW<->SAME, {lost_clears} clears lost", file=sys.stderr)


if __name__ == "__main__":
    main()
