"""Render cost + usage into a single static page at docs/index.html.

One page, scannable top to bottom: what CloudBank costs right now, how that
splits across the always-on floor vs student compute, and how much of it is
actually being used.

Charts are inline SVG -- no JS libraries, no CDN, nothing to break when a
version pins. The palette is CVD-validated in both light and dark.
"""

import datetime
import json
from datetime import date, timedelta
from html import escape

import pandas as pd

from common import DOCS_DIR, PT, BASE_DIR, read_data

# Categorical palette, validated for colour-vision deficiency in both modes.
BUCKETS = ["base", "cpu", "gpu"]
LABELS = {"base": "Base (always-on)", "cpu": "CPU notebooks", "gpu": "GPU notebooks"}
LIGHT = {"base": "#2D6BB5", "cpu": "#C07A15", "gpu": "#0E9177"}
DARK = {"base": "#4A90E2", "cpu": "#BE8430", "gpu": "#159578"}


# ----------------------------------------------------------------- helpers


def resolve_week_start(year_month, week_number):
    """Monday of the ISO week a (Year-Month, Week) row belongs to.

    The Otter source groups by month AND week, so a week straddling a month
    boundary produces two rows -- week 36 of 2026 appears once under 2026-08
    and once under 2026-09. Both resolve to the same Monday here, which is how
    they get recombined. Lifted from cloudbank-pilot-hub-users so the two
    dashboards agree.
    """
    year, month = map(int, str(year_month).split("-"))
    month_anchor = date(year, month, 1)
    candidates = []
    for iso_year in (year - 1, year, year + 1):
        try:
            week_start = date.fromisocalendar(iso_year, int(week_number), 1)
        except ValueError:
            continue
        week_end = week_start + timedelta(days=6)
        score = 0
        if week_start.year == year and week_start.month == month:
            score += 2
        if week_end.year == year and week_end.month == month:
            score += 2
        if week_start.year == year or week_end.year == year:
            score += 1
        distance = min(
            abs((week_start - month_anchor).days), abs((week_end - month_anchor).days)
        )
        candidates.append((score, -distance, week_start.toordinal(), week_start))
    if not candidates:
        raise ValueError(f"Unable to resolve week {week_number} for {year_month}")
    return max(candidates)[-1]


def money(x, dp=2):
    return f"${x:,.{dp}f}"


def load():
    costs = read_data("daily_costs.csv")
    if costs.empty:
        raise SystemExit("No data/daily_costs.csv -- run scripts/costs.py first.")
    daily = costs.groupby(["date", "bucket"])["usd"].sum().unstack(fill_value=0.0)
    for b in BUCKETS:
        if b not in daily:
            daily[b] = 0.0
    daily = daily[BUCKETS].sort_index()
    daily["total"] = daily.sum(axis=1)
    return daily, costs


def resample(daily, freq):
    idx = pd.to_datetime(daily.index)
    return daily.set_index(idx).resample(freq).sum()


def stacked_bars(frame, *, height=190, bar_w=26, gap=8, fmt="%b %-d"):
    """Stacked bar chart as inline SVG. Values in USD, stacked by bucket."""
    if frame.empty:
        return "<p class='muted'>No data.</p>"
    rows = list(frame.itertuples())
    peak = max((sum(getattr(r, b) for b in BUCKETS) for r in rows), default=1) or 1
    pad_l, pad_b = 52, 26
    width = pad_l + len(rows) * (bar_w + gap) + 12
    plot_h = height - pad_b

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" class="chart">'
    ]
    # gridlines + y labels
    for frac in (0, 0.5, 1):
        y = plot_h - frac * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 8}" y2="{y:.1f}" class="grid"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="ylab">{money(peak * frac, 0)}</text>'
        )
    for i, r in enumerate(rows):
        x = pad_l + i * (bar_w + gap)
        y = plot_h
        total = sum(getattr(r, b) for b in BUCKETS)
        tip = " · ".join(f"{LABELS[b]} {money(getattr(r, b))}" for b in BUCKETS)
        for b in BUCKETS:
            v = getattr(r, b)
            if v <= 0:
                continue
            h = v / peak * plot_h
            y -= h
            parts.append(
                f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{max(h,0.6):.1f}" '
                f'class="bar {b}"><title>{escape(str(r.Index)[:10])} — total '
                f'{money(total)}\n{escape(tip)}</title></rect>'
            )
        label = r.Index.strftime(fmt) if hasattr(r.Index, "strftime") else str(r.Index)[5:]
        parts.append(
            f'<text x="{x + bar_w/2:.1f}" y="{height - 8}" class="xlab">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def table(frame, label_fmt="%Y-%m-%d"):
    head = "".join(f"<th>{LABELS[b]}</th>" for b in BUCKETS)
    body = []
    for idx, r in frame.iloc[::-1].iterrows():
        name = idx.strftime(label_fmt) if hasattr(idx, "strftime") else str(idx)
        cells = "".join(f"<td class='num'>{money(r[b])}</td>" for b in BUCKETS)
        body.append(
            f"<tr><th scope='row'>{name}</th>{cells}"
            f"<td class='num strong'>{money(r['total'])}</td></tr>"
        )
    return (
        f"<table><thead><tr><th scope='col'>Period</th>{head}"
        f"<th scope='col'>Total</th></tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


BASE_LABELS = {
    "measured": "core-pool nodes",
    "estimated:storage": "Persistent disk (home dirs + other)",
    "estimated:oss-cluster": "oss-cluster (no Prometheus — modeled)",
    "estimated:snapshots": "Snapshots (GKE PD backups)",
    "estimated:gke-cluster-fee": "GKE cluster fees",
    "estimated:load-balancers": "Load balancers",
    "estimated:idle-static-ips": "Idle static IPs",
    "estimated:gcs-buckets": "GCS buckets",
}


def base_breakdown(costs):
    """One row per always-on component for the most recent complete day."""
    base = costs[costs["bucket"] == "base"]
    if base.empty:
        return "", 0.0
    latest = base["date"].max()
    day = base[base["date"] == latest].groupby("basis")["usd"].sum().sort_values(ascending=False)
    total = float(day.sum())
    rows = []
    for basis, usd in day.items():
        label = BASE_LABELS.get(basis, basis)
        pct = usd / total * 100 if total else 0
        est = "" if basis == "measured" else " <span class='tag'>modeled</span>"
        rows.append(
            f"<tr><th scope='row'>{escape(label)}{est}</th>"
            f"<td class='num'>{money(usd)}</td>"
            f"<td class='num'>{money(usd*30, 0)}</td>"
            f"<td class='num muted-cell'>{pct:.0f}%</td></tr>"
        )
    html = (
        "<table><thead><tr><th>Component</th><th>$/day</th><th>$/month</th>"
        f"<th>share</th></tr></thead><tbody>{''.join(rows)}"
        f"<tr class='tot'><th scope='row'>Total always-on</th>"
        f"<td class='num strong'>{money(total)}</td>"
        f"<td class='num strong'>{money(total*30,0)}</td><td></td></tr>"
        "</tbody></table>"
    )
    return html, total


CATEGORY_LABELS = {
    "vm": "Node VMs",
    "gpu": "GPUs",
    "disks": "Persistent disks",
    "snapshots": "Snapshots",
    "logging": "Cloud Logging",
    "monitoring": "Cloud Monitoring",
    "network": "Network & IPs",
    "gke-fee": "GKE cluster fees",
    "filestore": "Filestore",
    "other": "Other",
}


def billed_block(daily):
    """Modeled vs billed. Returns (html, stats) or (None, None) with no billed data."""
    billed = read_data("billed_costs.csv")
    if billed.empty:
        return None, None
    # A day counts as complete once the export has rows from 6h past its end;
    # anything later is still filling in and would read as a huge model error.
    newest = pd.to_datetime(billed["exported"]).max()
    net = billed.groupby("date")["net"].sum()
    done = [
        d for d in net.index
        if pd.Timestamp(d).tz_localize(PT) + pd.Timedelta(days=1, hours=6) <= newest
    ]

    rows = []
    for d in sorted(set(daily.index) | set(net.index), reverse=True)[:14]:
        m = daily["total"].get(d)
        b = net.get(d) if d in done else None
        diff = f"{(m / b - 1) * 100:+.1f}%" if m is not None and b else ""
        rows.append(
            f"<tr><th scope='row'>{escape(str(d))}</th>"
            f"<td class='num'>{money(m) if m is not None else ''}</td>"
            f"<td class='num strong'>{money(b) if b is not None else '<span class=muted-cell>pending</span>'}</td>"
            f"<td class='num'>{diff}</td></tr>"
        )

    last7 = done[-7:]
    recent = billed[billed["date"].isin(last7)]
    cat = (recent.groupby("category")["net"].sum() / max(len(last7), 1)).sort_values(ascending=False)
    cat_rows = "".join(
        f"<tr><th scope='row'>{escape(CATEGORY_LABELS.get(k, k))}</th>"
        f"<td class='num'>{money(v)}</td></tr>"
        for k, v in cat.items() if abs(v) >= 0.01
    )
    both = [d for d in last7 if d in daily.index]
    m7 = daily.loc[both, "total"].sum()
    b7 = net.loc[both].sum()
    credits = recent["credits"].sum() / max(len(last7), 1)

    html = f"""
<section>
  <h2>Modeled vs billed</h2>
  <p class="sub">Billed figures come from CloudBank's billing export, net of
     sustained-use and other credits. The export runs 1–2 days behind, so recent
     days show as pending and the model is the only figure for them.</p>
  <div class="cols">
    <div>
      <h3>Daily, last 14 days</h3>
      <table><thead><tr><th>Day</th><th>Modeled</th><th>Billed</th><th>Model vs billed</th>
      </tr></thead><tbody>{''.join(rows)}</tbody></table>
    </div>
    <div>
      <h3>Billed per day by category, last {len(last7)} complete days</h3>
      <table><tbody>{cat_rows}
      <tr class="tot"><th scope="row">Total</th><td class="num strong">{money(cat.sum())}</td></tr>
      </tbody></table>
      <p class="muted">Credits (mostly sustained-use discounts) take
         {money(-credits)}/day off list price. Over these days the model is
         {(m7 / b7 - 1) * 100:+.1f}% against the bill.</p>
    </div>
  </div>
</section>"""
    stats = {"billed_day": b7 / max(len(both), 1), "days": len(both), "through": done[-1] if done else None,
             "stale_h": (pd.Timestamp.now(tz="UTC") - newest).total_seconds() / 3600}
    return html, stats


def hub_cost_block(min_usd=1.0):
    """Cost by institution for the last complete Mon-Sun week, with term users.

    Same weeks as the Weekly chart (weeks ending Sunday), so the two agree.
    """
    hc = read_data("hub_costs.csv")
    if hc.empty:
        return None
    newest = date.fromisoformat(max(hc["date"]))
    sunday = newest - timedelta(days=(newest.weekday() + 1) % 7)
    dates = [str(sunday - timedelta(days=6 - i)) for i in range(7)]
    if not set(dates) <= set(hc["date"]):
        sunday -= timedelta(days=7)
        dates = [str(sunday - timedelta(days=6 - i)) for i in range(7)]
    span = f"{date.fromisoformat(dates[0]):%b %-d} – {sunday:%b %-d, %Y}"
    week = hc[hc["date"].isin(dates)].groupby("college")[["gpu_usd", "cpu_usd"]].sum()
    week["total"] = week["gpu_usd"] + week["cpu_usd"]

    users, term = {}, None
    upath = BASE_DIR / "users.csv"
    if upath.is_file():
        u = pd.read_csv(upath)
        u = u[~u["college"].astype(str).str.startswith("Total")]
        term_cols = [c for c in u.columns if "_20" in c]
        populated = [c for c in term_cols if u[c].sum() > 0]
        term = populated[-1] if populated else None
        if term:
            users = u.groupby("college")[term].sum().to_dict()

    week = week.sort_values("total", ascending=False)
    shown, rest = week[week["total"] >= min_usd], week[week["total"] < min_usd]

    def row(name, r, n_users, cls=""):
        per = money(r["total"] / n_users) if n_users else "—"
        return (
            f"<tr{cls}><th scope='row'>{escape(str(name))}</th>"
            f"<td class='num'>{int(n_users):,}</td>"
            f"<td class='num'>{money(r['gpu_usd'])}</td><td class='num'>{money(r['cpu_usd'])}</td>"
            f"<td class='num strong'>{money(r['total'])}</td><td class='num'>{per}</td></tr>"
        )

    body = "".join(row(name, r, users.get(name, 0)) for name, r in shown.iterrows())
    if len(rest):
        rest_users = sum(users.get(n, 0) for n in rest.index)
        body += row(f"{len(rest)} other hubs (under {money(min_usd, 0)} each)",
                    rest[["gpu_usd", "cpu_usd", "total"]].sum(), rest_users, " class='muted-cell'")
    all_users = sum(users.get(n, 0) for n in week.index)
    body += row("Total", week[["gpu_usd", "cpu_usd", "total"]].sum(), all_users, " class='tot'")

    term_label = (term or "").replace("_", " ").title()

    # The week's whole bill, base included, as a headline above the split.
    dc = read_data("daily_costs.csv")
    wk = dc[dc["date"].isin(dates)].groupby("bucket")["usd"].sum()
    parts = [(b, float(wk.get(b, 0.0))) for b in BUCKETS]
    grand = sum(v for _, v in parts) or 1.0
    bar = "".join(
        f"<span class='seg {b}' style='width:{v / grand * 100:.2f}%' "
        f"title='{LABELS[b]} {money(v)}'></span>"
        for b, v in parts if v > 0
    )
    split = " · ".join(
        f"<span class='lg'><i class='sw {b}'></i>{LABELS[b]} {money(v, 0)}</span>" for b, v in parts
    )
    headline = f"""
  <div class="weektotal">
    <span class="k">Total cost, week of {escape(span)}</span>
    <span class="big">{money(grand, 0)}</span>
    <span class="s">{money(grand / len(dates), 0)}/day · {money(week['total'].sum(), 0)} of it student compute,
      split by institution below</span>
    <div class="splitbar">{bar}</div>
    <div class="legend">{split}</div>
  </div>"""
    return f"""
<section>
  <h2>Cost and Users by Institutions</h2>{headline}
  <p class="sub">CPU and GPU pool cost for the week of {escape(span)} (Mon–Sun), split by each
     hub's measured usage. Users are accounts active in {escape(term_label or 'the current term')}.</p>
  <table class="sortable"><thead><tr><th>Institution</th><th>Users</th><th>GPU</th><th>CPU</th>
  <th>Total</th><th>Per user</th></tr></thead><tbody>{body}</tbody></table>
  <p class="muted">Click a column heading to sort.</p>
  <p class="muted">Modeled, not billed. GPU is split by GPU pod-hours and CPU by notebook memory
     requested, so idle pre-warm time lands on the hubs that used those nodes. The always-on base
     (core-pool, disks, cluster fees) is shared by every hub and is not split here. Per user is
     this period's cost over the term's users, so it reads high early in a term.</p>
</section>"""


def current_term(today=None):
    """(label, start_date) for the academic term `today` falls in.

    Boundaries match generate_dates() in users.py exactly -- summer Jun 15,
    fall Aug 11, spring Jan 1 -- so the notebook count and the user count on
    the dashboard are scoped to the same window. If those ever diverge the
    two figures stop being comparable, which is the whole point of showing
    them side by side.
    """
    today = today or datetime.datetime.now(PT).date()
    y = today.year
    if date(y, 6, 15) <= today <= date(y, 8, 10):
        return f"Summer {y}", date(y, 6, 15)
    if date(y, 8, 11) <= today <= date(y, 12, 31):
        return f"Fall {y}", date(y, 8, 11)
    return f"Spring {y}", date(y, 1, 1)


def usage_blocks():
    """Users by program and Otter grading volume, if those CSVs are present."""
    out = {}
    upath = BASE_DIR / "users.csv"
    if upath.is_file():
        u = pd.read_csv(upath)
        # users.py appends its own summary rows ("Total", "Total Schools > 5
        # Users") to the bottom of the CSV. Summing the column as-is counts
        # every user twice and inflates the institution count by two, so drop
        # them before any aggregate is taken.
        u = u[~u["college"].astype(str).str.startswith("Total")]
        term_cols = [c for c in u.columns if "_20" in c]
        # The rightmost column is a future term that is still all zeros, so
        # pick the latest term that actually has users rather than the last one.
        populated = [c for c in term_cols if u[c].sum() > 0]
        current = populated[-1] if populated else (term_cols[-1] if term_cols else None)
        out["users"] = {
            "institutions": int(u["college"].nunique()),
            "hubs": int(len(u)),
            "current_term": current,
            "current_users": int(u[current].sum()) if current else 0,
            "ever_active": int(u["all-users-ever-active"].sum()),
            "all_users": int(u["all-users"].sum()),
            # top hubs by users this term, which is more useful than a
            # one-row program table now that there is only one program
            "top": (
                u[["college", current]].sort_values(current, ascending=False)
                .head(12).values.tolist() if current else []
            ),
        }
    opath = BASE_DIR / "otter_standalone_use.csv"
    if opath.is_file():
        o = pd.read_csv(opath, skiprows=1, skipinitialspace=True)
        o.columns = [c.strip() for c in o.columns]
        # Combine the month-boundary split before display.
        o["week_start"] = o.apply(
            lambda r: resolve_week_start(r["Year-Month"], r["Week Of Year"]), axis=1
        )
        weekly = (
            o.groupby("week_start")[["Number of Users", "Number of Notebooks"]]
            .sum()
            .sort_index(ascending=False)
            .head(12)
            .reset_index()
        )
        weekly["label"] = weekly["week_start"].apply(
            lambda d: f"{d.strftime('%b %-d')} – {(d + timedelta(days=6)).strftime('%b %-d, %Y')}"
        )
        term_label, term_start = current_term()
        # week_start holds date objects, so the column dtype is `object` and
        # the .dt accessor is unavailable -- normalise before comparing.
        in_term = o[pd.to_datetime(o["week_start"]).dt.date >= term_start]
        out["otter"] = {
            "rows": weekly.to_dict("records"),
            "total": int(o["Number of Notebooks"].sum()),
            "term": term_label,
            "term_total": int(in_term["Number of Notebooks"].sum()),
            "term_users": int(in_term["Number of Users"].sum()),
        }
    return out


def nice_max(v):
    """Round a peak up to 1, 2, 2.5 or 5 x 10^n so the top gridline is a clean number."""
    if v <= 0:
        return 1
    exp = 10 ** int(f"{v:e}".split("e")[1])
    for step in (1, 2, 2.5, 5, 10):
        if v <= step * exp:
            return step * exp
    return 10 * exp


def column_path(x, top, w, base, r=4):
    """A column with a 4px rounded data end and a square foot on the baseline."""
    h = base - top
    if h < r * 1.5:
        return f'<rect x="{x:.1f}" y="{top:.1f}" width="{w}" height="{max(h, 0.8):.1f}"'
    return (f'<path d="M{x:.1f},{base:.1f}V{top + r:.1f}Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f}'
            f'H{x + w - r:.1f}Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f}V{base:.1f}Z"')


def grouped_columns(groups, series, *, fmt, height=210, bar_w=18, group_gap=16,
                    label_last=False, tick_every=1):
    """Grouped column chart as inline SVG.

    groups: x-axis labels. series: (name, values, css class); a None value leaves
    an empty slot so gaps in the data stay visible. Bars inside a group sit 2px
    apart; every bar carries a hover title. label_last puts the value above the
    last series' bars only, so labels stay sparing.
    """
    n = len(series)
    peak = nice_max(max((v for _, vals, _ in series for v in vals if v), default=1))
    pad_l, pad_b, pad_t = 52, 24, 18 if label_last else 8
    group_w = n * bar_w + (n - 1) * 2
    width = pad_l + len(groups) * (group_w + group_gap) + 8
    base = height - pad_b
    plot_h = base - pad_t

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
             f'role="img" class="chart">']
    for frac in (0, 0.5, 1):
        y = base - frac * plot_h
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 8}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="ylab">{fmt(peak * frac)}</text>')
    for gi, g in enumerate(groups):
        gx = pad_l + gi * (group_w + group_gap) + group_gap / 2
        for si, (name, vals, cls) in enumerate(series):
            v = vals[gi]
            if v is None or pd.isna(v) or v <= 0:
                continue
            x = gx + si * (bar_w + 2)
            top = base - v / peak * plot_h
            el = column_path(x, top, bar_w, base)
            close = "</rect>" if el.startswith("<rect") else "</path>"
            parts.append(f'{el} class="col {cls}"><title>{escape(str(g))} {escape(str(name))}: '
                         f'{fmt(v)}</title>{close}')
            if label_last and si == n - 1:
                parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{top - 5:.1f}" class="vlab">{fmt(v)}</text>')
        if g and gi % tick_every == 0:
            parts.append(f'<text x="{gx + group_w / 2:.1f}" y="{height - 7}" class="xlab">{escape(str(g))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def year_legend(items):
    return "<div class='legend'>" + "".join(
        f"<span class='lg'><i class='sw {cls}'></i>{escape(str(name))}</span>" for name, cls in items
    ) + "</div>"


PILOT_RAW = "https://raw.githubusercontent.com/sean-morris/cloudbank-pilot-hub-users/main"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def change_cell(new, old):
    if not old or pd.isna(old) or pd.isna(new):
        return "<td class='num muted'>–</td>"
    pct = (new - old) / old * 100
    cls = "up" if pct >= 0 else "down"
    return f"<td class='num {cls}'>{pct:+.0f}%</td>"


def yoy_block():
    """CloudBank users per term and monthly active users, year over year.

    Both come from the cloudbank-pilot-hub-users registry repo rather than this
    repo's users.csv: its term counts are restored from history after the
    Aug 2026 account loss, and it keeps Prometheus monthly active users.
    """
    try:
        u = pd.read_csv(f"{PILOT_RAW}/users.csv")
        m = pd.read_csv(f"{PILOT_RAW}/data/active_monthly.csv")
    except Exception as e:  # the page must still build if GitHub is unreachable
        print(f"  WARNING: year-over-year data unavailable: {e}")
        return ""

    u = u[~u["college"].astype(str).str.startswith("Total")]
    u = u[u["where"] == "cloudbank"]
    terms = [c for c in u.columns if "_20" in c and u[c].sum() > 0]
    years = sorted({int(t.split("_")[1]) for t in terms})
    current_term = terms[-1]

    term_rows = []
    for season in ["spring", "summer", "fall"]:
        cells, vals = [], []
        for y in years[-4:]:
            col = f"{season}_{y}"
            n = int(u[col].sum()) if col in u else 0
            schools = int((u[col] > 0).sum()) if col in u else 0
            vals.append(n)
            note = " *" if col == current_term else ""
            cells.append(
                f"<td class='num'>{n:,}{note}<span class='muted'> · {schools}</span></td>"
                if n else "<td class='num muted'>–</td>"
            )
        last = [v for v in vals if v]
        delta = change_cell(last[-1], last[-2]) if len(last) >= 2 else "<td class='num muted'>–</td>"
        term_rows.append(f"<tr><th scope='row'>{season.title()}</th>{''.join(cells)}{delta}</tr>")
    year_heads = "".join(f"<th class='num'>{y}</th>" for y in years[-4:])
    cyrs = years[-4:]
    ycls = {y: f"yr{i + 4 - len(cyrs)}" for i, y in enumerate(cyrs)}
    seasons = ["Spring", "Summer", "Fall"]
    term_chart = year_legend([(y, ycls[y]) for y in cyrs]) + grouped_columns(
        seasons,
        [(y, [int(u[f"{se.lower()}_{y}"].sum()) if f"{se.lower()}_{y}" in u else None for se in seasons],
          ycls[y]) for y in cyrs],
        fmt=lambda v: f"{v:,.0f}", bar_w=22, group_gap=28, label_last=True)

    m["year"] = m["month"].str[:4].astype(int)
    m["mon"] = m["month"].str[5:7].astype(int)
    mau = m.pivot_table(index="mon", columns="year", values="cloudbank", aggfunc="sum")
    mau_years = list(mau.columns)
    mau_rows = []
    for mon in range(1, 13):
        cells = "".join(
            f"<td class='num'>{int(mau.at[mon, y]):,}</td>"
            if mon in mau.index and pd.notna(mau.at[mon, y]) else "<td class='num muted'>–</td>"
            for y in mau_years
        )
        cur, prev = (mau.at[mon, mau_years[-1]], mau.at[mon, mau_years[-2]]) if (
            mon in mau.index and len(mau_years) >= 2) else (float("nan"), float("nan"))
        mau_rows.append(f"<tr><th scope='row'>{MONTHS[mon - 1]}</th>{cells}{change_cell(cur, prev)}</tr>")
    mau_heads = "".join(f"<th class='num'>{y}</th>" for y in mau_years)
    mcls = {y: f"yr{i + 4 - len(mau_years[-2:])}" for i, y in enumerate(mau_years[-2:])}
    mau_chart = year_legend([(y, mcls[y]) for y in mau_years[-2:]]) + grouped_columns(
        MONTHS,
        [(y, [mau.at[mon, y] if mon in mau.index else None for mon in range(1, 13)], mcls[y])
         for y in mau_years[-2:]],
        fmt=lambda v: f"{v:,.0f}", bar_w=11, group_gap=10)

    cost_html = ""
    bpath = BASE_DIR / "data" / "billed_monthly.csv"
    if bpath.is_file():
        b = pd.read_csv(bpath)
        b["year"] = b["month"].str[:4].astype(int)
        b["mon"] = b["month"].str[5:7].astype(int)
        net = b[b["year"] >= 2021].pivot_table(index="mon", columns="year", values="net", aggfunc="sum")
        cyears = list(net.columns)
        crow = []
        for mon in range(1, 13):
            cells = "".join(
                f"<td class='num'>{money(net.at[mon, y], 0)}</td>"
                if mon in net.index and pd.notna(net.at[mon, y]) else "<td class='num muted'>–</td>"
                for y in cyears
            )
            cur, prev = (net.at[mon, cyears[-1]], net.at[mon, cyears[-2]]) if mon in net.index else (
                float("nan"), float("nan"))
            crow.append(f"<tr><th scope='row'>{MONTHS[mon - 1]}</th>{cells}{change_cell(cur, prev)}</tr>")
        cheads = "".join(f"<th class='num'>{y}</th>" for y in cyears)
        span = pd.period_range(f"{cyears[0]}-01", b["month"].max(), freq="M")
        bnet = b.set_index("month")["net"]
        bill_chart = grouped_columns(
            [p.strftime("%Y") if p.month == 1 else "" for p in span],
            [("billed", [bnet.get(str(p)) for p in span], "yr2")],
            fmt=lambda v: money(v, 0), bar_w=8, group_gap=3, height=200)

        both = b.merge(m[["month", "cloudbank"]], on="month").dropna(subset=["cloudbank"])
        both = both[both["cloudbank"] > 0]
        per = both.sort_values("month")
        per_chart = grouped_columns(
            [pd.Timestamp(mo + "-01").strftime("%b %y") for mo in per["month"]],
            [("per active user", list(per["net"] / per["cloudbank"]), "yr2")],
            fmt=lambda v: money(v), bar_w=16, group_gap=12, height=170, tick_every=3)
        per_rows = "".join(
            f"<tr><th scope='row'>{pd.Timestamp(r['month'] + '-01'):%b %Y}</th>"
            f"<td class='num'>{money(r['net'], 0)}</td><td class='num'>{int(r['cloudbank']):,}</td>"
            f"<td class='num strong'>{money(r['net'] / r['cloudbank'])}</td></tr>"
            for _, r in both.sort_values("month", ascending=False).iterrows()
        )
        cost_html = f"""
  <h3>Billed per month</h3>
  <div class="chartwrap">{bill_chart}</div>
  <table><thead><tr><th>Month</th>{cheads}<th class='num'>YoY</th></tr></thead>
  <tbody>{''.join(crow)}</tbody></table>
  <p class="muted">Net of credits, from the CloudBank billing export. Mar 2024 – Aug 2025 is
     not in the export, so those months show as –. The current month fills in as the
     export catches up.</p>
  <h3>Cost per monthly active user</h3>
  <div class="chartwrap">{per_chart}</div>
  <table><thead><tr><th>Month</th><th class='num'>Billed</th><th class='num'>Active users</th>
  <th class='num'>Per user</th></tr></thead><tbody>{per_rows}</tbody></table>"""

    return f"""
<section>
  <h2>Year over year</h2>
  <p class="sub">CloudBank only. Term users are accounts whose last activity fell in that
     term, so someone active in both fall and spring counts in spring.</p>
  <div class="cols">
    <div>
      <h3>Users per term <span class="muted">· institutions</span></h3>
      <div class="chartwrap">{term_chart}</div>
      <div class="tablewrap"><table><thead><tr><th>Term</th>{year_heads}<th class='num'>YoY</th></tr></thead>
      <tbody>{''.join(term_rows)}</tbody></table></div>
      <p class="muted">* {escape(current_term.replace('_', ' ').title())} is in progress.
         Counts before Fall 2026 are restored from history after the August 2026 account loss.</p>
    </div>
    <div>
      <h3>Monthly active users</h3>
      <div class="chartwrap">{mau_chart}</div>
      <div class="tablewrap"><table><thead><tr><th>Month</th>{mau_heads}<th class='num'>YoY</th></tr></thead>
      <tbody>{''.join(mau_rows)}</tbody></table></div>
      <p class="muted">Distinct users active in each calendar month, from the hub's
         <code>jupyterhub_active_users</code> metric. Tracked from January 2025.</p>
    </div>
  </div>
  {cost_html}
</section>"""


# -------------------------------------------------------------------- page


def build():
    daily, costs = load()
    weekly = resample(daily, "W-SUN")
    monthly = resample(daily, "MS")
    usage = usage_blocks()
    base_table, _ = base_breakdown(costs)
    billed_html, billed = billed_block(daily)
    hub_html = hub_cost_block()
    yoy_html = yoy_block()

    last7 = daily.tail(7)
    base_day = last7["base"].mean()
    tot_day = last7["total"].mean()
    var_day = tot_day - base_day
    latest = daily.index.max()

    # what fraction of spend is the floor?
    base_pct = base_day / tot_day * 100 if tot_day else 0

    # Three boxes only: billed per day, the 30-day total, and term users.
    stat_cards = []
    if billed and billed["days"]:
        stat_cards.append((
            "Billed per day",
            money(billed["billed_day"]),
            f"{billed['days']}-day average through {billed['through']}",
        ))
    stat_cards.append(
        ("Last 30 days", money(daily.tail(30)["total"].sum(), 0), f"through {latest}")
    )
    if "users" in usage:
        u = usage["users"]
        term = (u["current_term"] or "").replace("_", " ").title()
        stat_cards.append(
            (f"Users, {term}", f"{u['current_users']:,}", f"{u['institutions']} institutions")
        )

    cards = "".join(
        f"<div class='stat'><span class='k'>{escape(k)}</span>"
        f"<span class='v'>{escape(v)}</span><span class='s'>{escape(s)}</span></div>"
        for k, v, s in stat_cards
    )

    legend = "".join(
        f"<span class='lg'><i class='sw {b}'></i>{LABELS[b]}</span>" for b in BUCKETS
    )

    # usage tables
    usage_html = ""
    if "users" in usage:
        u = usage["users"]
        rows = "".join(
            f"<tr><th scope='row'>{escape(str(name))}</th>"
            f"<td class='num strong'>{int(n):,}</td></tr>"
            for name, n in u["top"]
        )
        usage_html += (
            f"<h3>Busiest hubs, {escape((u['current_term'] or '').replace('_',' '))}</h3>"
            "<table><thead><tr><th>Institution</th><th>Users</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
            f"<p class='muted'>{u['hubs']} CloudBank hubs · {u['all_users']:,} accounts "
            f"in total; {u['ever_active']:,} have ever been active.</p>"
        )
    if "otter" in usage:
        rows = "".join(
            f"<tr><th scope='row'>{escape(str(r['label']))}</th>"
            f"<td class='num'>{int(r['Number of Users']):,}</td>"
            f"<td class='num'>{int(r['Number of Notebooks']):,}</td></tr>"
            for r in usage["otter"]["rows"]
        )
        usage_html += (
            "<h3>Otter grading, recent weeks</h3>"
            "<table><thead><tr><th>Week (Mon–Sun)</th><th>Submissions</th>"
            f"<th>Notebooks</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    generated = datetime.datetime.now(PT).strftime("%Y-%m-%d %H:%M %Z")
    est_note = ", ".join(
        sorted({b.split(":", 1)[1] for b in costs["basis"].unique() if b.startswith("estimated")})
    )

    css = """
:root{--bg:#fcfcfb;--panel:#fff;--ink:#1a1d23;--ink2:#464c58;--muted:#6b7280;
--rule:#e4e4e0;--sunken:#f5f5f3;--base:#2D6BB5;--cpu:#C07A15;--gpu:#0E9177;--warn:#B3402E}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--panel:#1c1f25;--ink:#eceef2;
--ink2:#b9bfcb;--muted:#8d94a2;--rule:#2e323a;--sunken:#212429;--base:#4A90E2;
--cpu:#BE8430;--gpu:#159578;--warn:#E0705C}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:40px 22px 80px;display:flex;
flex-direction:column;gap:34px}
h1{font-size:29px;margin:0;letter-spacing:-.02em}
h2{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
h3{font-size:14px;margin:22px 0 8px;color:var(--ink2)}
p{margin:0}
.sub{color:var(--muted);font-size:14px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));
gap:1px;background:var(--rule);border:1px solid var(--rule);border-radius:4px;overflow:hidden}
.stat{background:var(--panel);padding:15px 17px;display:flex;flex-direction:column;gap:3px}
table.sortable thead th{cursor:pointer;user-select:none}
table.sortable thead th:hover{text-decoration:underline}
table.sortable thead th[aria-sort=ascending]::after{content:' \2191'}
table.sortable thead th[aria-sort=descending]::after{content:' \2193'}
.stat .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.stat .v{font-size:25px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.15}
.stat .s{font-size:12.5px;color:var(--muted)}
section{background:var(--panel);border:1px solid var(--rule);border-radius:4px;padding:20px 22px}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0 4px;font-size:12.5px;color:var(--ink2)}
.lg{display:flex;align-items:center;gap:6px}
.sw{width:11px;height:11px;border-radius:2px;display:inline-block}
.sw.base,.bar.base{background:var(--base);fill:var(--base)}
.sw.cpu,.bar.cpu{background:var(--cpu);fill:var(--cpu)}
.sw.gpu,.bar.gpu{background:var(--gpu);fill:var(--gpu)}
.chartwrap{overflow-x:auto}
.chart{display:block;min-width:520px}
.grid{stroke:var(--rule);stroke-width:1}
.ylab{fill:var(--muted);font-size:10px;text-anchor:end;font-variant-numeric:tabular-nums}
.xlab{fill:var(--muted);font-size:10px;text-anchor:middle}
.bar{rx:2}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin-top:6px}
th,td{text-align:left;padding:7px 11px;border-bottom:1px solid var(--rule)}
thead th{font-size:10.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
background:var(--sunken);font-weight:500;white-space:nowrap}
tbody th{font-weight:500;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.strong{font-weight:600}
.muted{color:var(--muted);font-size:12.5px;margin-top:8px}
.up{color:var(--gpu)}.down{color:var(--warn)}
.col.yr0,.sw.yr0{fill:#93b6e3;background:#93b6e3}.col.yr1,.sw.yr1{fill:#6593d1;background:#6593d1}
.col.yr2,.sw.yr2{fill:#3a71b9;background:#3a71b9}.col.yr3,.sw.yr3{fill:#1f4c8a;background:#1f4c8a}
@media(prefers-color-scheme:dark){.col.yr0,.sw.yr0{fill:#36577f;background:#36577f}
.col.yr1,.sw.yr1{fill:#41689c;background:#41689c}.col.yr2,.sw.yr2{fill:#5287cc;background:#5287cc}
.col.yr3,.sw.yr3{fill:#86b3ee;background:#86b3ee}}
.vlab{fill:var(--ink2);font-size:10.5px;text-anchor:middle;font-variant-numeric:tabular-nums}
.col:hover{opacity:.8}
.cols>div{min-width:0}
.tablewrap{overflow-x:auto}
td .muted{font-size:11.5px;margin:0}
.muted-cell{color:var(--muted)}
.tag{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
border:1px solid var(--rule);border-radius:2px;padding:1px 4px;margin-left:6px;font-weight:400}
tr.tot th,tr.tot td{border-top:2px solid var(--rule);border-bottom:none}
.note{border-left:3px solid var(--warn);background:var(--sunken);padding:13px 16px;
border-radius:3px;font-size:13.5px;color:var(--ink2)}
.note b{color:var(--warn)}
.weektotal{margin:14px 0 18px;display:flex;flex-direction:column;gap:4px}
.weektotal .k{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.weektotal .big{font-size:44px;font-weight:650;letter-spacing:-.02em;line-height:1.05;font-variant-numeric:tabular-nums}
.weektotal .s{font-size:13px;color:var(--muted)}
.splitbar{display:flex;height:12px;border-radius:3px;overflow:hidden;margin-top:10px;background:var(--sunken)}
.splitbar .seg.base{background:var(--base)}.splitbar .seg.cpu{background:var(--cpu)}.splitbar .seg.gpu{background:var(--gpu)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:26px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
footer{color:var(--muted);font-size:12px;border-top:1px solid var(--rule);padding-top:16px}
"""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CloudBank Cost &amp; Usage</title>
<style>{css}</style></head><body><div class="wrap">

<header>
  <h1>CloudBank Cost &amp; Usage</h1>
  <p class="sub">Modeled spend by node pool, and how much of it is being used.
     Data through {escape(str(latest))} · generated {escape(generated)}</p>
</header>

<div class="stats">{cards}</div>

{hub_html or ""}

{yoy_html}

<div class="note">
  <b>The charts are modeled figures.</b> Cost is measured node-hours &times; Cloud
  Billing Catalog list price, which is the only figure for the last 1–2 days.
  {"Billed figures from the billing export are under <i>Modeled vs billed</i>." if billed_html else "No billed figures yet: this run could not read the billing export."}
  Sustained-use discounts are not modeled (the real bill is lower); snapshot
  uploads, egress, logging and monitoring are not modeled (higher).
  {"Estimated components: " + escape(est_note) + "." if est_note else ""}
</div>
{billed_html or ""}

<section>
  <h2>Daily</h2>
  <p class="sub">Last 30 days. The blue floor is what runs with zero students.</p>
  <div class="legend">{legend}</div>
  <div class="chartwrap">{stacked_bars(daily.tail(30))}</div>
</section>

<div class="cols">
  <section>
    <h2>Weekly</h2>
    <p class="sub">Weeks ending Sunday.</p>
    <div class="chartwrap">{stacked_bars(weekly.tail(12), bar_w=30, fmt="%b %-d")}</div>
    {table(weekly.tail(8), "%Y-%m-%d")}
  </section>
  <section>
    <h2>Monthly</h2>
    <p class="sub">Calendar months.</p>
    <div class="chartwrap">{stacked_bars(monthly.tail(12), bar_w=38, fmt="%b %Y")}</div>
    {table(monthly.tail(8), "%B %Y")}
  </section>
</div>

<section>
  <h2>Always-on base</h2>
  <p class="sub">core-pool + oss-cluster + GKE cluster fees + persistent storage.
     This is the number to quote when asked what CloudBank costs to simply exist.</p>
  <p style="font-size:31px;font-weight:600;margin:12px 0 2px;font-variant-numeric:tabular-nums">
     {money(base_day)}<span style="font-size:15px;color:var(--muted);font-weight:400"> / day</span>
     &nbsp;&nbsp;{money(base_day*30, 0)}<span style="font-size:15px;color:var(--muted);font-weight:400"> / month</span></p>
  <p class="muted">{base_pct:.0f}% of total spend over the last 7 days.</p>
  <h3>What makes it up</h3>
  {base_table}
  <p class="muted">Only the top line is measured node-hours. Everything below it is
     provisioned capacity or a flat fee — it costs the same at 3am on a Sunday.
     <b>Not included:</b> internet egress, Cloud Logging and Monitoring, and snapshot
     uploads. Their billed amounts are under <i>Modeled vs billed</i>.</p>
</section>

<section>
  <h2>Usage</h2>
  <p class="sub">CloudBank hub accounts and Otter grading volume.</p>
  {usage_html or "<p class='muted'>No usage data yet — run users.py and otter_standalone_use.py.</p>"}
</section>

<footer>
  Sources: CloudBank Prometheus (node-hours, 5-minute samples anchored to Pacific
  midnight) · Cloud Billing Catalog API (list prices) · <code>gcloud compute disks</code>
  (provisioned storage) · CloudBank billing export in BigQuery (billed) ·
  JupyterHub REST API (users) · Firestore (Otter).
  Built by <code>cloudbank-classroom-usage</code>.
</footer>

</div>
<script>
// Click a heading in a .sortable table to sort by that column. Values are
// compared as numbers when both parse after stripping $ , % and spaces, so
// "$1,234" and "12" sort numerically while institution names sort as text.
document.querySelectorAll("table.sortable").forEach(function (table) {{
  var head = table.tHead && table.tHead.rows[0];
  if (!head) return;
  var body = table.tBodies[0];
  if (!body) return;

  function value(row, i) {{
    var cell = row.cells[i];
    return cell ? cell.textContent.trim() : "";
  }}
  function num(text) {{
    var n = parseFloat(text.replace(/[$,%\\s]/g, ""));
    return isNaN(n) ? null : n;
  }}

  Array.prototype.forEach.call(head.cells, function (th, i) {{
    th.tabIndex = 0;
    th.setAttribute("role", "button");
    th.addEventListener("click", function () {{
      var dir = th.dataset.dir === "asc" ? "desc" : "asc";
      Array.prototype.forEach.call(head.cells, function (o) {{
        delete o.dataset.dir;
        o.removeAttribute("aria-sort");
      }});
      th.dataset.dir = dir;
      th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");

      var rows = Array.prototype.slice.call(body.rows);
      rows.sort(function (a, b) {{
        var x = value(a, i), y = value(b, i);
        var nx = num(x), ny = num(y), r;
        if (nx !== null && ny !== null) r = nx - ny;
        else r = x.localeCompare(y, undefined, {{ numeric: true }});
        return dir === "asc" ? r : -r;
      }});
      rows.forEach(function (r) {{ body.appendChild(r); }});
    }});
    th.addEventListener("keydown", function (e) {{
      if (e.key === "Enter" || e.key === " ") {{ e.preventDefault(); th.click(); }}
    }});
  }});
}});
</script>
</body></html>"""

    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  docs/index.html written ({len(html):,} bytes)")
    print(f"  base {money(base_day)}/day · variable {money(var_day)}/day · total {money(tot_day)}/day")


if __name__ == "__main__":
    build()
