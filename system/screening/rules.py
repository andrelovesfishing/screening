"""The SOP as an ordered list of small rules (principles 6 and 7).

Each contradiction rule returns an Outcome or None. The first one that fires clears the hit.
If none fires, the agreement rules decide between SAME_ENTITY and REVIEW.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .facts import Fact, HitProfile, Person, is_company
from .text import D, NameRules, parse_date


@dataclass
class Outcome:
    decision: str
    code: str
    evidence: list[str]
    reason: str
    trace: list[str] = field(default_factory=list)


def _src(*facts, exclude: str = "") -> list[str]:
    out = []
    for f in facts:
        if f is None:
            continue
        for s in (f.sources if isinstance(f, Fact) else f):
            if s and s != exclude and s not in out:
                out.append(s)
    return out


# ---------------------------------------------------------------- date logic


def dob_contradicted(c: D, h: D) -> bool:
    """Principle 6: the customer's birth year is more than 1 year outside every year this date
    could be, or same year with a differing month (or day)."""
    if all(abs(c.year - y) > 1 for y in h.years):
        return True
    if c.year in h.years and len(h.years) == 1:
        if c.month and h.month and c.month != h.month:
            return True
        if c.month and h.month and c.day and h.day and c.day != h.day:
            return True
    return False


def dob_agrees(c: D, h: D) -> bool:
    """Principle 7: birth year is one of the hit's possible years and every shared part is equal."""
    if c.year not in h.years:
        return False
    if len(h.years) > 1:
        return True
    if c.month and h.month and c.month != h.month:
        return False
    if c.day and h.day and c.day != h.day:
        return False
    return True


def _plus_years(d: D, n: int) -> tuple[int, int, int]:
    y, m, dd = d.earliest_ordinal()
    return (y + n, m, dd)


# ---------------------------------------------------------------- context


class Ctx:
    def __init__(self, person: Person, hit: HitProfile, alert: dict, nr: NameRules):
        self.p, self.h, self.alert, self.nr = person, hit, alert, nr
        self.company = is_company(person.entity_type or alert.get("entity_type"))
        self.records = person.record_ids
        # name agreement (for confirming): customer's best names vs hit names and aliases
        self.name_match = None
        for cn in person.names:
            if not self.company and len(nr.parts(cn.value)) < 2:
                continue
            for hn in hit.names:
                if nr.agree(cn.value, hn, self.company):
                    self.name_match = (cn, hn)
                    break
            if self.name_match:
                break
        self.dob_match = None
        if person.dob:
            for hd in hit.dobs:
                if dob_agrees(person.dob.value, hd.value):
                    self.dob_match = hd
                    break
        hit_cs = {c.value for c in hit.countries}
        self.shared_countries = [c for c in person.countries if c.value in hit_cs]


# ---------------------------------------------------------------- contradiction rules


def rule_type(x: Ctx):
    if is_company(x.h.entity_type) != x.company:
        kind = lambda b: "a company" if b else "a person"
        return Outcome("DIFFERENT_ENTITY", "ENTITY_TYPE_MISMATCH", _src(x.records),
                       f"The customer is {kind(x.company)} but the hit {x.h.names[0]} is {kind(is_company(x.h.entity_type))}.")


def rule_deceased(x: Ctx):
    d = x.h.deceased
    screened = parse_date(x.alert.get("screened_at", ""))
    if d and screened and d.value.latest_ordinal() < screened.earliest_ordinal():
        return Outcome("DIFFERENT_ENTITY", "DECEASED", _src(x.records),
                       f"The hit {x.h.names[0]} died in {d.value.text()}, before the customer was screened on {screened.text()}.")


def rule_temporal(x: Ctx):
    r, dob = x.h.earliest_report, x.p.dob
    if not r or not dob:
        return None
    if x.company:
        if dob.value.earliest_ordinal() > r.value.latest_ordinal():
            return Outcome("DIFFERENT_ENTITY", "TEMPORAL_IMPOSSIBILITY", _src(x.records, dob, exclude=x.h.hit_id),
                           f"The customer company was incorporated on {dob.value.text()}, after the hit's earliest dated report ({r.value.text()}).")
        return None
    if _plus_years(dob.value, 16) > r.value.latest_ordinal():
        return Outcome("DIFFERENT_ENTITY", "TEMPORAL_IMPOSSIBILITY", _src(x.records, dob, exclude=x.h.hit_id),
                       f"The customer was born on {dob.value.text()} ({', '.join(dob.sources)}), so was younger than 16 at the hit's earliest dated report ({r.value.text()}).")


def rule_dob(x: Ctx):
    if not x.p.dob or not x.h.dobs:
        return None
    c = x.p.dob.value
    if all(dob_contradicted(c, h.value) for h in x.h.dobs):
        hs = ", ".join(h.value.text() for h in x.h.dobs)
        return Outcome("DIFFERENT_ENTITY", "DOB_MISMATCH", _src(x.p.dob, *x.h.dobs, exclude=x.h.hit_id),
                       f"The customer's date of birth {c.text()} ({', '.join(x.p.dob.sources)}) contradicts every birth date the hit gives ({hs}, from its {x.h.dob_origin}).")


def rule_name(x: Ctx):
    v = x.p.verified_name
    if not v:
        return None
    if not x.company and len(x.nr.parts(v.value)) < 2:
        return None
    if not any(x.nr.agree(v.value, hn, x.company) for hn in x.h.names):
        return Outcome("DIFFERENT_ENTITY", "NAME_MISMATCH_VERIFIED", _src(v),
                       f"The customer's verified legal name {v.value} ({', '.join(v.sources)}) agrees with none of the hit's names ({'; '.join(x.h.names)}).")


def rule_location(x: Ctx):
    if not x.p.countries or not x.h.countries or x.shared_countries:
        return None
    if x.name_match and x.dob_match:
        return None  # a country difference alone does not clear a name+DOB agreement -> REVIEW
    cc = sorted({c.value for c in x.p.countries})
    hc = sorted({c.value for c in x.h.countries})
    return Outcome("DIFFERENT_ENTITY", "LOCATION_MISMATCH", _src(*x.p.countries),
                   f"Every country tied to the customer ({', '.join(cc)}) differs from every country tied to the hit ({', '.join(hc)}).")


CONTRADICTIONS = [rule_type, rule_deceased, rule_temporal, rule_dob, rule_name, rule_location]


# ---------------------------------------------------------------- agreement


def agreement(x: Ctx) -> Outcome:
    nm, dm, sc = x.name_match, x.dob_match, x.shared_countries
    if x.company:
        if nm and sc:
            return Outcome("SAME_ENTITY", "NAME_LOCATION_MATCH", _src(nm[0], *sc),
                           f"The customer's name {nm[0].value} agrees with the hit's name {nm[1]}, and both are linked to {sc[0].value}. Nothing contradicts them.")
    elif nm and dm:
        if sc:
            return Outcome("SAME_ENTITY", "NAME_DOB_LOCATION_MATCH", _src(nm[0], x.p.dob, dm, *sc, exclude=x.h.hit_id),
                           f"The customer's date of birth {x.p.dob.value.text()} ({', '.join(x.p.dob.sources)}) agrees with the hit's {dm.value.text()}. Both are linked to {sc[0].value}. The customer's name {nm[0].value} agrees with the hit's name {nm[1]}.")
        return Outcome("SAME_ENTITY", "NAME_DOB_MATCH", _src(nm[0], x.p.dob, dm, exclude=x.h.hit_id),
                       f"The customer's date of birth {x.p.dob.value.text()} ({', '.join(x.p.dob.sources)}) agrees with the hit's {dm.value.text()}, and the customer's name {nm[0].value} agrees with the hit's name {nm[1]}. Nothing contradicts them.")
    missing = []
    if not nm:
        missing.append("name agreement")
    if not x.company and not dm:
        missing.append("a date of birth to compare" if not (x.p.dob and x.h.dobs) else "date of birth agreement")
    if x.company and not sc:
        missing.append("a shared country")
    agreeing = _src(*sc, x.p.dob if dm else None, exclude=x.h.hit_id) or _src(nm[0] if nm else None) or _src(x.records)
    return Outcome("REVIEW", "INSUFFICIENT_DATA", agreeing,
                   f"No fact contradicts the hit {x.h.names[0]}, but there is not enough to confirm it: missing {' and '.join(missing) or 'agreement'}.")


def decide(person: Person, hit: HitProfile, alert: dict, nr: NameRules) -> Outcome:
    x = Ctx(person, hit, alert, nr)
    for rule in CONTRADICTIONS:
        o = rule(x)
        if o:
            o.trace.append(f"{rule.__name__} fired")
            return o
    o = agreement(x)
    o.trace.append("no contradiction; " + ("agreement" if o.decision == "SAME_ENTITY" else "not enough to confirm"))
    return o
