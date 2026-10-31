"""As-run report of a test campaign: markdown and PDF with plateau results,
deviations, hold-point decisions, a temperature chart and the signed log hash."""

from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import QMarginsF, QUrl
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

import app.common as common
from app.services import campaign_service as cs
from app.services import run_export

PLATEAU_COLORS = {"hot": "#dc2626", "cold": "#2563eb"}


class ReportError(ValueError):
    pass


def local_time(iso_text):
    if not iso_text:
        return ""
    return datetime.fromisoformat(iso_text).astimezone().strftime("%Y-%m-%d %H:%M")


def facts(campaign):
    """Numbers of the run the report summary may use."""
    spec = campaign["spec"]
    return {
        "test_article": spec["article"],
        "level": spec["level"],
        "rehearsal": spec["rehearsal"],
        "planned_cycles": spec["cycles"],
        "hot_plateau_c": spec["hot_c"],
        "cold_plateau_c": spec["cold_c"],
        "dwell_min": spec["dwell_min"],
        "state": campaign["state"],
        "end_reason": campaign.get("end_reason"),
        "plateaus_completed": [
            {
                "cycle": r["cycle"],
                "plateau": r["plateau"],
                "dwell_min": r["dwell_min"],
                "range_c": [r["min_c"], r["max_c"]],
                "within_band": r["within_band"],
            }
            for r in campaign["results"] or []
        ],
        "deviations": campaign["deviations"] or [],
        "operator_decisions": cs.decisions(campaign["id"]),
        "blocked_commands": [
            {"actor": b["actor"], "command": b["command"]} for b in campaign["blocked"] or []
        ],
        "overrides": spec["overrides"],
    }


def template_summary(campaign):
    spec = campaign["spec"]
    results = campaign["results"] or []
    cycles_done = sum(1 for r in results if r["plateau"] == "cold")
    text = (
        f"The {spec['article']} completed {cycles_done} of {spec['cycles']} planned thermal cycles between "
        f"{cs.deg(spec['cold_c'])} and {cs.deg(spec['hot_c'])}"
        + (
            " before the campaign was aborted. "
            if campaign["state"] == cs.ABORTED
            else ", every plateau held for its dwell. "
        )
    )
    deviations = campaign["deviations"] or []
    if deviations:
        text += f"{len(deviations)} deviation{'s were' if len(deviations) > 1 else ' was'} recorded; the first: {deviations[0]['text']} "
    else:
        text += "No deviations were recorded. "
    blocked = campaign["blocked"] or []
    if blocked:
        text += f"The gateway blocked {len(blocked)} command{'s' if len(blocked) > 1 else ''}. "
    text += "Review the plateau results and the decisions below before signing."
    return text


def results_rows(campaign):
    spec = campaign["spec"]
    return [
        (
            str(r["cycle"]),
            r["plateau"],
            cs.deg(r["target_c"]),
            local_time(r["stabilised_at"]),
            f"{r['dwell_min']:g} min",
            f"{r['min_c']:.1f} to {r['max_c']:.1f} °C",
            ("yes" if r["within_band"] else f"no (±{spec['band_c']:g} °C)")
            + ("; dwell forced" if r.get("forced") else ""),
        )
        for r in campaign["results"] or []
    ]


def evidence(campaign):
    check = cs.verify_chain(campaign["id"])
    out = {"entries": check["entries"], "ok": check["ok"]}
    if check["ok"]:
        out["last_hash"] = check["last_hash"]
        try:
            out["signature"], out["public_key"] = cs.sign(check["last_hash"])
        except Exception as exc:
            out["signature_error"] = str(exc)
    else:
        out["broken_at"] = check["broken_at"]
    return out


def markdown(campaign, ev):
    spec = campaign["spec"]
    lines = ["# Thermal test campaign report (as-run)", ""]
    lines.append(
        f"Campaign {campaign['id']} on {campaign['chamber_name']}, DeepVac Insight. "
        + (
            "**Rehearsal: shortened values, not a qualification record.** "
            if spec["rehearsal"]
            else ""
        )
        + f"Standards pack: {spec['pack']['name']}. {spec['pack']['note']}"
    )
    lines += [
        "",
        f"Started: {local_time(campaign.get('started_at'))}  ",
        f"Ended: {local_time(campaign.get('ended_at'))} ({campaign.get('end_reason') or campaign['state']})",
        "",
    ]
    lines += ["## Summary", "", campaign.get("summary") or "", ""]
    lines += [f"_{campaign.get('summary_source') or ''}_", ""]
    lines += [
        "## Specification (as approved)",
        "",
        "| Item | Value | Source |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {i} | {v} | {s} |" for i, v, s in cs.spec_rows(spec)]
    lines += ["", "## As-run results", ""]
    rows = results_rows(campaign)
    if rows:
        lines += [
            "| Cycle | Plateau | Target | Stabilised | Dwell | Range | Within band |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += ["| " + " | ".join(r) + " |" for r in rows]
    else:
        lines.append("- No plateau completed.")
    lines += ["", "## Deviations", ""]
    deviations = campaign["deviations"] or []
    lines += [
        f"- {local_time(d['time'])}, {d['where']}: {d['text']}"
        + (f" Decision: {d['decision']}." if d.get("decision") else "")
        for d in deviations
    ] or ["- None recorded."]
    lines += ["", "## Operator decisions at hold points", ""]
    lines += [
        f"- {local_time(d['time'])}, {d['hold']} **{d['choice']}** by {d['by']}"
        + (f" ({d['comment']})" if d["comment"] else "")
        + (f"; recommended: {d['recommended']}" if d["recommended"] else "")
        for d in cs.decisions(campaign["id"])
    ] or ["- None."]
    lines += ["", "## Commands blocked by the gateway", ""]
    lines += [
        f"- {local_time(b['time'])}: {b['actor']} asked to {b['command']}. {' '.join(b['reasons'])}"
        for b in campaign["blocked"] or []
    ] or ["- None."]
    lines += ["", "## Evidence", "", f"Log entries: {ev['entries']}  "]
    if ev["ok"]:
        lines.append("Chain check: intact  ")
        lines.append(f"Last hash (SHA-256): `{ev['last_hash']}`  ")
        if ev.get("signature"):
            lines.append(f"Signature (Ed25519, device key): `{ev['signature']}`  ")
            lines.append(f"Public key: `{ev['public_key']}`")
    else:
        lines.append(f"Chain check: **broken at entry {ev['broken_at']}**")
    lines += ["", "## Sign-off", ""]
    if campaign.get("signed_off_by"):
        lines.append(
            f"Signed off by {campaign['signed_off_by']} on {local_time(campaign['signed_off_at'])}."
        )
    else:
        lines.append("Not signed off yet.")
    lines.append("")
    return "\n".join(lines)


def chart(campaign):
    """Temperature and setpoint chart with dwells shaded; None without a trace."""
    points = cs.trace(campaign["id"])
    if len(points) < 2:
        return None
    t0 = points[0]["t"]
    xs = [p["t"] - t0 for p in points]
    series = [
        run_export.Series("Setpoint", "#94a3b8", xs, [p["setpoint"] for p in points], 1.5),
        run_export.Series("Temperature", "#dc2626", xs, [p["temp"] for p in points]),
    ]
    regions = []
    for r in campaign["results"] or []:
        start = cs.epoch(r["stabilised_at"]) - t0
        regions.append(
            run_export.Region(
                start,
                start + r["dwell_min"] * 60.0,
                f"C{r['cycle']} {r['plateau']}",
                PLATEAU_COLORS.get(r["plateau"], "#16a34a"),
            )
        )
    try:
        return run_export.render_chart(series, regions, title="Chamber temperature")
    except run_export.ExportError:
        return None


def _table(headers, rows):
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table border='1' cellspacing='0' cellpadding='4'><tr>{head}</tr>{body}</table>"


def html(campaign, ev, with_chart):
    spec = campaign["spec"]
    parts = [
        "<html><body style='font-family: Segoe UI, Arial; font-size: 9pt'>",
        "<h1>Thermal test campaign report (as-run)</h1>",
        f"<p>Campaign {campaign['id']} on {escape(campaign['chamber_name'])}, DeepVac Insight. "
        + (
            "<b>Rehearsal: shortened values, not a qualification record.</b> "
            if spec["rehearsal"]
            else ""
        )
        + f"Standards pack: {escape(spec['pack']['name'])}. <i>{escape(spec['pack']['note'])}</i></p>",
        f"<p>Started {local_time(campaign.get('started_at'))}; ended {local_time(campaign.get('ended_at'))} "
        f"({escape(campaign.get('end_reason') or campaign['state'])}).</p>",
        "<h2>Summary</h2>",
        f"<p>{escape(campaign.get('summary') or '')}</p>",
        f"<p><i>{escape(campaign.get('summary_source') or '')}</i></p>",
        "<h2>Specification (as approved)</h2>",
        _table(("Item", "Value", "Source"), cs.spec_rows(spec)),
        "<h2>As-run results</h2>",
    ]
    rows = results_rows(campaign)
    parts.append(
        _table(("Cycle", "Plateau", "Target", "Stabilised", "Dwell", "Range", "Within band"), rows)
        if rows
        else "<p>No plateau completed.</p>"
    )
    if with_chart:
        parts.append("<p><img src='chart.png' width='640'></p>")
    deviations = campaign["deviations"] or []
    parts.append("<h2>Deviations</h2>")
    parts.append(
        _table(
            ("Time", "Where", "What", "Decision"),
            [
                (local_time(d["time"]), d["where"], d["text"], d.get("decision", ""))
                for d in deviations
            ],
        )
        if deviations
        else "<p>None recorded.</p>"
    )
    decisions = cs.decisions(campaign["id"])
    parts.append("<h2>Operator decisions at hold points</h2>")
    parts.append(
        _table(
            ("Time", "Hold point", "Choice", "By", "Recommended"),
            [
                (
                    local_time(d["time"]),
                    d["hold"],
                    d["choice"] + (f" ({d['comment']})" if d["comment"] else ""),
                    d["by"],
                    d["recommended"],
                )
                for d in decisions
            ],
        )
        if decisions
        else "<p>None.</p>"
    )
    blocked = campaign["blocked"] or []
    parts.append("<h2>Commands blocked by the gateway</h2>")
    parts.append(
        _table(
            ("Time", "Actor", "Command", "Reasons"),
            [
                (local_time(b["time"]), b["actor"], b["command"], " ".join(b["reasons"]))
                for b in blocked
            ],
        )
        if blocked
        else "<p>None.</p>"
    )
    parts.append("<h2>Evidence</h2>")
    if ev["ok"]:
        parts.append(
            f"<p>Log entries: {ev['entries']}. Chain check: intact.<br>"
            f"Last hash (SHA-256): <code style='font-family: Consolas, monospace; font-size: 7pt'>{ev['last_hash']}</code>"
            + (
                f"<br>Signature (Ed25519, device key): <code style='font-family: Consolas, monospace; font-size: 7pt'>{ev['signature']}</code>"
                f"<br>Public key: <code style='font-family: Consolas, monospace; font-size: 7pt'>{ev['public_key']}</code>"
                if ev.get("signature")
                else ""
            )
            + "</p>"
        )
    else:
        parts.append(f"<p><b>Chain check: broken at entry {ev['broken_at']}.</b></p>")
    parts.append("<h2>Sign-off</h2>")
    parts.append(
        f"<p>Signed off by {escape(campaign['signed_off_by'])} on {local_time(campaign['signed_off_at'])}.</p>"
        if campaign.get("signed_off_by")
        else "<p>Not signed off yet.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts)


def _paths(campaign):
    files = campaign.get("report_files") or {}
    if files.get("pdf"):
        return Path(files["pdf"]), Path(files["markdown"])
    stem = f"campaign{campaign['id']}_{run_export.safe_name(campaign['title'])}_report_{datetime.now():%Y%m%d_%H%M%S}"
    return common.REPORTS_DIR / f"{stem}.pdf", common.REPORTS_DIR / f"{stem}.md"


def write(campaign):
    """Writes the report files of an ended campaign; returns their paths and links."""
    ev = evidence(campaign)
    pdf_path, md_path = _paths(campaign)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown(campaign, ev), encoding="utf-8")
    image = chart(campaign)
    with run_export._paint_lock:
        document = QTextDocument()
        if image is not None:
            document.addResource(QTextDocument.ImageResource, QUrl("chart.png"), image)
        document.setHtml(html(campaign, ev, image is not None))
        writer = QPdfWriter(str(pdf_path))
        writer.setPageLayout(
            QPageLayout(
                QPageSize(QPageSize.A4),
                QPageLayout.Portrait,
                QMarginsF(14, 14, 14, 14),
                QPageLayout.Millimeter,
            )
        )
        writer.setTitle(f"Campaign {campaign['id']}: {campaign['title']}")
        writer.setCreator("DeepVac Insight")
        document.print_(writer)
        del writer
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise ReportError(f"Could not write {pdf_path}.")
    return {
        "pdf": str(pdf_path),
        "markdown": str(md_path),
        "pdf_link": pdf_path.resolve().as_uri(),
        "markdown_link": md_path.resolve().as_uri(),
        "chain_ok": ev["ok"],
        "last_hash": ev.get("last_hash"),
        "signed": bool(ev.get("signature")),
    }


def issue(campaign_id, summary="", by="Reporter"):
    """Issues the report of an ended campaign with the agent's summary or a template's."""
    campaign = cs.require(campaign_id)
    if campaign["state"] not in cs.ENDED:
        raise ReportError(
            f"Campaign {campaign_id} is {campaign['state']}: a report is issued once it has completed or "
            "been aborted."
        )
    if campaign.get("signed_off_by"):
        raise ReportError(f"The report of campaign {campaign_id} is already signed off.")
    summary = (summary or "").strip()
    if summary:
        source = f"Drafted by {by} from the campaign data. Review before signing."
    else:
        summary, source = template_summary(campaign), "Written from a template."
    cs.update(campaign_id, summary=summary, summary_source=source, report_issued_at=cs.now_iso())
    cs.record(
        campaign_id,
        by,
        "Report issued",
        f"{len(campaign['results'] or [])} plateaus, {len(campaign['deviations'] or [])} deviation(s)",
    )
    files = write(cs.require(campaign_id))
    cs.update(campaign_id, report_files=files)
    return files


def sign_off(campaign_id, user_name):
    """Signs off the issued report and writes it again."""
    if not user_name:
        raise ReportError("Only a signed-in user can sign a report off.")
    campaign = cs.require(campaign_id)
    if not campaign.get("report_issued_at"):
        raise ReportError("Issue the report first.")
    if campaign.get("signed_off_by"):
        raise ReportError(f"Already signed off by {campaign['signed_off_by']}.")
    cs.update(campaign_id, signed_off_by=user_name, signed_off_at=cs.now_iso())
    cs.record(campaign_id, user_name, "Report signed off", "")
    files = write(cs.require(campaign_id))
    cs.update(campaign_id, report_files=files)
    return files
