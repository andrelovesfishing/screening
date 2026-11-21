"""Stage 3 of the Grok audit: fact recall under new wording. Which facts does our engine stop reading
when the same text is written in another style?

  python audit/stage3.py <data folder> [--system <path to system/>] [--limit N] [--workers 16]

Decision flips undercount the risk: a lost fact only changes a decision when the other facts of that hit
happen to line up, and on the unseen set they line up differently. So this stage counts facts.

1. For every hit, the fact-bearing free text: the first paragraph of the profile notes and its
   "According to ...; <date>:" lines, the search snippets for its name, and the customer's notes.
2. Grok rewrites each text in three styles (label, terse, formal), keeping every fact.
3. Code rejects any rewrite that breaks an SOP invariant (the unseen set keeps these fixed) or changes a
   fact: a listed name, a listing phrase, the count of dated-report lines, a year, or that introduces an
   ambiguous all-numeric date. A rejected rewrite keeps the original text, so it cannot raise a false alarm.
4. One reworded copy of the data folder per style; our unchanged engine runs on each with --trace.
5. Per fact type and style: facts read before, lost, changed and gained, with the sentence that lost each;
   plus decision changes priced with the brief's cost table (taking the original decisions as right).

Output: audit/out/stage3.md, audit/out/stage3.jsonl; reworded folders in audit/out/reworded3-<style>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_VERSION = "reword3-v1"
STYLES = ["label", "terse", "formal"]
MAX_CHARS = 1500

SYSTEM = """You rewrite short texts for a robustness test of a sanctions-screening system that reads \
facts from free text with hand-written patterns. For every input text, write THREE versions that state \
exactly the same facts:

- "label": record or form style, facts as labelled fields or short labelled phrases, e.g. \
"Date of birth: 17 July 1939", "DOB 17-Jul-1939", "Year of birth: 1952", "Age: 36 (as of Oct 1987)", \
"Residence: Kenya", "Nationality: Kenyan".
- "terse": a busy clerk's shorthand: abbreviations, few verbs, minimal punctuation, e.g. \
"cust says lived in UK 2015-16, now NG", "b. 1966", "aged 36 in Oct 87" is NOT allowed (keep 4-digit years).
- "formal": long, formal, official prose with subordinate clauses and synonyms ("date of birth", \
"was born", "came into the world", "resident in", "domiciled in", "a national of").

Hard rules for every version:
1. Keep every fact: every person's name exactly as written (same spelling and order), every date, age, \
year, country, document, role and event. Add nothing, drop nothing, never change a value.
2. Years always as 4 digits. Never write an all-numeric date whose day and month could be swapped \
(e.g. 05/06/1980); use a month name, or ISO YYYY-MM-DD.
3. Keep these phrases word for word when they occur: "a sanctioned individual", "a politically exposed \
person", "the subject of enforcement action", "named in adverse media", "a state-owned enterprise \
official", "named on a watchlist".
4. A line of the form "According to <source>; <date>: <text>" keeps exactly that form (the date may be \
written in another format). Never create such a line where the original has none.
5. Countries may be written as a country name or a nationality adjective, never as a city or region.
6. Return every input text with its id."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rewrites"],
          "properties": {"rewrites": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["id"] + STYLES,
              "properties": {"id": {"type": "string"}, **{s: {"type": "string"} for s in STYLES}}}}}}

PHRASES = ["sanctioned individual", "politically exposed person", "subject of enforcement action",
           "named in adverse media", "state-owned enterprise official", "named on a watchlist"]
_REPORT = re.compile(r"^\s*According to .+?;\s*([^;:]{3,40}?)\s*:", re.M)
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b")
_NUMDATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b")
COST = {("DIFFERENT_ENTITY", "SAME_ENTITY"): 3, ("DIFFERENT_ENTITY", "REVIEW"): 3,
        ("SAME_ENTITY", "DIFFERENT_ENTITY"): 60, ("SAME_ENTITY", "REVIEW"): 2,
        ("REVIEW", "DIFFERENT_ENTITY"): 60, ("REVIEW", "SAME_ENTITY"): 2}


def violation(orig: str, new: str, names: list[str]) -> str | None:
    """Why this rewrite may not be used (it changes something the SOP fixes, or a fact), else None."""
    lo, ln = orig.casefold(), new.casefold()
    for n in names:
        if n and n.casefold() in lo and n.casefold() not in ln:
            return "name"
    for p in PHRASES:
        if lo.count(p) != ln.count(p):
            return "listing phrase"
    if len(_REPORT.findall(orig)) != len(_REPORT.findall(new)):
        return "dated-report line"
    if not set(_YEAR.findall(orig)) <= set(_YEAR.findall(new)):
        return "year"
    small = lambda s: set(re.findall(r"(?<![\d.,/:-])\b\d{1,3}\b(?![\d.,/:-]\d)", s))  # noqa: E731
    if not small(orig) <= set(re.findall(r"\d+", new)):
        return "number written out"
    for m in _NUMDATE.finditer(new):
        a, b = int(m.group(1)), int(m.group(2))
        if a <= 12 and b <= 12 and a != b and m.group(0) not in orig:
            return "ambiguous numeric date"
    return None


def first_part(notes: str) -> tuple[str, str]:
    """(fact-bearing head, untouched rest): the first paragraph, plus any dated-report lines."""
    paras = notes.split("\n\n")
    head = paras[0]
    rest = "\n\n".join(paras[1:])
    return head[:MAX_CHARS], head[MAX_CHARS:] + ("\n\n" + rest if rest else "")


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

    def run(folder, tag):
        o, t = os.path.join(out_dir, f"s3_{tag}.jsonl"), os.path.join(out_dir, f"s3_{tag}_trace.jsonl")
        subprocess.run([sys.executable, os.path.join(system, "run.py"), folder, o, "--trace", t],
                       check=True, stderr=subprocess.DEVNULL)
        rows = {r["hit_id"]: r for r in map(json.loads, open(o, encoding="utf-8"))}
        tr = {r["hit_id"]: r for r in map(json.loads, open(t, encoding="utf-8"))}
        return rows, tr

    base, base_tr = run(a.data, "base")
    eng = Engine(a.data)
    st = eng.store
    hit_alert = {h: al for al in st.alerts for h in st.hit_ids(al)}
    hids = sorted(base)
    if a.limit:
        hids = hids[: a.limit]

    # ---- fact-bearing texts, each owned by the first hit that needs it
    owned, jobs, text_names = set(), [], {}
    for hid in hids:
        hit = st.hits[hid]
        names = [n for p in base_tr[hid]["persons"] for n in p["names"]] + base_tr[hid]["hit"]["names"]
        texts = []
        notes = hit.get("profile_notes") or ""
        if notes.strip():
            texts.append((f"hit:{hid}", first_part(notes)[0]))
            for line in notes.split("\n"):
                if _REPORT.match(line):
                    texts.append((f"rep:{hid}:{len(texts)}", line[:MAX_CHARS]))
        q = " ".join(sorted(fold(hit.get("name") or "").split()))
        for sr in st.search_by_query.get(q, []):
            texts.append((f"sr:{sr['result_id']}", sr.get("snippet") or ""))
        persons, _ = eng.resolver.resolve(hit_alert[hid])
        emails = {(st.customers[r].get("email") or "").lower() for p in persons for r in p} - {""}
        for e in sorted(emails):
            for n in st.notes_by_email.get(e, []):
                texts.append((f"note:{n['note_id']}", (n.get("text") or "")[:MAX_CHARS]))
        texts = [(i, t) for i, t in texts if i not in owned and t.strip()]
        owned |= {i for i, _ in texts}
        for i, _ in texts:
            text_names[i] = names
        for k in range(0, len(texts), 6):  # small batches: Grok answers every text
            jobs.append((hid, texts[k:k + 6]))
    print(f"{len(hids)} hits, {len(owned)} texts in {len(jobs)} calls", file=sys.stderr)

    grok = Grok(PROMPT_VERSION)

    def reword(job):
        _, texts = job
        user = "\n\n".join(f"[TEXT {i}]\n{t}" for i, t in texts)
        ans = grok.ask(SYSTEM, user, SCHEMA)
        bare = {i.split(":", 1)[1]: i for i, _ in texts}
        out = {}
        for r in ans["rewrites"]:
            rid = r["id"].strip()
            out[bare.get(rid, rid)] = {s: r[s] for s in STYLES}
        return out

    got = {}
    for r in grok.map(reword, jobs, workers=a.workers):
        if not (isinstance(r, tuple) and r and r[0] == "ERROR"):
            got.update(r)
    print(grok.summary(), file=sys.stderr)
    orig_text = {i: t for _, ts in jobs for i, t in ts}

    results, rejected, used = {}, collections.Counter(), collections.Counter()
    for style in STYLES:
        new_text = {}
        for i, t in orig_text.items():
            nt = (got.get(i) or {}).get(style, "")
            if not nt.strip():
                rejected[(style, "missing")] += 1
                continue
            why = violation(t, nt, text_names.get(i, []))
            if why:
                rejected[(style, why)] += 1
                continue
            new_text[i] = nt
            used[style] += 1

        rdir = os.path.join(out_dir, f"reworded3-{style}")
        shutil.rmtree(rdir, ignore_errors=True)
        shutil.copytree(a.data, rdir)

        def rewrite_file(name, fn):
            p = os.path.join(rdir, name)
            rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
            for r in rows:
                fn(r)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

        def hits_fn(r):
            notes = r.get("profile_notes") or ""
            k = f"hit:{r['hit_id']}"
            if k in new_text:
                head, rest = first_part(notes)
                notes = new_text[k] + rest
            lines = notes.split("\n")
            for key, nt in new_text.items():
                if key.startswith(f"rep:{r['hit_id']}:"):
                    old = orig_text[key]
                    lines = [nt if ln[:MAX_CHARS] == old else ln for ln in lines]
            r["profile_notes"] = "\n".join(lines) if (r.get("profile_notes") or "") else r.get("profile_notes")

        def sr_fn(r):
            k = f"sr:{r['result_id']}"
            if k in new_text:
                r["snippet"] = new_text[k]

        def note_fn(r):
            k = f"note:{r['note_id']}"
            if k in new_text:
                r["text"] = new_text[k] + (r.get("text") or "")[MAX_CHARS:]

        rewrite_file("hits.jsonl", hits_fn)
        rewrite_file("search_results.jsonl", sr_fn)
        rewrite_file("notes.jsonl", note_fn)
        rows, tr = run(rdir, style)
        results[style] = (rows, tr, new_text)

    # ---- compare facts and decisions
    def sources_of(t, kind):
        return t

    report = collections.OrderedDict()
    examples = collections.defaultdict(list)
    lines_out = []
    for style in STYLES:
        rows, tr, new_text = results[style]
        c = collections.Counter()
        cost = 0
        for hid in hids:
            b0, b1 = base_tr[hid], tr[hid]
            h0, h1 = b0["hit"], b1["hit"]
            ev = []
            for k in ("dobs", "countries"):
                x, y = set(h0[k]), set(h1[k])
                c[f"hit {k[:-1] if k == 'dobs' else 'country'}: read"] += len(x)
                if x - y:
                    c[f"hit {k[:-1] if k == 'dobs' else 'country'}: lost"] += len(x - y)
                    ev.append(f"hit {k} lost {sorted(x - y)} (from {h0.get('dob_origin') if k == 'dobs' else 'fields'})")
                if y - x:
                    c[f"hit {k[:-1] if k == 'dobs' else 'country'}: gained"] += len(y - x)
                    ev.append(f"hit {k} gained {sorted(y - x)}")
            c["report date: read"] += bool(h0["earliest_report"])
            if h0["earliest_report"] != h1["earliest_report"]:
                c["report date: changed"] += 1
                ev.append(f"earliest report {h0['earliest_report']} -> {h1['earliest_report']}")
            for p0, p1 in zip(b0["persons"], b1["persons"]):
                c["customer dob: read"] += bool(p0["dob"])
                # an ambiguous date ("X (or Y if read month-first)") reworded unambiguously as X or Y is no change
                if p0["dob"] != p1["dob"] and not (p1["dob"] and "(or " in (p0["dob"] or "") and p1["dob"] in p0["dob"]):
                    c["customer dob: changed"] += 1
                    ev.append(f"customer dob {p0['dob']} -> {p1['dob']} ({p0['facts'][:1]})")
                x, y = set(p0["countries"]), set(p1["countries"])
                c["customer country: read"] += len(x)
                if x - y:
                    c["customer country: lost"] += len(x - y)
                    ev.append(f"customer countries lost {sorted(x - y)}")
                if y - x:
                    c["customer country: gained"] += len(y - x)
                    ev.append(f"customer countries gained {sorted(y - x)}")
            d0, d1 = base[hid]["decision"], rows[hid]["decision"]
            cst = COST.get((d0, d1), 0)
            cost += cst
            if d0 != d1:
                c[f"decision {d0} -> {d1}"] += 1
            if ev or d0 != d1:
                changed = [{"id": i, "original": orig_text[i][:600], "reworded": new_text[i][:600]}
                           for i in orig_text if i in new_text and (i.startswith(f"hit:{hid}") or i.startswith(f"rep:{hid}:")
                                                                    or i[3:] in json.dumps(b0) or i[5:] in json.dumps(b0))]
                item = {"style": style, "hit_id": hid, "facts": ev, "decision": f"{d0} -> {d1}", "cost": cst,
                        "texts": changed[:4]}
                lines_out.append(item)
                for e in ev:
                    examples[(style, e.split(" ")[0] + " " + e.split(" ")[1])].append(item)
        report[style] = (c, cost)

    with open(os.path.join(out_dir, "stage3.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for x in lines_out:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    keys = sorted({k for c, _ in report.values() for k in c})
    L = ["# Stage 3: fact recall under new wording", "",
         f"- Hits: {len(hids)}; texts: {len(orig_text)}; Grok: {grok.summary()}",
         "- Rewrites used per style: " + ", ".join(f"{s} {used[s]}" for s in STYLES),
         "- Rewrites rejected (kept original): " + ", ".join(f"{s}/{w} {n}" for (s, w), n in sorted(rejected.items())), "",
         "| Fact / decision | " + " | ".join(STYLES) + " |", "|---|" + "---|" * len(STYLES)]
    L += [f"| {k} | " + " | ".join(str(report[s][0].get(k, 0)) for s in STYLES) + " |" for k in keys]
    L += ["| **decision cost vs original** | " + " | ".join(str(report[s][1]) for s in STYLES) + " |", ""]
    L += ["## Examples (up to 4 per style and fact type)", ""]
    for (style, kind), items in sorted(examples.items()):
        L.append(f"### {style}: {kind}")
        for it in items[:4]:
            L.append(f"- {it['hit_id']} ({it['decision']}, cost {it['cost']}): " + "; ".join(it["facts"]))
            for t in it["texts"][:2]:
                if t["original"] != t["reworded"]:
                    L.append(f"  - {t['id']} ORIG: {t['original'][:220]!r}")
                    L.append(f"  - {t['id']} NEW: {t['reworded'][:220]!r}")
        L.append("")
    with open(os.path.join(out_dir, "stage3.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L[:len(keys) + 9]), file=sys.stderr)


if __name__ == "__main__":
    main()
