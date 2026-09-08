"""Render cost + usage into a single static page at docs/index.html.

One page, scannable top to bottom: what CloudBank costs right now, how that
splits across the always-on floor vs student compute, and how much of it is
actually being used.

Charts are inline SVG -- no JS libraries, no CDN, nothing to break when a
version pins. The palette is CVD-validated in both light and dark.
"""

import datetime
from html import escape

import pandas as pd

from common import DOCS_DIR, PT, BASE_DIR, read_data

# Categorical palette, validated for colour-vision deficiency in both modes.
BUCKETS = ["base", "cpu", "gpu"]
LABELS = {"base": "Base (always-on)", "cpu": "CPU notebooks", "gpu": "GPU notebooks"}
LIGHT = {"base": "#2D6BB5", "cpu": "#C07A15", "gpu": "#0E9177"}
DARK = {"base": "#4A90E2", "cpu": "#BE8430", "gpu": "#159578"}


# ----------------------------------------------------------------- helpers


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


def usage_blocks():
    """Users by program and Otter grading volume, if those CSVs are present."""
    out = {}
    upath = BASE_DIR / "users.csv"
    if upath.is_file():
        u = pd.read_csv(upath)
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
        recent = o.head(12).copy()
        recent["label"] = recent["Year-Month"].astype(str) + " w" + recent["Week Of Year"].astype(str)
        out["otter"] = {
            "rows": recent.to_dict("records"),
            "total": int(o["Number of Notebooks"].sum()),
        }
    return out


# -------------------------------------------------------------------- page


def build():
    daily, costs = load()
    weekly = resample(daily, "W-SUN")
    monthly = resample(daily, "MS")
    usage = usage_blocks()

    last7 = daily.tail(7)
    base_day = last7["base"].mean()
    tot_day = last7["total"].mean()
    var_day = tot_day - base_day
    latest = daily.index.max()

    # what fraction of spend is the floor?
    base_pct = base_day / tot_day * 100 if tot_day else 0

    stat_cards = [
        ("Always-on base", money(base_day), f"{money(base_day*30, 0)}/mo · {base_pct:.0f}% of spend"),
        ("Variable (students)", money(var_day), f"{money(var_day*30, 0)}/mo · CPU + GPU"),
        ("Total per day", money(tot_day), "7-day average"),
        ("Last 30 days", money(daily.tail(30)["total"].sum(), 0), f"through {latest}"),
    ]
    if "users" in usage:
        u = usage["users"]
        term = (u["current_term"] or "").replace("_", " ").title()
        stat_cards.append(
            (f"Users, {term}", f"{u['current_users']:,}", f"{u['institutions']} institutions")
        )
    if "otter" in usage:
        stat_cards.append(
            ("Notebooks graded", f"{usage['otter']['total']:,}", "all time, Otter standalone")
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
            "<table><thead><tr><th>Week</th><th>Submissions</th>"
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
.note{border-left:3px solid var(--warn);background:var(--sunken);padding:13px 16px;
border-radius:3px;font-size:13.5px;color:var(--ink2)}
.note b{color:var(--warn)}
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

<div class="note">
  <b>These are modeled figures, not billed ones.</b> Cost is measured node-hours
  &times; Cloud Billing Catalog list price. The BigQuery billing export has never
  been enabled for this project, so there is no invoice to reconcile against.
  Sustained-use discounts are not modeled (the real bill would be lower); egress,
  load balancers, logging and Artifact Registry are not modeled (higher).
  {"Estimated components: " + escape(est_note) + "." if est_note else ""}
</div>

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
  <p class="muted">{base_pct:.0f}% of total spend over the last 7 days.
     Storage alone is provisioned capacity — it costs the same whether or not anyone logs in.</p>
</section>

<section>
  <h2>Usage</h2>
  <p class="sub">CloudBank hub accounts and Otter grading volume.</p>
  {usage_html or "<p class='muted'>No usage data yet — run users.py and otter_standalone_use.py.</p>"}
</section>

<footer>
  Sources: CloudBank Prometheus (node-hours, 5-minute samples anchored to Pacific
  midnight) · Cloud Billing Catalog API (list prices) · <code>gcloud compute disks</code>
  (provisioned storage) · JupyterHub REST API (users) · Firestore (Otter).
  Built by <code>cloudbank-classroom-usage</code>.
</footer>

</div></body></html>"""

    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")
    print(f"  docs/index.html written ({len(html):,} bytes)")
    print(f"  base {money(base_day)}/day · variable {money(var_day)}/day · total {money(tot_day)}/day")


if __name__ == "__main__":
    build()
