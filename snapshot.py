#!/usr/bin/env python3
"""Dump pull request metadata for one public repo to a JSON snapshot.

Usage: snapshot.py <previous_snapshot.json or missing path> <output.json>

Env: GITHUB_TOKEN (required), SOURCE_REPO (default ZecHub/zechub),
     WINDOW_DAYS (closed PRs kept this long, default 45),
     GITHUB_API (default https://api.github.com; tests point it elsewhere).

Stdlib only. Details (files, comments, reviews, merged_by) are fetched only for
PRs whose updated_at or head sha changed since the previous snapshot, so a quiet
run costs a handful of API calls.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
REPO = os.environ.get("SOURCE_REPO", "ZecHub/zechub")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "45"))
MAX_FILE_PAGES = 10  # 1000 files; GitHub caps the endpoint at 3000
CALLS = 0


def get(path, params=None):
    """GET one page. Returns (json, next_url)."""
    global CALLS
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url += ("&" if "?" in url else "?") + "&".join(f"{k}={v}" for k, v in params.items())
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "zechub-pr-snapshot"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for attempt in range(4):
        CALLS += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                body = json.loads(r.read().decode() or "null")
                m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", "") or "")
                return body, (m.group(1) if m else None)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 3:
                reset = e.headers.get("Retry-After") or "20"
                time.sleep(min(int(reset) if reset.isdigit() else 20, 60))
                continue
            if e.code >= 500 and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"gave up on {url}")


def get_all(path, params=None, max_pages=20):
    out, url, page = [], path, 0
    while url and page < max_pages:
        body, url = get(url, params if page == 0 else None)
        out.extend(body or [])
        page += 1
    return out, bool(url)


def base_fields(p):
    merged = bool(p.get("merged_at"))
    return {
        "number": p["number"],
        "title": (p.get("title") or "")[:200],
        "author": (p.get("user") or {}).get("login") or "ghost",
        "state": "merged" if merged else p.get("state"),
        "draft": bool(p.get("draft")),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "closed_at": p.get("closed_at"),
        "merged_at": p.get("merged_at"),
        "head_sha": (p.get("head") or {}).get("sha"),
        "head_label": (p.get("head") or {}).get("label"),
        "url": p.get("html_url"),
        "labels": [l.get("name") for l in p.get("labels") or []],
    }


def details(pr):
    n = pr["number"]
    d = {}
    if pr["state"] == "open":
        files, truncated = get_all(f"/repos/{REPO}/pulls/{n}/files", {"per_page": 100}, MAX_FILE_PAGES)
        d["files"] = sorted({f["filename"] for f in files} | {f.get("previous_filename") for f in files if f.get("previous_filename")})
        d["files_truncated"] = truncated
    comments, _ = get_all(f"/repos/{REPO}/issues/{n}/comments", {"per_page": 100}, 5)
    reviews, _ = get_all(f"/repos/{REPO}/pulls/{n}/reviews", {"per_page": 100}, 5)
    acts = []
    for c in comments:
        acts.append({"kind": "comment", "user": (c.get("user") or {}).get("login"),
                     "at": c.get("created_at"), "url": c.get("html_url"),
                     "body": (c.get("body") or "")[:280]})
    for r in reviews:
        if not r.get("submitted_at"):
            continue  # pending review, not visible to others
        acts.append({"kind": "review", "state": r.get("state"), "user": (r.get("user") or {}).get("login"),
                     "at": r.get("submitted_at"), "url": r.get("html_url"),
                     "body": (r.get("body") or "")[:280]})
    acts.sort(key=lambda a: a["at"] or "")
    d["activity_count"] = len(acts)
    d["activity"] = acts[-15:]
    if pr["state"] == "merged":
        full, _ = get(f"/repos/{REPO}/pulls/{n}")
        d["merged_by"] = ((full or {}).get("merged_by") or {}).get("login")
    return d


def main():
    prev_path, out_path = sys.argv[1], sys.argv[2]
    if not TOKEN:
        sys.exit("GITHUB_TOKEN is not set")
    prev = {}
    try:
        with open(prev_path) as f:
            prev = {p["number"]: p for p in json.load(f).get("prs", [])}
    except (FileNotFoundError, ValueError):
        pass

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    open_prs, _ = get_all(f"/repos/{REPO}/pulls", {"state": "open", "per_page": 100}, 20)
    closed = []
    url, params = f"/repos/{REPO}/pulls", {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100}
    for _ in range(10):
        body, url = get(url, params)
        params = None
        body = body or []
        closed.extend(p for p in body if (p.get("updated_at") or "") >= cutoff)
        if not url or not body or (body[-1].get("updated_at") or "") < cutoff:
            break

    seen, prs, fetched = set(), [], 0
    for p in open_prs + closed:
        if p["number"] in seen:
            continue
        seen.add(p["number"])
        pr = base_fields(p)
        old = prev.get(pr["number"])
        reuse = old and old.get("updated_at") == pr["updated_at"] and old.get("head_sha") == pr["head_sha"] \
            and old.get("state") == pr["state"] and "activity" in old
        if reuse:
            for k in ("files", "files_truncated", "activity", "activity_count", "merged_by"):
                if k in old:
                    pr[k] = old[k]
        else:
            pr.update(details(pr))
            fetched += 1
        prs.append(pr)

    prs.sort(key=lambda x: x["number"], reverse=True)
    snap = {"schema": 1, "repo": REPO, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_days": WINDOW_DAYS, "api_calls": CALLS, "detail_fetches": fetched, "prs": prs}
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    print(f"{len(prs)} PRs ({sum(p['state']=='open' for p in prs)} open), "
          f"{fetched} refreshed, {CALLS} API calls")


if __name__ == "__main__":
    main()
