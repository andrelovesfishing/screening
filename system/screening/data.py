"""Data adapter: reads a data folder (JSON Lines from the data room, or the CSV browse export)
into one in-memory Store with the indexes the rules need. Nothing downstream knows the file format.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import defaultdict

from .text import as_list, fold, parse_date

csv.field_size_limit(10**8)

TABLES = {
    "alerts": ["alerts"],
    "hits": ["hits"],
    "customers": ["customers", "system_of_record", "customer"],
    "accounts": ["accounts"],
    "onboarding": ["onboarding"],
    "kyc": ["kyc_checks", "kyc checks", "kyc"],
    "notes": ["notes"],
    "search": ["search_results", "search results", "search"],
    "labels": ["practice_labels", "practice labels", "labels"],
}


def _flat(v) -> str:
    """One representation for every source format: lists become '; '-joined text (split again
    by as_list where a field is a list), nulls become '', numbers become text, objects become
    their values joined. Downstream code never sees format differences."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "; ".join(x for x in (_flat(i) for i in v) if x)
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_flat(x)}" if not isinstance(x, (dict, list)) else _flat(x)
                         for k, x in v.items() if _flat(x))
    return str(v)


def _read_any(path: str) -> list[dict]:
    if path.endswith(".jsonl"):
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    elif path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else [data]
    else:
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    return [{k: _flat(v) for k, v in r.items()} for r in rows if isinstance(r, dict)]


def _find(folder: str, keys: list[str]) -> str | None:
    files = []
    for root, _, names in os.walk(folder):
        files += [os.path.join(root, n) for n in names]

    def stem(p):
        s = os.path.basename(p).lower()
        s = re.sub(r"\.(jsonl|json|csv)$", "", s)
        return s.split(" - ")[-1].strip()

    for k in keys:
        for ext in (".jsonl", ".json", ".csv"):
            for p in files:
                if p.lower().endswith(ext) and stem(p) == k:
                    return p
    return None


def _load_variants(folder: str) -> tuple[list[list[str]], list[str]]:
    p = _find(folder, ["name_variants"])
    if p and p.endswith(".json"):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        groups, forms = [], []

        def walk(x, key=""):
            is_forms = any(w in key for w in ("legal", "form", "compan"))
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, k.lower())
            elif isinstance(x, list) and x and all(isinstance(i, list) for i in x):
                groups.extend([[str(n) for n in g] for g in x])
            elif isinstance(x, list) and all(isinstance(i, str) for i in x):
                if is_forms:
                    forms.extend(x)
                elif len(x) > 1:
                    groups.append(x)
            elif isinstance(x, str) and "," in x:
                (forms.extend if is_forms else (lambda v: groups.append(v)))(
                    [s.strip() for s in x.split(",") if s.strip()])

        walk(data)
        return groups, forms
    # CSV browse export: the policy tab holds the groups as comma-separated lines.
    pol = _find(folder, ["policy"])
    groups, forms = [], []
    if pol:
        with open(pol, encoding="utf-8") as f:
            lines = [l.strip().strip('"') for l in f]
        on = False
        for l in lines:
            if l.startswith("name_variants.json"):
                on = True
                continue
            if not on or not l:
                continue
            if l.lower().startswith("company legal forms:"):
                forms = [x.strip() for x in l.split(":", 1)[1].split(",") if x.strip()]
            elif "," in l and len(l) < 300:
                groups.append([x.strip() for x in l.split(",") if x.strip()])
    return groups, forms


class Store:
    def __init__(self, folder: str):
        self.folder = folder
        t = {}
        for name, keys in TABLES.items():
            p = _find(folder, keys)
            t[name] = _read_any(p) if p else []
        self.alerts = t["alerts"]
        self.hits = {h["hit_id"]: h for h in t["hits"]}
        self.customers = {c["record_id"]: c for c in t["customers"]}
        self.accounts = t["accounts"]
        self.onboarding = t["onboarding"]
        self.kyc = t["kyc"]
        self.notes = t["notes"]
        self.search = t["search"]
        self.labels = t["labels"]
        self.variant_groups, self.legal_forms = _load_variants(folder)
        self._index()

    def _index(self):
        self.acc_by_id = defaultdict(list)
        self.acc_by_login = defaultdict(list)
        self.acc_by_customer = defaultdict(list)
        for a in self.accounts:
            self.acc_by_id[a["account_id"]].append(a)
            if a.get("login_handle"):
                self.acc_by_login[a["login_handle"].lower()].append(a)
            self.acc_by_customer[a["customer_id"]].append(a)
        self.cust_by_email = defaultdict(list)
        self.cust_by_login = defaultdict(list)
        self.cust_by_phone_dob = defaultdict(list)
        self.cust_by_year = defaultdict(list)
        self.cust_by_part = defaultdict(set)
        for c in self.customers.values():
            if c.get("email"):
                self.cust_by_email[c["email"].lower()].append(c["record_id"])
            if c.get("login_name"):
                self.cust_by_login[c["login_name"].lower()].append(c["record_id"])
            d = parse_date(c.get("dob", ""))
            if c.get("phone") and d and d.month and d.day:
                self.cust_by_phone_dob[(re.sub(r"\D", "", c["phone"]), d)].append(c["record_id"])
            if d:
                self.cust_by_year[d.year].append(c["record_id"])
            for field in ("full_name", "preferred_name", "login_name"):
                for p in fold(c.get(field) or "").split():
                    if len(p) >= 2:
                        self.cust_by_part[p].add(c["record_id"])
        self.applicants_by_customer = defaultdict(list)
        for o in self.onboarding:
            self.applicants_by_customer[o["customer_id"]].append(o["kyc_applicant_id"])
        self.kyc_by_applicant = defaultdict(list)
        self.customers_by_docnum = defaultdict(set)
        cust_by_applicant = defaultdict(set)
        for o in self.onboarding:
            cust_by_applicant[o["kyc_applicant_id"]].add(o["customer_id"])
        for k in self.kyc:
            self.kyc_by_applicant[k["applicant_id"]].append(k)
            if k.get("result") == "approved" and k.get("document_number"):
                for cid in cust_by_applicant.get(k["applicant_id"], ()):
                    self.customers_by_docnum[k["document_number"].strip().upper()].add(cid)
        self.notes_by_email = defaultdict(list)
        for n in self.notes:
            self.notes_by_email[(n.get("requester_email") or "").lower()].append(n)
        self.search_by_query = defaultdict(list)
        for s in self.search:
            self.search_by_query[(s.get("query") or "").strip().lower()].append(s)

    def hit_ids(self, alert: dict) -> list[str]:
        return as_list(alert.get("hit_ids"))
