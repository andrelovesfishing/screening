"""Orchestration: alert -> persons -> per-hit decision, with a full decision trace."""

from __future__ import annotations

import sys

from .data import Store
from .facts import CustomerResolver, hit_profile
from .rules import Outcome, decide
from .text import NameRules


class Engine:
    def __init__(self, folder: str):
        self.store = Store(folder)
        self.nr = NameRules(self.store.variant_groups, self.store.legal_forms)
        self.resolver = CustomerResolver(self.store, self.nr)

    def run(self):
        """One row per hit in hits.jsonl, always. Anything that fails on one hit (or its alert) gives that
        hit a REVIEW row instead of stopping the run: when in doubt, REVIEW. Errors go to stderr only,
        so the output stays byte-identical run to run."""
        done = set()
        by_alert: dict = {}
        for hid, h in self.store.hits.items():
            by_alert.setdefault(h.get("alert_id"), []).append(hid)
        for alert in sorted(self.store.alerts, key=lambda a: str(a.get("alert_id"))):
            aid = alert.get("alert_id")
            hids = list(dict.fromkeys(self.store.hit_ids(alert) + sorted(by_alert.get(aid, []))))
            try:
                persons, rtrace = self.resolver.resolve(alert)
                profiles = [self.resolver.profile(p, alert) for p in persons]
                if not profiles:
                    profiles = [self.resolver.screened_only(alert)]
            except Exception as e:  # noqa: BLE001 - one bad alert must not sink the file
                _warn(aid, e)
                persons = None
            for hid in hids:
                hit = self.store.hits.get(hid)
                if hit is None or hid in done:
                    continue
                done.add(hid)
                if persons is None:
                    yield fallback(hid, hit.get("alert_id") or aid, "its customer could not be resolved")
                    continue
                try:
                    hp = hit_profile(self.store, hit)
                    outs = [decide(p, hp, alert, self.nr) for p in profiles]
                    row = self._combine(alert, hid, persons, profiles, outs, rtrace, hp)
                    row["alert_id"] = hit.get("alert_id") or row["alert_id"]
                    yield row
                except Exception as e:  # noqa: BLE001
                    _warn(hid, e)
                    yield fallback(hid, hit.get("alert_id") or aid, "its records could not be read")
        for hid in sorted(self.store.hits):
            if hid not in done:
                yield fallback(hid, self.store.hits[hid].get("alert_id"), "no alert lists it")

    def _combine(self, alert, hid, persons, profiles, outs: list[Outcome], rtrace, hp) -> dict:
        if len(outs) > 1:
            if len({o.decision for o in outs}) == 1:
                o = outs[0]
                evidence = sorted({e for x in outs for e in x.evidence})
                code, decision = o.code, o.decision
                reason = (f"{len(outs)} people in the system of record fit the alert, and each leads to this decision; "
                          f"for the first ({', '.join(persons[0])}): {o.reason}")
            else:
                decision, code = "REVIEW", "AMBIGUOUS_CUSTOMER_RECORD"
                evidence = sorted({r for p in persons for r in p})
                reason = ("More than one person in the system of record fits the alert ("
                          + "; ".join(", ".join(p) for p in persons)
                          + ") and they lead to different decisions ("
                          + ", ".join(f"{o.decision} {o.code}" for o in outs) + ")."
                          + " The decision depends on which of them the customer is, so a person should review it.")
            rid = "AMBIGUOUS"
        else:
            o = outs[0]
            decision, code, evidence, reason = o.decision, o.code, o.evidence, o.reason
            rid = persons[0][0] if persons else None
        trace = {
            "customer": rtrace,
            "persons": [{"records": p.record_ids, "facts": p.trace,
                         "names": [n.value for n in p.names], "dob": p.dob.value.text() if p.dob else None,
                         "countries": sorted({c.value for c in p.countries})} for p in profiles],
            "hit": {"names": hp.names, "dobs": [d.value.text() for d in hp.dobs], "dob_origin": hp.dob_origin,
                    "countries": [c.value for c in hp.countries],
                    "earliest_report": hp.earliest_report.value.text() if hp.earliest_report else None,
                    "deceased": hp.deceased.value.text() if hp.deceased else None, "notes": hp.trace},
            "rules": [t for o in outs for t in o.trace],
        }
        return {"hit_id": hid, "alert_id": alert["alert_id"], "decision": decision, "reason_code": code,
                "customer_record_id": rid, "evidence": evidence, "reason": reason, "_trace": trace}


def fallback(hid: str, aid, why: str) -> dict:
    """The safe answer when a hit cannot be decided automatically."""
    return {"hit_id": hid, "alert_id": aid, "decision": "REVIEW", "reason_code": "INSUFFICIENT_DATA",
            "customer_record_id": None, "evidence": [],
            "reason": f"This hit was not decided automatically because {why}. "
                      "Nothing has been checked for or against it, so a person should review it.",
            "_trace": {"fallback": why}}


def _warn(where, e: Exception) -> None:
    print(f"warning: {where}: {type(e).__name__}: {e} -> REVIEW", file=sys.stderr)
