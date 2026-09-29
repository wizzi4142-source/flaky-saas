"""
report.py — turns flaky_report.json (produced by analyze.py) into a
single self-contained HTML dashboard you can open in any browser.

Usage:
    python report.py --report flaky_report.json --out report.html
"""

import argparse
import html
import json


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Flaky Test Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 2rem;
          background: #0f1115; color: #e6e6e6; }}
  h1 {{ font-size: 1.4rem; margin-bottom: .25rem; }}
  .sub {{ color: #9aa0a6; margin-bottom: 1.5rem; }}
  .stats {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
  .card {{ background: #1a1d24; border-radius: 10px; padding: 1rem 1.25rem; min-width: 140px; }}
  .card .n {{ font-size: 1.6rem; font-weight: 700; }}
  .card .l {{ font-size: .8rem; color: #9aa0a6; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #262a33; font-size: .9rem; }}
  th {{ color: #9aa0a6; font-weight: 500; }}
  tr:hover {{ background: #1a1d24; }}
  .bar {{ height: 8px; border-radius: 4px; background: linear-gradient(90deg,#f97316,#ef4444); }}
  canvas {{ max-height: 320px; }}
  .chart-wrap {{ position: relative; height: 320px; margin-bottom: 1rem; }}
</style>
</head>
<body>
  <h1>Flaky Test Report</h1>
  <div class="sub">{n_tests} tests analyzed &middot; {n_flaky} flagged as flaky &middot; generated from {n_runs} recorded test runs</div>

  <div class="stats">
    <div class="card"><div class="n">{n_flaky}</div><div class="l">Flaky tests</div></div>
    <div class="card"><div class="n">{total_wasted:.1f} min</div><div class="l">Estimated wasted CI time</div></div>
    <div class="card"><div class="n">{top_score:.0f}%</div><div class="l">Worst flake rate</div></div>
  </div>

  <div class="chart-wrap"><canvas id="chart"></canvas></div>

  <table>
    <thead><tr><th>Test</th><th>Flake rate</th><th>Flips</th><th>Wasted min</th><th>Runs seen</th></tr></thead>
    <tbody>
      {rows}
    </tbody>
  </table>

<script>
const labels = {labels_json};
const scores = {scores_json};
new Chart(document.getElementById('chart'), {{
  type: 'bar',
  data: {{
    labels: labels,
    datasets: [{{
      label: 'Flake rate (%)',
      data: scores,
      backgroundColor: '#f97316',
      maxBarThickness: 36,
      categoryPercentage: 0.6,
      barPercentage: 0.9
    }}]
  }},
  options: {{
    indexAxis: 'y',
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{
        min: 0, max: 100,
        ticks: {{ color: '#c9ccd1' }},
        grid: {{ color: '#2a2e37' }}
      }},
      y: {{
        ticks: {{ color: '#c9ccd1' }},
        grid: {{ color: '#2a2e37' }}
      }}
    }}
  }}
}});
</script>
</body>
</html>
"""

ROW_TEMPLATE = """
<tr>
  <td>{test}</td>
  <td>
    <div>{score:.0f}%</div>
    <div class="bar" style="width:{score:.0f}%"></div>
  </td>
  <td>{flips}</td>
  <td>{wasted:.1f}</td>
  <td>{runs}</td>
</tr>
"""


def build_html(report):
    flaky = [r for r in report if r["flake_score"] > 0]
    top20 = flaky[:20]

    rows_html = "".join(
        ROW_TEMPLATE.format(
            test=html.escape(r["test"]),
            score=r["flake_score"] * 100,
            flips=r["flip_commits"],
            wasted=r["wasted_minutes"],
            runs=r["total_runs"],
        )
        for r in flaky
    ) or "<tr><td colspan='5'>No flaky tests detected yet — nice.</td></tr>"

    labels = [r["test"] for r in top20]
    scores = [round(r["flake_score"] * 100, 1) for r in top20]

    return TEMPLATE.format(
        n_tests=len(report),
        n_flaky=len(flaky),
        n_runs=sum(r["total_runs"] for r in report),
        total_wasted=sum(r["wasted_minutes"] for r in flaky),
        top_score=(flaky[0]["flake_score"] * 100 if flaky else 0),
        rows=rows_html,
        labels_json=json.dumps(labels).replace("<", "\\u003c"),
        scores_json=json.dumps(scores),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", default="flaky_report.json")
    ap.add_argument("--out", default="report.html")
    args = ap.parse_args()

    with open(args.report) as f:
        report = json.load(f)

    html = build_html(report)
    with open(args.out, "w") as f:
        f.write(html)

    print(f"Wrote {args.out} — open it in a browser.")


if __name__ == "__main__":
    main()
