"""Cached, parallel Grok (xAI) client for the offline audit. Never imported by system/.

Every answer is stored in audit/cache/grok.jsonl under a key made of (model, prompt version,
system prompt, user prompt). A re-run with the same inputs makes no API call and gets the same
answer, so the audit is reproducible even though the model is not. The cache holds dev text: it is
git-ignored and must never be committed.

The key is read from XAI_API_KEY, else from the file named by XAI_KEY_FILE
(default ~/.secrets/xai_key.txt). It is never printed or written anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MODEL = "grok-4.20-0309-non-reasoning"
URL = "https://api.x.ai/v1/chat/completions"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "grok.jsonl")


def _api_key() -> str:
    k = os.environ.get("XAI_API_KEY", "").strip()
    if k:
        return k
    path = os.environ.get("XAI_KEY_FILE") or os.path.join(os.path.expanduser("~"), ".secrets", "xai_key.txt")
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class Grok:
    def __init__(self, prompt_version: str, model: str = MODEL):
        self.model, self.pv = model, prompt_version
        self.lock = threading.Lock()
        self.cache: dict[str, dict] = {}
        self.calls = self.hits = self.errors = 0
        self.cost_ticks = 0
        self.tokens = 0
        self._key = None
        if os.path.exists(CACHE):
            with open(CACHE, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        self.cache[r["key"]] = r

    def _cache_key(self, system: str, user: str, schema) -> str:
        blob = json.dumps([self.model, self.pv, system, user, schema], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def ask(self, system: str, user: str, schema: dict | None = None) -> dict | None:
        """JSON answer for this prompt: from the cache if present, else from the API (then cached)."""
        key = self._cache_key(system, user, schema)
        with self.lock:
            if key in self.cache:
                self.hits += 1
                return self.cache[key]["answer"]
            if self._key is None:
                self._key = _api_key()
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "facts", "schema": schema, "strict": True}}
        else:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._key, "Content-Type": "application/json",
                   "User-Agent": "screening-audit"}
        resp, last_err = None, ""
        for attempt in range(5):
            try:
                req = urllib.request.Request(URL, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
                if e.code in (408, 409, 429) or e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = repr(e)
                time.sleep(2 ** attempt)
        if resp is None:
            with self.lock:
                self.errors += 1
            raise RuntimeError(last_err)
        content = resp["choices"][0]["message"]["content"]
        answer = json.loads(content)
        usage = resp.get("usage", {})
        rec = {"key": key, "model": self.model, "model_returned": resp.get("model"), "pv": self.pv,
               "answer": answer, "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost_in_usd_ticks")}}
        with self.lock:
            self.calls += 1
            self.tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            self.cost_ticks += usage.get("cost_in_usd_ticks") or 0
            self.cache[key] = rec
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            with open(CACHE, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return answer

    def map(self, fn, items, workers: int = 16) -> list:
        """fn(item) for every item in parallel; an item whose call fails gives ('ERROR', message)."""
        def safe(x):
            try:
                return fn(x)
            except Exception as e:  # noqa: BLE001 - one failed call must not stop the audit
                return ("ERROR", repr(e)[:300])
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(safe, items))

    def summary(self) -> str:
        # xAI reports cost in 1e-10 USD ticks
        return (f"{self.calls} API calls, {self.hits} cache hits, {self.errors} errors, "
                f"{self.tokens} tokens, ${self.cost_ticks / 1e10:.4f}")
