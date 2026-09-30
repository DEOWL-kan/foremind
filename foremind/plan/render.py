"""Read-only views of a plan (DESIGN §4.2 S8): a terminal summary and a single-file HTML page.

The HTML has no external resources: inline CSS, the dependency graph as inline SVG (column = wave), a <details>
per batch. It is generated from the plan files and is never the source of truth.
"""
import json
from html import escape

from foremind.plan.model import TERMINAL

_MODES = {"auto": "自动", "watch": "盯着", "accompany": "陪同", "user": "我来做"}


def _waves(report):
    return {b: i + 1 for i, w in enumerate(report["waves"]) for b in w}


def _reasons(report):
    out = {}
    for r in report["serial_reasons"]:
        out.setdefault(r["batch"], []).append(f"{r['depends_on']}（{r['reason']}）")
    return out


def _sig(x):
    return "未知" if x is None else str(x)


def summary(plan, report) -> str:
    wave, reasons = _waves(report), _reasons(report)
    cp = report["critical_path"]
    lines = [f"计划 {plan.id}：{'已批准' if 'approved_at' in plan.doc.header else '未批准'}；"
             f"关键路径 {len(cp)} 批（{' → '.join(cp)}）；最大并行宽度 {report['max_width']}",
             "批次 | 波次 | 档 | 参与方式 | 状态 | 串行原因"]
    for b, d in plan.batches.items():
        h = d.header
        lines.append(" | ".join([b, str(wave.get(b, "-")), h["tiers"]["difficulty"], _MODES[h["mode"]],
                                 h.get("state", "草稿"), "；".join(reasons.get(b, [])) or "-"]))
    lines += [f"耦合：{c['a']} / {c['b']}：引用 {_sig(c['ref'])} · 共改 {_sig(c['cochange'])} · 语义 {c['semantic']}"
              f" → C={c['score']:.2f} {c['tier']}" + ("（同改契约）" if c["contract"] else "") for c in report["coupling"]]
    lines += [f"错误：{e}" for e in report["errors"]] + [f"提示：{w}" for w in report["warnings"]]
    lines += [f"重叠：{o['a']} / {o['b']} → 补边 {o['b']} depends_on {o['a']}" for o in report["overlaps"]]
    return "\n".join(lines) + "\n"


def rejected(report) -> str:
    lines = [f"错误：{e}" for e in report["errors"]]
    lines += [f"重叠：{o['a']} / {o['b']} 并行且 owns_paths 重叠：{o['paths']}" for o in report["overlaps"]]
    return "S6 未通过\n" + "\n".join(lines) + "\n"


_CSS ="""body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2em;color:#222}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:2px 8px;text-align:left}
.node rect{fill:#f4f6fa;stroke:#789}.crit rect{fill:#fff4e0;stroke:#d80;stroke-width:2}
line{stroke:#999;marker-end:url(#arrow)}line.crit{stroke:#d80;stroke-width:2}
details{margin:.4em 0;border:1px solid #ddd;padding:.3em .6em}details:target{border-color:#d80}
pre{white-space:pre-wrap;background:#f7f7f7;padding:.5em}.err{color:#b00}"""

_W, _H, _DX, _DY = 150, 36, 200, 56


def _svg(plan, report):
    pos, crit = {}, report["critical_path"]
    crit_edges = set(zip(crit, crit[1:]))
    for x, w in enumerate(report["waves"]):
        for y, b in enumerate(w):
            pos[b] = (20 + x * _DX, 20 + y * _DY)
    width = 40 + max(len(report["waves"]) - 1, 0) * _DX + _W
    height = 40 + max((len(w) for w in report["waves"]), default=1) * _DY
    out = [f'<svg width="{width}" height="{height}" role="img" '
           'aria-label="依赖图"><defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" '
           'markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="#999"/></marker></defs>']
    for r in report["serial_reasons"]:
        a, b = r["depends_on"], r["batch"]
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            cls = ' class="crit"' if (a, b) in crit_edges else ""
            out.append(f'<line{cls} x1="{x1 + _W}" y1="{y1 + _H // 2}" x2="{x2}" y2="{y2 + _H // 2}"/>')
    for b, (x, y) in pos.items():
        cls = "node crit" if b in crit else "node"
        out.append(f'<a href="#b-{escape(b)}" class="{cls}"><rect x="{x}" y="{y}" width="{_W}" height="{_H}" rx="4"/>'
                   f'<text x="{x + 8}" y="{y + 23}">{escape(b)}</text></a>')
    return "".join(out) + "</svg>"


def _field(v):
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        v = ", ".join(v)
    return escape(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))


def html(plan, report) -> str:
    wave, reasons = _waves(report), _reasons(report)
    h = plan.doc.header
    parts = [f'<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>计划 {escape(plan.id)}</title>'
             f"<style>{_CSS}</style></head><body><h1>计划 {escape(plan.id)}</h1>",
             f"<p>{'已批准 ' + escape(h['approved_at']) if 'approved_at' in h else '未批准'} · "
             f"目标哈希 <code>{escape(h['goal_hash'][:12])}</code> · 关键路径 {report['critical_path_length']} 批 · "
             f"最大并行宽度 {report['max_width']}</p>"]
    parts += [f'<p class="err">错误：{escape(e)}</p>' for e in report["errors"]]
    parts += [f"<p>提示：{escape(w)}</p>" for w in report["warnings"]]
    parts += ["<h2>依赖图</h2>", _svg(plan, report), "<h2>批次</h2><table><tr><th>批次</th><th>波次</th><th>档</th>"
              "<th>参与方式</th><th>状态</th><th>串行原因</th></tr>"]
    for b, d in plan.batches.items():
        bh = d.header
        parts.append(f'<tr><td><a href="#b-{escape(b)}">{escape(b)}</a></td><td>{wave.get(b, "-")}</td>'
                     f"<td>{escape(bh['tiers']['difficulty'])}</td><td>{_MODES[bh['mode']]}</td>"
                     f"<td>{escape(bh.get('state', '草稿'))}</td><td>{escape('；'.join(reasons.get(b, [])) or '-')}</td></tr>")
    parts.append("</table>")
    if report["coupling"]:
        parts.append("<h2>耦合</h2><table><tr><th>批次对</th><th>引用</th><th>共改</th><th>语义</th><th>C</th><th>档</th></tr>")
        parts += [f"<tr><td>{escape(c['a'])} / {escape(c['b'])}</td><td>{_sig(c['ref'])}</td>"
                  f"<td>{_sig(c['cochange'])}</td>"
                  f"<td>{c['semantic']}</td><td>{c['score']:.2f}</td><td>{escape(c['tier'])}</td></tr>"
                  for c in report["coupling"]]
        parts.append("</table>")
    parts.append("<h2>批次详情</h2>")
    for b, d in plan.batches.items():
        muted = " (已结束)" if d.header.get("state") in TERMINAL else ""
        rows = "".join(f"<tr><th>{escape(k)}</th><td>{_field(v)}</td></tr>" for k, v in d.header.items())
        parts.append(f'<details id="b-{escape(b)}"><summary>{escape(b)}{muted} · {_field(d.header["reqs"])}</summary>'
                     f"<table>{rows}</table><pre>{escape(d.body)}</pre></details>")
    if plan.goal is not None:
        parts.append(f"<h2>目标</h2><pre>{escape(plan.goal.body)}</pre>")
    parts.append(f"<h2>计划说明</h2><pre>{escape(plan.doc.body)}</pre></body></html>\n")
    return "".join(parts)
