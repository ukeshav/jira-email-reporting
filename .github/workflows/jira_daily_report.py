"""
Jira Daily Sprint Progress Report Generator
============================================
Generates an HTML email replicating EVERY section from the PDF report:

  1.  Sprint meta (dates, duration, remaining, state)
  2.  KPI banner (Total, Epics/Stories, Bugs, Closed, In Progress, In QA, Open, % Completion)
  3.  Overall Status Summary  — pie chart + table (all raw Jira statuses)
  4.  Issue Type Breakdown    — pie chart + table (Bug/Task/Story/Epic/Subtask)
  5.  Epics & Stories Combined — table
  6.  Bug Status Breakdown    — pie chart + table
  7.  Release-wise Bifurcation — bar chart + table
  8.  App Version Bifurcation  — pie chart + table (Android/iOS/Web/Backend/Admin)
  9.  Sprint Task Allocation – Per Person — full table with totals row
  10. Sprint Burndown (Issues Remaining by Day) — line chart + table
  11. Bug Sheet – Full Details — complete bug table (all bugs, all columns)

Usage:
    pip install requests matplotlib
    python jira_daily_report.py

Cron (8 AM daily, Mon-Fri):
    0 8 * * 1-5 cd /path/to/folder && python3 jira_daily_report.py >> report.log 2>&1
"""

import smtplib, base64, io
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from collections import defaultdict

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import config   # config.py in the same folder


# ══════════════════════════════════════════════════════════════
#  JIRA CLIENT
# ══════════════════════════════════════════════════════════════

class JiraClient:
    def __init__(self):
        self.base = config.JIRA_BASE_URL.rstrip("/")
        self.auth = (config.JIRA_EMAIL, config.JIRA_API_TOKEN)
        self.hdr  = {"Accept": "application/json"}

    def _get(self, path, params=None, agile=False):
        root = f"{self.base}/rest/{'agile/1.0' if agile else 'api/3'}/{path}"
        r = requests.get(root, auth=self.auth, headers=self.hdr, params=params)
        r.raise_for_status()
        return r.json()

    def active_sprint(self):
        """Return (board_id, sprint_dict) for the first active sprint."""
        boards = self._get("board", {"projectKeyOrId": config.JIRA_PROJECT}, agile=True)
        for board in boards.get("values", []):
            sprints = self._get(f"board/{board['id']}/sprint",
                                {"state": "active"}, agile=True)
            active = sprints.get("values", [])
            if active:
                return board["id"], active[0]
        raise RuntimeError("No active sprint found for project: " + config.JIRA_PROJECT)

    def sprint_issues(self, board_id, sprint_id):
        """Fetch every issue in the sprint (paginated)."""
        issues, start = [], 0
        fields = ("summary,status,issuetype,priority,assignee,"
                  "fixVersions,created,updated,description")
        while True:
            data = self._get(
                f"board/{board_id}/sprint/{sprint_id}/issue",
                {"startAt": start, "maxResults": 100, "fields": fields},
                agile=True,
            )
            chunk = data.get("issues", [])
            issues.extend(chunk)
            start += len(chunk)
            if start >= data.get("total", 0):
                break
        return issues


# ══════════════════════════════════════════════════════════════
#  STATUS / TYPE HELPERS
# ══════════════════════════════════════════════════════════════

DONE_SET   = {"Done","Closed","Resolved","Dev Done","QA Approved","Ready For Release"}
PROG_SET   = {"In Progress","In Development"}
QA_SET     = {"Ready For QA","In QA","QA In Progress"}
OPEN_SET   = {"Open","To Do","Reopened","Backlog","New"}

def bucket(status):
    s = status.strip()
    if s in DONE_SET:  return "Done"
    if s in PROG_SET:  return "In Progress"
    if s in QA_SET:    return "QA"
    if s in OPEN_SET:  return "Open"
    return "Other"

def type_key(issue_type):
    t = issue_type.lower()
    for k in ("bug","task","story","epic","subtask"):
        if k in t: return k
    return "task"

def platform_from_versions(fix_versions):
    for v in fix_versions:
        vl = v.lower()
        if "android" in vl: return "Android"
        if "ios"     in vl: return "iOS"
        if "web"     in vl: return "Web"
        if "backend" in vl: return "Backend"
        if "admin"   in vl: return "Admin Panel"
    return "Unversioned"

def adf_to_text(desc):
    if not desc: return ""
    if isinstance(desc, str): return desc[:300]
    texts = []
    def walk(node):
        if node.get("type") == "text":
            texts.append(node.get("text",""))
        for child in node.get("content", []):
            walk(child)
    walk(desc)
    return " ".join(texts)[:300]


# ══════════════════════════════════════════════════════════════
#  DATA PROCESSING
# ══════════════════════════════════════════════════════════════

def process(issues, sprint):
    status_counts = defaultdict(int)
    type_data     = defaultdict(lambda: dict(total=0,done=0,in_progress=0,qa=0,open=0))
    epic_story    = []
    assignee_data = defaultdict(lambda: dict(
        total=0,epic=0,story=0,task=0,subtask=0,bug=0,
        done=0,in_progress=0,qa=0,open=0))
    release_data  = defaultdict(lambda: dict(total=0,bugs=0,done=0,in_progress=0,qa=0,open=0))
    platform_data = defaultdict(lambda: dict(total=0,bugs=0,done=0,in_progress=0,qa=0,open=0))
    bug_list      = []

    # Burndown setup
    try:
        start_dt = datetime.strptime(sprint["startDate"][:10], "%Y-%m-%d")
        end_dt   = datetime.strptime(sprint["endDate"][:10],   "%Y-%m-%d")
    except Exception:
        start_dt = end_dt = datetime.now()
    today        = datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)
    sprint_days  = max(1,(end_dt-start_dt).days)
    total_issues = len(issues)

    closed_by_day = defaultdict(int)
    for issue in issues:
        f = issue["fields"]
        if bucket(f["status"]["name"]) == "Done":
            upd = (f.get("updated") or "")[:10]
            if upd: closed_by_day[upd] += 1

    burndown_rows = []
    cumulative_closed = 0
    for i in range(sprint_days + 1):
        day       = start_dt + timedelta(days=i)
        day_str   = day.strftime("%Y-%m-%d")
        day_label = day.strftime("%-d %b %Y")
        ideal_rem = max(0, round(total_issues - (total_issues/sprint_days)*i))
        cumulative_closed += closed_by_day.get(day_str, 0)
        actual_rem = total_issues - cumulative_closed
        if day <= today:
            st = "On Track" if actual_rem <= ideal_rem else "Behind"
        else:
            st = "Future"
        burndown_rows.append(dict(
            date=day_label, ideal=ideal_rem,
            actual=actual_rem if day <= today else None,
            status=st,
        ))

    # Main loop
    for issue in issues:
        f          = issue["fields"]
        raw_status = f["status"]["name"]
        bkt        = bucket(raw_status)
        itype      = type_key(f["issuetype"]["name"])
        assignee   = (f.get("assignee") or {}).get("displayName","Unassigned")
        fix_vers   = [v["name"] for v in (f.get("fixVersions") or [])]
        platform   = platform_from_versions(fix_vers)
        priority   = (f.get("priority") or {}).get("name","")
        key        = issue["key"]
        summary_   = f.get("summary","")
        created    = (f.get("created") or "")[:10]
        updated    = (f.get("updated") or "")[:10]
        desc       = adf_to_text(f.get("description"))

        status_counts[raw_status] += 1

        type_data[itype]["total"] += 1
        if   bkt == "Done":        type_data[itype]["done"] += 1
        elif bkt == "In Progress": type_data[itype]["in_progress"] += 1
        elif bkt == "QA":          type_data[itype]["qa"] += 1
        else:                      type_data[itype]["open"] += 1

        if itype in ("epic","story"):
            epic_story.append(dict(key=key, summary=summary_,
                                   status=raw_status, type=itype, assignee=assignee))

        ad = assignee_data[assignee]
        ad["total"] += 1;  ad[itype] += 1
        if   bkt == "Done":        ad["done"] += 1
        elif bkt == "In Progress": ad["in_progress"] += 1
        elif bkt == "QA":          ad["qa"] += 1
        else:                      ad["open"] += 1

        for rv in (fix_vers or ["Unversioned"]):
            rd = release_data[rv]
            rd["total"] += 1
            if itype == "bug": rd["bugs"] += 1
            if   bkt == "Done":        rd["done"] += 1
            elif bkt == "In Progress": rd["in_progress"] += 1
            elif bkt == "QA":          rd["qa"] += 1
            else:                      rd["open"] += 1

        pd = platform_data[platform]
        pd["total"] += 1
        if itype == "bug": pd["bugs"] += 1
        if   bkt == "Done":        pd["done"] += 1
        elif bkt == "In Progress": pd["in_progress"] += 1
        elif bkt == "QA":          pd["qa"] += 1
        else:                      pd["open"] += 1

        if itype == "bug":
            bug_list.append(dict(
                key=key, summary=summary_, status=raw_status,
                priority=priority, assignee=assignee,
                release=", ".join(fix_vers) if fix_vers else "",
                created=created, updated=updated, description=desc,
            ))

    done_count  = sum(d["done"]        for d in type_data.values())
    in_prog     = sum(d["in_progress"] for d in type_data.values())
    qa_count    = sum(d["qa"]          for d in type_data.values())
    open_count  = sum(d["open"]        for d in type_data.values())
    completion  = round(done_count/total_issues*100) if total_issues else 0

    es_status_counts = defaultdict(int)
    for es in epic_story: es_status_counts[es["status"]] += 1

    return dict(
        total=total_issues,
        done=done_count, in_progress=in_prog, qa=qa_count, open=open_count,
        epics_stories=type_data["epic"]["total"]+type_data["story"]["total"],
        bugs_total=type_data["bug"]["total"],
        completion=completion,
        status_counts=dict(status_counts),
        type_data=dict(type_data),
        epic_story=epic_story,
        es_status_counts=dict(es_status_counts),
        assignee_data=dict(assignee_data),
        release_data=dict(release_data),
        platform_data=dict(platform_data),
        bug_list=bug_list,
        burndown_rows=burndown_rows,
    )


# ══════════════════════════════════════════════════════════════
#  CHARTS  (all return base64 PNG strings)
# ══════════════════════════════════════════════════════════════

C = dict(done="#4CAF50", in_progress="#2196F3", qa="#9C27B0", open="#F44336")
TYPE_COLORS = {"bug":"#F44336","task":"#4CAF50","story":"#2196F3",
               "epic":"#9C27B0","subtask":"#FF9800"}
PLAT_COLORS = {"Android":"#4CAF50","iOS":"#2196F3","Web":"#FF9800",
               "Backend":"#9C27B0","Admin Panel":"#009688","Unversioned":"#aaa"}

def _b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=130)
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode()
    plt.close(fig)
    return b64

def _pie(sizes, colors, labels, title):
    fig, ax = plt.subplots(figsize=(5,4))
    wedges,_,atexts = ax.pie(
        sizes, colors=colors,
        autopct=lambda p: str(int(round(p*sum(sizes)/100))) if p>1 else "",
        startangle=90, pctdistance=0.72,
    )
    for at in atexts: at.set(color="white",fontsize=11,fontweight="bold")
    ax.legend(wedges,[f"{l} ({s})" for l,s in zip(labels,sizes)],
              loc="upper center",bbox_to_anchor=(0.5,-0.04),ncol=2,fontsize=9)
    ax.set_title(title, fontweight="bold", pad=10)
    fig.tight_layout()
    return _b64(fig)

def chart_overall_pie(s):
    return _pie(
        [s["done"],s["in_progress"],s["qa"],s["open"]],
        [C["done"],C["in_progress"],C["qa"],C["open"]],
        ["Done/Closed","In Progress","In QA","Open/To Do"],
        "Overall Status",
    )

def chart_type_pie(td):
    keys   = [k for k in td if td[k]["total"]>0]
    return _pie(
        [td[k]["total"] for k in keys],
        [TYPE_COLORS.get(k,"#aaa") for k in keys],
        [k.capitalize() for k in keys],
        "Issue Type Breakdown",
    )

def chart_bug_pie(td):
    bd = td.get("bug",{})
    return _pie(
        [bd.get("done",0),bd.get("in_progress",0),bd.get("open",0)],
        [C["done"],C["in_progress"],C["open"]],
        ["Fixed","In Progress","Open"],
        "Bug Status",
    )

def chart_release_bar(rd):
    releases = sorted(rd)
    totals   = [rd[r]["total"] for r in releases]
    fig, ax  = plt.subplots(figsize=(7, max(3, len(releases)*0.6)))
    bars = ax.barh(releases, totals, color="#2196F3", height=0.5)
    ax.bar_label(bars, padding=3, fontsize=9)
    ax.set_xlabel("Issues")
    ax.set_title("Release-wise Issues", fontweight="bold")
    ax.spines[["top","right"]].set_visible(False)
    fig.tight_layout()
    return _b64(fig)

def chart_platform_pie(pd):
    keys = [k for k in pd if pd[k]["total"]>0]
    return _pie(
        [pd[k]["total"] for k in keys],
        [PLAT_COLORS.get(k,"#aaa") for k in keys],
        keys,
        "App Version Bifurcation",
    )

def chart_burndown(rows, sprint_name):
    all_dates = [r["date"] for r in rows]
    ideal     = [r["ideal"] for r in rows]
    past      = [(i,r["actual"]) for i,r in enumerate(rows) if r["actual"] is not None]
    fig, ax   = plt.subplots(figsize=(8,3.5))
    ax.plot(range(len(all_dates)), ideal, "--", color="#aaa", label="Ideal", lw=1.5)
    if past:
        ax.plot([p[0] for p in past],[p[1] for p in past],
                "-o", color=C["in_progress"], label="Actual", lw=2, markersize=4)
    ax.set_xticks(range(len(all_dates)))
    ax.set_xticklabels(all_dates, rotation=35, ha="right", fontsize=8)
    ax.set_ylabel("Issues Remaining")
    ax.set_title(f"Sprint Burndown — {sprint_name}", fontweight="bold")
    ax.legend(fontsize=9)
    ax.spines[["top","right"]].set_visible(False)
    fig.tight_layout()
    return _b64(fig)


# ══════════════════════════════════════════════════════════════
#  HTML BUILDER
# ══════════════════════════════════════════════════════════════

CSS = """
body{font-family:Arial,sans-serif;font-size:12px;color:#333;
     margin:0;padding:0;background:#f4f6f8}
.wrap{max-width:980px;margin:16px auto;background:#fff;border-radius:8px;
      box-shadow:0 2px 10px rgba(0,0,0,.13);overflow:hidden}
.hdr{background:#1a3c6e;color:#fff;padding:20px 28px}
.hdr h1{margin:0;font-size:21px}
.hdr p{margin:4px 0 0;font-size:12px;opacity:.85}
.sec{padding:18px 28px;border-bottom:1px solid #eee}
.sec h2{color:#1a3c6e;margin:0 0 14px;font-size:14px;
         border-left:4px solid #1a3c6e;padding-left:10px}
.meta{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.mi{background:#f0f4ff;border-radius:6px;padding:8px 16px;font-size:12px}
.mi strong{display:block;color:#1a3c6e;font-size:10px;
           text-transform:uppercase;margin-bottom:2px}
.kpis{display:flex;gap:10px;flex-wrap:wrap}
.kpi{background:#f8f9fa;border-radius:8px;padding:12px 16px;
     text-align:center;border-top:3px solid #ccc;flex:1;min-width:90px}
.kpi .v{font-size:26px;font-weight:bold}
.kpi .l{font-size:10px;color:#666;margin-top:3px}
.k-total{border-color:#1a3c6e}.k-total .v{color:#1a3c6e}
.k-es{border-color:#9C27B0}  .k-es .v{color:#9C27B0}
.k-bug{border-color:#F44336} .k-bug .v{color:#F44336}
.k-done{border-color:#4CAF50}.k-done .v{color:#4CAF50}
.k-prog{border-color:#2196F3}.k-prog .v{color:#2196F3}
.k-qa{border-color:#00BCD4}  .k-qa .v{color:#00BCD4}
.k-open{border-color:#FF5722}.k-open .v{color:#FF5722}
.k-pct{border-color:#FF9800} .k-pct .v{color:#FF9800}
.charts{display:flex;gap:14px;flex-wrap:wrap;
        justify-content:center;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:8px}
th{padding:7px 9px;text-align:left;font-size:11px}
td{padding:6px 9px;border-bottom:1px solid #f2f2f2}
tr:hover td{background:#f9f9ff}
.badge{display:inline-block;padding:2px 7px;border-radius:10px;
       font-size:10px;font-weight:bold;color:#fff;white-space:nowrap}
.bdone{background:#4CAF50}.bprog{background:#2196F3}
.bqa{background:#9C27B0}  .bopen{background:#F44336}.bother{background:#888}
.p5{color:#b71c1c;font-weight:bold}.p4{color:#e53935}
.p3{color:#fb8c00}.p2{color:#43a047}.p1{color:#888}
.on-track{color:#4CAF50;font-weight:bold}
.behind{color:#F44336;font-weight:bold}
.ftr{background:#f4f6f8;padding:10px 28px;
     font-size:10px;color:#999;text-align:center}
"""

def _badge(status):
    s = status.lower()
    if any(x in s for x in ("done","closed","resolved","approved","release","dev done")):
        cls="bdone"
    elif "progress" in s: cls="bprog"
    elif "qa" in s or "review" in s: cls="bqa"
    elif "open" in s or "to do" in s or "todo" in s: cls="bopen"
    else: cls="bother"
    return f'<span class="badge {cls}">{status}</span>'

def _pri(p):
    cls={"Highest":"p5","High":"p4","Medium":"p3","Low":"p2","Lowest":"p1"}.get(p,"p3")
    return f'<span class="{cls}">{p}</span>'

def _pct(done,total):
    return f"{round(done/total*100)}%" if total else "0%"

def _img(b64):
    return f'<img src="data:image/png;base64,{b64}" style="max-width:100%;border-radius:6px;">'

def _th(*cols, bg="#1a3c6e"):
    return (f'<tr style="background:{bg};color:white">'
            + "".join(f"<th>{c}</th>" for c in cols) + "</tr>")

def build_html(sprint, summary, charts):
    today     = datetime.now()
    start_raw = sprint.get("startDate","")[:10]
    end_raw   = sprint.get("endDate","")[:10]
    try:
        sd = datetime.strptime(start_raw,"%Y-%m-%d")
        ed = datetime.strptime(end_raw,  "%Y-%m-%d")
        duration  = (ed-sd).days
        remaining = max(0,(ed-today).days)
        start_lbl = sd.strftime("%-d %b %Y")
        end_lbl   = ed.strftime("%-d %b %Y")
    except Exception:
        duration=remaining="?"
        start_lbl,end_lbl=start_raw,end_raw
    report_date = today.strftime("%a %d %b %Y")

    # ─── Sprint meta + KPI banner ─────────────────────────────
    sec_meta = f"""
<div class="sec">
  <div class="meta">
    <div class="mi"><strong>Start Date</strong>{start_lbl}</div>
    <div class="mi"><strong>End Date</strong>{end_lbl}</div>
    <div class="mi"><strong>Duration</strong>{duration} days</div>
    <div class="mi"><strong>Remaining</strong>{remaining} days left</div>
    <div class="mi"><strong>State</strong>{sprint.get('state','')}</div>
  </div>
  <div class="kpis">
    <div class="kpi k-total"><div class="v">{summary['total']}</div><div class="l">Total Issues</div></div>
    <div class="kpi k-es">  <div class="v">{summary['epics_stories']}</div><div class="l">Epics/Stories</div></div>
    <div class="kpi k-bug"> <div class="v">{summary['bugs_total']}</div><div class="l">Bugs</div></div>
    <div class="kpi k-done"><div class="v">{summary['done']}</div><div class="l">Closed</div></div>
    <div class="kpi k-prog"><div class="v">{summary['in_progress']}</div><div class="l">In Progress</div></div>
    <div class="kpi k-qa">  <div class="v">{summary['qa']}</div><div class="l">In QA</div></div>
    <div class="kpi k-open"><div class="v">{summary['open']}</div><div class="l">Open</div></div>
    <div class="kpi k-pct"> <div class="v">{summary['completion']}%</div><div class="l">% Completion</div></div>
  </div>
</div>"""

    # ─── 1. Overall Status Summary ────────────────────────────
    total = summary["total"]
    rows = _th("Status","Count","%","Bar")
    for s,cnt in sorted(summary["status_counts"].items(),key=lambda x:-x[1]):
        p   = round(cnt/total*100) if total else 0
        bar = f'<div style="background:#2196F3;height:8px;border-radius:4px;width:{min(p,100)}%"></div>'
        rows += f'<tr><td>{_badge(s)}</td><td><strong>{cnt}</strong></td><td>{p}%</td><td>{bar}</td></tr>'
    rows += f'<tr style="font-weight:bold;background:#f0f4ff"><td>Total</td><td>{total}</td><td>100%</td><td></td></tr>'
    overall_chart_img = _img(charts['overall_pie'])
    sec_overall = f"""
<div class="sec">
  <h2>📊 Overall Status Summary</h2>
  <div class="charts">{overall_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 2. Issue Type Breakdown ──────────────────────────────
    rows = _th("Type","Total","Done","In Progress","QA","Open","Completion",bg="#2c3e50")
    for t,d in summary["type_data"].items():
        if d["total"]==0: continue
        rows += (f'<tr><td style="text-transform:capitalize;font-weight:bold">{t}</td>'
                 f'<td>{d["total"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td></tr>')
    type_chart_img = _img(charts['type_pie'])
    sec_types = f"""
<div class="sec">
  <h2>🔖 Issue Type Breakdown</h2>
  <div class="charts">{type_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 3. Epics & Stories Combined ─────────────────────────
    es_total = len(summary["epic_story"])
    rows = _th("Status","Count","%","Bar",bg="#7b3fa0")
    for s,cnt in sorted(summary["es_status_counts"].items(),key=lambda x:-x[1]):
        p   = round(cnt/es_total*100) if es_total else 0
        bar = f'<div style="background:#9C27B0;height:8px;border-radius:4px;width:{min(p,100)}%"></div>'
        rows += f'<tr><td>{_badge(s)}</td><td><strong>{cnt}</strong></td><td>{p}%</td><td>{bar}</td></tr>'
    rows += f'<tr style="font-weight:bold;background:#f5f0ff"><td>Total</td><td>{es_total}</td><td>100%</td><td></td></tr>'
    sec_es = f"""
<div class="sec">
  <h2>📌 Epics &amp; Stories — Combined ({es_total} issues)</h2>
  <table>{rows}</table>
</div>"""

    # ─── 4. Bug Status Breakdown ──────────────────────────────
    bug_total = summary["bugs_total"]
    bug_raw   = defaultdict(int)
    for b in summary["bug_list"]: bug_raw[b["status"]] += 1
    rows = _th("Status","Count","%","Bar",bg="#c0392b")
    for s,cnt in sorted(bug_raw.items(),key=lambda x:-x[1]):
        p   = round(cnt/bug_total*100) if bug_total else 0
        bar = f'<div style="background:#c0392b;height:8px;border-radius:4px;width:{min(p,100)}%"></div>'
        rows += f'<tr><td>{_badge(s)}</td><td><strong>{cnt}</strong></td><td>{p}%</td><td>{bar}</td></tr>'
    rows += f'<tr style="font-weight:bold;background:#fff5f5"><td>Total</td><td>{bug_total}</td><td>100%</td><td></td></tr>'
    bug_chart_img = _img(charts['bug_pie'])
    sec_bugs = f"""
<div class="sec">
  <h2>🐛 Bug Status Breakdown ({bug_total} bugs)</h2>
  <div class="charts">{bug_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 5. Release-wise Bifurcation ─────────────────────────
    rows = _th("Release / Fix Version","Total","Bugs","Done","In Progress","QA","Open","Completion",bg="#1565c0")
    for rv,d in sorted(summary["release_data"].items()):
        rows += (f'<tr><td><strong>{rv}</strong></td><td>{d["total"]}</td>'
                 f'<td style="color:#F44336">{d["bugs"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td></tr>')
    release_chart_img = _img(charts['release_bar'])
    sec_release = f"""
<div class="sec">
  <h2>🚀 Release-wise Bifurcation</h2>
  <div class="charts">{release_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 6. App Version Bifurcation ──────────────────────────
    rows = _th("Platform / Version","Total","Bugs","Done","In Progress","QA","Open","Completion",bg="#00695c")
    for pl,d in sorted(summary["platform_data"].items()):
        rows += (f'<tr><td><strong>{pl}</strong></td><td>{d["total"]}</td>'
                 f'<td style="color:#F44336">{d["bugs"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td></tr>')
    platform_chart_img = _img(charts['platform_pie'])
    sec_platform = f"""
<div class="sec">
  <h2>📱 App Version Bifurcation</h2>
  <div class="charts">{platform_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 7. Sprint Task Allocation – Per Person ───────────────
    rows = _th("Assignee","Total","Epic","Story","Task","Subtask","Bug",
               "Done","In Progress","QA","Open","Completion",bg="#37474f")
    for name,d in sorted(summary["assignee_data"].items(),key=lambda x:-x[1]["total"]):
        rows += (f'<tr><td><strong>{name}</strong></td><td>{d["total"]}</td>'
                 f'<td>{d["epic"]}</td><td>{d["story"]}</td>'
                 f'<td>{d["task"]}</td><td>{d["subtask"]}</td>'
                 f'<td style="color:#F44336">{d["bug"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td></tr>')
    td_ = summary["type_data"]
    rows += (f'<tr style="font-weight:bold;background:#eceff1">'
             f'<td>Total ({len(summary["assignee_data"])} members)</td>'
             f'<td>{summary["total"]}</td>'
             f'<td>{td_.get("epic",{}).get("total",0)}</td>'
             f'<td>{td_.get("story",{}).get("total",0)}</td>'
             f'<td>{td_.get("task",{}).get("total",0)}</td>'
             f'<td>{td_.get("subtask",{}).get("total",0)}</td>'
             f'<td>{summary["bugs_total"]}</td>'
             f'<td style="color:#4CAF50">{summary["done"]}</td>'
             f'<td style="color:#2196F3">{summary["in_progress"]}</td>'
             f'<td style="color:#9C27B0">{summary["qa"]}</td>'
             f'<td style="color:#F44336">{summary["open"]}</td>'
             f'<td>{summary["completion"]}%</td></tr>')
    sec_assign = f"""
<div class="sec">
  <h2>👤 Sprint Task Allocation — Per Person</h2>
  <div style="overflow-x:auto"><table>{rows}</table></div>
</div>"""

    # ─── 8. Sprint Burndown ───────────────────────────────────
    burn_rows_html = _th("Date","Ideal Remaining","Actual Remaining","Status",bg="#4a148c")
    for r in summary["burndown_rows"]:
        act   = str(r["actual"]) if r["actual"] is not None else "—"
        scls  = ("on-track" if r["status"]=="On Track"
                 else "behind" if r["status"]=="Behind" else "")
        burn_rows_html += (f'<tr><td>{r["date"]}</td><td>{r["ideal"]}</td>'
                           f'<td>{act}</td><td class="{scls}">{r["status"]}</td></tr>')
    burndown_chart_img = _img(charts['burndown'])
    sec_burndown = (
        '<div class="sec">'
        '<h2>📉 Sprint Burndown (Issues Remaining by Day)</h2>'
        f'<div class="charts">{burndown_chart_img}</div>'
        f'<table>{burn_rows_html}</table>'
        '</div>'
    )

    # ─── 9. Bug Sheet – Full Details ─────────────────────────
    rows = _th("Key","Summary","Status","Priority","Assignee",
               "Release","Created","Updated","Description",bg="#b71c1c")
    for b in summary["bug_list"]:
        url  = f'{config.JIRA_BASE_URL}/browse/{b["key"]}'
        desc = b["description"].replace('"',"'")
        rows += (f'<tr>'
                 f'<td><a href="{url}" style="color:#1a3c6e;font-weight:bold">{b["key"]}</a></td>'
                 f'<td title="{desc}">{b["summary"][:65]}{"…" if len(b["summary"])>65 else ""}</td>'
                 f'<td>{_badge(b["status"])}</td>'
                 f'<td>{_pri(b["priority"])}</td>'
                 f'<td>{b["assignee"]}</td>'
                 f'<td>{b["release"]}</td>'
                 f'<td>{b["created"]}</td>'
                 f'<td>{b["updated"]}</td>'
                 f'<td style="color:#555;max-width:200px">{b["description"][:130]}{"…" if len(b["description"])>130 else ""}</td>'
                 f'</tr>')
    sec_bugsheet = f"""
<div class="sec">
  <h2>📋 Bug Sheet — Full Details ({len(summary["bug_list"])} bugs)</h2>
  <div style="overflow-x:auto"><table>{rows}</table></div>
</div>"""

    # ─── Assemble full email ──────────────────────────────────
    # Use string concatenation so base64 content in sections never
    # conflicts with f-string brace parsing (root cause of missing burndown).
    parts = [
        '<!DOCTYPE html><html><head><meta charset="UTF-8">',
        '<style>', CSS, '</style></head><body><div class="wrap">',
        '<div class="hdr">',
        '<h1>&#128202; Daily Progress Report &#8212; ' + config.JIRA_PROJECT + '</h1>',
        '<p>Sprint: <strong>' + sprint.get("name","") + '</strong>',
        ' &nbsp;|&nbsp; Report Date: ' + report_date + '</p></div>',
        sec_meta, sec_overall, sec_types, sec_es, sec_bugs,
        sec_release, sec_platform, sec_assign, sec_burndown, sec_bugsheet,
        '<div class="ftr">Automated Daily Progress Report &#8212; ',
        config.JIRA_PROJECT + ' &#8212; ' + report_date,
        ' &nbsp;|&nbsp; Generated by jira_daily_report.py</div>',
        '</div></body></html>',
    ]
    return "".join(parts)


# ══════════════════════════════════════════════════════════════
#  PDF GENERATION  (using reportlab)
# ══════════════════════════════════════════════════════════════

def generate_pdf(sprint, summary, charts, pdf_path):
    """Generate a multi-page PDF report matching the HTML email sections."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image, PageBreak, HRFlowable)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import io as _io

    W, H   = A4
    margin = 15 * mm
    doc    = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin,  bottomMargin=margin,
    )

    styles = getSampleStyleSheet()
    NAVY   = colors.HexColor("#1a3c6e")
    GREEN  = colors.HexColor("#4CAF50")
    BLUE   = colors.HexColor("#2196F3")
    PURPLE = colors.HexColor("#9C27B0")
    RED    = colors.HexColor("#F44336")
    ORANGE = colors.HexColor("#FF9800")
    WHITE  = colors.white
    LGREY  = colors.HexColor("#f0f4ff")

    title_style = ParagraphStyle("rptTitle", parent=styles["Normal"],
        fontSize=16, textColor=WHITE, fontName="Helvetica-Bold", spaceAfter=2)
    sub_style   = ParagraphStyle("rptSub",   parent=styles["Normal"],
        fontSize=10, textColor=WHITE, fontName="Helvetica")
    h2_style    = ParagraphStyle("rptH2",    parent=styles["Normal"],
        fontSize=12, textColor=NAVY, fontName="Helvetica-Bold",
        spaceBefore=10, spaceAfter=6)
    body_style  = ParagraphStyle("rptBody",  parent=styles["Normal"],
        fontSize=8,  fontName="Helvetica", leading=11)
    cell_style  = ParagraphStyle("rptCell",  parent=styles["Normal"],
        fontSize=7.5, fontName="Helvetica", leading=10, wordWrap="LTR")

    today      = datetime.now()
    report_date = today.strftime("%a %d %b %Y")

    # Sprint dates
    try:
        sd = datetime.strptime(sprint["startDate"][:10], "%Y-%m-%d")
        ed = datetime.strptime(sprint["endDate"][:10],   "%Y-%m-%d")
        duration  = (ed - sd).days
        remaining = max(0, (ed - today).days)
        start_lbl = sd.strftime("%-d %b %Y")
        end_lbl   = ed.strftime("%-d %b %Y")
    except Exception:
        duration = remaining = "?"
        start_lbl = sprint.get("startDate","")[:10]
        end_lbl   = sprint.get("endDate","")[:10]

    story = []

    # ── Header banner ──────────────────────────────────────────
    header_data = [[
        Paragraph(f"Daily Progress Report — {config.JIRA_PROJECT}", title_style),
        Paragraph(f"Sprint: {sprint.get('name','')}  |  {report_date}", sub_style),
    ]]
    header_tbl = Table(header_data, colWidths=[W - 2*margin])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), NAVY),
        ("TOPPADDING",  (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0),(-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 6))

    # ── Sprint meta strip ──────────────────────────────────────
    meta_data = [[start_lbl, end_lbl, f"{duration} days", f"{remaining} days left",
                  sprint.get("state","")]]
    meta_hdrs = [["Start Date","End Date","Duration","Remaining","State"]]
    meta_tbl  = Table(meta_hdrs + meta_data,
                      colWidths=[(W-2*margin)/5]*5)
    meta_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0), LGREY),
        ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 8),
        ("TEXTCOLOR",    (0,0), (-1,0), NAVY),
        ("ALIGN",        (0,0), (-1,-1), "CENTER"),
        ("FONTNAME",     (0,1), (-1,1), "Helvetica-Bold"),
        ("FONTSIZE",     (0,1), (-1,1), 11),
        ("GRID",         (0,0), (-1,-1), 0.5, colors.HexColor("#dee2e6")),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 8))

    # ── KPI row ────────────────────────────────────────────────
    kpi_labels = ["Total Issues","Epics/Stories","Bugs","Closed",
                  "In Progress","In QA","Open","% Complete"]
    kpi_values = [summary["total"], summary["epics_stories"], summary["bugs_total"],
                  summary["done"], summary["in_progress"], summary["qa"],
                  summary["open"], f"{summary['completion']}%"]
    kpi_colors = [NAVY, PURPLE, RED, GREEN, BLUE,
                  colors.HexColor("#00BCD4"), colors.HexColor("#FF5722"), ORANGE]
    kpi_data   = [[Paragraph(f'<font color="white"><b>{v}</b></font>',
                              ParagraphStyle("kv", fontSize=14, alignment=TA_CENTER,
                                             fontName="Helvetica-Bold"))
                   for v in kpi_values],
                  [Paragraph(f'<font color="white">{l}</font>',
                              ParagraphStyle("kl", fontSize=7, alignment=TA_CENTER,
                                             fontName="Helvetica"))
                   for l in kpi_labels]]
    kpi_tbl = Table(kpi_data, colWidths=[(W-2*margin)/8]*8)
    kpi_style = [("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]
    for i, col in enumerate(kpi_colors):
        kpi_style.append(("BACKGROUND",(i,0),(i,-1), col))
    kpi_style.append(("ROUNDEDCORNERS",[4,4,4,4]))
    kpi_tbl.setStyle(TableStyle(kpi_style))
    story.append(kpi_tbl)
    story.append(Spacer(1, 10))

    # ── Helper: chart image from base64 ───────────────────────
    def b64_to_img(b64str, width_mm=75):
        raw = base64.b64decode(b64str)
        buf = _io.BytesIO(raw)
        return Image(buf, width=width_mm*mm, height=width_mm*mm*0.75)

    # ── Helper: section heading ────────────────────────────────
    def sec_head(txt):
        story.append(HRFlowable(width="100%", thickness=1, color=NAVY, spaceAfter=4))
        story.append(Paragraph(txt, h2_style))

    # ── Helper: standard data table ───────────────────────────
    def std_table(headers, rows_data, col_widths=None, hdr_bg=NAVY):
        hdr_row = [Paragraph(f'<font color="white"><b>{h}</b></font>',
                              ParagraphStyle("th", fontSize=7.5, fontName="Helvetica-Bold",
                                             alignment=TA_CENTER))
                   for h in headers]
        tbl_data = [hdr_row]
        for row in rows_data:
            tbl_data.append([
                Paragraph(str(c), cell_style) if not isinstance(c, Paragraph) else c
                for c in row
            ])
        cw = col_widths or [(W-2*margin)/len(headers)]*len(headers)
        tbl = Table(tbl_data, colWidths=cw, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (-1,0), hdr_bg),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [WHITE, colors.HexColor("#f8f9fa")]),
            ("GRID",          (0,0), (-1,-1), 0.4, colors.HexColor("#dee2e6")),
            ("FONTSIZE",      (0,0), (-1,-1), 7.5),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 5),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ]))
        return tbl

    # ── 1. Overall Status Summary ──────────────────────────────
    sec_head("Overall Status Summary")
    story.append(b64_to_img(charts["overall_pie"], 90))
    story.append(Spacer(1, 4))
    total = summary["total"]
    rows_data = []
    for s, cnt in sorted(summary["status_counts"].items(), key=lambda x: -x[1]):
        p = round(cnt/total*100) if total else 0
        rows_data.append([s, cnt, f"{p}%", f"{'█'*int(p/5)}"])
    rows_data.append(["Total", total, "100%", ""])
    story.append(std_table(["Status","Count","%","Bar"], rows_data,
                           col_widths=[80*mm, 30*mm, 25*mm, 45*mm]))
    story.append(Spacer(1, 8))

    # ── 2. Issue Type Breakdown ────────────────────────────────
    sec_head("Issue Type Breakdown")
    story.append(b64_to_img(charts["type_pie"], 90))
    story.append(Spacer(1, 4))
    rows_data = []
    for t, d in summary["type_data"].items():
        if d["total"] == 0: continue
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        rows_data.append([t.capitalize(), d["total"], d["done"],
                          d["in_progress"], d["qa"], d["open"], comp])
    story.append(std_table(
        ["Type","Total","Done","In Progress","QA","Open","Completion"], rows_data,
        col_widths=[40*mm,25*mm,25*mm,30*mm,25*mm,25*mm,30*mm],
        hdr_bg=colors.HexColor("#2c3e50")))
    story.append(Spacer(1, 8))

    # ── 3. Epics & Stories ─────────────────────────────────────
    sec_head(f"Epics & Stories — Combined ({len(summary['epic_story'])} issues)")
    es_total = len(summary["epic_story"])
    rows_data = []
    for s, cnt in sorted(summary["es_status_counts"].items(), key=lambda x: -x[1]):
        p = round(cnt/es_total*100) if es_total else 0
        rows_data.append([s, cnt, f"{p}%"])
    rows_data.append(["Total", es_total, "100%"])
    story.append(std_table(["Status","Count","%"], rows_data,
                           col_widths=[100*mm, 40*mm, 40*mm],
                           hdr_bg=colors.HexColor("#7b3fa0")))
    story.append(Spacer(1, 8))

    # ── 4. Bug Status Breakdown ────────────────────────────────
    sec_head(f"Bug Status Breakdown ({summary['bugs_total']} bugs)")
    story.append(b64_to_img(charts["bug_pie"], 90))
    story.append(Spacer(1, 4))
    bug_raw = defaultdict(int)
    for b in summary["bug_list"]: bug_raw[b["status"]] += 1
    rows_data = []
    for s, cnt in sorted(bug_raw.items(), key=lambda x: -x[1]):
        p = round(cnt/summary["bugs_total"]*100) if summary["bugs_total"] else 0
        rows_data.append([s, cnt, f"{p}%"])
    rows_data.append(["Total", summary["bugs_total"], "100%"])
    story.append(std_table(["Status","Count","%"], rows_data,
                           col_widths=[100*mm, 40*mm, 40*mm],
                           hdr_bg=colors.HexColor("#c0392b")))
    story.append(Spacer(1, 8))

    # ── 5. Release-wise Bifurcation ────────────────────────────
    sec_head("Release-wise Bifurcation")
    story.append(b64_to_img(charts["release_bar"], 130))
    story.append(Spacer(1, 4))
    rows_data = []
    for rv, d in sorted(summary["release_data"].items()):
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        rows_data.append([rv, d["total"], d["bugs"], d["done"],
                          d["in_progress"], d["qa"], d["open"], comp])
    story.append(std_table(
        ["Release","Total","Bugs","Done","In Progress","QA","Open","Completion"],
        rows_data,
        col_widths=[45*mm,20*mm,20*mm,20*mm,25*mm,20*mm,20*mm,25*mm],
        hdr_bg=colors.HexColor("#1565c0")))
    story.append(PageBreak())

    # ── 6. App Version Bifurcation ─────────────────────────────
    sec_head("App Version Bifurcation")
    story.append(b64_to_img(charts["platform_pie"], 90))
    story.append(Spacer(1, 4))
    rows_data = []
    for pl, d in sorted(summary["platform_data"].items()):
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        rows_data.append([pl, d["total"], d["bugs"], d["done"],
                          d["in_progress"], d["qa"], d["open"], comp])
    story.append(std_table(
        ["Platform","Total","Bugs","Done","In Progress","QA","Open","Completion"],
        rows_data,
        col_widths=[40*mm,20*mm,20*mm,20*mm,25*mm,20*mm,20*mm,25*mm],
        hdr_bg=colors.HexColor("#00695c")))
    story.append(Spacer(1, 8))

    # ── 7. Sprint Task Allocation ──────────────────────────────
    sec_head("Sprint Task Allocation — Per Person")
    rows_data = []
    for name, d in sorted(summary["assignee_data"].items(), key=lambda x: -x[1]["total"]):
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        rows_data.append([name, d["total"], d["epic"], d["story"], d["task"],
                          d["subtask"], d["bug"], d["done"], d["in_progress"],
                          d["qa"], d["open"], comp])
    td_ = summary["type_data"]
    rows_data.append([
        f"Total ({len(summary['assignee_data'])} members)",
        summary["total"],
        td_.get("epic",{}).get("total",0), td_.get("story",{}).get("total",0),
        td_.get("task",{}).get("total",0), td_.get("subtask",{}).get("total",0),
        summary["bugs_total"], summary["done"], summary["in_progress"],
        summary["qa"], summary["open"], f"{summary['completion']}%",
    ])
    story.append(std_table(
        ["Assignee","Total","Epic","Story","Task","Subtask","Bug",
         "Done","In Prog","QA","Open","Completion"],
        rows_data,
        col_widths=[38*mm,16*mm,14*mm,14*mm,14*mm,16*mm,14*mm,
                    14*mm,16*mm,14*mm,14*mm,16*mm],
        hdr_bg=colors.HexColor("#37474f")))
    story.append(Spacer(1, 8))

    # ── 8. Sprint Burndown ─────────────────────────────────────
    sec_head("Sprint Burndown (Issues Remaining by Day)")
    story.append(b64_to_img(charts["burndown"], 155))
    story.append(Spacer(1, 4))
    rows_data = []
    for r in summary["burndown_rows"]:
        act = str(r["actual"]) if r["actual"] is not None else "—"
        rows_data.append([r["date"], r["ideal"], act, r["status"]])
    story.append(std_table(
        ["Date","Ideal Remaining","Actual Remaining","Status"], rows_data,
        col_widths=[50*mm, 45*mm, 45*mm, 40*mm],
        hdr_bg=colors.HexColor("#4a148c")))
    story.append(PageBreak())

    # ── 9. Bug Sheet – Full Details ────────────────────────────
    sec_head(f"Bug Sheet — Full Details ({len(summary['bug_list'])} bugs)")
    rows_data = []
    for b in summary["bug_list"]:
        rows_data.append([
            b["key"],
            b["summary"][:55] + ("…" if len(b["summary"]) > 55 else ""),
            b["status"],
            b["priority"],
            b["assignee"],
            b["release"],
            b["updated"],
        ])
    story.append(std_table(
        ["Key","Summary","Status","Priority","Assignee","Release","Updated"],
        rows_data,
        col_widths=[20*mm, 58*mm, 28*mm, 20*mm, 32*mm, 22*mm, 20*mm],
        hdr_bg=colors.HexColor("#b71c1c")))

    doc.build(story)
    print(f"📄  PDF report saved: {pdf_path}")


# ══════════════════════════════════════════════════════════════
#  EMAIL SENDER  (HTML body + PDF attachment)
# ══════════════════════════════════════════════════════════════

def send_email(subject, html_body, pdf_path):
    from email.mime.base import MIMEBase
    from email import encoders

    msg            = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_FROM
    msg["To"]      = ", ".join(config.EMAIL_TO)

    # HTML body
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    # PDF attachment
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition",
                    f'attachment; filename="{os.path.basename(pdf_path)}"')
    msg.attach(part)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as srv:
        srv.ehlo(); srv.starttls()
        srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
        srv.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_string())
    print(f"✅  Email sent → {', '.join(config.EMAIL_TO)}")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

import os

def main():
    print("🔄  Connecting to Jira…")
    client = JiraClient()

    print("🔄  Fetching active sprint…")
    board_id, sprint = client.active_sprint()
    print(f"    Sprint: {sprint['name']}")

    print("🔄  Fetching sprint issues…")
    issues = client.sprint_issues(board_id, sprint["id"])
    print(f"    Found {len(issues)} issues")

    print("🔄  Processing data…")
    summary = process(issues, sprint)
    print(f"    {summary['total']} issues | {summary['bugs_total']} bugs | "
          f"{summary['completion']}% done")

    print("🔄  Generating charts…")
    charts = {
        "overall_pie":  chart_overall_pie(summary),
        "type_pie":     chart_type_pie(summary["type_data"]),
        "bug_pie":      chart_bug_pie(summary["type_data"]),
        "release_bar":  chart_release_bar(summary["release_data"]),
        "platform_pie": chart_platform_pie(summary["platform_data"]),
        "burndown":     chart_burndown(summary["burndown_rows"], sprint["name"]),
    }

    print("🔄  Building HTML email…")
    html = build_html(sprint, summary, charts)

    datestamp = datetime.now().strftime("%Y%m%d")
    html_file = f"report_{datestamp}.html"
    pdf_file  = f"report_{datestamp}.pdf"

    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"💾  HTML preview saved: {html_file}")

    print("🔄  Generating PDF…")
    generate_pdf(sprint, summary, charts, pdf_file)

    today_str = datetime.now().strftime("%d %b %Y")
    subject   = (f"Daily Sprint Report — {config.JIRA_PROJECT} | "
                 f"{sprint['name']} | {today_str}")
    print("📧  Sending email with PDF attachment…")
    send_email(subject, html, pdf_file)


if __name__ == "__main__":
    main()
