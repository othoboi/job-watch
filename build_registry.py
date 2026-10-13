#!/usr/bin/env python3
"""Build boards.json: every ATS board seen in the Simplify feeds (2026 + 2027 intern rows), plus extra_boards.
Companies that interned last year will post again on the SAME board, so polling the board catches the new req at publish time."""
import json, re, sys, urllib.request, collections, os
HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS = ["https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"]
EXTRA = [  # (ats, token/host, display name)  — add freely
 ("greenhouse","waymo","Waymo"),("greenhouse","togetherai","Together AI"),("greenhouse","samayaai","Samaya AI"),
 ("greenhouse","datacor","Datacor"),("greenhouse","imc","IMC"),("greenhouse","drweng","DRW"),("greenhouse","optiverus","Optiver"),
 ("ashby","pangramlabs","Pangram Labs"),("ashby","phonic","Phonic"),("lever","tri","Toyota Research Institute"),
]
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"}), timeout=120).read().decode()
boards = {}
stats = collections.Counter()
last_year = collections.defaultdict(list)
for f in FEEDS:
    for x in json.loads(get(f)):
        u = x["url"]; name = x["company_name"]
        key = None
        m = re.search(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)", u)
        if m and m.group(1) not in ("embed",): key = ("greenhouse", m.group(1))
        m = key is None and re.search(r"jobs\.lever\.co/([^/?#]+)", u)
        if m: key = ("lever", m.group(1))
        m = key is None and re.search(r"jobs\.ashbyhq\.com/([^/?#]+)", u)
        if m: key = ("ashby", m.group(1))
        m = key is None and re.search(r"jobs\.smartrecruiters\.com/([^/?#]+)", u)
        if m: key = ("smartrecruiters", m.group(1))
        m = key is None and re.search(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", u)
        if m: key = ("workday", f"{m.group(1)}.{m.group(2)}/{m.group(3)}")
        if key is None:
            stats["no_api:" + re.sub(r"^https?://([^/]+)/.*", r"\1", u).split(".")[-2] if "." in u else "no_api"] += 1
            continue
        boards.setdefault(key, name); stats[key[0]] += 1
        if any(t in ("Summer 2026",) for t in x.get("terms", [])) and x["category"] in ("AI/ML/Data","Quant","Data Science, AI & Machine Learning"):
            last_year[name].append(x["date_posted"])
for a,t,n in EXTRA: boards.setdefault((a,t), n)
out = [{"ats":a,"token":t,"name":n} for (a,t),n in sorted(boards.items(), key=lambda kv: kv[1].lower())]
json.dump(out, open(os.path.join(HERE,"boards.json"),"w"), indent=0)
json.dump({k:sorted(v) for k,v in last_year.items()}, open(os.path.join(HERE,"last_year_posts.json"),"w"))
print(len(out),"boards;", collections.Counter(b["ats"] for b in out)); print("rows w/o public API (top):", stats.most_common(12))
