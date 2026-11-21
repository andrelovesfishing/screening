"""Grok as a blind second reader: given every source record behind one hit, it returns the facts our
rules use, each with the source ID and a verbatim quote. Our code then drops any fact whose quote is
not literally in that source, so an invented fact cannot survive. Grok never sees our extracted facts
(no anchoring) and never decides SAME/DIFFERENT.
"""

from __future__ import annotations

import re
import unicodedata

PROMPT_VERSION = "facts-v1"

SYSTEM = """You extract facts for an audit of a sanctions-screening system. You do NOT decide whether \
the customer and the listed person are the same person; you only report what the sources say.

There are two subjects:
- LISTED PERSON: the person or company on a watchlist (the "hit"). Sources marked LISTING or SEARCH.
- CUSTOMER: the bank's client. Sources marked CUSTOMER.

Each source starts with a line "[SOURCE <id>] <kind>". Rules:
1. Report only what the text states. No guessing, no outside knowledge.
2. Every fact has source_id (the id of the source it came from) and quote: an exact, contiguous excerpt \
of at most 30 words, copied character for character from that source, that contains the fact.
3. Dates: value as YYYY-MM-DD, YYYY-MM or YYYY, only as precise as the text. If a numeric date such as \
05/06/1980 could be day-first or month-first, report both readings as separate entries with the same quote.
4. Ages: when a source gives an age at a time ("who was 36 in October 1987"), report it in ages with the \
date it applies to as as_of (YYYY-MM-DD, YYYY-MM or YYYY).
5. Countries: English short country name. Include every country the text links to that subject in any \
way: residence, nationality, citizenship, birth, address, document, travel, work, family, business.
6. Names: every name the text gives for that subject: full names, aliases, former or maiden names, \
nicknames, other spellings. Copy them as written.
7. Customer facts only from CUSTOMER sources. Listed-person facts from LISTING sources, and from SEARCH \
sources only when you judge the result to be about the listed person.
8. For every SEARCH source, say whether it is about the listed person (yes, no or unclear), with a quote.
9. report_dates: every date on which a report, article, listing entry or source about the listed person \
was dated or published.
10. Use empty lists when nothing is found."""

_FACT = {"type": "object", "additionalProperties": False, "required": ["value", "source_id", "quote"],
         "properties": {"value": {"type": "string"}, "source_id": {"type": "string"}, "quote": {"type": "string"}}}
_AGE = {"type": "object", "additionalProperties": False, "required": ["age", "as_of", "source_id", "quote"],
        "properties": {"age": {"type": "integer"}, "as_of": {"type": "string"},
                       "source_id": {"type": "string"}, "quote": {"type": "string"}}}
_ARR = lambda item: {"type": "array", "items": item}  # noqa: E731
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["listed_person", "customer", "search_results"],
    "properties": {
        "listed_person": {
            "type": "object", "additionalProperties": False,
            "required": ["entity_type", "names", "dobs", "ages", "countries", "report_dates", "death_dates"],
            "properties": {"entity_type": {"type": "string", "enum": ["individual", "company", "unknown"]},
                           "names": _ARR(_FACT), "dobs": _ARR(_FACT), "ages": _ARR(_AGE),
                           "countries": _ARR(_FACT), "report_dates": _ARR(_FACT), "death_dates": _ARR(_FACT)}},
        "customer": {
            "type": "object", "additionalProperties": False, "required": ["names", "dobs", "ages", "countries"],
            "properties": {"names": _ARR(_FACT), "dobs": _ARR(_FACT), "ages": _ARR(_AGE), "countries": _ARR(_FACT)}},
        "search_results": _ARR({"type": "object", "additionalProperties": False,
                                "required": ["source_id", "about_listed_person", "quote"],
                                "properties": {"source_id": {"type": "string"},
                                               "about_listed_person": {"type": "string", "enum": ["yes", "no", "unclear"]},
                                               "quote": {"type": "string"}}}),
    },
}

HIT_FIELDS = ["name", "aliases", "first_name", "middle_name", "last_name", "best_matching_name", "entity_type",
              "gender", "dobs", "countries", "nationality", "citizenship", "addresses", "place_of_birth",
              "life_status", "deceased_date", "primary_category", "categories", "subcategories",
              "position_or_allegation", "details", "identifiers", "relationships", "profile_notes"]
CUS_FIELDS = ["entity_type", "full_name", "preferred_name", "login_name", "dob", "nationality",
              "residence_country", "city", "address", "occupation", "employer", "registration_number"]
KYC_FIELDS = ["verified_name", "verified_dob", "document_type", "document_country", "result", "checked_at"]


def _render(fields: list[str], rec: dict) -> str:
    return "\n".join(f"{k}: {rec[k]}" for k in fields if (rec.get(k) or "").strip())


def sources_for(store, hit: dict, person_records: list[list[str]], search_query_fn) -> list[tuple[str, str, str, str]]:
    """(source_id, kind, owner, text) for every record behind this hit. owner is 'hit' or the
    first record id of the customer person the source belongs to."""
    out = [(hit["hit_id"], "LISTING record", "hit", _render(HIT_FIELDS, hit))]
    for sr in sorted(search_query_fn(hit), key=lambda s: s.get("result_id", "")):
        txt = _render(["title", "snippet", "source"], sr)
        out.append((sr["result_id"], "SEARCH result for the listed person's name", "hit", txt))
    seen = {s[0] for s in out}
    for recs in person_records:
        owner = recs[0]
        emails = []
        for rid in recs:
            c = store.customers[rid]
            if rid not in seen:
                out.append((rid, "CUSTOMER record", owner, _render(CUS_FIELDS, c)))
                seen.add(rid)
            if c.get("email") and c["email"].lower() not in emails:
                emails.append(c["email"].lower())
            for app in store.applicants_by_customer.get(rid, []):
                for k in store.kyc_by_applicant.get(app, []):
                    if k["check_id"] not in seen:
                        out.append((k["check_id"], "CUSTOMER identity check", owner, _render(KYC_FIELDS, k)))
                        seen.add(k["check_id"])
        for e in emails:
            for n in store.notes_by_email.get(e, []):
                if n["note_id"] not in seen:
                    txt = f"created_at: {n.get('created_at', '')}\nchannel: {n.get('channel', '')}\ntext: {n.get('text', '')}"
                    out.append((n["note_id"], "CUSTOMER note", owner, txt))
                    seen.add(n["note_id"])
    return out


def user_prompt(sources) -> str:
    return "\n\n".join(f"[SOURCE {sid}] {kind}\n{text}" for sid, kind, _, text in sources)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().strip(".,;:")


def quote_ok(quote: str, source_text: str) -> bool:
    """The quote must literally occur in the source (case, spacing and typographic quotes aside).
    A quote Grok shortened with '...' passes only if every piece occurs, in order."""
    src = _norm(source_text)
    # Structured fields are rendered "label: a; b; c"; Grok often quotes one item as "label: b".
    m = re.match(r"\s*([a-z_]+):\s+(.*)", quote or "", re.S)
    if m and m.group(1) in source_text and not quote_ok_plain(src, quote):
        return quote_ok_plain(src, m.group(2))
    return quote_ok_plain(src, quote)


def quote_ok_plain(src: str, quote: str) -> bool:
    pieces = [p for p in (_norm(x) for x in re.split(r"\.\.\.|…", quote or "")) if p]
    if not pieces:
        return False
    pos = 0
    for p in pieces:
        i = src.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def verified(answer: dict, sources) -> tuple[dict, list[dict]]:
    """Answer with every fact whose quote is not in its stated source removed; plus the dropped facts."""
    by_id = {sid: text for sid, _, _, text in sources}
    dropped = []

    def keep(f: dict, where: str) -> bool:
        ok = f.get("source_id") in by_id and quote_ok(f.get("quote", ""), by_id[f["source_id"]])
        if not ok:
            dropped.append({"where": where, **f})
        return ok

    out = {"listed_person": {"entity_type": answer["listed_person"].get("entity_type", "unknown")},
           "customer": {}, "search_results": []}
    for side in ("listed_person", "customer"):
        for k, v in answer[side].items():
            if isinstance(v, list):
                out[side][k] = [f for f in v if keep(f, f"{side}.{k}")]
    out["search_results"] = [s for s in answer.get("search_results", []) if keep(s, "search_results")]
    return out, dropped
