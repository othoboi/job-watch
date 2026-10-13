#!/usr/bin/env python3
"""watch.py — poll company ATS endpoints directly (the primary source every job board and GitHub list scrapes),
read each new posting's own sponsorship language the moment it appears, and push an alert.

Needs outbound HTTPS to: boards-api.greenhouse.io, api.lever.co, api.ashbyhq.com, api.smartrecruiters.com,
*.myworkdayjobs.com, and ntfy.sh (alerts). Run it on your own machine or on GitHub Actions (see watch.yml).
It will NOT work from the Claude cloud sandbox (egress policy blocks those hosts).

  python3 watch.py                # all ATS types once
  python3 watch.py --ats greenhouse,lever,ashby,smartrecruiters   # fast set (every ~10 min)
  python3 watch.py --ats workday  # slower set (every 30-60 min)
  python3 watch.py --baseline     # record everything currently open as seen, alert nothing (first run)
Env: NTFY_TOPIC (push alerts via ntfy.sh app), optional WATCH_MIN_AGE_DAYS.
"""
import json, re, os, sys, time, html, argparse, urllib.request, urllib.error, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (personal job watcher)"}

INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|student researcher|graduate researcher|research fellow|\bfellowship\b|apprentice|summer analyst|summer associate|\bpredoc", re.I)
SHAPE = re.compile(r"data|statist|research|scientist|analytic|machine learning|\bml\b|\bai\b|\bllm|quant|model|simulat|forecast|optimi|algorithm|applied|insight|"
                   r"econom|decision|experiment|causal|geospatial|mobility|transport|risk|actuar|biostat|bioinformat|intelligence|autonom|robot|perception|sensing", re.I)
OUT_TITLE = re.compile(r"\b(phd|ph\.d)\b(?!.*(master|ms\b|m\.s))|sales|marketing|recruit|legal|counsel|accounting|audit|nurs|pharmac(y|ist)|clinical|"
                       r"defense|clearance|ts/sci|weapon|missile|hardware|firmware|mechanical|civil engineer|electrical engineer|plant|warehouse|retail|store", re.I)
DEFENSE_CO = re.compile(r"general dynamics|gdit|lockheed|northrop|raytheon|rtx|l3harris|bae systems|booz allen|leidos|saic|anduril|boeing|mitre|sandia|caci|parsons|peraton|huntington ingalls|collins aerospace", re.I)
US_LOC = re.compile(r"\bUS\b|USA|United States|Remote|, [A-Z]{2}\b|New York|San Francisco|Seattle|Boston|Austin|Chicago", re.I)
NON_US = re.compile(r"\b(UK|London|Canada|Toronto|Vancouver|India|Bangalore|Germany|France|Paris|Singapore|Tokyo|Dublin|Ireland|Poland|Krakow|Brazil|Mexico|Sydney|Amsterdam|Zurich|Tel Aviv)\b", re.I)
SEASON_OLD = re.compile(r"summer 2026|fall 2026|winter 2026|spring 2026|\b2026 (summer|intern)", re.I)

NEG = [r"not (be )?(able|eligible|available|offer|provid)\w*[^.]{0,60}sponsor", r"unable to (provide )?(visa )?sponsor", r"\bno (visa )?sponsorship",
       r"(will|does|do|can) not (provide |offer |support )?(visa )?sponsor", r"cannot (provide |offer )?(visa )?sponsor", r"not sponsor",
       r"without (the need for )?(current or future )?(visa )?sponsorship", r"now or in the future", r"now,? or in the future",
       r"(current|future) (need|requirement)[^.]{0,30}sponsor", r"u\.?s\.? citizen(s|ship)?\b(?! status)", r"security clearance", r"\bitar\b", r"export control",
       r"permanent resident", r"sponsorship (is )?not (available|offered|provided)", r"not eligible for (employment )?(visa )?sponsorship"]
POS = [r"(we|will|can|may|does|do) (do |consider )?(provide |offer )?(visa )?sponsor", r"sponsorship (is )?(available|offered|provided)", r"offers? (visa )?sponsorship",
       r"\bcpt\b", r"\bopt\b", r"international students? (are )?(welcome|encouraged|eligible)", r"visa (support|assistance)"]
NEG_RE = [re.compile(p, re.I) for p in NEG]; POS_RE = [re.compile(p, re.I) for p in POS]


def http(url, data=None, method=None, timeout=25):
    h = dict(UA); h["Accept"] = "application/json"
    if data is not None:
        data = json.dumps(data).encode(); h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def clean(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(t or ""))))


def classify(text):
    if not text.strip():
        return "UNREAD", []
    neg = [m.group(0)[:80] for r in NEG_RE for m in [r.search(text)] if m]
    if neg:
        return "BLOCKED", neg
    pos = [m.group(0)[:80] for r in POS_RE for m in [r.search(text)] if m]
    return ("POSITIVE" if pos else "NEUTRAL"), pos


# ---- per-ATS fetchers: each returns list of dicts {id,title,loc,url,posted(epoch|None),text(str|None, may be filled lazily)} ----
def f_greenhouse(b):
    d = http(f"https://boards-api.greenhouse.io/v1/boards/{b['token']}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        ts = j.get("first_published") or j.get("updated_at")
        out.append(dict(id=str(j["id"]), title=j["title"], loc=(j.get("location") or {}).get("name", ""), url=j["absolute_url"],
                        posted=parse_ts(ts), text=clean(j.get("content", ""))))
    return out


def f_lever(b):
    d = http(f"https://api.lever.co/v0/postings/{b['token']}?mode=json")
    out = []
    for j in d:
        text = (j.get("descriptionPlain", "") + " " + " ".join(l.get("text", "") + " " + clean(l.get("content", "")) for l in j.get("lists", [])) + " " + j.get("additionalPlain", ""))
        out.append(dict(id=j["id"], title=j["text"], loc=(j.get("categories") or {}).get("location", ""), url=j["hostedUrl"],
                        posted=(j.get("createdAt") or 0) / 1000 or None, text=text))
    return out


def f_ashby(b):
    d = http(f"https://api.ashbyhq.com/posting-api/job-board/{b['token']}")
    return [dict(id=j["id"], title=j["title"], loc=j.get("location", ""), url=j.get("jobUrl", ""), posted=parse_ts(j.get("publishedAt")),
                 text=j.get("descriptionPlain", "")) for j in d.get("jobs", []) if j.get("isListed", True)]


def f_smartrecruiters(b):
    d = http(f"https://api.smartrecruiters.com/v1/companies/{b['token']}/postings?q=intern&limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location", {})
        out.append(dict(id=j["id"], title=j["name"], loc=", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])),
                        url=f"https://jobs.smartrecruiters.com/{b['token']}/{j['id']}", posted=parse_ts(j.get("releasedDate")), text=None, _detail=j.get("ref")))
    return out


def detail_smartrecruiters(job):
    d = http(job["_detail"])
    secs = (d.get("jobAd") or {}).get("sections", {})
    return clean(" ".join((s or {}).get("text", "") for s in secs.values()))


def f_workday(b):
    host, site = b["token"].split("/", 1)
    tenant = host.split(".")[0]
    out = []
    for off in (0, 20, 40):
        d = http(f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs", data={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": "intern"}, method="POST")
        for j in d.get("jobPostings", []):
            out.append(dict(id=j["externalPath"], title=j["title"], loc=j.get("locationsText", ""), url=f"https://{host}.myworkdayjobs.com/{site}{j['externalPath']}",
                            posted=None, text=None, _detail=f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{j['externalPath']}", _posted_txt=j.get("postedOn", "")))
        if len(d.get("jobPostings", [])) < 20:
            break
    return out


def detail_workday(job):
    d = http(job["_detail"])
    return clean(d.get("jobPostingInfo", {}).get("jobDescription", ""))


FETCH = dict(greenhouse=f_greenhouse, lever=f_lever, ashby=f_ashby, smartrecruiters=f_smartrecruiters, workday=f_workday)
DETAIL = dict(smartrecruiters=detail_smartrecruiters, workday=detail_workday)


def parse_ts(s):
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return None


def relevant(j, company):
    t = j["title"]
    if not INTERN.search(t) or not SHAPE.search(t) or OUT_TITLE.search(t) or DEFENSE_CO.search(company):
        return False
    if SEASON_OLD.search(t):
        return False
    loc = j.get("loc", "")
    if loc:
        segs = [x for x in re.split(r"[;|/]| or ", loc) if x.strip()]
        if segs and all(NON_US.search(x) and not re.search(r"\b(US|USA|United States)\b", x) for x in segs):
            return False  # every listed location is outside the US
    return True


def notify(title, body, url=None):
    topic = os.environ.get("NTFY_TOPIC")
    print(f"ALERT: {title} | {body} | {url}")
    if not topic:
        return
    h = {"Title": title.encode("ascii", "ignore").decode()[:250], "Priority": "high", "Tags": "briefcase"}
    if url:
        h["Click"] = url
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=h), timeout=15)
    except Exception as e:
        print("ntfy failed:", e)


def poll(b, seen, baseline):
    alerts = []
    try:
        jobs = FETCH[b["ats"]](b)
    except Exception as e:
        return alerts, f"{b['ats']}:{b['token']}:{type(e).__name__}"
    for j in jobs:
        key = f"{b['ats']}:{b['token']}:{j['id']}"
        if key in seen:
            continue
        if not relevant(j, b["name"]):
            seen[key] = 0
            continue
        text = j["text"]
        if text is None and b["ats"] in DETAIL and not baseline:
            try:
                text = DETAIL[b["ats"]](j)
            except Exception:
                text = ""
        status, hits = classify(text or "")
        seen[key] = int(time.time())
        if baseline:
            continue
        if status != "BLOCKED":
            alerts.append(dict(company=b["name"], title=j["title"], loc=j["loc"], url=j["url"], status=status, hits=hits, posted=j["posted"]))
    return alerts, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", default="greenhouse,lever,ashby,smartrecruiters,workday")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()
    boards = [b for b in json.load(open(os.path.join(HERE, "boards.json"))) if b["ats"] in a.ats.split(",")]
    sp = os.path.join(HERE, "seen.json")
    seen = json.load(open(sp)) if os.path.exists(sp) else {}
    first = a.baseline or not seen
    errs, alerts = [], []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for al, err in ex.map(lambda b: poll(b, seen, first), boards):
            alerts += al
            if err:
                errs.append(err)
    json.dump(seen, open(sp, "w"))
    order = {"POSITIVE": 0, "NEUTRAL": 1, "UNREAD": 2}
    alerts.sort(key=lambda x: order[x["status"]])
    with open(os.path.join(HERE, "alerts.md"), "a") as f:
        for x in alerts:
            f.write(f"- {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} [{x['status']}] **{x['company']}** — {x['title']} ({x['loc']}) {x['url']}\n")
    for x in alerts:
        notify(f"{x['status']}: {x['company']}", f"{x['title']} — {x['loc']}" + (f" | {x['hits'][:1]}" if x['hits'] else ""), x["url"])
    print(f"boards={len(boards)} errors={len(errs)} new_relevant={len(alerts)} baseline={first}")
    if errs[:5]:
        print("sample errors:", errs[:5])


if __name__ == "__main__":
    main()
