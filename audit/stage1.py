"""Stage 1 of the Grok fact audit: check every DIFFERENT_ENTITY (clear) decision on a data folder.

  python audit/stage1.py <data folder> [--system <path to system/>] [--limit N] [--hits H-1,H-2]
                         [--repeat TAG] [--workers 16]

(a) Blocking facts we missed: Grok's quoted facts are added to ours (names, DOBs, ages, countries;
    adding can only block a clear) and our own unchanged rules decide again. A clear that no longer
    clears is a finding.
(b) The fact a clear rests on, where Grok cannot find it: the hit DOB behind a DOB clear (and whether
    a search result it came from is about the listed person), the report date behind a temporal clear,
    a customer DOB read from a free-text note. Deceased and entity-type clears are listed for a look.

Writes audit/out/stage1<tag>.jsonl (one line per clear) and audit/out/stage1<tag>.md (the readable report).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_engine(system_dir: str):
    sys.path.insert(0, os.path.abspath(system_dir))
    from screening.engine import Engine  # noqa: E402
    from screening import facts as F, rules as R, text as T  # noqa: E402
    return Engine, F, R, T


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--system", default=os.path.join(HERE, "..", "system"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hits", default="")
    ap.add_argument("--repeat", default="", help="tag for an independent second run (fresh calls, separate cache keys)")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args(argv)

    Engine, F, R, T = load_engine(a.system)
    sys.path.insert(0, HERE)
    import extract as X  # noqa: E402
    from grok import Grok  # noqa: E402

    eng = Engine(a.data)
    st = eng.store

    def search_for(hit):
        q = " ".join(sorted(T.fold(hit.get("name") or "").split()))
        return st.search_by_query.get(q, [])

    # ---- our own decisions, keeping the objects the rules used
    cases = {}
    for alert in sorted(st.alerts, key=lambda x: x["alert_id"]):
        persons, rtrace = eng.resolver.resolve(alert)
        profiles = [eng.resolver.profile(p, alert) for p in persons] or [eng.resolver.screened_only(alert)]
        for hid in st.hit_ids(alert):
            hit = st.hits.get(hid)
            if hit is None:
                continue
            hp = F.hit_profile(st, hit)
            outs = [R.decide(p, hp, alert, eng.nr) for p in profiles]
            row = eng._combine(alert, hid, persons, profiles, outs, rtrace, hp)
            cases[hid] = dict(alert=alert, persons=persons, profiles=profiles, rtrace=rtrace, hp=hp, row=row, hit=hit)

    clears = [h for h, c in cases.items() if c["row"]["decision"] == "DIFFERENT_ENTITY"]
    if a.hits:
        want = set(a.hits.split(","))
        clears = [h for h in clears if h in want]
    if a.limit:
        clears = clears[: a.limit]
    print(f"{len(cases)} hits, auditing {len(clears)} clears", file=sys.stderr)

    grok = Grok(X.PROMPT_VERSION + (f"-repeat-{a.repeat}" if a.repeat else ""))

    def ask(hid):
        c = cases[hid]
        srcs = X.sources_for(st, c["hit"], c["persons"], search_for)
        ans = grok.ask(X.SYSTEM, X.user_prompt(srcs), X.SCHEMA)
        return srcs, ans

    answers = grok.map(ask, clears, workers=a.workers)
    print(grok.summary(), file=sys.stderr)

    def countries_of(v: str):
        c = T.country(v)
        return [c] if c else T.countries_in(v)

    results, n_facts, n_dropped, errors = [], 0, 0, []
    for hid, got in zip(clears, answers):
        c = cases[hid]
        if isinstance(got, tuple) and got and got[0] == "ERROR":
            errors.append((hid, got[1]))
            continue
        srcs, ans = got
        facts, dropped = X.verified(ans, srcs)
        n_dropped += len(dropped)
        n_facts += len(dropped) + sum(len(v) for side in ("listed_person", "customer") for v in facts[side].values()
                                      if isinstance(v, list)) + len(facts["search_results"])
        owner = {sid: own for sid, _, own, _ in srcs}
        sr_verdict = {s["source_id"]: s for s in facts["search_results"]}
        lp, cu = facts["listed_person"], facts["customer"]

        def hit_src_ok(sid):
            return not sid.startswith("SR-") or sr_verdict.get(sid, {}).get("about_listed_person") == "yes"

        def ages_to_dates(items):
            out = []
            for f in items:
                at = T.parse_date(f.get("as_of", ""))
                if at and 0 < int(f.get("age") or 0) < 120:
                    out.append((T.age_to_years(int(f["age"]), at), f))
            return out

        grok_hit_dobs = [(T.parse_date(f["value"]), f) for f in lp["dobs"] if hit_src_ok(f["source_id"])]
        grok_hit_dobs = [(d, f) for d, f in grok_hit_dobs if d] + \
            [(d, f) for d, f in ages_to_dates(lp["ages"]) if hit_src_ok(f["source_id"])]

        # ---- (a) add Grok's quoted facts, decide again with our own rules
        hp, profiles, added = copy.deepcopy(c["hp"]), copy.deepcopy(c["profiles"]), []
        have = {d.value.text() for d in hp.dobs}
        for d, f in grok_hit_dobs:
            if d.text() not in have:
                hp.dobs.append(F.Fact(d, (f["source_id"],)))
                have.add(d.text())
                added.append(("hit DOB", d.text(), f))
        for f in lp["names"]:
            if f["value"] not in hp.names and hit_src_ok(f["source_id"]):
                hp.names.append(f["value"])
                added.append(("hit name", f["value"], f))
        for f in lp["countries"]:
            if not hit_src_ok(f["source_id"]):
                continue
            for cc in countries_of(f["value"]):
                if cc not in {x.value for x in hp.countries}:
                    hp.countries.append(F.Fact(cc, (f["source_id"],)))
                    added.append(("hit country", cc, f))
        for p in profiles:
            own = p.record_ids[0] if p.record_ids else None
            for f in cu["names"]:
                if owner.get(f["source_id"]) == own and f["value"] not in [n.value for n in p.names]:
                    p.names.append(F.Fact(f["value"], (f["source_id"],)))
                    added.append((f"customer name ({own})", f["value"], f))
            for f in cu["countries"]:
                if owner.get(f["source_id"]) != own:
                    continue
                for cc in countries_of(f["value"]):
                    if cc not in {x.value for x in p.countries}:
                        p.countries.append(F.Fact(cc, (f["source_id"],)))
                        added.append((f"customer country ({own})", cc, f))
        outs = [R.decide(p, hp, c["alert"], eng.nr) for p in profiles]
        new = eng._combine(c["alert"], hid, c["persons"], profiles, outs, c["rtrace"], hp)
        flip = None
        if new["decision"] != "DIFFERENT_ENTITY":
            flip = {"decision": new["decision"], "reason_code": new["reason_code"], "reason": new["reason"]}

        # ---- (b) the fact the clear rests on, which Grok cannot find
        code, checks = c["row"]["reason_code"], []
        grok_years = [d.year for d, _ in grok_hit_dobs]
        if code == "DOB_MISMATCH":
            for d in c["hp"].dobs:
                for s in d.sources:
                    if s.startswith("SR-"):
                        v = sr_verdict.get(s)
                        if not v or v["about_listed_person"] != "yes":
                            checks.append({"check": "search result behind the hit DOB may not be about the listed person",
                                           "our_fact": f"hit DOB {d.value.text()} from {s}",
                                           "grok": v or "Grok gave no (quoted) verdict for this result"})
                if not any(abs(y - d.value.year) <= 1 for y in grok_years):
                    checks.append({"check": "Grok finds no hit birth date near ours",
                                   "our_fact": f"hit DOB {d.value.text()} from {', '.join(d.sources)}",
                                   "grok": [f for _, f in grok_hit_dobs]})
        if code == "TEMPORAL_IMPOSSIBILITY" and c["hp"].earliest_report:
            er = c["hp"].earliest_report.value
            rd = [(T.parse_date(f["value"]), f) for f in lp["report_dates"]]
            if not any(d and d.year == er.year and (not d.month or not er.month or d.month == er.month) for d, _ in rd):
                checks.append({"check": "Grok cannot confirm the earliest report date",
                               "our_fact": f"earliest report {er.text()}", "grok": [f for _, f in rd]})
        if code in ("DOB_MISMATCH", "TEMPORAL_IMPOSSIBILITY"):
            for p in c["profiles"]:
                if p.dob and any(s.startswith("NOTE-") for s in p.dob.sources):
                    cd = [T.parse_date(f["value"]) for f in cu["dobs"] if f["source_id"] in p.dob.sources]
                    if not any(x and x.text() == p.dob.value.text() for x in cd):
                        checks.append({"check": "customer DOB read from a free-text note; Grok reads it differently",
                                       "our_fact": f"customer DOB {p.dob.value.text()} from {', '.join(p.dob.sources)}",
                                       "grok": [f for f in cu["dobs"] if f["source_id"] in p.dob.sources]})
        if code == "DECEASED":
            checks.append({"check": "deceased clear: look", "our_fact": c["hp"].deceased.value.text() if c["hp"].deceased else None,
                           "grok": lp.get("death_dates")})
        if code == "ENTITY_TYPE_MISMATCH":
            checks.append({"check": "entity-type clear: look", "our_fact": c["hp"].entity_type, "grok": lp.get("entity_type")})

        results.append({"hit_id": hid, "reason_code": code, "reason": c["row"]["reason"], "flip": flip,
                        "added": [{"kind": k, "value": v, "source_id": f["source_id"], "quote": f["quote"]} for k, v, f in added],
                        "checks": checks, "dropped": dropped})

    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    tag = f"-{a.repeat}" if a.repeat else ""
    with open(os.path.join(HERE, "out", f"stage1{tag}.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_report(os.path.join(HERE, "out", f"stage1{tag}.md"), results, n_facts, n_dropped, errors, grok, len(clears))
    flips = sum(1 for r in results if r["flip"])
    flagged = sum(1 for r in results if r["checks"])
    print(f"done: {len(results)} audited, {len(errors)} errors, {flips} flips, {flagged} with checks, "
          f"{n_dropped}/{n_facts} facts dropped for a bad quote", file=sys.stderr)


def write_report(path, results, n_facts, n_dropped, errors, grok, n):
    L = ["# Stage 1: Grok audit of the clears", "",
         f"- Clears audited: {len(results)} of {n} ({len(errors)} API errors)",
         f"- Grok: {grok.summary()}",
         f"- Facts Grok returned: {n_facts}; dropped because the quote is not in the source: {n_dropped}",
         f"- (a) Clears that stop clearing once Grok's quoted facts are added: {sum(1 for r in results if r['flip'])}",
         f"- (b) Clears whose deciding fact Grok cannot confirm: {sum(1 for r in results if r['checks'])}", ""]
    L += ["## (a) Clears that stop clearing", ""]
    for r in results:
        if not r["flip"]:
            continue
        L += [f"### {r['hit_id']}: {r['reason_code']} -> {r['flip']['decision']} {r['flip']['reason_code']}",
              f"- Ours: {r['reason']}", f"- With Grok's facts: {r['flip']['reason']}", "- Facts Grok added:"]
        L += [f"  - {x['kind']}: **{x['value']}** from {x['source_id']}: \"{x['quote']}\"" for x in r["added"]]
        L.append("")
    L += ["## (b) Deciding facts Grok cannot confirm", ""]
    for r in results:
        for ch in r["checks"]:
            L += [f"### {r['hit_id']} ({r['reason_code']}): {ch['check']}", f"- Ours: {ch['our_fact']}",
                  f"- Grok: {json.dumps(ch['grok'], ensure_ascii=False)[:600]}", f"- Our reason: {r['reason']}", ""]
    if errors:
        L += ["## API errors", ""] + [f"- {h}: {e}" for h, e in errors]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
