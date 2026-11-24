#!/usr/bin/env python3
"""Instagram competitor tracker, powered by the Upscrape API.

Standard library only (Python 3.8+). Subcommands:

  plan     Show what a run will call and what it costs, before anything runs.
  collect  Pull each account through the Upscrape REST API and save today's snapshot.
  ingest   Save a raw result you already fetched (for example through the Upscrape MCP server).
  build    Work out the metrics from every snapshot and write dashboard.html, data.json and brief.md.

Everything lives in one tracker folder (default ./ig-tracker):

  ig-tracker/
    accounts.json                    the tracked accounts, saved by collect
    snapshots/YYYY-MM-DD/<handle>.json
    dashboard.html  data.json  brief.md

Docs: https://docs.upscrape.com/docs/api/execute.md
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_DIR / "assets" / "dashboard.html"
BASE_URL = os.environ.get("UPSCRAPE_BASE_URL", "https://data.upscrape.com").rstrip("/")
USER_AGENT = f"upscrape-skills/instagram-competitor-tracker/{VERSION}"

CAPABILITY = "instagram.profile.get"
CREDITS_PER_ACCOUNT = 5  # published price of instagram.profile.get, read 24 Nov 2026
RECENT_POSTS = 12  # the most profile.get returns in one call

# Error codes worth one more try with a fresh key (https://docs.upscrape.com/docs/api/errors.md)
RETRYABLE = {
    "timeout", "upstream_timeout", "upstream_error", "blocked", "upstream_rate_limited",
    "temporarily_unavailable", "execution_lost", "internal_error", "rate_limited",
}

FRESH_HOURS = 24          # posts younger than this are still collecting likes: shown, not scored
BREAKOUT_MULTIPLE = 3.0   # a post at 3x the account's own median engagement is a breakout
WINDOW_DAYS = 28          # cadence, format mix and promo share look at the last 28 days
PROMO_RE = re.compile(
    r"\b(\d{1,2}\s?% off|sale|discount|promo|coupon|use code|code [A-Z0-9]{3,}|black friday|cyber (week|monday)"
    r"|giveaway|free gift|gift with purchase|sitewide|bogo|buy one get one)\b",
    re.IGNORECASE,
)
FORMATS = {1: "photo", 2: "video", 8: "carousel"}


# ---------------------------------------------------------------- helpers

def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def clean_handle(raw):
    h = raw.strip()
    h = re.sub(r"^https?://(www\.)?instagram\.com/", "", h, flags=re.I)
    h = h.strip("/").split("/")[0].split("?")[0].lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", h):
        die(f"'{raw}' is not an Instagram username or profile link")
    return h


def handles_from(args, root):
    if args.handles:
        return [clean_handle(h) for h in re.split(r"[,\s]+", args.handles) if h.strip()]
    saved = root / "accounts.json"
    if saved.exists():
        return json.loads(saved.read_text())["handles"]
    die("name the accounts with --handles, for example --handles glossier,rarebeauty")


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


# ---------------------------------------------------------------- API

def http(method, url, key, body=None, headers=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=75) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {"error": {"code": f"http_{e.code}", "message": raw[:200].decode("utf-8", "replace")}}
        return e.code, payload, dict(e.headers)


def run_job(handle, key, max_credits, idem_key):
    """Run profile.get once; follow the job until it ends. Returns (job, error_code)."""
    body = {
        "capability": CAPABILITY,
        "input": {"url": handle, "recent_posts_limit": RECENT_POSTS},
        "max_credits": max_credits,
    }
    status, job, hdrs = http("POST", f"{BASE_URL}/execute", key, body,
                             {"Prefer": "wait=30", "Idempotency-Key": idem_key})
    if status == 429:
        wait = float(hdrs.get("Retry-After") or 5)
        time.sleep(min(wait, 60))
        return None, "rate_limited"
    deadline = time.time() + 180
    delay = (job.get("poll_after_ms") or 1000) / 1000
    while status == 202 or job.get("state") in ("queued", "running", "retrying"):
        if time.time() > deadline:
            return None, "timeout"
        time.sleep(delay)
        delay = min(delay * 2, 10)
        status, job, _ = http("GET", job.get("poll_url") or f"{BASE_URL}/jobs/{job['job_id']}", key)
    if job.get("state") == "completed" and job.get("success"):
        return job, None
    err = (job.get("error") or {}).get("code") or f"http_{status}"
    return job, err


def fetch_account(handle, key, max_credits, run_id):
    """At most two tries. Each try has its own Idempotency-Key, unique to this run, so a lost
    answer is never paid twice, and a later run after a failure starts a fresh job."""
    last = None
    for attempt in range(2):
        idem = f"igct-{handle}-{run_id}" + (f"-r{attempt}" if attempt else "")
        job, err = run_job(handle, key, max_credits, idem)
        if not err:
            return job, None
        last = err
        if err not in RETRYABLE:
            break
        time.sleep(3)
    return None, last


# ---------------------------------------------------------------- normalise

def normalise(raw, handle, fetched_at, source):
    """Turn instagram.profile.get output into one snapshot. Accepts the whole job,
    results[0], or results[0].data."""
    d = raw
    if isinstance(d, dict) and "results" in d:
        d = d["results"][0]
    if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict) and "data" in d["data"]:
        d = d["data"]
    prof = d.get("data", d) if isinstance(d, dict) else {}
    if not isinstance(prof, dict) or "follower_count" not in prof:
        die(f"{handle}: this doesn't look like an instagram.profile.get result (no follower_count)")
    posts = []
    for p in prof.get("recent_posts") or []:
        cap = p.get("caption") or {}
        caption = cap.get("text") if isinstance(cap, dict) else (cap or "")
        own = p.get("owner") or {}
        owner = ((own.get("username") if isinstance(own, dict) else own) or handle).lower()
        coauthors = sorted({(c.get("username") or "").lower() for c in (p.get("coauthor_producers") or [])}
                           - {"", handle, owner})
        posts.append({
            "code": p.get("code") or p.get("shortcode"),
            "taken_at": int(p.get("taken_at") or p.get("taken_at_timestamp") or 0),
            "likes": p.get("like_count"),
            "comments": p.get("comment_count"),
            "format": FORMATS.get(p.get("media_type"), "other"),
            "counts_hidden": bool(p.get("like_and_view_counts_disabled")),
            "owner": owner,
            "coauthors": coauthors,
            "caption": (caption or "")[:600],
        })
    return {
        "handle": handle,
        "fetched_at": fetched_at,
        "source": source,
        "profile": {
            "username": prof.get("username") or handle,
            "full_name": prof.get("full_name") or "",
            "followers": prof.get("follower_count"),
            "following": prof.get("following_count"),
            "media_count": prof.get("media_count"),
            "is_verified": bool(prof.get("is_verified")),
            "is_private": bool(prof.get("is_private")),
            "category": prof.get("category") or "",
            "external_url": prof.get("external_url") or "",
        },
        "posts": [p for p in posts if p["code"]],
    }


# ---------------------------------------------------------------- commands

def cmd_plan(args):
    root = Path(args.dir)
    handles = handles_from(args, root)
    credits = len(handles) * CREDITS_PER_ACCOUNT
    print(f"{len(handles)} accounts x 1 call ({CAPABILITY}, {CREDITS_PER_ACCOUNT} credits) = {credits} credits a run")
    print(f"Daily for 30 days: {credits * 30} credits. Failed calls are free; a re-run on the same day reuses the day's result.")
    print("Accounts: " + ", ".join("@" + h for h in handles))


def cmd_collect(args):
    key = os.environ.get("UPSCRAPE_API_KEY")
    if not key:
        die("set UPSCRAPE_API_KEY (create one at https://app.upscrape.com), or fetch through the Upscrape MCP server and use `ingest`")
    root = Path(args.dir)
    handles = handles_from(args, root)
    budget = args.max_run_credits if args.max_run_credits is not None else len(handles) * CREDITS_PER_ACCOUNT
    day = today()
    saved = _saved(root)
    saved.update({"handles": handles, "you": clean_handle(args.you) if args.you else saved.get("you")})
    write_json(root / "accounts.json", saved)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    spent, ok, failed = 0.0, [], []
    for h in handles:
        out = root / "snapshots" / day / f"{h}.json"
        if out.exists() and not args.force:
            ok.append((h, "already saved today"))
            continue
        if spent + CREDITS_PER_ACCOUNT > budget:
            failed.append((h, "run_budget_reached"))
            continue
        job, err = fetch_account(h, key, CREDITS_PER_ACCOUNT, run_id)
        if err:
            failed.append((h, err))
            continue
        charged = float((job.get("billing") or {}).get("credits_charged") or 0)
        spent += charged
        snap = normalise(job, h, job.get("completed_at") or dt.datetime.now(dt.timezone.utc).isoformat(), "rest")
        snap["credits_charged"] = charged
        write_json(out, snap)
        ok.append((h, f"{snap['profile']['followers']:,} followers, {len(snap['posts'])} posts, {charged:g} credits"))
    for h, msg in ok:
        print(f"ok      @{h}: {msg}")
    for h, err in failed:
        print(f"failed  @{h}: {err} (not charged)")
    print(f"credits charged this run: {spent:g}")
    if failed and not ok:
        sys.exit(2)


def _saved(root):
    p = root / "accounts.json"
    return json.loads(p.read_text()) if p.exists() else {"handles": [], "you": None}


LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}


def cmd_brand(args):
    """Agency branding, saved with the tracker: report title, logo, "prepared by", accent colour."""
    root = Path(args.dir)
    saved = _saved(root)
    if args.clear:
        saved.pop("brand", None)
        write_json(root / "accounts.json", saved)
        print("branding removed")
        return
    b = saved.get("brand") or {}
    if args.name is not None:
        b["name"] = args.name.strip()[:60]
    if args.client is not None:
        b["client"] = args.client.strip()[:60]
    if args.accent is not None:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", args.accent):
            die("--accent must look like #1A73E8")
        b["accent"] = args.accent
    if args.logo is not None:
        f = Path(args.logo)
        kind = LOGO_TYPES.get(f.suffix.lower())
        if not kind or not f.exists():
            die("--logo must be an existing .png, .jpg, .webp or .svg file")
        raw = f.read_bytes()
        if len(raw) > 300_000:
            die("logo is over 300 KB; use a smaller file, it is embedded in every dashboard")
        b["logo"] = f"data:{kind};base64," + base64.b64encode(raw).decode()
    saved["brand"] = {k: v for k, v in b.items() if v}
    write_json(root / "accounts.json", saved)
    shown = {k: (v if k != "logo" else "embedded") for k, v in saved["brand"].items()}
    print("branding saved: " + ", ".join(f"{k}={v}" for k, v in shown.items()) + ". Run build to apply it.")


WORKFLOW = """# Runs the Instagram competitor tracker every day and saves the history in this repo.
# Needs one repository secret: UPSCRAPE_API_KEY (Settings > Secrets and variables > Actions).
name: Instagram competitor tracker
on:
  schedule:
    - cron: "{minute} {hour} * * *"
  workflow_dispatch:
permissions:
  contents: write
concurrency: ig-tracker
jobs:
  track:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - name: Collect and build
        env:
          UPSCRAPE_API_KEY: ${{{{ secrets.UPSCRAPE_API_KEY }}}}
        run: |
          python3 {script} --dir {tracker} collect{budget}
          python3 {script} --dir {tracker} build
      - name: Save history
        run: |
          git config user.name "ig-tracker"
          git config user.email "ig-tracker@users.noreply.github.com"
          git add {tracker}
          git diff --cached --quiet || git commit -m "Instagram tracker: $(date -u +%F)"
          git push
"""


def cmd_schedule(args):
    """Print a cron line, or write a GitHub Actions workflow that runs daily even when no laptop is on."""
    root = Path(args.dir)
    if not (root / "accounts.json").exists():
        die("run collect (or ingest) once first, so the accounts are saved")
    if not 0 <= args.hour <= 23:
        die("--hour must be 0 to 23")
    hour, minute = args.hour, 17
    script = Path(__file__).resolve()
    if not args.github:
        print("Add this line with `crontab -e` to run daily at "
              f"{hour:02d}:{minute:02d} (this computer's time zone):")
        print(f"{minute} {hour} * * * cd {Path.cwd()} && UPSCRAPE_API_KEY=... python3 {script} "
              f"--dir {root} collect && python3 {script} --dir {root} build")
        print("For runs while this computer is off, use `schedule --github` instead.")
        return
    repo = Path.cwd().resolve()
    if not (repo / ".git").exists():
        die("run this from the root of a git repository (the one you will push to GitHub)")
    try:
        rel_script = script.relative_to(repo)
        rel_tracker = root.resolve().relative_to(repo)
    except ValueError:
        die("the skill and the tracker folder must both be inside this repository; "
            "install the skill into the project (not globally) and keep --dir inside it")
    budget = f" --max-run-credits {args.max_run_credits:g}" if args.max_run_credits is not None else ""
    out = repo / ".github" / "workflows" / "ig-tracker.yml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(WORKFLOW.format(minute=minute, hour=hour, script=rel_script.as_posix(),
                                   tracker=rel_tracker.as_posix(), budget=budget))
    n = len(_saved(root).get("handles") or [])
    print(f"wrote {out.relative_to(repo)}: runs daily at {hour:02d}:{minute:02d} UTC, "
          f"{n} accounts x {CREDITS_PER_ACCOUNT} credits = {n * CREDITS_PER_ACCOUNT} credits a day")
    print("Next: add the UPSCRAPE_API_KEY repository secret, commit, and push. "
          "Run it once by hand from the Actions tab to check it.")


def _saved_you(root):
    p = root / "accounts.json"
    return json.loads(p.read_text()).get("you") if p.exists() else None


def unpoint(obj):
    """Turn a projection keyed by JSON Pointers ({"/data/username": ...}) back into nested objects."""
    if isinstance(obj, list):
        return [unpoint(x) for x in obj]
    if not isinstance(obj, dict) or not obj or not all(k.startswith("/") for k in obj):
        return obj
    out = {}
    for k, v in obj.items():
        parts = [p.replace("~1", "/").replace("~0", "~") for p in k[1:].split("/")]
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = v
    return out


def job_payload(raw):
    """The data part of a saved job answer, with any pointer projection undone."""
    if isinstance(raw, dict) and raw.get("result_complete") is False:
        die("that file is a cut-off preview (result_complete is false). Fetch it again with "
            "`fields` / `collection_path` as SKILL.md path B shows, and save that answer instead.")
    d = raw
    if isinstance(d, dict) and "results" in d:
        d = d["results"][0]
    if isinstance(d, dict) and set(d) >= {"data"} and ("index" in d or "error" in d):
        d = d["data"]
    return unpoint(d)


def cmd_ingest(args):
    root = Path(args.dir)
    h = clean_handle(args.handle)
    stamp = None
    if args.profile:
        if not args.posts:
            die("--profile needs --posts too")
        praw, qraw = (json.loads(Path(f).read_text()) for f in (args.profile, args.posts))
        prof = job_payload(praw)
        prof = prof.get("data", prof)
        posts = job_payload(qraw)
        if not isinstance(posts, list):
            die("--posts should be the answer fetched with collection_path /data/recent_posts")
        raw = {"data": {**prof, "recent_posts": posts}}
        stamp = praw.get("collected_at") if isinstance(praw, dict) else None
    elif args.file:
        raw = json.loads(Path(args.file).read_text())
        if isinstance(raw, dict) and raw.get("result_complete") is False:
            job_payload(raw)
        stamp = raw.get("collected_at") if isinstance(raw, dict) else None
        raw = unpoint(raw)
    else:
        die("give --file, or --profile and --posts")
    snap = normalise(raw, h, args.fetched_at or stamp or dt.datetime.now(dt.timezone.utc).isoformat(), "mcp")
    if snap["profile"]["media_count"] and len(snap["posts"]) < min(RECENT_POSTS, snap["profile"]["media_count"]) // 2:
        print(f"warning: only {len(snap['posts'])} posts for @{h}; the saved answer may be cut off", file=sys.stderr)
    day = (args.date or snap["fetched_at"][:10])
    write_json(root / "snapshots" / day / f"{h}.json", snap)
    acc = root / "accounts.json"
    saved = json.loads(acc.read_text()) if acc.exists() else {"handles": [], "you": None}
    if h not in saved["handles"]:
        saved["handles"].append(h)
    if args.you:
        saved["you"] = clean_handle(args.you)
    write_json(acc, saved)
    print(f"saved @{h} for {day}: {snap['profile']['followers']:,} followers, {len(snap['posts'])} posts")


# ---------------------------------------------------------------- metrics

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def build_data(root):
    snaps = sorted((root / "snapshots").glob("*/*.json"))
    if not snaps:
        die(f"no snapshots in {root}/snapshots yet; run collect or ingest first")
    saved = json.loads((root / "accounts.json").read_text()) if (root / "accounts.json").exists() else {}
    you = saved.get("you")
    by_handle = {}
    for f in snaps:
        s = json.loads(f.read_text())
        by_handle.setdefault(s["handle"], []).append((f.parent.name, s))

    now_ts = max(int(dt.datetime.fromisoformat(s["fetched_at"].replace("Z", "+00:00")).timestamp())
                 for items in by_handle.values() for _, s in items)
    window_start = now_ts - WINDOW_DAYS * 86400
    accounts, all_posts = [], []

    for h, items in sorted(by_handle.items()):
        items.sort(key=lambda x: x[0])
        latest_day, latest = items[-1]
        prof = latest["profile"]
        followers = prof["followers"] or 0
        series = [{"date": d, "followers": s["profile"]["followers"]} for d, s in items]

        def change_since(days):
            target = (dt.date.fromisoformat(latest_day) - dt.timedelta(days=days)).isoformat()
            older = [p for p in series if p["date"] <= target]
            if not older:
                return None
            base = older[-1]["followers"]
            return {"from": older[-1]["date"], "abs": followers - base,
                    "pct": (followers - base) / base * 100 if base else None}

        # every post ever seen, newest counts win
        posts = {}
        for d, s in items:
            for p in s["posts"]:
                fa = int(dt.datetime.fromisoformat(s["fetched_at"].replace("Z", "+00:00")).timestamp())
                posts[p["code"]] = dict(p, seen_at=fa)
        plist = sorted(posts.values(), key=lambda p: -p["taken_at"])
        for p in plist:
            p["engagement"] = (p["likes"] or 0) + (p["comments"] or 0) if not p["counts_hidden"] else None
            p["age_hours"] = (p["seen_at"] - p["taken_at"]) / 3600
            p["fresh"] = p["age_hours"] < FRESH_HOURS
            p["rate"] = (p["engagement"] / followers * 100) if (p["engagement"] is not None and followers) else None
        scored = [p for p in plist if not p["fresh"] and p["engagement"] is not None]
        med_eng = median([p["engagement"] for p in scored])
        for p in plist:
            p["multiple"] = (p["engagement"] / med_eng) if (med_eng and p["engagement"] is not None) else None
            p["breakout"] = bool(p["multiple"] and not p["fresh"] and p["multiple"] >= BREAKOUT_MULTIPLE)

        recent = [p for p in plist if p["taken_at"] >= window_start]
        oldest_seen = min((p["taken_at"] for p in plist), default=now_ts)
        span_days = max(1.0, (now_ts - max(oldest_seen, window_start)) / 86400)
        per_week = len(recent) / span_days * 7 if recent else 0.0
        fmt = {k: 0 for k in ("video", "carousel", "photo", "other")}
        for p in recent:
            fmt[p["format"]] += 1
        fmt_eng = {}
        for k in fmt:
            vals = [p["engagement"] for p in scored if p["format"] == k and p["taken_at"] >= window_start]
            if vals:
                fmt_eng[k] = median(vals)
        promo = [p for p in recent if PROMO_RE.search(p["caption"] or "")]
        collabs = {}
        for p in plist:
            partners = set(p["coauthors"])
            if p["owner"] and p["owner"] != h:
                partners.add(p["owner"])
            for c in partners:
                collabs.setdefault(c, []).append(p["code"])

        accounts.append({
            "handle": h,
            "name": prof["full_name"] or h,
            "is_you": h == you,
            "verified": prof["is_verified"],
            "private": prof["is_private"],
            "followers": followers,
            "following": prof["following"],
            "media_count": prof["media_count"],
            "series": series,
            "change_1d": change_since(1),
            "change_7d": change_since(7),
            "change_30d": change_since(30),
            "posts_per_week": round(per_week, 1),
            "cadence_partial": oldest_seen > window_start,
            "posts_in_window": len(recent),
            "median_engagement": med_eng,
            "median_likes": median([p["likes"] for p in scored]),
            "median_comments": median([p["comments"] for p in scored]),
            "engagement_rate": (med_eng / followers * 100) if (med_eng is not None and followers) else None,
            "format_mix": fmt,
            "format_engagement": fmt_eng,
            "promo_share": (len(promo) / len(recent)) if recent else None,
            "collabs": [{"handle": c, "posts": codes} for c, codes in sorted(collabs.items(), key=lambda x: -len(x[1]))],
            "last_snapshot": latest_day,
            "snapshots": len(items),
        })
        for p in plist:
            all_posts.append({
                "account": h, "code": p["code"], "taken_at": p["taken_at"], "format": p["format"],
                "likes": p["likes"], "comments": p["comments"], "engagement": p["engagement"],
                "rate": p["rate"], "multiple": p["multiple"], "breakout": p["breakout"], "fresh": p["fresh"],
                "counts_hidden": p["counts_hidden"], "coauthors": p["coauthors"],
                "owner": p["owner"], "promo": bool(PROMO_RE.search(p["caption"] or "")),
                "caption": (p["caption"] or "")[:220],
            })

    days = sorted({d for items in by_handle.values() for d, _ in items})
    data = {
        "generated_at": dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).isoformat(),
        "first_day": days[0], "last_day": days[-1], "days_tracked": len(days),
        "window_days": WINDOW_DAYS, "fresh_hours": FRESH_HOURS, "breakout_multiple": BREAKOUT_MULTIPLE,
        "you": you,
        "brand": saved.get("brand"),
        "accounts": accounts,
        "posts": all_posts,
        "notes": [
            "Engagement is likes plus comments. Engagement rate is an account's median post engagement divided by its followers.",
            f"Posts younger than {FRESH_HOURS} hours are shown but not scored: they are still collecting likes.",
            f"A breakout is a post at {BREAKOUT_MULTIPLE:g}x or more of that account's own median engagement.",
            "Reel view counts and paid-partnership labels are not included.",
        ],
        "version": VERSION,
    }
    data["highlights"] = highlights(data)
    return data


def fmt_n(n):
    if n is None:
        return "n/a"
    n = float(n)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{suf}".replace(".0" + suf, suf)
    return f"{n:,.0f}"


def highlights(data):
    """Plain sentences built only from computed numbers; no guessing."""
    out = []
    brk = sorted([p for p in data["posts"] if p["breakout"]], key=lambda p: -(p["multiple"] or 0))
    if brk:
        p = brk[0]
        out.append({"kind": "breakout", "account": p["account"], "code": p["code"],
                    "text": f"@{p['account']} has a breakout {p['format']}: {fmt_n(p['engagement'])} likes and comments, "
                            f"{p['multiple']:.0f}x its usual post."})
    rated = [a for a in data["accounts"] if a["engagement_rate"] is not None]
    if len(rated) >= 2:
        top = max(rated, key=lambda a: a["engagement_rate"])
        low = min(rated, key=lambda a: a["engagement_rate"])
        if low["engagement_rate"]:
            out.append({"kind": "rate", "account": top["handle"],
                        "text": f"@{top['handle']} gets {top['engagement_rate'] / low['engagement_rate']:.1f}x the engagement rate "
                                f"of @{low['handle']} ({top['engagement_rate']:.2f}% vs {low['engagement_rate']:.2f}%)."})
    busiest = max(data["accounts"], key=lambda a: a["posts_per_week"])
    if busiest["posts_per_week"]:
        out.append({"kind": "cadence", "account": busiest["handle"],
                    "text": f"@{busiest['handle']} posts most often: {busiest['posts_per_week']:g} posts a week"
                            + (f" (from its last {busiest['posts_in_window']} posts)." if busiest["cadence_partial"] else ".")})
    comm = sorted([p for p in data["posts"] if p["comments"] and p["likes"] and not p["fresh"]],
                  key=lambda p: -(p["comments"] / max(p["likes"], 1)))
    if comm and comm[0]["comments"] / max(comm[0]["likes"], 1) >= 0.5:
        p = comm[0]
        why = " It's a giveaway." if re.search(r"giveaway|to enter|winners?", p["caption"] or "", re.IGNORECASE) else ""
        out.append({"kind": "comments", "account": p["account"], "code": p["code"],
                    "text": f"@{p['account']} drew {fmt_n(p['comments'])} comments on one post, "
                            f"{p['comments'] / max(p['likes'], 1):.1f} per like.{why}"})
    growth = [a for a in data["accounts"] if a["change_7d"] and a["change_7d"]["pct"] is not None]
    if growth:
        g = max(growth, key=lambda a: a["change_7d"]["pct"])
        out.append({"kind": "growth", "account": g["handle"],
                    "text": f"@{g['handle']} grew fastest this week: {g['change_7d']['abs']:+,} followers ({g['change_7d']['pct']:+.2f}%)."})
    collab = [(a["handle"], c) for a in data["accounts"] for c in a["collabs"]]
    if collab:
        h, c = max(collab, key=lambda x: len(x[1]["posts"]))
        out.append({"kind": "collab", "account": h,
                    "text": f"@{h} co-posted with @{c['handle']} ({len(c['posts'])} post{'s' if len(c['posts']) > 1 else ''})."})
    promos = [a for a in data["accounts"] if a["promo_share"] is not None]
    if promos:
        pr = max(promos, key=lambda a: a["promo_share"])
        if pr["promo_share"] >= 0.5:
            out.append({"kind": "promo", "account": pr["handle"],
                        "text": f"{pr['promo_share'] * 100:.0f}% of @{pr['handle']}'s recent posts push an offer or a sale."})
    return out


def brief_md(data):
    lines = [f"# Instagram competitors: {data['last_day']}", ""]
    lines += [f"- {h['text']}" for h in data["highlights"]] + [""]
    lines += ["| Account | Followers | 7-day change | Posts a week | Engagement rate | Breakouts |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for a in sorted(data["accounts"], key=lambda a: -(a["engagement_rate"] or 0)):
        ch = a["change_7d"]
        chs = f"{ch['abs']:+,}" if ch else "after 7 days"
        br = sum(1 for p in data["posts"] if p["account"] == a["handle"] and p["breakout"])
        er = f"{a['engagement_rate']:.2f}%" if a["engagement_rate"] is not None else "n/a"
        lines.append(f"| @{a['handle']} | {a['followers']:,} | {chs} | {a['posts_per_week']:g} | {er} | {br} |")
    tops = sorted((p for p in data["posts"] if p["breakout"]), key=lambda p: -p["multiple"])[:5]
    if tops:
        lines += ["", "## Breakout posts", ""]
        lines += [f"- {p['multiple']:.1f}x @{p['account']}: https://www.instagram.com/p/{p['code']}/" for p in tops]
    you = next((a["handle"] for a in data["accounts"] if a["is_you"]), None)
    lines += ["", "## Rerun", "", "```",
              f"accounts: {', '.join('@' + a['handle'] for a in data['accounts'])}",
              f"you: {'@' + you if you else 'not set'}",
              "command: python3 scripts/track.py collect && python3 scripts/track.py build",
              "```"]
    lines += ["", *[f"_{n}_" for n in data["notes"]], "",
              "Data by [Upscrape](https://upscrape.com/?utm_source=skill&utm_medium=brief&utm_campaign=ig-tracker).", ""]
    return "\n".join(lines)


def cmd_build(args):
    root = Path(args.dir)
    data = build_data(root)
    write_json(root / "data.json", data)
    html = TEMPLATE.read_text()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if "/*__DATA__*/null" not in html:
        die("dashboard template is missing its data slot")
    (root / "dashboard.html").write_text(html.replace("/*__DATA__*/null", blob))
    (root / "brief.md").write_text(brief_md(data))
    print(f"wrote {root / 'dashboard.html'}, {root / 'data.json'}, {root / 'brief.md'}")
    for h in data["highlights"]:
        print(" - " + h["text"])


def main():
    ap = argparse.ArgumentParser(description="Instagram competitor tracker (Upscrape)")
    ap.add_argument("--dir", default="ig-tracker", help="tracker folder (default ./ig-tracker)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--handles")
    c = sub.add_parser("collect"); c.add_argument("--handles"); c.add_argument("--you")
    c.add_argument("--max-run-credits", type=float, help="stop the run before spending more than this")
    c.add_argument("--force", action="store_true", help="pull again even if today's snapshot exists")
    i = sub.add_parser("ingest"); i.add_argument("--handle", required=True); i.add_argument("--file")
    i.add_argument("--profile", help="saved answer fetched with the profile `fields`")
    i.add_argument("--posts", help="saved answer fetched with collection_path /data/recent_posts")
    i.add_argument("--date"); i.add_argument("--fetched-at"); i.add_argument("--you")
    b = sub.add_parser("brand", help="agency branding: report title, logo, prepared-by, accent colour")
    b.add_argument("--name", help="your agency's name, shown as 'Prepared by ...'")
    b.add_argument("--client", help="who the report is for; becomes the report title")
    b.add_argument("--logo", help="logo file (.png, .jpg, .webp or .svg, under 300 KB)")
    b.add_argument("--accent", help="accent colour, like #1A73E8")
    b.add_argument("--clear", action="store_true", help="remove all branding")
    sc = sub.add_parser("schedule", help="run daily: print a cron line, or write a GitHub Actions workflow")
    sc.add_argument("--github", action="store_true", help="write .github/workflows/ig-tracker.yml")
    sc.add_argument("--hour", type=int, default=7, help="hour to run, 0-23 (UTC on GitHub)")
    sc.add_argument("--max-run-credits", type=float)
    sub.add_parser("build")
    args = ap.parse_args()
    {"plan": cmd_plan, "collect": cmd_collect, "ingest": cmd_ingest, "build": cmd_build,
     "brand": cmd_brand, "schedule": cmd_schedule}[args.cmd](args)


if __name__ == "__main__":
    main()
