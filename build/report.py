"""Build the central desk calling report from the GEMS Frappe CRM.

Env: FRAPPE_TOKEN ("token key:secret"), CENTRAL_REPS (JSON {"email": "Name"}).
Writes site/index.html (overview) and site/day.html (single-day view).
"""
import collections as C, datetime, json, os, time, urllib.parse, urllib.request

B = "https://gemseducation.m.frappe.cloud"
T = os.environ["FRAPPE_TOKEN"]
REPS = json.loads(os.environ["CENTRAL_REPS"])
START = "2026-10-08"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "site")
NOW = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
TODAY = NOW.strftime("%Y-%m-%d")


def get(doctype, fields, filters=()):
    out, start = [], 0
    while True:
        q = urllib.parse.urlencode({"fields": json.dumps(fields), "filters": json.dumps(list(filters)),
                                    "limit_start": start, "limit_page_length": 500})
        for attempt in range(6):
            try:
                req = urllib.request.Request(f"{B}/api/resource/{urllib.parse.quote(doctype)}?{q}", headers={"Authorization": T})
                rows = json.load(urllib.request.urlopen(req, timeout=90))["data"]
                break
            except Exception as e:
                if attempt == 5:
                    raise
                print("retry", doctype, e, flush=True)
                time.sleep(5 * (attempt + 1))
        out += rows
        if len(rows) < 500:
            return out
        start += 500


d10 = lambda p: (lambda s: s[-10:] if len(s) >= 10 else None)("".join(c for c in (p or "") if c.isdigit()))
r1 = lambda x: round(x, 1)

leads = get("CRM Lead", ["name", "mobile_no", "phone"])
calls = get("CRM Call Log", ["caller", "status", "duration", "to", "type", "reference_doctype", "reference_docname",
                             "start_time", "call_outcome", "creation"], [["type", "=", "Outgoing"]])
todos = get("ToDo", ["reference_name", "allocated_to", "creation"], [["reference_type", "=", "CRM Lead"]])
visits = get("GEMS Visit", ["name", "owner", "creation", "status", "attended_on"])
print("leads", len(leads), "calls", len(calls), "todos", len(todos), "visits", len(visits))

by_phone = C.defaultdict(list)
for l in leads:
    for p in {d10(l["mobile_no"]), d10(l["phone"])} - {None}:
        by_phone[p].append(l["name"])


def lids(c):
    if c.get("reference_doctype") == "CRM Lead" and c.get("reference_docname"):
        return [c["reference_docname"]]
    return by_phone.get(d10(c["to"]), []) or [c["to"]]


day = lambda c: (c["start_time"] or c["creation"])[:10]
conn = lambda c: c["status"] == "Completed" and (c["duration"] or 0) > 0
ec = [c for c in calls if c["caller"] in REPS and START <= day(c) <= TODAY]
called = {i for c in calls for i in lids(c)}

# Per day, per counsellor
blank = lambda: dict(assigned=set(), dials=0, connects=0, talk=0, leads=set(), vb=0, vd=0, oc=C.Counter())
D = C.defaultdict(blank)
for t in todos:
    if t["allocated_to"] in REPS:
        D[(t["creation"][:10], REPS[t["allocated_to"]])]["assigned"].add(t["reference_name"])
for c in ec:
    x = D[(day(c), REPS[c["caller"]])]
    x["dials"] += 1; x["connects"] += conn(c); x["talk"] += c["duration"] or 0
    x["leads"].update(lids(c)); x["oc"][c["call_outcome"] or "Not logged"] += 1
for v in visits:
    if v["owner"] in REPS:
        D[(v["creation"][:10], REPS[v["owner"]])]["vb"] += 1
        if v["status"] == "Done" and v["attended_on"]:
            D[(v["attended_on"][:10], REPS[v["owner"]])]["vd"] += 1

days, d0 = [], datetime.date.fromisoformat(START)
while d0.isoformat() <= TODAY:
    days.append(d0.isoformat()); d0 += datetime.timedelta(days=1)
names = list(REPS.values())
daily, daily_oc = [], []
for dd in days:
    for nm in names:
        x = D.get((dd, nm)) or blank()
        daily.append(dict(date=dd, counsellor=nm, leads_assigned=len(x["assigned"]), dials=x["dials"],
                          unique_dials=len(x["leads"]), connects=x["connects"], talk_minutes=r1(x["talk"] / 60),
                          visits_booked=x["vb"], visits_done=x["vd"]))
        for o, n in x["oc"].items():
            daily_oc.append(dict(date=dd, counsellor=nm, outcome=o, calls=n))

# Period totals and per-calling-day averages (averages use full days only, before today)
reps, outcomes = [], []
for nm, group in [(n, [n]) for n in names] + [("Team", names)]:
    rows = [x for x in daily if x["counsellor"] in group]
    full = [x for x in rows if x["date"] < TODAY]
    act = sorted({x["date"] for x in full if x["dials"]})
    s = lambda k, rs=rows: sum(x[k] for x in rs)
    avg = lambda k: r1(sum(x[k] for x in full if x["date"] in act) / len(act)) if act else 0
    mine = [c for c in ec if REPS[c["caller"]] in group]
    reps.append(dict(counsellor=nm, dials=s("dials"), unique_leads_dialled=len({i for c in mine for i in lids(c)}),
                     connects=s("connects"), connect_rate=round(s("connects") / s("dials"), 3) if s("dials") else 0,
                     talk_minutes=r1(s("talk_minutes")), visits_booked=s("visits_booked"), visits_done=s("visits_done"),
                     calling_days=len(act), avg_daily_dials=avg("dials"), avg_daily_connects=avg("connects"),
                     avg_daily_talk_minutes=avg("talk_minutes"), avg_daily_visits_booked=avg("visits_booked"),
                     avg_daily_visits_done=avg("visits_done")))
    oc = C.Counter((c["call_outcome"] or "Not logged") for c in mine)
    outcomes += [dict(counsellor=nm, outcome=o, calls=n) for o, n in oc.most_common()]

data = dict(reps=reps, daily=daily, outcomes=outcomes, daily_outcomes=daily_oc,
            meta=dict(start=START, end=TODAY, today=TODAY, partial=True, taken=NOW.strftime("%-d %b %Y, %-I:%M %p IST")))
blob = json.dumps(data, separators=(",", ":"))
os.makedirs(OUT, exist_ok=True)
for src, dst in [("overview.html", "index.html"), ("day.html", "day.html")]:
    html = open(os.path.join(HERE, src)).read().replace("__DATA__", blob)
    open(os.path.join(OUT, dst), "w").write(html)
for x in reps:
    print(x)
