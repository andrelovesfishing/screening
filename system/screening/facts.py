"""Fact gathering. Turns raw records into typed facts that each carry their source IDs.

Customer side: resolve the alert to person(s) (principles 2-4).
Hit side: names, birth dates, countries, death date, earliest dated report (principle 5).

Extractors are plain functions; any one of them can be swapped (e.g. for a cached LLM
extractor) without touching the rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .data import Store
from .text import NOT_COUNTRIES, D, NameRules, age_to_years, as_list, countries_in, country, find_dates, fold, parse_date


@dataclass
class Fact:
    value: object
    sources: tuple[str, ...]


@dataclass
class Person:
    record_ids: list[str]
    entity_type: str
    names: list[Fact] = field(default_factory=list)  # names used to compare (verified first)
    verified_name: Fact | None = None
    dob: Fact | None = None  # D
    countries: list[Fact] = field(default_factory=list)  # each value a country
    # Silent guards: customer locations we saw but could not read. Each blocks a LOCATION clear,
    # because the unread place might be the shared country. Expected to be empty on dev.
    unread_locations: list[str] = field(default_factory=list)
    # Silent guard: a KYC note that records both a failed and a passed check. Its facts are not used,
    # and no clear may rest on the customer's facts. Expected to be empty on dev.
    unclear_checks: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


@dataclass
class HitProfile:
    hit_id: str
    entity_type: str
    names: list[str]
    dobs: list[Fact]  # each value a D
    dob_origin: str  # "dobs field" | "profile notes" | "search results" | ""
    countries: list[Fact]
    deceased: Fact | None
    earliest_report: Fact | None
    listing_phrase: str
    unparsed_countries: list[str] = field(default_factory=list)  # location values we could not read
    # Silent guard: birth-date text we saw but could not read. Blocks a DOB clear (the unread date might agree).
    unread_dobs: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """How reasons name the hit: its first name, or its ID when it has none."""
        return self.names[0] if self.names else self.hit_id


_COMPANY_TYPES = {"company", "organisation", "organization", "entity", "business", "corporation", "corporate",
                  "legal entity", "legal person", "firm", "partnership", "trust", "fund", "llc"}
_PERSON_TYPES = {"individual", "person", "natural person", "private individual", "human"}


def is_company(t: str) -> bool:
    return (t or "").strip().lower() in _COMPANY_TYPES


def known_type(t: str) -> bool:
    """Silent guard: only a recognised type can clear on ENTITY_TYPE_MISMATCH."""
    return (t or "").strip().lower() in _COMPANY_TYPES | _PERSON_TYPES


# ============================================================== customer side


class CustomerResolver:
    def __init__(self, store: Store, names: NameRules):
        self.s = store
        self.nr = names

    # ---- principle 2: find the records that fit what was screened
    def fits(self, rec: dict, alert: dict) -> bool:
        company = is_company(alert.get("entity_type"))
        screened = alert.get("customer_name") or ""
        cand_names = [rec.get("full_name"), rec.get("preferred_name"), rec.get("login_name")]
        if not any(n and self.nr.agree(screened, n, company) for n in cand_names):
            return False
        ad, rd = parse_date(alert.get("dob", "")), parse_date(rec.get("dob", ""))
        if ad and rd and not _same_where_both_give(ad, rd):
            return False
        ac = country(alert.get("country", "")) if alert.get("country") else None
        if ac:
            rc = {country(rec.get("residence_country", "")), country(rec.get("nationality", ""))}
            if ac not in rc:
                return False
        city = fold(alert.get("city", ""))
        if city and city not in fold(rec.get("city", "")) and city not in fold(rec.get("address", "")):
            return False
        return True

    def linked_records(self, alert: dict) -> tuple[list[str], str]:
        if alert.get("account_ref"):
            ids = [a["customer_id"] for a in self.s.acc_by_id.get(alert["account_ref"], [])]
            return _uniq(ids), f"account_ref {alert['account_ref']}"
        if alert.get("login_handle"):
            h = alert["login_handle"].lower()
            ids = [a["customer_id"] for a in self.s.acc_by_login.get(h, [])] + self.s.cust_by_login.get(h, [])
            return _uniq(ids), f"login_handle {alert['login_handle']}"
        if alert.get("email"):
            return _uniq(self.s.cust_by_email.get(alert["email"].lower(), [])), f"email {alert['email']}"
        return [], ""

    def search(self, alert: dict) -> list[str]:
        ad = parse_date(alert.get("dob", ""))
        if ad:
            pool = set(self.s.cust_by_year.get(ad.year, []))
        else:
            pool = set()
            for p in self.nr.parts(alert.get("customer_name") or "", is_company(alert.get("entity_type"))):
                for key in self._similar_parts(p):
                    pool |= self.s.cust_by_part.get(key, set())
        return sorted(r for r in pool if self.fits(self.s.customers[r], alert))

    def _similar_parts(self, p: str) -> list[str]:
        out = [p]
        for key in self.s.cust_by_part:
            if key != p and abs(len(key) - len(p)) <= 2 and self.nr.part_match(p, key):
                out.append(key)
        return out

    # ---- principle 3: one person, one picture
    def expand(self, seed: list[str]) -> list[str]:
        seen, todo = set(), list(seed)
        while todo:
            r = todo.pop()
            if r in seen or r not in self.s.customers:
                continue
            seen.add(r)
            c = self.s.customers[r]
            d = parse_date(c.get("dob", ""))
            if c.get("phone") and d and d.month and d.day:
                todo += self.s.cust_by_phone_dob.get((re.sub(r"\D", "", c["phone"]), d), [])
            for app in self.s.applicants_by_customer.get(r, []):
                for k in self.s.kyc_by_applicant.get(app, []):
                    if k.get("result") == "approved" and k.get("document_number"):
                        todo += sorted(self.s.customers_by_docnum.get(k["document_number"].strip().upper(), ()))
        return sorted(seen)

    def resolve(self, alert: dict) -> tuple[list[list[str]], list[str]]:
        """Returns the distinct persons (each a list of record IDs) that fit the alert, and a trace."""
        trace = []
        linked, via = self.linked_records(alert)
        if linked:
            fit = [r for r in linked if self.fits(self.s.customers[r], alert)] if len(linked) > 1 else linked
            trace.append(f"{via} -> {', '.join(linked)}" + (f" -> screened details fit {', '.join(fit)}" if len(linked) > 1 else ""))
            seeds = fit or linked
        else:
            seeds = self.search(alert)
            trace.append(f"no link; search by screened details -> {', '.join(seeds) or 'nothing fits'}")
        persons, assigned = [], set()
        for r in seeds:
            if r in assigned:
                continue
            group = self.expand([r])
            assigned |= set(group)
            persons.append([r] + [g for g in group if g != r])
        if any(len(p) > 1 for p in persons):
            trace.append("one person, one picture: " + " | ".join(", ".join(p) for p in persons))
        return persons, trace

    # ---- principle 4: build the person's facts
    def profile(self, record_ids: list[str], alert: dict) -> Person:
        recs = [self.s.customers[r] for r in record_ids]
        p = Person(record_ids=record_ids, entity_type=recs[0].get("entity_type") or alert.get("entity_type", ""))
        checks = []  # (date, name, dob, doc_country, source)
        for r in record_ids:
            for app in self.s.applicants_by_customer.get(r, []):
                for k in self.s.kyc_by_applicant.get(app, []):
                    if k.get("result") == "approved":
                        checks.append((parse_date(k.get("checked_at", "")), k.get("verified_name", ""),
                                       parse_date(k.get("verified_dob", "")), country(k.get("document_country", "")),
                                       k["check_id"], (k.get("document_country") or "").strip()))
        emails = _uniq([(c.get("email") or "").lower() for c in recs if c.get("email")])
        lived = []
        for e in emails:
            for n in self.s.notes_by_email.get(e, []):
                if (n.get("channel") or "").lower() == "kyc":
                    k = parse_kyc_note(n)
                    text = n.get("text", "")
                    # Principle 4: "Rejected or expired checks are not evidence", also when only written in a note.
                    if _NEG_CHECK.search(text):
                        if _POS_CHECK.search(_NEG_CHECK.sub(" ", text)):
                            p.unclear_checks.append(f"{n['note_id']} records both a failed and a passed check")
                        p.trace.append(f"ignored {n['note_id']}: not an approved check")
                        if k[3]:  # inclusive: a country the customer presented a document from only blocks clears
                            lived.append(Fact(k[3], (n["note_id"],)))
                        continue
                    cands = caps_names(text)
                    fit = [c for c in cands if self._names_overlap(c, recs, alert)]
                    if fit:  # the holder is the capitalised name that is this customer, not e.g. "IDV OK"
                        k = (k[0], fit[0]) + k[2:]
                    elif k[1]:
                        if not self._mentions_customer(text, recs, alert):
                            # Principle 2, "never use another customer's records": a note reached through a
                            # shared email that never names this customer is about someone else.
                            p.trace.append(f"ignored {n['note_id']}: check for {k[1]} never names the customer")
                            continue
                        # It names the customer but no clean holder name: keep its facts, not a name we guessed
                        # (a wrong verified name would cause a NAME_MISMATCH clear).
                        p.trace.append(f"{n['note_id']}: holder name unclear, not used")
                        k = (k[0], "") + k[2:]
                    checks.append(k + ("",))
                else:  # inclusive on purpose: a country we miss could cause an unsafe LOCATION clear
                    found = countries_in(n.get("text", ""), codes=True)
                    for c in found:
                        lived.append(Fact(c, (n["note_id"],)))
                    if not found and _RESIDENCE.search(n.get("text", "")):
                        p.unread_locations.append(f"{n['note_id']} mentions where they live but names no country we read")
        for c in checks:
            if c[0] and c[5] and not c[3]:
                p.unread_locations.append(f"{c[4]} document country {c[5]!r} not read")
        checks = [c[:5] for c in checks if c[0]]
        if checks:
            latest = max(checks, key=lambda c: (c[0].earliest_ordinal(), c[4]))
            _, vname, vdob, vctry, src = latest
            if vname:
                p.verified_name = Fact(vname, (src,))
            if vdob:
                p.dob = Fact(vdob, (src,))
            p.trace.append(f"latest approved check {src}: {vname or '?'}, {vdob.text() if vdob else '?'}, {vctry or '?'}")
        for c in checks:
            if c[3]:
                p.countries.append(Fact(c[3], (c[4],)))
        if p.verified_name:
            p.names = [p.verified_name]
        else:
            p.names = [Fact(c["full_name"], (c["record_id"],)) for c in recs if c.get("full_name")]
        if not p.dob:
            for c in recs:
                d = parse_date(c.get("dob", ""))
                if d:
                    p.dob = Fact(d, (c["record_id"],))
                    break
        for c in recs:
            for f in ("residence_country", "nationality"):
                cc = country(c.get(f, ""))
                if cc:
                    p.countries.append(Fact(cc, (c["record_id"],)))
                elif (c.get(f) or "").strip() and fold(c[f]) not in NOT_COUNTRIES:
                    p.unread_locations.append(f"{c['record_id']} {f} {c[f]!r} not read")
        p.countries += lived
        return p

    def _names_overlap(self, name: str, recs: list[dict], alert: dict) -> bool:
        company = is_company(alert.get("entity_type"))
        own = [alert.get("customer_name") or ""] + [r.get(f) or "" for r in recs
                                                     for f in ("full_name", "preferred_name", "login_name")]
        mine = self.nr.parts(name, company)
        return any(self.nr.part_match(a, b) for o in own for b in self.nr.parts(o, company) for a in mine)

    def _mentions_customer(self, text: str, recs: list[dict], alert: dict) -> bool:
        """Any of the customer's own name parts occurs anywhere in the text."""
        company = is_company(alert.get("entity_type"))
        own = [alert.get("customer_name") or ""] + [r.get(f) or "" for r in recs
                                                     for f in ("full_name", "preferred_name", "login_name")]
        parts = {b for o in own for b in self.nr.parts(o, company) if len(b) >= 3}
        words = set(self.nr.parts(text, company))
        return any(w == b or self.nr.part_match(w, b) for w in words for b in parts)

    def screened_only(self, alert: dict) -> Person:
        """No customer record found: fall back to the screened details (unverified)."""
        p = Person(record_ids=[], entity_type=alert.get("entity_type", ""))
        aid = alert["alert_id"]
        if alert.get("customer_name"):
            p.names = [Fact(alert["customer_name"], (aid,))]
        d = parse_date(alert.get("dob", ""))
        if d:
            p.dob = Fact(d, (aid,))
        c = country(alert.get("country", ""))
        if c:
            p.countries = [Fact(c, (aid,))]
        return p


_LIVED = re.compile(r"\b(lived|living|live|resid\w*|reside\w*|work\w*|employ\w*|based|home|job|occupation|"
                    r"career|profession\w*|stay\w*|spent|history)\b|\b(19|20)\d\d\b", re.I)
_CAPS_NAME = re.compile(r"\b([A-Z][A-Z'\-\.]+(?:\s+[A-Z][A-Z'\-\.]+)+)\b")


_RESIDENCE = re.compile(r"\b(lived|lives|living|live in|moved|relocat\w*|resid\w*|grew up|based in|originally from|"
                        r"hometown|citizen\w*|national of|emigrat\w*|immigrat\w*|home country|born in|res|"
                        r"domicil\w*|based)\b", re.I)
_NOT_NAME = {"DOB", "ID", "KYC", "UK", "USA", "US", "EU", "NIN", "BVN", "SSN", "PEP", "N/A", "NA", "AML"}
_NEG_CHECK = re.compile(
    r"\b(?:reject\w*|fail\w*|declin\w*|expired|unsuccessful\w*|denied|refused|invalid|pending|awaiting|inconclusive"
    r"|under review|not (?:been |be )?(?:approved|verified|validated|passed|cleared|accepted|confirmed|successful\w*)"
    r"|(?:could|can)\s?not (?:be )?verif\w*|unable to verif\w*)\b", re.I)
_POS_CHECK = re.compile(r"\b(?:approved|verified|validated|passed|cleared|accepted|confirmed|success\w*|ok)\b", re.I)
_BIRTH_KW = re.compile(r"\b(born|dob|d\.o\.b|birth|birthdate)\b", re.I)


def parse_kyc_note(n: dict):
    """An onboarding-team note recording an approved identity check."""
    text = n.get("text", "")
    created = parse_date(n.get("created_at", ""))
    names = caps_names(text)
    name = names[0] if names else ""
    dates = [(s0, d) for s0, _, d in find_dates(text) if d.month and d.day
             and not (created and (d.year, d.month, d.day) == (created.year, created.month, created.day))
             and (not created or d.year <= created.year - 10)]
    dob = None
    kws = [m.end() for m in _BIRTH_KW.finditer(text)]
    if dates and kws:  # the date closest after (or else nearest) a birth keyword
        dob = min(dates, key=lambda t: min((t[0] - k) if t[0] >= k else 10_000 + (k - t[0]) for k in kws))[1]
    elif len(dates) == 1:
        dob = dates[0][1]
    # The document country: a country name first; a bare code ("RUS nat ID") only when no name is given,
    # because the holder's own UPPERCASE name can contain a code ("LI WEI").
    cs = countries_in(text) or countries_in(text, codes=True)
    return (created, name, dob, cs[0] if cs else None, n["note_id"])


def caps_names(text: str) -> list[str]:
    """Candidate holder names in a KYC note: runs of two or more UPPERCASE words, in order."""
    out = []
    for m in _CAPS_NAME.finditer(text):
        toks = [t.strip(".-'’") for t in m.group(1).split()]
        toks = [t for t in toks if t and t not in _NOT_NAME]
        if len(toks) >= 2:
            out.append(" ".join(toks))
    return out


def _same_where_both_give(a: D, b: D) -> bool:
    if a.year != b.year:
        return False
    if a.month and b.month and a.month != b.month:
        return False
    if a.day and b.day and a.day != b.day:
        return False
    return True


def _uniq(xs):
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


# ============================================================== hit side

LISTING_PHRASES = {
    "sanctions": "a sanctioned individual",
    "pep": "a politically exposed person",
    "enforcement": "the subject of enforcement action",
    "adverse media": "named in adverse media",
    "soe": "a state-owned enterprise official",
}
DEFAULT_PHRASE = "named on a watchlist"

# After a weak trigger (at, then, was, a comma), a number followed by a month ("As at 19 November 2025",
# "3rd March") is the day of a date, not an age. Explicit "age 33 May 2019" is an age then a date.
_NOT_DAY = r"(?!\s*(?:st|nd|rd|th)?\.?\s*(?:of\s+)?(?i:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)"
_AGE = re.compile(
    # "Age at time of questioning: 27" / "Age at the time: 48": label lines are explicit ages too
    r"\b(?i:aged|age of|at age|age at (?:the )?time(?: of(?: [a-z]+){1,3}?)?|age)\s*:?\s*(\d{1,3})\b"
    r"|\b(\d{1,3})[\s-]*(?:years?|yrs?)[\s-]*(?:of age|old)\b"
    r"|\b(?:reached|turned|turning)\s+(\d{1,3})" + _NOT_DAY + r"\b"
    r"|\b(?:then|at)\s+(\d{1,3})" + _NOT_DAY + r"\b(?![:.]\d)"
    r"|\b(?:he|she|they|who|was|were|is)\s+(?:was\s+|were\s+|is\s+)?(?:then\s+)?(\d{1,3})" + _NOT_DAY
    + r"\b(?!\s*(?:%|percent|million|people|times))"
    r"|,\s*(\d{1,3})" + _NOT_DAY + r",")
# Principle 5 birth-date sentences: "born", and the other ways a profile states a date of birth.
_HIT_BIRTH = re.compile(r"\b(?:born|date of birth|year of birth|birth\s?date|birth\s?year|dob|came into the world)\b",
                        re.I)
_REPORT = re.compile(r"^\s*According to .+?;\s*([^;:]{3,40}?)\s*:")


def _age_in(text: str, loose: bool = False) -> int | None:
    m = _AGE.search(text)
    if m:
        v = int(next(g for g in m.groups() if g))
        if 0 < v < 120:
            return v
    if not loose:
        return None
    # Short snippets: the age is the only small standalone number outside any date.
    spans = [(s, e) for s, e, _ in find_dates(text)]
    nums = [int(n.group()) for n in re.finditer(r"(?<![\d.,/-])\b\d{1,3}" + _NOT_DAY + r"\b(?![\d.,/-]\d)", text)
            if not any(s <= n.start() < e for s, e in spans)]
    nums = [n for n in nums if 0 < n < 120]
    return nums[0] if len(set(nums)) == 1 else None


def _dated(text: str) -> D | None:
    """First date in text with at least a month (the 'at a date' of an age)."""
    for _, _, d in find_dates(text):
        if d.month:
            return d
    found = find_dates(text)
    return found[0][2] if found else None


def hit_profile(store: Store, hit: dict) -> HitProfile:
    hid = hit["hit_id"]
    # Inclusive on purpose: every form of the hit's name can only block a NAME_MISMATCH clear.
    full = " ".join(x for x in (hit.get("first_name"), hit.get("middle_name"), hit.get("last_name")) if x)
    names = [hit.get("name") or ""] + as_list(hit.get("aliases")) + [hit.get("best_matching_name") or "", full]
    names = list(dict.fromkeys(n for n in names if n.strip()))
    cats = as_list(hit.get("categories"))
    cat = (hit.get("primary_category") or (cats[0] if cats else "")).strip().lower()
    phrase = LISTING_PHRASES.get(cat, DEFAULT_PHRASE)

    countries, unparsed = [], []
    for f in ("countries", "nationality", "citizenship", "addresses", "place_of_birth"):
        for v in as_list(hit.get(f)):
            c = country(v)
            # an address may name its country anywhere ("Nigeria, Lagos"): inclusive, every country in it counts
            found = [c] if c else (countries_in(v) if f == "addresses" else [])
            for c in found:
                if all(c != x.value for x in countries):
                    countries.append(Fact(c, (hid,)))
            if not found:
                last = fold(v.split(",")[-1])
                if last and last not in NOT_COUNTRIES:
                    unparsed.append(v)

    trace = []
    raw_dobs = [v for v in as_list(hit.get("dobs")) if v.strip()]
    dobs = [Fact(d, (hid,)) for d in (parse_date(v) for v in raw_dobs) if d]
    unread_dobs = [f"dobs value {v!r} not read" for v in raw_dobs if not parse_date(v)]
    origin = "dobs field" if dobs else ""
    notes = hit.get("profile_notes") or ""
    n_field = len(dobs)
    # Principle 5: "For companies, only the dobs field is an incorporation date."
    person_hit = not is_company(hit.get("entity_type"))
    if person_hit:  # inclusive on purpose: an extra possible birth date can only block a DOB clear
        first = notes.strip().split("\n\n")[0] if notes.strip() else ""
        first = re.sub(r"\bD\.\s?O\.\s?B\.?", "DOB", first, flags=re.I)  # so the sentence split keeps it whole
        # "b. 1973", "b. 17 Jul 1939", "b. 10-Apr-1960": lowercase b. before a date means born
        first = re.sub(r"(?<![A-Za-z])b\.\s*(?=\d|(?i:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))", "born ", first)
        for sent in re.split(r"(?<=[.!?])\s+", first):
            m = _HIT_BIRTH.search(sent)
            if m:
                after = sent[m.end():]
                d = parse_date(after)
                if d:
                    dobs.append(Fact(d, (hid,)))
                    break
                if re.search(r"\b(on|in)\b|\d", after[:40]) and not re.match(r"\s*(in|at)\s+[A-Z]", after):
                    unread_dobs.append(f"profile notes say born but give no date we read: {sent.strip()[:80]!r}")
            age = _age_in(sent)
            at = _dated(sent) if age else None
            if age and at:
                dobs.append(Fact(age_to_years(age, at), (hid,)))
                break
        if len(dobs) > n_field:
            origin = "dobs field and profile notes" if n_field else "profile notes"
    if not dobs and person_hit:
        q = " ".join(sorted(fold(hit.get("name") or "").split()))
        hit_cs = {c.value for c in countries}
        for sr in sorted(store.search_by_query.get(q, []), key=lambda s: s.get("result_id", "")):
            snip = sr.get("snippet") or ""
            core = re.sub(r"^(a|the)\s+", "", fold(phrase))
            if core not in fold(snip):
                continue
            if not (set(countries_in(snip)) & hit_cs):
                continue
            age, at = _age_in(snip, loose=True), _dated(snip)
            if age and at:
                dobs.append(Fact(age_to_years(age, at), (sr["result_id"],)))
                trace.append(f"search result {sr['result_id']} is about the hit (same listing and country)")
        if dobs:
            origin = "search results"

    deceased = None
    dd = parse_date(hit.get("deceased_date") or "")
    if dd:
        deceased = Fact(dd, (hid,))

    earliest = None
    for line in notes.split("\n"):
        m = _REPORT.match(line)
        if not m:
            continue
        d = parse_date(m.group(1))
        if d and d.month and (earliest is None or d.earliest_ordinal() < earliest.value.earliest_ordinal()):
            earliest = Fact(d, (hid,))

    return HitProfile(hit_id=hid, entity_type=hit.get("entity_type", ""), names=names, dobs=dobs,
                      dob_origin=origin, countries=countries, deceased=deceased, earliest_report=earliest,
                      listing_phrase=phrase, unparsed_countries=unparsed, unread_dobs=unread_dobs, trace=trace)
