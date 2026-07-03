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

    def _search(self, jql, fields, max_results=100):
        """
        Search Jira issues with automatic pagination.
        Tries the new /search/jql endpoint (nextPageToken) first;
        falls back to legacy /search (startAt) if 404/410.
        """
        results = []
        url_new = f"{self.base}/rest/api/3/search/jql"
        url_old = f"{self.base}/rest/api/3/search"
        token   = None
        start   = 0
        use_new = True   # will flip to False on first 404/410

        while True:
            if use_new:
                params = {"jql": jql, "fields": fields, "maxResults": max_results}
                if token:
                    params["nextPageToken"] = token
                r = requests.get(url_new, auth=self.auth,
                                 headers=self.hdr, params=params)
                if r.status_code in (404, 410):
                    use_new = False          # Jira instance uses legacy API
                    continue
                r.raise_for_status()
                data  = r.json()
                chunk = data.get("issues", [])
                results.extend(chunk)
                token = data.get("nextPageToken")
                if not token or len(chunk) < max_results:
                    break
            else:
                params = {"jql": jql, "fields": fields,
                          "startAt": start, "maxResults": max_results}
                r = requests.get(url_old, auth=self.auth,
                                 headers=self.hdr, params=params)
                r.raise_for_status()
                data  = r.json()
                chunk = data.get("issues", [])
                results.extend(chunk)
                start += len(chunk)
                if start >= data.get("total", 0) or not chunk:
                    break
        return results

    def active_sprint(self):
        """Return (board_id, sprint_dict) for the first active sprint."""
        boards = self._get("board", {"projectKeyOrId": config.JIRA_PROJECT}, agile=True)
        for board in boards.get("values", []):
            # Skip boards not owned by this project
            location = board.get("location", {})
            if location.get("projectKey", "").upper() != config.JIRA_PROJECT.upper():
                continue
            sprints = self._get(f"board/{board['id']}/sprint",
                                {"state": "active"}, agile=True)
            active = sprints.get("values", [])
            if active:
                return board["id"], active[0]
        raise RuntimeError("No active sprint found for project: " + config.JIRA_PROJECT)

    def sprint_issues(self, board_id, sprint_id):
        """
        Fetch sprint issues via board endpoint (reliable), then enrich
        each issue's customfield_10014 (Epic Link) via a separate search,
        because the board endpoint silently strips that field.
        """
        # ── Step 1: board endpoint (stable, paginated with startAt) ──
        issues, start = [], 0
        fields = ("summary,status,issuetype,priority,assignee,"
                  "fixVersions,created,updated,description,parent")
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

        # ── Step 2: enrich with Epic Link via search ──────────────────
        # Fetch in batches of 50 keys to stay within JQL limits
        keys = [i["key"] for i in issues]
        epic_link_map = {}
        batch_size = 50
        for i in range(0, len(keys), batch_size):
            batch = keys[i : i + batch_size]
            jql   = f'key in ({",".join(batch)})'
            try:
                rows = self._search(jql, "customfield_10014,parent",
                                    max_results=batch_size)
                for row in rows:
                    f  = row["fields"]
                    ek = f.get("customfield_10014")
                    if not ek:
                        parent = f.get("parent") or {}
                        ptype  = ((parent.get("fields") or {})
                                  .get("issuetype", {}).get("name", ""))
                        if ptype.lower() == "epic":
                            ek = parent.get("key")
                    if ek:
                        epic_link_map[row["key"]] = ek
            except Exception as ex:
                print(f"    ⚠️  Epic link batch {i//batch_size+1} failed: {ex}")

        # Apply enrichment back to issues
        for issue in issues:
            ek = epic_link_map.get(issue["key"])
            if ek:
                issue["fields"]["customfield_10014"] = ek

        print(f"    Epic links resolved: {len(epic_link_map)}/{len(keys)} issues")
        return issues

    def fetch_sprint_epics(self, sprint_name):
        """
        Fetch all Epics belonging to the active sprint.
        The board endpoint NEVER returns Epics (Jira limitation),
        so we query them separately via JQL on sprint name.
        """
        fields = ("summary,status,assignee,priority,"
                  "fixVersions,labels,customfield_10014,parent")
        jql = (f'project = {config.JIRA_PROJECT} '
               f'AND sprint = "{sprint_name}" '
               f'AND issuetype = Epic '
               f'ORDER BY priority ASC')
        return self._search(jql, fields, max_results=100)


        """
        Grouping is now done inside build_epic_tracker() using the already-
        enriched sprint issues. This method is retained for API compatibility.
        """
        return {}


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


def build_epic_tracker(epics_in_sprint, epic_children_map, jira_base_url,
                       all_sprint_issues=None):
    """
    Build rich per-epic metrics for the CTO Epic Tracker section.
    epics_in_sprint   – list of epic issues from sprint_issues()
    epic_children_map – legacy param (now unused; kept for API compat)
    all_sprint_issues – full sprint issue list (used to build children map)
    """
    # Build children map from all sprint issues using customfield_10014
    epic_keys = {e["key"] for e in epics_in_sprint}
    children_map = {k: [] for k in epic_keys}
    if all_sprint_issues:
        for issue in all_sprint_issues:
            f  = issue["fields"]
            ek = f.get("customfield_10014")
            if not ek:
                parent = f.get("parent") or {}
                ptype  = ((parent.get("fields") or {})
                          .get("issuetype", {}).get("name", ""))
                if ptype.lower() == "epic":
                    ek = parent.get("key")
            if ek and ek in epic_keys:
                children_map[ek].append(issue)

    tracker = []
    for epic in epics_in_sprint:
        f       = epic["fields"]
        key     = epic["key"]
        name    = f.get("summary","")
        owner   = (f.get("assignee") or {}).get("displayName","Unassigned")
        status  = f["status"]["name"]
        priority = (f.get("priority") or {}).get("name","")
        releases = [v["name"] for v in (f.get("fixVersions") or [])]
        labels   = f.get("labels") or []
        url      = f"{jira_base_url}/browse/{key}"

        children = children_map.get(key, [])
        total_ch  = len(children)

        counts  = dict(done=0, qa=0, in_progress=0, open=0, other=0)
        assignees = set()
        platforms = set()
        blocked   = []  # items with no assignee or stuck Open too long

        for ch in children:
            cf       = ch["fields"]
            cs       = cf["status"]["name"]
            cb       = bucket(cs)
            cassign  = (cf.get("assignee") or {}).get("displayName","Unassigned")
            cvers    = [v["name"] for v in (cf.get("fixVersions") or [])]
            assignees.add(cassign)

            for v in cvers:
                vl = v.lower()
                if   "android" in vl: platforms.add("Android")
                elif "ios"     in vl: platforms.add("iOS")
                elif "web"     in vl: platforms.add("Web")
                elif "backend" in vl: platforms.add("Backend")
                elif "admin"   in vl: platforms.add("Admin")

            if   cb == "Done":        counts["done"]        += 1
            elif cb == "QA":          counts["qa"]          += 1
            elif cb == "In Progress": counts["in_progress"] += 1
            elif cb == "Open":        counts["open"]        += 1
            else:                     counts["other"]       += 1

            if cassign == "Unassigned":
                blocked.append(f'{ch["key"]}: {cf["summary"][:50]} (Unassigned)')

        remaining = total_ch - counts["done"]
        pct_done  = round(counts["done"] / total_ch * 100) if total_ch else 0
        pct_qa    = round((counts["done"] + counts["qa"]) / total_ch * 100) if total_ch else 0

        # Health signal: use child issue completion if available,
        # otherwise fall back to the epic's own Jira status
        status_lower = status.lower()
        if pct_done == 100:
            health = "✅ Complete"
            health_cls = "complete"
        elif pct_done >= 60:
            health = "🟢 On Track"
            health_cls = "on-track"
        elif pct_done >= 30 or counts["in_progress"] > 0:
            health = "🟡 In Progress"
            health_cls = "in-prog"
        elif total_ch == 0:
            # No children linked — use the epic's own status from Jira
            if any(s in status_lower for s in ("done", "complete", "closed", "resolved")):
                health = "✅ Complete"
                health_cls = "complete"
            elif any(s in status_lower for s in ("in progress", "in-progress", "inprogress", "active")):
                health = "🟡 In Progress"
                health_cls = "in-prog"
            elif any(s in status_lower for s in ("review", "qa", "testing")):
                health = "🟢 On Track"
                health_cls = "on-track"
            else:
                health = "🔴 At Risk"
                health_cls = "at-risk"
        else:
            health = "🔴 At Risk"
            health_cls = "at-risk"

        tracker.append(dict(
            key=key, name=name, owner=owner, status=status,
            priority=priority, releases=releases, labels=labels,
            url=url, total=total_ch, counts=counts,
            remaining=remaining, pct_done=pct_done, pct_qa=pct_qa,
            assignees=sorted(assignees), platforms=sorted(platforms),
            blocked=blocked, health=health, health_cls=health_cls,
        ))

    # Sort: complete last, then by pct_done desc
    tracker.sort(key=lambda x: (x["health_cls"]=="complete", -x["pct_done"]))
    return tracker


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
.epic-card{background:#fff;border:1px solid #e0e0e0;border-radius:10px;
           margin-bottom:14px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.epic-card-hdr{padding:10px 16px;display:flex;align-items:center;
               justify-content:space-between;gap:10px;flex-wrap:wrap}
.epic-title{font-weight:bold;font-size:13px;color:#1a3c6e}
.epic-key{font-size:10px;color:#666;margin-left:6px}
.epic-meta{display:flex;gap:8px;flex-wrap:wrap;font-size:10.5px;color:#555;
           padding:0 16px 8px;align-items:center}
.epic-meta span{background:#f0f4ff;border-radius:4px;padding:2px 7px}
.prog-bar-wrap{padding:0 16px 4px}
.prog-bar-bg{background:#e0e0e0;border-radius:6px;height:10px;overflow:hidden;position:relative}
.prog-bar-done{background:#4CAF50;height:100%;border-radius:6px}
.prog-bar-qa{background:#9C27B0;height:100%;position:absolute;top:0;border-radius:0 6px 6px 0}
.prog-label{font-size:10px;color:#555;margin-top:3px}
.epic-tbl{width:100%;font-size:11px;margin:0}
.epic-tbl th{padding:5px 10px;font-size:10px}
.epic-tbl td{padding:5px 10px}
.health-complete{color:#4CAF50;font-weight:bold}
.health-on-track{color:#2196F3;font-weight:bold}
.health-in-prog{color:#FF9800;font-weight:bold}
.health-at-risk{color:#F44336;font-weight:bold}
.ftr{background:#f4f6f8;padding:10px 28px;
     font-size:10px;color:#999;text-align:center}
"""

def chart_epic_progress(tracker):
    """Horizontal stacked bar — one bar per epic showing done/qa/inprog/open."""
    if not tracker:
        return None
    names  = [f'{e["key"]}: {e["name"][:30]}{"…" if len(e["name"])>30 else ""}' for e in tracker]
    done   = [e["counts"]["done"]        for e in tracker]
    qa     = [e["counts"]["qa"]          for e in tracker]
    inprog = [e["counts"]["in_progress"] for e in tracker]
    open_  = [e["counts"]["open"]        for e in tracker]
    totals = [e["total"] or 1            for e in tracker]

    fig, ax = plt.subplots(figsize=(9, max(3, len(tracker)*0.55)))
    y = range(len(tracker))
    ax.barh(list(y), [d/t*100 for d,t in zip(done,totals)],   color="#4CAF50", label="Done",        height=0.55)
    ax.barh(list(y), [q/t*100 for q,t in zip(qa,totals)],     color="#9C27B0", label="QA",          height=0.55,
            left=[d/t*100 for d,t in zip(done,totals)])
    left2 = [(d+q)/t*100 for d,q,t in zip(done,qa,totals)]
    ax.barh(list(y), [i/t*100 for i,t in zip(inprog,totals)], color="#2196F3", label="In Progress", height=0.55, left=left2)
    left3 = [(d+q+i)/t*100 for d,q,i,t in zip(done,qa,inprog,totals)]
    ax.barh(list(y), [o/t*100 for o,t in zip(open_,totals)],  color="#F44336", label="Open",        height=0.55, left=left3)

    ax.set_yticks(list(y)); ax.set_yticklabels(names, fontsize=7.5)
    ax.set_xlabel("% Completion", fontsize=8)
    ax.set_xlim(0,100)
    ax.legend(loc="lower right", fontsize=7)
    ax.set_title("Epic Progress Overview", fontsize=10, fontweight="bold", pad=8)
    plt.tight_layout()
    return _b64(fig)

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

def build_sec_epic_tracker(tracker, epic_chart_b64):
    """Build the full CTO Epic Tracker HTML section with live Jira links."""
    import urllib.parse

    def _epic_jira_link(url, text, style=""):
        return f'<a href="{url}" target="_blank" style="text-decoration:none;{style}">{text}</a>'

    def _epic_filter_link(base_url, jql, label, color="#1565c0"):
        encoded = urllib.parse.quote(jql)
        return (f'<a href="{base_url}/issues/?jql={encoded}" target="_blank" '
                f'style="color:{color};font-size:9.5px;text-decoration:none;'
                f'background:#e8f0fe;border-radius:3px;padding:1px 6px">'
                f'🔗 {label}</a>')

    # Summary KPI strip
    total_epics = len(tracker)
    complete    = sum(1 for e in tracker if e["health_cls"]=="complete")
    on_track    = sum(1 for e in tracker if e["health_cls"]=="on-track")
    in_prog     = sum(1 for e in tracker if e["health_cls"]=="in-prog")
    at_risk     = sum(1 for e in tracker if e["health_cls"]=="at-risk")

    summary_bar = (
        f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">'
        f'<div class="kpi k-done"  style="flex:1;min-width:80px"><div class="v">{complete}</div><div class="l">Complete</div></div>'
        f'<div class="kpi k-prog"  style="flex:1;min-width:80px"><div class="v">{on_track}</div><div class="l">On Track</div></div>'
        f'<div class="kpi k-qa"    style="flex:1;min-width:80px"><div class="v">{in_prog}</div><div class="l">In Progress</div></div>'
        f'<div class="kpi k-bug"   style="flex:1;min-width:80px"><div class="v">{at_risk}</div><div class="l">At Risk</div></div>'
        f'<div class="kpi k-total" style="flex:1;min-width:80px"><div class="v">{total_epics}</div><div class="l">Total Epics</div></div>'
        f'</div>'
    )

    chart_html = ""
    if epic_chart_b64:
        chart_html = f'<div class="charts">{_img(epic_chart_b64)}</div>'

    # Per-epic cards
    cards = []
    for e in tracker:
        base_url = e["url"].split("/browse/")[0]   # e.g. https://hike-platform.atlassian.net
        hcls_map = {"complete":"health-complete","on-track":"health-on-track",
                    "in-prog":"health-in-prog","at-risk":"health-at-risk"}
        hcls = hcls_map.get(e["health_cls"],"")

        # Jira filter links for each status bucket under this epic
        jql_base = f'"Epic Link" = {e["key"]}'
        lnk_done  = _epic_filter_link(base_url, jql_base + ' AND statusCategory = Done',        f'{e["counts"]["done"]} done',        "#4CAF50")
        lnk_qa    = _epic_filter_link(base_url, jql_base + ' AND status in ("Ready For QA","QA In Progress","In QA")', f'{e["counts"]["qa"]} QA', "#9C27B0")
        lnk_prog  = _epic_filter_link(base_url, jql_base + ' AND statusCategory = "In Progress"', f'{e["counts"]["in_progress"]} in progress', "#2196F3")
        lnk_open  = _epic_filter_link(base_url, jql_base + ' AND statusCategory = "To Do"',    f'{e["counts"]["open"]} open',        "#F44336")
        lnk_all   = _epic_filter_link(base_url, jql_base,                                       "all tasks →",                        "#555")

        # Progress bar
        done_pct = e["pct_done"]
        prog_bar = (
            f'<div class="prog-bar-wrap">'
            f'<div class="prog-bar-bg">'
            f'<div class="prog-bar-done" style="width:{done_pct}%"></div>'
            f'</div>'
            f'<div class="prog-label" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px">'
            f'{lnk_done} {lnk_qa} {lnk_prog} {lnk_open}'
            f'&nbsp;·&nbsp; <strong>{e["remaining"]} remaining</strong>'
            f'&nbsp;·&nbsp; {done_pct}% complete'
            f'&nbsp;·&nbsp; {lnk_all}'
            f'</div></div>'
        )

        # Release pills (each clickable → Jira filter for that version)
        rel_pills = " ".join(
            _epic_filter_link(base_url,
                f'"Epic Link" = {e["key"]} AND fixVersion = "{r}"', r, "#1565c0")
            for r in e["releases"]
        ) or '<span style="color:#aaa">None</span>'

        platforms_str = ", ".join(e["platforms"]) or "—"

        # Assignees — each clickable
        assignee_links = ", ".join(
            _epic_filter_link(base_url,
                f'"Epic Link" = {e["key"]} AND assignee = "{a}"', a, "#333")
            for a in e["assignees"][:5]
        ) + ("…" if len(e["assignees"]) > 5 else "")

        # Blocked / unassigned items
        blocked_html = ""
        if e["blocked"]:
            items = "".join(
                f'<li style="color:#b71c1c;font-size:10px">'
                f'<a href="{base_url}/browse/{b.split(":")[0].strip()}" target="_blank" '
                f'style="color:#b71c1c;font-weight:bold">{b.split(":")[0].strip()}</a>'
                f':{":".join(b.split(":")[1:])}</li>'
                for b in e["blocked"][:3]
            )
            blocked_html = (
                f'<div style="padding:4px 16px 8px">'
                f'<strong style="font-size:10px;color:#b71c1c">⚠️ Needs Owner:</strong>'
                f'<ul style="margin:2px 0 0 16px">{items}</ul></div>'
            )

        e_name = e["name"]
        e_key  = e["key"]
        title_html = f'<span class="epic-title">{e_name}</span><span class="epic-key">&nbsp;{e_key}</span>'
        card = (
            f'<div class="epic-card">'
            # Header: epic title links to epic in Jira
            f'<div class="epic-card-hdr" style="background:#f5f7ff">'
            f'  <div>'
            f'    {_epic_jira_link(e["url"], title_html)}'
            f'  </div>'
            f'  <div style="display:flex;gap:8px;align-items:center">'
            f'    {_badge(e["status"])}'
            f'    <span class="{hcls}">{e["health"]}</span>'
            f'    {_epic_filter_link(base_url, jql_base, "Open in Jira", "#1a3c6e")}'
            f'  </div>'
            f'</div>'
            # Meta row
            f'<div class="epic-meta">'
            f'  <span>👤 Owner: <strong>{e["owner"]}</strong></span>'
            f'  <span>🏷 Priority: {_pri(e["priority"])}</span>'
            f'  <span>📱 Platforms: {platforms_str}</span>'
            f'  <span>🚀 Releases: {rel_pills}</span>'
            f'  <span>👥 Contributors: {assignee_links or "—"}</span>'
            f'</div>'
            + prog_bar
            + blocked_html
            + '</div>'
        )
        cards.append(card)

    return (
        '<div class="sec">'
        '<h2>🗺️ Epic Tracker — CTO View</h2>'
        + summary_bar
        + chart_html
        + "".join(cards)
        + '</div>'
    )



def build_html(sprint, summary, charts, tracker=None):
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

    jira_url = config.JIRA_BASE_URL.rstrip("/")

    def _jira_link(key, text=None, color="#1a3c6e"):
        """Clickable Jira issue link."""
        label = text or key
        return f'<a href="{jira_url}/browse/{key}" target="_blank" style="color:{color};font-weight:bold;text-decoration:none">{label}</a>'

    def _jira_filter(jql, text, color="#1a3c6e"):
        """Clickable Jira filter link (opens issue search)."""
        import urllib.parse
        encoded = urllib.parse.quote(jql)
        return f'<a href="{jira_url}/issues/?jql={encoded}" target="_blank" style="color:{color};text-decoration:none;font-size:10px">🔗 {text}</a>'

    sprint_name = sprint.get("name", "")

    # ─── 1. Overall Status Summary ────────────────────────────
    total = summary["total"]
    rows = _th("Status","Count","%","Bar","")
    for s,cnt in sorted(summary["status_counts"].items(),key=lambda x:-x[1]):
        p   = round(cnt/total*100) if total else 0
        bar = f'<div style="background:#2196F3;height:8px;border-radius:4px;width:{min(p,100)}%"></div>'
        jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND status = "{s}"'
        rows += (f'<tr><td>{_badge(s)}</td><td><strong>{cnt}</strong></td>'
                 f'<td>{p}%</td><td>{bar}</td>'
                 f'<td>{_jira_filter(jql, "View in Jira")}</td></tr>')
    rows += f'<tr style="font-weight:bold;background:#f0f4ff"><td>Total</td><td>{total}</td><td>100%</td><td></td><td></td></tr>'
    overall_chart_img = _img(charts['overall_pie'])
    jql_all = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}"'
    sec_overall = (
        '<div class="sec">'
        '<h2>📊 Overall Status Summary &nbsp;' + _jira_filter(jql_all, "Open full sprint in Jira", "#555") + '</h2>'
        '<div class="charts">' + overall_chart_img + '</div>'
        '<table>' + rows + '</table>'
        '</div>'
    )

    # ─── 4. Bug Status Breakdown ──────────────────────────────
    bug_total = summary["bugs_total"]
    bug_raw   = defaultdict(int)
    for b in summary["bug_list"]: bug_raw[b["status"]] += 1
    rows = _th("Status","Count","%","Bar","",bg="#c0392b")
    for s,cnt in sorted(bug_raw.items(),key=lambda x:-x[1]):
        p   = round(cnt/bug_total*100) if bug_total else 0
        bar = f'<div style="background:#c0392b;height:8px;border-radius:4px;width:{min(p,100)}%"></div>'
        jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND issuetype = Bug AND status = "{s}"'
        rows += (f'<tr><td>{_badge(s)}</td><td><strong>{cnt}</strong></td>'
                 f'<td>{p}%</td><td>{bar}</td>'
                 f'<td>{_jira_filter(jql, "View bugs")}</td></tr>')
    rows += f'<tr style="font-weight:bold;background:#fff5f5"><td>Total</td><td>{bug_total}</td><td>100%</td><td></td><td></td></tr>'
    bug_chart_img = _img(charts['bug_pie'])
    jql_bugs = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND issuetype = Bug'
    sec_bugs = (
        '<div class="sec">'
        '<h2>🐛 Bug Status Breakdown (' + str(bug_total) + ' bugs) &nbsp;'
        + _jira_filter(jql_bugs, "All bugs in Jira", "#555") + '</h2>'
        '<div class="charts">' + bug_chart_img + '</div>'
        '<table>' + rows + '</table>'
        '</div>'
    )

    # ─── 5. Release-wise Bifurcation ─────────────────────────
    rows = _th("Release / Fix Version","Total","Bugs","Done","In Progress","QA","Open","Completion","",bg="#1565c0")
    for rv,d in sorted(summary["release_data"].items()):
        if rv == "Unversioned":
            jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND fixVersion is EMPTY'
        else:
            jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND fixVersion = "{rv}"'
        rows += (f'<tr><td><strong>{rv}</strong></td><td>{d["total"]}</td>'
                 f'<td style="color:#F44336">{d["bugs"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td>'
                 f'<td>{_jira_filter(jql, "View")}</td></tr>')
    release_chart_img = _img(charts['release_bar'])
    sec_release = f"""
<div class="sec">
  <h2>🚀 Release-wise Bifurcation</h2>
  <div class="charts">{release_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 6. App Version Bifurcation ──────────────────────────
    rows = _th("Platform / Version","Total","Bugs","Done","In Progress","QA","Open","Completion","",bg="#00695c")
    PLATFORM_VERSION_MAP = {
        "Android": "Android", "iOS": "iOS", "Web": "Web",
        "Backend": "Backend", "Admin Panel": "Admin",
    }
    for pl,d in sorted(summary["platform_data"].items()):
        if pl == "Unversioned":
            jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND fixVersion is EMPTY'
        else:
            jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND fixVersion ~ "{PLATFORM_VERSION_MAP.get(pl, pl)}"'
        rows += (f'<tr><td><strong>{pl}</strong></td><td>{d["total"]}</td>'
                 f'<td style="color:#F44336">{d["bugs"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td>'
                 f'<td>{_jira_filter(jql, "View")}</td></tr>')
    platform_chart_img = _img(charts['platform_pie'])
    sec_platform = f"""
<div class="sec">
  <h2>📱 App Version Bifurcation</h2>
  <div class="charts">{platform_chart_img}</div>
  <table>{rows}</table>
</div>"""

    # ─── 7. Sprint Task Allocation – Per Person ───────────────
    rows = _th("Assignee","Total","Epic","Story","Task","Subtask","Bug",
               "Done","In Progress","QA","Open","Completion","",bg="#37474f")
    for name,d in sorted(summary["assignee_data"].items(),key=lambda x:-x[1]["total"]):
        jql = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND assignee = "{name}"'
        rows += (f'<tr><td><strong>{name}</strong></td><td>{d["total"]}</td>'
                 f'<td>{d["epic"]}</td><td>{d["story"]}</td>'
                 f'<td>{d["task"]}</td><td>{d["subtask"]}</td>'
                 f'<td style="color:#F44336">{d["bug"]}</td>'
                 f'<td style="color:#4CAF50">{d["done"]}</td>'
                 f'<td style="color:#2196F3">{d["in_progress"]}</td>'
                 f'<td style="color:#9C27B0">{d["qa"]}</td>'
                 f'<td style="color:#F44336">{d["open"]}</td>'
                 f'<td>{_pct(d["done"],d["total"])}</td>'
                 f'<td>{_jira_filter(jql, "View")}</td></tr>')
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
             f'<td>{summary["completion"]}%</td><td></td></tr>')
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
        url  = f'{jira_url}/browse/{b["key"]}'
        desc = b["description"].replace('"',"'")
        rows += (f'<tr>'
                 f'<td><a href="{url}" target="_blank" style="color:#1a3c6e;font-weight:bold;text-decoration:none">{b["key"]}</a></td>'
                 f'<td><a href="{url}" target="_blank" style="color:#333;text-decoration:none" title="{desc}">{b["summary"][:65]}{"…" if len(b["summary"])>65 else ""}</a></td>'
                 f'<td>{_badge(b["status"])}</td>'
                 f'<td>{_pri(b["priority"])}</td>'
                 f'<td>{b["assignee"]}</td>'
                 f'<td>{b["release"]}</td>'
                 f'<td>{b["created"]}</td>'
                 f'<td>{b["updated"]}</td>'
                 f'<td style="color:#555;max-width:200px">{b["description"][:130]}{"…" if len(b["description"])>130 else ""}</td>'
                 f'</tr>')
    jql_bugs_all = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}" AND issuetype = Bug ORDER BY priority DESC'
    sec_bugsheet = (
        '<div class="sec">'
        '<h2>📋 Bug Sheet — Full Details (' + str(len(summary["bug_list"])) + ' bugs) &nbsp;'
        + _jira_filter(jql_bugs_all, "Open all bugs in Jira", "#555") + '</h2>'
        '<div style="overflow-x:auto"><table>' + rows + '</table></div>'
        '</div>'
    )

    # ─── Epic Tracker (CTO View) — built, shown after Overall ─
    sec_epic = ""
    if tracker:
        sec_epic = build_sec_epic_tracker(tracker, charts.get("epic_progress"))

    # ─── Assemble full email ──────────────────────────────────
    # Epic tracker is position 2 — right after Overall Status Summary.
    # Use string concatenation (never f-strings) so base64 in chart imgs
    # never conflicts with brace parsing.
    parts = [
        '<!DOCTYPE html><html><head><meta charset="UTF-8">',
        '<style>', CSS, '</style></head><body><div class="wrap">',
        '<div class="hdr">',
        '<h1>&#128202; Daily Progress Report &#8212; ' + config.JIRA_PROJECT + '</h1>',
        '<p>Sprint: <strong>' + sprint.get("name","") + '</strong>',
        ' &nbsp;|&nbsp; Report Date: ' + report_date + '</p></div>',
        sec_meta, sec_overall,
        sec_release,       # ← Release-wise Bifurcation before Epic Tracker
        sec_epic,          # ← Epic Tracker — CTO View
        sec_bugs,
        sec_platform, sec_assign, sec_burndown, sec_bugsheet,
        '<div style="margin:32px 0 16px;padding:20px 24px;background:#f0f4ff;border-left:4px solid #1a3c6e;border-radius:6px;text-align:center;">'
        '<span style="font-size:15px;color:#1a3c6e;">&#128206; <strong>For detailed metrics, charts, and full issue breakdown</strong> &mdash; '
        'open the <strong>HTML or PDF attachment</strong> in this email.</span>'
        '</div>',
        '<div class="ftr">Automated Daily Progress Report &#8212; ',
        config.JIRA_PROJECT + ' &#8212; ' + report_date,
        ' &nbsp;|&nbsp; Generated by jira_daily_report.py</div>',
        '</div></body></html>',
    ]
    return "".join(parts)


# ══════════════════════════════════════════════════════════════
#  PDF GENERATION  (using reportlab)
# ══════════════════════════════════════════════════════════════

def generate_pdf(sprint, summary, charts, pdf_path, tracker=None):
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

    import urllib.parse as _urlparse

    jira_url    = config.JIRA_BASE_URL.rstrip("/")
    sprint_name = sprint.get("name", "")
    jql_sprint  = f'project = {config.JIRA_PROJECT} AND sprint = "{sprint_name}"'

    # ── Helpers ────────────────────────────────────────────────
    def _jql_url(jql):
        return f"{jira_url}/issues/?jql={_urlparse.quote(jql)}"

    def _lnk(text, url, color="#1a3c6e", trunc=None, bold=False):
        """Clickable paragraph."""
        label = (str(text)[:trunc] + "…") if trunc and len(str(text)) > trunc else str(text)
        label = label.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        w = "bold" if bold else "normal"
        return Paragraph(
            f'<link href="{url}" color="{color}"><b>{label}</b></link>' if bold else
            f'<link href="{url}" color="{color}"><u>{label}</u></link>',
            ParagraphStyle("lnk", parent=cell_style, textColor=colors.HexColor(color)))

    def _jlnk(text, jql, color="#1a3c6e", trunc=None, bold=False):
        return _lnk(text, _jql_url(jql), color=color, trunc=trunc, bold=bold)

    def _p(text, trunc=None, color=None):
        label = (str(text)[:trunc] + "…") if trunc and len(str(text)) > trunc else str(text)
        label = label.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        style = cell_style if not color else ParagraphStyle(
            "cp", parent=cell_style, textColor=colors.HexColor(color))
        return Paragraph(label, style)

    def _badge_p(status):
        STATUS_COLORS = {
            "Done":"#4CAF50","Closed":"#4CAF50","Resolved":"#4CAF50","Dev Done":"#4CAF50",
            "QA Approved":"#4CAF50","Ready For Release":"#4CAF50",
            "In Progress":"#2196F3","In Development":"#2196F3",
            "Ready For QA":"#9C27B0","In QA":"#9C27B0","QA In Progress":"#9C27B0",
            "Open":"#F44336","To Do":"#F44336","Reopened":"#F44336","Backlog":"#aaa",
        }
        col = STATUS_COLORS.get(status, "#607d8b")
        s = status.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        return Paragraph(
            f'<font color="white"><b> {s} </b></font>',
            ParagraphStyle("bdg", parent=cell_style,
                           backColor=colors.HexColor(col),
                           borderPadding=2, textColor=WHITE))

    def _pri_p(pri):
        PRI_COLORS = {"Highest":"#d32f2f","High":"#f57c00","Medium":"#fbc02d",
                      "Low":"#388e3c","Lowest":"#0288d1"}
        col = PRI_COLORS.get(pri, "#607d8b")
        p = pri.replace("&","&amp;")
        return Paragraph(f'<font color="{col}"><b>{p}</b></font>', cell_style)

    # ═══════════════════════════════════════════════════════════
    # ── 1. Overall Status Summary  (matches HTML sec_overall) ──
    # ═══════════════════════════════════════════════════════════
    jql_all = jql_sprint
    sec_head(f'📊 Overall Status Summary  '
             f'<link href="{_jql_url(jql_all)}" color="#555"><u>🔗 Open full sprint in Jira</u></link>')
    story.append(b64_to_img(charts["overall_pie"], 90))
    story.append(Spacer(1, 4))
    total = summary["total"]
    rows_data = []
    for s, cnt in sorted(summary["status_counts"].items(), key=lambda x: -x[1]):
        p   = round(cnt/total*100) if total else 0
        jql = f'{jql_sprint} AND status = "{s}"'
        bar_pct = min(p, 100)
        rows_data.append([
            _badge_p(s),
            _jlnk(str(cnt), jql, color="#1a3c6e", bold=True),
            _p(f"{p}%"),
            _p(f"{'█'*int(p/5)}"),
            _jlnk("🔗 View in Jira", jql, color="#555"),
        ])
    rows_data.append([_p("Total"), _p(total, color="#1a3c6e"), _p("100%"), _p(""), _p("")])
    story.append(std_table(["Status","Count","%","Bar",""],
                           rows_data, col_widths=[55*mm, 22*mm, 18*mm, 38*mm, 27*mm]))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 2. Epic Tracker — CTO View  (matches HTML sec_epic) ────
    # ═══════════════════════════════════════════════════════════
    if tracker:
        # KPI strip
        complete  = sum(1 for e in tracker if e["health_cls"]=="complete")
        on_track  = sum(1 for e in tracker if e["health_cls"]=="on-track")
        in_prog_e = sum(1 for e in tracker if e["health_cls"]=="in-prog")
        at_risk   = sum(1 for e in tracker if e["health_cls"]=="at-risk")
        kpi_data  = [[
            Paragraph(f'<font color="white"><b>{complete}</b></font>',
                      ParagraphStyle("kv", fontSize=14, alignment=1, fontName="Helvetica-Bold")),
            Paragraph(f'<font color="white"><b>{on_track}</b></font>',
                      ParagraphStyle("kv", fontSize=14, alignment=1, fontName="Helvetica-Bold")),
            Paragraph(f'<font color="white"><b>{in_prog_e}</b></font>',
                      ParagraphStyle("kv", fontSize=14, alignment=1, fontName="Helvetica-Bold")),
            Paragraph(f'<font color="white"><b>{at_risk}</b></font>',
                      ParagraphStyle("kv", fontSize=14, alignment=1, fontName="Helvetica-Bold")),
            Paragraph(f'<font color="white"><b>{len(tracker)}</b></font>',
                      ParagraphStyle("kv", fontSize=14, alignment=1, fontName="Helvetica-Bold")),
        ],[
            Paragraph('<font color="white">Complete</font>',
                      ParagraphStyle("kl", fontSize=7, alignment=1, fontName="Helvetica")),
            Paragraph('<font color="white">On Track</font>',
                      ParagraphStyle("kl", fontSize=7, alignment=1, fontName="Helvetica")),
            Paragraph('<font color="white">In Progress</font>',
                      ParagraphStyle("kl", fontSize=7, alignment=1, fontName="Helvetica")),
            Paragraph('<font color="white">At Risk</font>',
                      ParagraphStyle("kl", fontSize=7, alignment=1, fontName="Helvetica")),
            Paragraph('<font color="white">Total Epics</font>',
                      ParagraphStyle("kl", fontSize=7, alignment=1, fontName="Helvetica")),
        ]]
        kpi_tbl = Table(kpi_data, colWidths=[(W-2*margin)/5]*5)
        kpi_tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(0,-1), GREEN),
            ("BACKGROUND",(1,0),(1,-1), BLUE),
            ("BACKGROUND",(2,0),(2,-1), colors.HexColor("#00BCD4")),
            ("BACKGROUND",(3,0),(3,-1), RED),
            ("BACKGROUND",(4,0),(4,-1), NAVY),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ]))
        sec_head("🗺️ Epic Tracker — CTO View")
        story.append(kpi_tbl)
        story.append(Spacer(1, 4))

        # Progress chart
        epic_chart_b64 = charts.get("epic_progress")
        if epic_chart_b64:
            import io as _io2
            img_data = base64.b64decode(epic_chart_b64)
            img_buf  = _io2.BytesIO(img_data)
            from reportlab.platypus import Image as RLImage
            rl_img = RLImage(img_buf, width=170*mm, height=max(40*mm, len(tracker)*8*mm))
            story.append(rl_img)
            story.append(Spacer(1, 4))

        # Per-epic table — matches HTML epic cards
        HCLS_COLORS = {"complete":"#4CAF50","on-track":"#2196F3",
                       "in-prog":"#FF9800","at-risk":"#F44336"}
        epic_rows = []
        for e in tracker:
            counts  = e["counts"]
            jql_e   = f'"Epic Link" = {e["key"]}'
            hcol    = HCLS_COLORS.get(e["health_cls"],"#607d8b")
            rel_str = ", ".join(e["releases"][:3]) or "—"
            contrib = ", ".join(e["assignees"][:3]) + ("…" if len(e["assignees"])>3 else "") or "—"
            epic_rows.append([
                _lnk(e["key"],  e["url"], color="#1a3c6e", bold=True),
                _lnk(e["name"], e["url"], color="#1a3c6e", trunc=34),
                _badge_p(e["status"]),
                Paragraph(f'<font color="{hcol}"><b>{e["health"]}</b></font>', cell_style),
                _p(f'{e["pct_done"]}%', color="#1a3c6e"),
                _jlnk(str(counts["done"]),        jql_e+' AND statusCategory = Done',           color="#4CAF50", bold=True),
                _jlnk(str(counts["qa"]),          jql_e+' AND status in ("Ready For QA","In QA","QA In Progress")', color="#9C27B0", bold=True),
                _jlnk(str(counts["in_progress"]), jql_e+' AND statusCategory = "In Progress"',  color="#2196F3", bold=True),
                _jlnk(str(counts["open"]),        jql_e+' AND statusCategory = "To Do"',        color="#F44336", bold=True),
                _p(str(e["remaining"]), color="#555"),
                _p(e["owner"], trunc=16),
                _p(rel_str, trunc=20),
            ])
        story.append(std_table(
            ["Key","Epic","Status","Health","%","✅","🔬","🔄","⏳","Left","Owner","Releases"],
            epic_rows,
            col_widths=[16*mm,36*mm,22*mm,20*mm,10*mm,10*mm,10*mm,10*mm,10*mm,12*mm,22*mm,14*mm],
            hdr_bg=NAVY))
        story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 3. Issue Type Breakdown  (matches HTML sec_types) ──────
    # ═══════════════════════════════════════════════════════════
    sec_head("🔖 Issue Type Breakdown")
    rows_data = []
    for t, d in summary["type_data"].items():
        if d["total"] == 0: continue
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        jql_t = f'{jql_sprint} AND issuetype = "{t}"'
        rows_data.append([
            _p(t.capitalize()),
            _jlnk(str(d["total"]),       jql_t,                                              color="#1a3c6e", bold=True),
            _jlnk(str(d["done"]),        jql_t+' AND statusCategory = Done',                color="#4CAF50", bold=True),
            _jlnk(str(d["in_progress"]), jql_t+' AND statusCategory = "In Progress"',        color="#2196F3", bold=True),
            _jlnk(str(d["qa"]),          jql_t+' AND status in ("Ready For QA","In QA")',    color="#9C27B0", bold=True),
            _jlnk(str(d["open"]),        jql_t+' AND statusCategory = "To Do"',              color="#F44336", bold=True),
            _p(comp),
            _jlnk("🔗 View", jql_t, color="#555"),
        ])
    story.append(std_table(
        ["Type","Total","Done","In Progress","QA","Open","Completion",""],
        rows_data,
        col_widths=[32*mm,20*mm,20*mm,26*mm,20*mm,20*mm,22*mm,20*mm],
        hdr_bg=colors.HexColor("#2c3e50")))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 4. Epics & Stories Combined  (matches HTML sec_es) ─────
    # ═══════════════════════════════════════════════════════════
    sec_head(f'📌 Epics & Stories — Combined ({len(summary["epic_story"])} issues)')
    rows_data = []
    for es in summary["epic_story"]:
        issue_url = f"{jira_url}/browse/{es['key']}"
        jql_es = f'{jql_sprint} AND issue = {es["key"]}'
        rows_data.append([
            _lnk(es["key"], issue_url, color="#1a3c6e", bold=True),
            _lnk(es["summary"], issue_url, color="#333", trunc=62),
            _p(es["type"].capitalize()),
            _badge_p(es["status"]),
            _jlnk("🔗 Open", jql_es, color="#555"),
        ])
    story.append(std_table(["Key","Summary","Type","Status",""],
                           rows_data, col_widths=[18*mm,88*mm,20*mm,28*mm,18*mm],
                           hdr_bg=colors.HexColor("#7b3fa0")))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 5. Bug Status Breakdown  (matches HTML sec_bugs) ───────
    # ═══════════════════════════════════════════════════════════
    jql_bugs = f'{jql_sprint} AND issuetype = Bug'
    sec_head(f'🐛 Bug Status Breakdown ({summary["bugs_total"]} bugs)  '
             f'<link href="{_jql_url(jql_bugs)}" color="#555"><u>🔗 All bugs in Jira</u></link>')
    story.append(b64_to_img(charts["bug_pie"], 90))
    story.append(Spacer(1, 4))
    bug_raw = defaultdict(int)
    for b in summary["bug_list"]: bug_raw[b["status"]] += 1
    rows_data = []
    for s, cnt in sorted(bug_raw.items(), key=lambda x: -x[1]):
        p   = round(cnt/summary["bugs_total"]*100) if summary["bugs_total"] else 0
        jql = f'{jql_bugs} AND status = "{s}"'
        rows_data.append([
            _badge_p(s),
            _jlnk(str(cnt), jql, color="#1a3c6e", bold=True),
            _p(f"{p}%"),
            _p(f"{'█'*int(p/5)}"),
            _jlnk("🔗 View bugs", jql, color="#555"),
        ])
    rows_data.append([_p("Total"), _p(summary["bugs_total"]), _p("100%"), _p(""), _p("")])
    story.append(std_table(["Status","Count","%","Bar",""],
                           rows_data, col_widths=[55*mm,22*mm,18*mm,38*mm,27*mm],
                           hdr_bg=colors.HexColor("#c0392b")))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 6. Release-wise Bifurcation  (matches HTML sec_release)─
    # ═══════════════════════════════════════════════════════════
    sec_head("🚀 Release-wise Bifurcation")
    story.append(b64_to_img(charts["release_bar"], 130))
    story.append(Spacer(1, 4))
    rows_data = []
    for rv, d in sorted(summary["release_data"].items()):
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        jql_rv = (f'{jql_sprint} AND fixVersion is EMPTY' if rv == "Unversioned"
                  else f'{jql_sprint} AND fixVersion = "{rv}"')
        rows_data.append([
            _p(rv),
            _jlnk(str(d["total"]),       jql_rv,                                             color="#1a3c6e", bold=True),
            _jlnk(str(d["bugs"]),        jql_rv+' AND issuetype = Bug',                      color="#F44336", bold=True),
            _jlnk(str(d["done"]),        jql_rv+' AND statusCategory = Done',                color="#4CAF50", bold=True),
            _jlnk(str(d["in_progress"]), jql_rv+' AND statusCategory = "In Progress"',       color="#2196F3", bold=True),
            _jlnk(str(d["qa"]),          jql_rv+' AND status in ("Ready For QA","In QA")',   color="#9C27B0", bold=True),
            _jlnk(str(d["open"]),        jql_rv+' AND statusCategory = "To Do"',             color="#F44336", bold=True),
            _p(comp),
            _jlnk("🔗 View", jql_rv, color="#555"),
        ])
    story.append(std_table(
        ["Release","Total","Bugs","Done","In Progress","QA","Open","Completion",""],
        rows_data,
        col_widths=[38*mm,16*mm,16*mm,16*mm,22*mm,16*mm,16*mm,20*mm,16*mm],
        hdr_bg=colors.HexColor("#1565c0")))
    story.append(PageBreak())

    # ═══════════════════════════════════════════════════════════
    # ── 7. App Version Bifurcation  (matches HTML sec_platform)─
    # ═══════════════════════════════════════════════════════════
    PLATFORM_VERSION_MAP = {"Android":"Android","iOS":"iOS","Web":"Web",
                            "Backend":"Backend","Admin Panel":"Admin"}
    sec_head("📱 App Version Bifurcation")
    story.append(b64_to_img(charts["platform_pie"], 90))
    story.append(Spacer(1, 4))
    rows_data = []
    for pl, d in sorted(summary["platform_data"].items()):
        comp = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        jql_pl = (f'{jql_sprint} AND fixVersion is EMPTY' if pl == "Unversioned"
                  else f'{jql_sprint} AND fixVersion ~ "{PLATFORM_VERSION_MAP.get(pl,pl)}"')
        rows_data.append([
            _p(pl),
            _jlnk(str(d["total"]),       jql_pl,                                             color="#1a3c6e", bold=True),
            _jlnk(str(d["bugs"]),        jql_pl+' AND issuetype = Bug',                      color="#F44336", bold=True),
            _jlnk(str(d["done"]),        jql_pl+' AND statusCategory = Done',                color="#4CAF50", bold=True),
            _jlnk(str(d["in_progress"]), jql_pl+' AND statusCategory = "In Progress"',       color="#2196F3", bold=True),
            _jlnk(str(d["qa"]),          jql_pl+' AND status in ("Ready For QA","In QA")',   color="#9C27B0", bold=True),
            _jlnk(str(d["open"]),        jql_pl+' AND statusCategory = "To Do"',             color="#F44336", bold=True),
            _p(comp),
            _jlnk("🔗 View", jql_pl, color="#555"),
        ])
    story.append(std_table(
        ["Platform","Total","Bugs","Done","In Progress","QA","Open","Completion",""],
        rows_data,
        col_widths=[35*mm,16*mm,16*mm,16*mm,22*mm,16*mm,16*mm,20*mm,16*mm],
        hdr_bg=colors.HexColor("#00695c")))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 8. Sprint Task Allocation – Per Person  (HTML sec_assign)
    # ═══════════════════════════════════════════════════════════
    sec_head("👤 Sprint Task Allocation — Per Person")
    rows_data = []
    for name, d in sorted(summary["assignee_data"].items(), key=lambda x: -x[1]["total"]):
        comp  = f"{round(d['done']/d['total']*100)}%" if d["total"] else "0%"
        jql_a = f'{jql_sprint} AND assignee = "{name}"'
        rows_data.append([
            _jlnk(name, jql_a, color="#1a3c6e", trunc=18, bold=True),
            _jlnk(str(d["total"]),       jql_a,                                               color="#1a3c6e", bold=True),
            _p(str(d["epic"])),
            _p(str(d["story"])),
            _p(str(d["task"])),
            _p(str(d["subtask"])),
            _jlnk(str(d["bug"]),         jql_a+' AND issuetype = Bug',                        color="#F44336", bold=True),
            _jlnk(str(d["done"]),        jql_a+' AND statusCategory = Done',                  color="#4CAF50", bold=True),
            _jlnk(str(d["in_progress"]), jql_a+' AND statusCategory = "In Progress"',         color="#2196F3", bold=True),
            _jlnk(str(d["qa"]),          jql_a+' AND status in ("Ready For QA","In QA")',     color="#9C27B0", bold=True),
            _jlnk(str(d["open"]),        jql_a+' AND statusCategory = "To Do"',               color="#F44336", bold=True),
            _p(comp),
            _jlnk("🔗 View", jql_a, color="#555"),
        ])
    td_ = summary["type_data"]
    rows_data.append([
        _p(f'Total ({len(summary["assignee_data"])} members)'),
        _p(str(summary["total"])),
        _p(str(td_.get("epic",{}).get("total",0))),
        _p(str(td_.get("story",{}).get("total",0))),
        _p(str(td_.get("task",{}).get("total",0))),
        _p(str(td_.get("subtask",{}).get("total",0))),
        _p(str(summary["bugs_total"]), color="#F44336"),
        _p(str(summary["done"]),       color="#4CAF50"),
        _p(str(summary["in_progress"]),color="#2196F3"),
        _p(str(summary["qa"]),         color="#9C27B0"),
        _p(str(summary["open"]),       color="#F44336"),
        _p(f'{summary["completion"]}%'),
        _p(""),
    ])
    story.append(std_table(
        ["Assignee","Total","Epic","Story","Task","Sub","Bug",
         "Done","In Prog","QA","Open","Comp",""],
        rows_data,
        col_widths=[34*mm,14*mm,12*mm,12*mm,12*mm,12*mm,12*mm,
                    14*mm,16*mm,12*mm,12*mm,14*mm,14*mm],
        hdr_bg=colors.HexColor("#37474f")))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════════════════════
    # ── 9. Sprint Burndown  (matches HTML sec_burndown) ─────────
    # ═══════════════════════════════════════════════════════════
    sec_head("📉 Sprint Burndown (Issues Remaining by Day)")
    story.append(b64_to_img(charts["burndown"], 155))
    story.append(Spacer(1, 4))
    rows_data = []
    for r in summary["burndown_rows"]:
        act = str(r["actual"]) if r["actual"] is not None else "—"
        col = "#4CAF50" if r["status"]=="On Track" else "#F44336" if r["status"]=="Behind" else "#888"
        rows_data.append([_p(r["date"]), _p(str(r["ideal"])), _p(act), _p(r["status"], color=col)])
    story.append(std_table(
        ["Date","Ideal Remaining","Actual Remaining","Status"], rows_data,
        col_widths=[50*mm, 45*mm, 45*mm, 40*mm],
        hdr_bg=colors.HexColor("#4a148c")))
    story.append(PageBreak())

    # ═══════════════════════════════════════════════════════════
    # ── 10. Bug Sheet – Full Details  (matches HTML sec_bugsheet)
    # ═══════════════════════════════════════════════════════════
    jql_bugs_all = f'{jql_sprint} AND issuetype = Bug ORDER BY priority DESC'
    sec_head(f'📋 Bug Sheet — Full Details ({len(summary["bug_list"])} bugs)  '
             f'<link href="{_jql_url(jql_bugs_all)}" color="#555"><u>🔗 Open all bugs in Jira</u></link>')
    rows_data = []
    for b in summary["bug_list"]:
        issue_url = f"{jira_url}/browse/{b['key']}"
        jql_a = f'{jql_sprint} AND issuetype = Bug AND assignee = "{b["assignee"]}"'
        rows_data.append([
            _lnk(b["key"],     issue_url, color="#1a3c6e", bold=True),
            _lnk(b["summary"], issue_url, color="#333",    trunc=52),
            _badge_p(b["status"]),
            _pri_p(b["priority"]),
            _p(b["assignee"], trunc=16),
            _p(b["release"],  trunc=14),
            _p(b["created"]),
            _p(b["updated"]),
        ])
    story.append(std_table(
        ["Key","Summary","Status","Priority","Assignee","Release","Created","Updated"],
        rows_data,
        col_widths=[18*mm, 52*mm, 26*mm, 18*mm, 28*mm, 20*mm, 18*mm, 16*mm],
        hdr_bg=colors.HexColor("#b71c1c")))

    doc.build(story)
    print(f"📄  PDF report saved: {pdf_path}")


# ══════════════════════════════════════════════════════════════
#  EMAIL SENDER  (HTML body + PDF attachment)
# ══════════════════════════════════════════════════════════════

def send_email(subject, html_body, pdf_path, html_path=None):
    """Send email with HTML body + PDF + HTML file as attachments."""
    from email.mime.base import MIMEBase
    from email import encoders

    msg            = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_FROM
    msg["To"]      = ", ".join(config.EMAIL_TO)

    # HTML body (inline, readable in email client)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    # PDF attachment
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition",
                    f'attachment; filename="{os.path.basename(pdf_path)}"')
    msg.attach(part)

    # HTML file attachment (has clickable Jira links)
    if html_path and os.path.exists(html_path):
        with open(html_path, "rb") as f:
            html_part = MIMEBase("text", "html", charset="utf-8")
            html_part.set_payload(f.read())
        encoders.encode_base64(html_part)
        html_part.add_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(html_path)}"')
        msg.attach(html_part)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as srv:
        srv.ehlo(); srv.starttls()
        srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
        srv.sendmail(config.EMAIL_FROM, config.EMAIL_TO, msg.as_string())
    print(f"✅  Email sent → {', '.join(config.EMAIL_TO)}")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

import os
import sys

def main():
    # ── Argument parsing ──────────────────────────────────────
    local_only = "--local" in sys.argv   # run locally: generate HTML/PDF, skip email

    print(f"    Project: {config.JIRA_PROJECT}")
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

    # ── Epic Tracker ─────────────────────────────────────────
    # NOTE: Jira board endpoint NEVER returns Epics — they must be
    # fetched separately via JQL. This is a hard Jira limitation.
    print("🔄  Fetching sprint epics (separate JQL call)…")
    try:
        epics_in_sprint = client.fetch_sprint_epics(sprint["name"])
        epic_keys = [e["key"] for e in epics_in_sprint]
        print(f"    Found {len(epic_keys)} epics: {', '.join(epic_keys)}")
    except Exception as ex:
        print(f"    ⚠️  Epic fetch failed: {ex} — Epic Tracker will be empty")
        epics_in_sprint = []
        epic_keys = []

    tracker = build_epic_tracker(
        epics_in_sprint, {}, config.JIRA_BASE_URL,
        all_sprint_issues=issues
    )
    total_children = sum(e["total"] for e in tracker)
    print(f"    Epic tracker built: {len(tracker)} epics, {total_children} child issues linked")

    # ── Charts ───────────────────────────────────────────────
    print("🔄  Generating charts…")
    charts = {
        "overall_pie":  chart_overall_pie(summary),
        "type_pie":     chart_type_pie(summary["type_data"]),
        "bug_pie":      chart_bug_pie(summary["type_data"]),
        "release_bar":  chart_release_bar(summary["release_data"]),
        "platform_pie": chart_platform_pie(summary["platform_data"]),
        "burndown":     chart_burndown(summary["burndown_rows"], sprint["name"]),
        "epic_progress": chart_epic_progress(tracker) if tracker else None,
    }

    print("🔄  Building HTML email…")
    html = build_html(sprint, summary, charts, tracker=tracker)

    datestamp = datetime.now().strftime("%Y%m%d")
    html_file = f"report_{datestamp}.html"
    pdf_file  = f"report_{datestamp}.pdf"

    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"💾  HTML preview saved: {html_file}")

    print("🔄  Generating PDF…")
    generate_pdf(sprint, summary, charts, pdf_file, tracker=tracker)

    if local_only:
        print("\n✅  LOCAL MODE — report files generated, email skipped.")
        print(f"    Open: {os.path.abspath(html_file)}")
        print(f"    PDF:  {os.path.abspath(pdf_file)}")
        return

    today_str = datetime.now().strftime("%d %b %Y")
    subject   = f"Cinema - Daily Sprint Report ({sprint['name']}) | {today_str}"
    print("📧  Sending email with PDF attachment…")
    send_email(subject, html, pdf_file, html_path=html_file)


if __name__ == "__main__":
    main()

