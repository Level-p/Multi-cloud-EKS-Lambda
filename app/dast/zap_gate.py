#!/usr/bin/env python3
"""DAST gate: decide pass/fail from an OWASP ZAP JSON report (traditional-json template).

    python app/dast/zap_gate.py app/dast/reports/zap-report.json --policy app/dast/zap-policy.json \
        [--fail-on medium] [--summary "$GITHUB_STEP_SUMMARY"] [--junit app/dast/reports/zap-junit.xml]

Rules
  * An alert at or above --fail-on (default: the policy's "fail_on", else "medium") fails the
    build, unless an exception in the policy covers it.
  * An exception names a ZAP rule (plugin_id), optionally a URL regex, a reason, an owner and an
    expiry date. After the expiry date it no longer applies: the finding fails again until
    someone reviews it. That keeps "accepted risk" from becoming "forgotten risk".
  * Alerts ZAP marked as false positives (confidence 0) are ignored.
  * Exceptions that matched nothing are listed so they can be removed.

Exit codes: 0 pass, 1 findings fail the gate, 2 the report or the policy is missing or invalid.
Standard library only, so it runs on any CI runner (GitHub Actions, GitLab, Jenkins) without installs.
"""
import argparse
import datetime as dt
import html
import json
import re
import sys
import xml.etree.ElementTree as ET

RISKS = {"informational": 0, "info": 0, "low": 1, "medium": 2, "high": 3}
RISK_NAMES = {0: "Informational", 1: "Low", 2: "Medium", 3: "High"}


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"zap_gate: cannot read {what} {path}: {err}") from None


def parse_alerts(report):
    """Flatten site -> alerts into one list of dicts (one per ZAP rule per site)."""
    if not isinstance(report, dict) or "site" not in report:
        raise SystemExit("zap_gate: not a ZAP traditional-json report (no 'site' key)")
    alerts = []
    for site in report.get("site") or []:
        for a in site.get("alerts") or []:
            alerts.append({
                "site": site.get("@name", ""),
                "plugin_id": str(a.get("pluginid", "")),
                "name": a.get("alert") or a.get("name") or "",
                "risk": int(a.get("riskcode", 0)),
                "confidence": int(a.get("confidence", 1)),
                "cwe": str(a.get("cweid", "")),
                "solution": strip_html(a.get("solution", "")),
                "uris": sorted({i.get("uri", "") for i in a.get("instances") or []}),
                "count": int(a.get("count") or len(a.get("instances") or [])),
            })
    return alerts


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def validate_policy(policy, today):
    problems = []
    for i, ex in enumerate(policy.get("exceptions", [])):
        for key in ("plugin_id", "reason", "owner", "expires"):
            if not str(ex.get(key, "")).strip():
                problems.append(f"exception #{i + 1}: '{key}' is required")
        try:
            expires = dt.date.fromisoformat(str(ex.get("expires", "")))
            if (expires - today).days > 180:
                problems.append(f"exception #{i + 1} ({ex.get('plugin_id')}): expires more than 180 days ahead; review at least twice a year")
        except ValueError:
            problems.append(f"exception #{i + 1}: 'expires' must be YYYY-MM-DD")
        if ex.get("uri_regex"):
            try:
                re.compile(ex["uri_regex"])
            except re.error as err:
                problems.append(f"exception #{i + 1}: bad uri_regex: {err}")
    return problems


def matching_exception(alert, exceptions):
    for ex in exceptions:
        if str(ex["plugin_id"]) != alert["plugin_id"]:
            continue
        rx = ex.get("uri_regex")
        if not rx or all(re.search(rx, u) for u in alert["uris"] or [""]):
            return ex
    return None


def evaluate(alerts, policy, fail_on, today):
    exceptions = policy.get("exceptions", [])
    used = set()
    result = {"failed": [], "accepted": [], "expired": [], "below": []}
    for a in alerts:
        if a["confidence"] == 0:
            continue
        if a["risk"] < fail_on:
            result["below"].append(a)
            continue
        ex = matching_exception(a, exceptions)
        if ex is None:
            result["failed"].append(a)
            continue
        used.add(id(ex))
        if dt.date.fromisoformat(ex["expires"]) < today:
            result["expired"].append((a, ex))
        else:
            result["accepted"].append((a, ex))
    result["unused"] = [ex for ex in exceptions if id(ex) not in used]
    return result


def markdown(result, fail_on, title):
    ok = not result["failed"] and not result["expired"]
    lines = [f"### {title}: {'PASSED' if ok else 'FAILED'}", "",
             f"Gate: findings at **{RISK_NAMES[fail_on]}** risk or above fail the build.", ""]

    def table(rows, extra_header=None):
        head = "| Risk | ZAP rule | Alert | CWE | URLs |" + (f" {extra_header} |" if extra_header else "")
        sep = "|---|---|---|---|---|" + ("---|" if extra_header else "")
        out = [head, sep]
        for row in rows:
            a, ex = row if isinstance(row, tuple) else (row, None)
            urls = ", ".join(a["uris"][:3]) + (f" (+{len(a['uris']) - 3})" if len(a["uris"]) > 3 else "")
            cell = f" {ex['reason']} (owner {ex['owner']}, until {ex['expires']}) |" if ex else ""
            out.append(f"| {RISK_NAMES[a['risk']]} | {a['plugin_id']} | {a['name']} | {a['cwe']} | {urls} |{cell}")
        return out + [""]

    if result["failed"]:
        lines += ["#### Failing findings (fix, or add a reviewed exception to the policy)"] + table(result["failed"])
    if result["expired"]:
        lines += ["#### Expired exceptions (the finding is back: review it)"] + table(result["expired"], "Exception")
    if result["accepted"]:
        lines += ["#### Accepted risk (reviewed exceptions)"] + table(result["accepted"], "Exception")
    if result["below"]:
        lines += [f"#### Below the gate ({len(result['below'])}; see the HTML report)"] + table(
            sorted(result["below"], key=lambda a: -a["risk"])[:15])
    if result["unused"]:
        lines += ["#### Unused exceptions (remove them from the policy)", ""] + [
            f"- {ex['plugin_id']}: {ex['reason']}" for ex in result["unused"]] + [""]
    return "\n".join(lines), ok


def junit(result, path):
    """One test case per ZAP rule found, so any JUnit viewer (GitHub test reporters, GitLab, Jenkins) can show DAST results."""
    suite = ET.Element("testsuite", name="OWASP ZAP DAST")
    cases = [(a, None, "failed") for a in result["failed"]] + [(a, ex, "expired") for a, ex in result["expired"]] + \
            [(a, ex, "accepted") for a, ex in result["accepted"]] + [(a, None, "below") for a in result["below"]]
    for a, ex, state in cases:
        tc = ET.SubElement(suite, "testcase", classname=f"zap.{RISK_NAMES[a['risk']].lower()}",
                           name=f"{a['plugin_id']} {a['name']}")
        detail = "\n".join([f"Risk: {RISK_NAMES[a['risk']]}  CWE: {a['cwe']}", "URLs:"] + [f"  {u}" for u in a["uris"]] +
                           ["Solution: " + a["solution"]])
        if state in ("failed", "expired"):
            msg = "exception expired " + ex["expires"] if state == "expired" else f"{RISK_NAMES[a['risk']]} risk finding"
            ET.SubElement(tc, "failure", message=msg).text = detail
        elif state == "accepted":
            ET.SubElement(tc, "skipped", message=f"accepted until {ex['expires']}: {ex['reason']}")
        ET.SubElement(tc, "system-out").text = detail
    suite.set("tests", str(len(cases)))
    suite.set("failures", str(len(result["failed"]) + len(result["expired"])))
    suite.set("skipped", str(len(result["accepted"])))
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("report")
    p.add_argument("--policy", required=True)
    p.add_argument("--fail-on", choices=["low", "medium", "high"])
    p.add_argument("--summary", help="append the Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    p.add_argument("--junit", help="write JUnit XML here")
    p.add_argument("--title", default="OWASP ZAP DAST")
    p.add_argument("--today", help=argparse.SUPPRESS)  # tests
    args = p.parse_args(argv)

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    try:
        policy = load_json(args.policy, "policy")
        alerts = parse_alerts(load_json(args.report, "report"))
    except SystemExit as err:
        print(err, file=sys.stderr)
        return 2
    problems = validate_policy(policy, today)
    if problems:
        print("zap_gate: invalid policy:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    fail_on = RISKS[args.fail_on or policy.get("fail_on", "medium")]

    result = evaluate(alerts, policy, fail_on, today)
    text, ok = markdown(result, fail_on, args.title)
    print(text)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if args.junit:
        junit(result, args.junit)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
