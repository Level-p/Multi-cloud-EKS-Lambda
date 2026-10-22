#!/usr/bin/env python3
"""Preflight for a Terraform plan: catch the errors only AWS reports, BEFORE the apply.

`terraform plan` checks types, not values; AWS checks values during the apply, after other resources were
already created (a partial apply). This step reads the plan (`terraform show -json tfplan`) and asks AWS's own
read-only APIs the same questions the apply would:

  db-parameters    every RDS parameter: exists in the family, modifiable, value allowed, apply method fits
                   (describe-engine-default-parameters)
  sg-description   security group and rule descriptions: EC2's character set, < 256 characters
  event-pattern    EventBridge patterns: lists of values only (no nested lists), then events test-event-pattern
                   with a sample event built from the pattern; the pattern must match it
  name-taken       a resource Terraform will CREATE with a fixed name already exists in AWS (left by a failed
                   apply, or made by hand): prints the terraform import command

Usage: tf_preflight.py tfplan.json     (AWS credentials and AWS_REGION in the environment)
Exit 1 with ::error annotations and a table on the run's summary page when something would fail.
"""

import json
import os
import re
import subprocess
import sys

SG_TYPES = {"aws_security_group", "aws_security_group_rule",
            "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule"}
SG_DESCRIPTION = re.compile(r"^[a-zA-Z0-9. _\-:/()#,@\[\]+=&;{}!$*]*$")
RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)$")


def aws(args, region=None):
    """Runs the AWS CLI: (exit code, parsed JSON or None, stderr)."""
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    p = subprocess.run(cmd, capture_output=True, text=True)
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = None
    return p.returncode, out, p.stderr.strip()


def changes(plan):
    """(address, type, after values, actions) of every resource the plan creates or updates."""
    for rc in plan.get("resource_changes", []):
        actions = rc["change"]["actions"]
        if rc.get("mode") == "managed" and ("create" in actions or "update" in actions):
            yield rc["address"], rc["type"], rc["change"].get("after") or {}, actions


# ---------------------------------------------------------------------------------------------------------
# RDS parameters
# ---------------------------------------------------------------------------------------------------------
def value_allowed(value, allowed, data_type):
    """None if the value is allowed, else why not. Formulas ({DBInstanceClassMemory...}) and regex lists are
    left to AWS (they cannot be checked offline)."""
    if not allowed or "{" in value or allowed.startswith("^"):
        return None
    allowed = allowed.strip()
    m = RANGE.match(allowed)
    if m:
        try:
            n = float(value)
        except ValueError:
            return f"{value!r} is not a number (allowed {allowed})"
        return None if float(m.group(1)) <= n <= float(m.group(2)) else f"{value} is outside {allowed}"
    options = {o.strip() for o in allowed.split(",")}
    items = [v.strip() for v in value.split(",")] if data_type == "list" else [value.strip()]
    bad = [v for v in items if v not in options]
    return f"{', '.join(bad)} not in: {allowed}" if bad else None


def check_db_parameters(plan, run=aws):
    errors, defaults = [], {}
    for addr, rtype, after, _ in changes(plan):
        if rtype not in ("aws_db_parameter_group", "aws_rds_cluster_parameter_group") or not after.get("family"):
            continue
        family, region = after["family"], after.get("region")
        if family not in defaults:
            cmd = ("describe-engine-default-cluster-parameters" if rtype == "aws_rds_cluster_parameter_group"
                   else "describe-engine-default-parameters")
            code, out, err = run(["rds", cmd, "--db-parameter-group-family", family], region)
            if code != 0:
                errors.append((addr, "db-parameters", f"cannot read the defaults of {family}: {err}"))
                continue
            params = (out.get("EngineDefaults") or out.get("DBClusterEngineDefaults") or {}).get("Parameters", [])
            defaults[family] = {p["ParameterName"]: p for p in params}
        known = defaults.get(family)
        if known is None:
            continue
        for p in after.get("parameter") or []:
            name, value = p["name"], str(p["value"])
            d = known.get(name)
            if d is None:
                errors.append((addr, "db-parameters", f"{name}: no such parameter in {family}"))
                continue
            if not d.get("IsModifiable", True):
                errors.append((addr, "db-parameters", f"{name}: not modifiable"))
            if p.get("apply_method", "immediate") == "immediate" and d.get("ApplyType") == "static":
                errors.append((addr, "db-parameters", f"{name}: static parameter, needs apply_method = \"pending-reboot\""))
            why = value_allowed(value, d.get("AllowedValues"), d.get("DataType"))
            if why:
                errors.append((addr, "db-parameters", f"{name} = {value!r}: {why}"))
    return errors


# ---------------------------------------------------------------------------------------------------------
# Security group descriptions
# ---------------------------------------------------------------------------------------------------------
def check_sg_descriptions(plan, run=None):
    errors = []
    for addr, rtype, after, _ in changes(plan):
        if rtype not in SG_TYPES:
            continue
        texts = [after.get("description")]
        for block in (after.get("ingress") or []) + (after.get("egress") or []):   # inline rules
            texts.append(block.get("description"))
        for t in texts:
            if isinstance(t, str) and (len(t) >= 256 or not SG_DESCRIPTION.match(t)):
                bad = sorted({c for c in t if not SG_DESCRIPTION.match(c)})
                errors.append((addr, "sg-description", f"{t!r}: EC2 allows only a-zA-Z0-9 . _-:/()#,@[]+=&;{{}}!$* and "
                                                       f"spaces, < 256 characters" + (f" (found {' '.join(bad)})" if bad else "")))
    return errors


# ---------------------------------------------------------------------------------------------------------
# EventBridge patterns
# ---------------------------------------------------------------------------------------------------------
def pattern_shape(node, path="pattern"):
    """Structural errors: every leaf is a list of values or filter objects; a list never holds a list."""
    if isinstance(node, dict):
        return [e for k, v in node.items() for e in pattern_shape(v, f"{path}.{k}")]
    if not isinstance(node, list):
        return [f"{path}: must be a list of values, got {json.dumps(node)}"]
    if not node:
        return [f"{path}: empty list matches nothing"]
    return [f"{path}: a list holds values, not lists ({json.dumps(x)})" for x in node if isinstance(x, list)]


def sample_value(options):
    """A value the list of options matches, or None if only content filters we cannot satisfy are present."""
    for o in options:
        if not isinstance(o, dict):
            return o
        if "prefix" in o and isinstance(o["prefix"], str):
            return o["prefix"] + "sample"
        if "suffix" in o and isinstance(o["suffix"], str):
            return "sample" + o["suffix"]
        if "equals-ignore-case" in o:
            return o["equals-ignore-case"]
        if "wildcard" in o:
            return o["wildcard"].replace("*", "x")
        if o.get("exists") is True:
            return "sample"
    return None


def sample_event(pattern):
    def build(node):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                val = build(v)
                if val is None:
                    return None
                out[k] = val
            return out
        return sample_value(node)

    body = build({k: v for k, v in pattern.items() if k not in ("source", "detail-type", "account", "region")})
    if body is None:
        return None
    src, dtype = sample_value(pattern.get("source", ["sample.source"])), sample_value(pattern.get("detail-type", ["Sample"]))
    if src is None or dtype is None:
        return None
    return {"id": "00000000-0000-0000-0000-000000000000", "account": "123456789012", "time": "2026-01-01T00:00:00Z",
            "region": "eu-west-2", "resources": [], "source": src, "detail-type": dtype, **({"detail": {}} | body)}


def check_event_patterns(plan, run=aws):
    errors = []
    for addr, rtype, after, _ in changes(plan):
        raw = after.get("event_pattern") if rtype == "aws_cloudwatch_event_rule" else None
        if not isinstance(raw, str):
            continue
        try:
            pattern = json.loads(raw)
        except ValueError as e:
            errors.append((addr, "event-pattern", f"not JSON: {e}"))
            continue
        shape = pattern_shape(pattern) if isinstance(pattern, dict) else ["pattern: must be a JSON object"]
        if shape:
            errors += [(addr, "event-pattern", s) for s in shape]
            continue
        event = sample_event(pattern)
        code, out, err = run(["events", "test-event-pattern", "--event-pattern", raw,
                              "--event", json.dumps(event or sample_event({}))], after.get("region"))
        if code != 0:
            errors.append((addr, "event-pattern", f"rejected by EventBridge: {err}"))
        elif event is not None and not (out or {}).get("Result"):
            errors.append((addr, "event-pattern", f"does not match its own sample event {json.dumps(event)}"))
    return errors


# ---------------------------------------------------------------------------------------------------------
# Fixed names that already exist in AWS but not in the state
# ---------------------------------------------------------------------------------------------------------
# type -> (attribute holding the name, CLI arguments to look it up)
LOOKUPS = {
    "aws_db_parameter_group": ("name", lambda n: ["rds", "describe-db-parameter-groups", "--db-parameter-group-name", n]),
    "aws_db_subnet_group": ("name", lambda n: ["rds", "describe-db-subnet-groups", "--db-subnet-group-name", n]),
    "aws_db_instance": ("identifier", lambda n: ["rds", "describe-db-instances", "--db-instance-identifier", n]),
    "aws_iam_role": ("name", lambda n: ["iam", "get-role", "--role-name", n]),
    "aws_eks_cluster": ("name", lambda n: ["eks", "describe-cluster", "--name", n]),
    "aws_ecr_repository": ("name", lambda n: ["ecr", "describe-repositories", "--repository-names", n]),
    "aws_lambda_function": ("function_name", lambda n: ["lambda", "get-function", "--function-name", n]),
    "aws_cloudwatch_event_rule": ("name", lambda n: ["events", "describe-rule", "--name", n]),
    "aws_cloudwatch_event_connection": ("name", lambda n: ["events", "describe-connection", "--name", n]),
    "aws_cloudwatch_event_api_destination": ("name", lambda n: ["events", "describe-api-destination", "--name", n]),
    "aws_secretsmanager_secret": ("name", lambda n: ["secretsmanager", "describe-secret", "--secret-id", n]),
    "aws_cloudwatch_log_group": ("name", lambda n: ["logs", "describe-log-groups", "--log-group-name-prefix", n]),
    "aws_kms_alias": ("name", lambda n: ["kms", "list-aliases"]),
    "aws_s3_bucket": ("bucket", lambda n: ["s3api", "head-bucket", "--bucket", n]),
}
NOT_FOUND = re.compile(r"NotFound|NoSuch|does not exist|cannot be found|not found|\(404\)|Not Found", re.I)


def exists(rtype, name, out):
    """Some lookups list instead of get: confirm the exact name is in the answer."""
    if rtype == "aws_cloudwatch_log_group":
        return any(g.get("logGroupName") == name for g in (out or {}).get("logGroups", []))
    if rtype == "aws_kms_alias":
        return any(a.get("AliasName") == name for a in (out or {}).get("Aliases", []))
    return True


def check_names_taken(plan, run=aws):
    errors = []
    for addr, rtype, after, actions in changes(plan):
        attr, lookup = LOOKUPS.get(rtype, (None, None))
        name = after.get(attr) if attr else None
        if actions != ["create"] or not isinstance(name, str) or not name:
            continue   # updates and replacements are already in the state; generated names cannot collide
        code, out, err = run(lookup(name), None if rtype == "aws_s3_bucket" else after.get("region"))
        if code == 0 and exists(rtype, name, out):
            if rtype == "aws_secretsmanager_secret" and (out or {}).get("DeletedDate"):
                fix = (f"it is scheduled for deletion: aws secretsmanager restore-secret --secret-id '{name}' then "
                       f"terraform import '{addr}' '{out.get('ARN')}', or delete it now with --force-delete-without-recovery")
            else:
                ident = (out or {}).get("ARN") if rtype == "aws_secretsmanager_secret" else name
                fix = f"terraform import '{addr}' '{ident}'  (or delete it in AWS if it is not wanted)"
            errors.append((addr, "name-taken", f"{name!r} already exists in AWS but not in the Terraform state: {fix}"))
        elif code != 0 and rtype == "aws_s3_bucket" and "403" in err:
            errors.append((addr, "name-taken", f"bucket {name!r} belongs to another AWS account: choose another name"))
        elif code != 0 and not NOT_FOUND.search(err):
            print(f"::warning::{addr}: could not check whether {name!r} exists: {err}")
    return errors


CHECKS = [check_db_parameters, check_sg_descriptions, check_event_patterns, check_names_taken]


def main(path):
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    errors = [e for check in CHECKS for e in check(plan)]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not errors:
        print("preflight: every planned value passed the AWS checks")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### Preflight\nEvery planned value passed the AWS checks.\n")
        return 0
    for addr, check, msg in errors:
        print(f"::error title=preflight {check}::{addr}: {msg}")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### Preflight: the apply would fail\n\n| Resource | Check | Problem |\n|---|---|---|\n")
            for addr, check, msg in errors:
                f.write(f"| `{addr}` | {check} | {msg.replace('|', '/')} |\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
