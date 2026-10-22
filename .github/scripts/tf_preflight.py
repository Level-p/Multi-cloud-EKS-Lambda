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
  name-taken       something Terraform will CREATE already exists (AWS, Kubernetes object, Helm release), left by
                   a failed apply or a partial destroy. The plan job adopts it first (--adopt FILE writes import
                   blocks, the plan is made again and shows "will be imported"); a Helm release left FAILED by an
                   install is uninstalled by the apply job instead (--remove-failed-releases), after approval
  helm-pods        every Helm release is rendered (helm template, the planned values) and each container's
                   runAsNonRoot/runAsUser is compared with its image's USER (read from the registry): the kubelet
                   refuses root or a NAMED user, and Terraform would only report "context deadline exceeded"

Usage: tf_preflight.py tfplan.json [--adopt FILE | --remove-failed-releases]
       (AWS credentials and AWS_REGION in the environment; kubectl/helm configured for the platform stack)
Exit 1 with ::error annotations and a table on the run's summary page when something would fail.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import yaml

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


def cli(args):
    """Runs kubectl or helm: (exit code, stdout, stderr)."""
    p = subprocess.run(args, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr.strip()


# Kubernetes objects: type -> (kubectl kind, namespaced). Import id: namespace/name, or name
KUBE_KINDS = {
    "kubernetes_namespace_v1": ("namespace", False),
    "kubernetes_secret_v1": ("secret", True),
    "kubernetes_config_map_v1": ("configmap", True),
    "kubernetes_network_policy_v1": ("networkpolicy", True),
    "kubernetes_storage_class_v1": ("storageclass", False),
    "kubernetes_service_account_v1": ("serviceaccount", True),
}
# A release in one of these states never worked: the approved apply job uninstalls it, Terraform installs it again
LEFTOVER_RELEASE = {"failed", "pending-install", "pending-upgrade", "pending-rollback", "uninstalling"}


def existing(plan, run=aws, kube=cli):
    """Every object the plan CREATES that already exists (left by a failed apply, a partial destroy, or made by
    hand): dicts with addr, name, import_id (None when it cannot be adopted), problem, release_status."""
    found = []
    for addr, rtype, after, actions in changes(plan):
        if actions != ["create"]:
            continue   # updates and replacements are already in the state
        if rtype in LOOKUPS:
            attr, lookup = LOOKUPS[rtype]
            name = after.get(attr)
            if not isinstance(name, str) or not name:
                continue   # generated names (name_prefix) cannot collide
            code, out, err = run(lookup(name), None if rtype == "aws_s3_bucket" else after.get("region"))
            if code == 0 and exists(rtype, name, out):
                if rtype == "aws_secretsmanager_secret" and (out or {}).get("DeletedDate"):
                    found.append(dict(addr=addr, name=name, import_id=None, release_status=None, problem=(
                        f"scheduled for deletion: aws secretsmanager restore-secret --secret-id '{name}' (it is then "
                        f"adopted), or delete it now with --force-delete-without-recovery")))
                else:
                    ident = (out or {}).get("ARN") if rtype == "aws_secretsmanager_secret" else name
                    found.append(dict(addr=addr, name=name, import_id=ident, release_status=None, problem=None))
            elif code != 0 and rtype == "aws_s3_bucket" and "403" in err:
                found.append(dict(addr=addr, name=name, import_id=None, release_status=None,
                                  problem="the bucket name belongs to another AWS account: choose another name"))
            elif code != 0 and not NOT_FOUND.search(err):
                print(f"::warning::{addr}: could not check whether {name!r} exists: {err}")
        elif rtype == "helm_release" and isinstance(after.get("name"), str):
            name, ns = after["name"], after.get("namespace") or "default"
            code, out, err = kube(["helm", "status", name, "-n", ns, "-o", "json"])
            if code == 0:
                status = (json.loads(out or "{}").get("info") or {}).get("status") or "unknown"
                leftover = status in LEFTOVER_RELEASE
                found.append(dict(addr=addr, name=f"{ns}/{name}", release_status=status, problem=None,
                                  import_id=None if leftover else f"{ns}/{name}"))
            elif not NOT_FOUND.search(err):
                print(f"::warning::{addr}: could not check whether release {ns}/{name} exists: {err}")
        elif rtype in KUBE_KINDS:
            kind, namespaced = KUBE_KINDS[rtype]
            meta = (after.get("metadata") or [{}])[0]
            name, ns = meta.get("name"), meta.get("namespace") or "default"
            if not isinstance(name, str) or not name:
                continue
            code, _, err = kube(["kubectl", "get", kind, name, "-o", "name"] + (["-n", ns] if namespaced else []))
            if code == 0:
                ident = f"{ns}/{name}" if namespaced else name
                found.append(dict(addr=addr, name=ident, import_id=ident, release_status=None, problem=None))
            elif not NOT_FOUND.search(err):
                print(f"::warning::{addr}: could not check whether {kind} {name} exists: {err}")
    return found


def check_names_taken(plan, run=aws, kube=cli):
    """Objects that exist but are neither in the state nor adopted: their create would fail."""
    errors = []
    for f in existing(plan, run, kube):
        if f["release_status"] in LEFTOVER_RELEASE:
            print(f"::notice::{f['addr']}: release {f['name']} is {f['release_status']} (a failed install): the apply "
                  f"job uninstalls it, then Terraform installs it again")
        elif f["problem"]:
            errors.append((f["addr"], "name-taken", f"{f['name']!r} already exists: {f['problem']}"))
        else:
            errors.append((f["addr"], "name-taken", f"{f['name']!r} already exists but is not in the Terraform state: "
                                                    f"terraform import '{f['addr']}' '{f['import_id']}' (the plan job "
                                                    f"does this by itself with --adopt)"))
    return errors


def write_imports(plan, path, run=aws, kube=cli):
    """--adopt: an import block for every object that exists but is missing from the state. The next plan then
    shows "will be imported" (reviewed and approved like any change) instead of failing in the apply."""
    adopt = [f for f in existing(plan, run, kube) if f["import_id"] and not f["problem"]]
    with open(path, "w", encoding="utf-8") as out:
        out.write("# Generated by .github/scripts/tf_preflight.py --adopt: these exist but were missing from the state\n")
        for f in adopt:
            out.write(f'\nimport {{\n  to = {f["addr"]}\n  id = {json.dumps(f["import_id"])}\n}}\n')
            print(f"::notice::adopting {f['addr']} (exists: {f['name']})")
    return len(adopt)


def remove_failed_releases(plan, kube=cli):
    """--remove-failed-releases (apply job, after approval): uninstall the releases left by a failed install that
    the plan installs again; Helm would otherwise refuse with "cannot re-use a name that is still in use"."""
    no_aws = lambda *a, **k: (1, None, "NotFound")   # noqa: E731  (only Helm releases matter here)
    for f in existing(plan, run=no_aws, kube=kube):
        if f["release_status"] in LEFTOVER_RELEASE:
            ns, name = f["name"].split("/", 1)
            print(f"uninstalling {f['name']} ({f['release_status']}, left by a failed install)")
            code, _, err = kube(["helm", "uninstall", name, "-n", ns, "--wait", "--timeout", "5m"])
            if code != 0:
                print(f"::error::{f['addr']}: helm uninstall {f['name']} failed: {err}")
                return 1
    return 0


# ---------------------------------------------------------------------------------------------------------
# Helm releases: pods the kubelet will refuse to start
# ---------------------------------------------------------------------------------------------------------
POD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "Rollout"}


def helm(args):
    p = subprocess.run(["helm", *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr.strip()


def render_release(after, workdir, render=helm):
    """helm template with the release's planned values: (manifests, error)."""
    args = ["template", after.get("name") or "preflight"]
    chart = after["chart"]
    if after.get("repository"):
        args += [chart, "--repo", after["repository"]]
    else:
        args += [os.path.join(workdir, chart)]   # local chart, relative to the stack directory
    if after.get("version"):
        args += ["--version", after["version"]]
    args += ["--namespace", after.get("namespace") or "default", "--skip-tests"]
    with tempfile.TemporaryDirectory() as tmp:
        for i, v in enumerate(after.get("values") or []):
            path = os.path.join(tmp, f"values-{i}.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(v)
            args += ["-f", path]
        for s in after.get("set") or []:
            args += ["--set-string" if s.get("type") == "string" else "--set", f"{s['name']}={s['value']}"]
        code, out, err = render(args)
    return (out, None) if code == 0 else (None, err)


def pod_specs(manifests):
    """(kind/name, pod spec) of every workload in a multi-document YAML stream."""
    for doc in yaml.safe_load_all(manifests):
        if not isinstance(doc, dict):
            continue
        spec = doc.get("spec") or {}
        if doc.get("kind") == "CronJob":
            spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
        elif doc.get("kind") not in POD_KINDS:
            continue
        pod = ((spec.get("template") or {}).get("spec"))
        if pod:
            yield f"{doc['kind']}/{(doc.get('metadata') or {}).get('name')}", pod


def image_ref(image):
    """registry, repository, tag-or-digest (Docker Hub names expanded)."""
    name, ref = image, "latest"
    if "@" in name:
        name, ref = name.split("@", 1)
    elif ":" in name.rsplit("/", 1)[-1]:
        name, ref = name.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        registry, repo = name.split("/", 1)
    else:
        registry, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
    return ("registry-1.docker.io" if registry == "docker.io" else registry), repo, ref


def image_user(image, _cache={}):
    """The image's configured USER from its registry (anonymous pull token), or raises."""
    if image in _cache:
        return _cache[image]
    registry, repo, ref = image_ref(image)
    base = f"https://{registry}/v2/{repo}"
    accept = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])
    headers = {}

    def get(url, acc):
        req = urllib.request.Request(url, headers={"Accept": acc, **headers})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code != 401 or "Authorization" in headers or not challenge.lower().startswith("bearer"):
                raise
            params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            q = "&".join(f"{k}={params[k]}" for k in ("service", "scope") if k in params)
            with urllib.request.urlopen(f"{params['realm']}?{q or 'scope=repository:' + repo + ':pull'}", timeout=20) as r:
                tok = json.load(r)
            headers["Authorization"] = f"Bearer {tok.get('token') or tok.get('access_token')}"
            return get(url, acc)

    m = get(f"{base}/manifests/{ref}", accept)
    if "manifests" in m:
        d = next(x["digest"] for x in m["manifests"]
                 if (x.get("platform") or {}).get("os") == "linux" and (x.get("platform") or {}).get("architecture") == "amd64")
        m = get(f"{base}/manifests/{d}", accept)
    cfg = get(f"{base}/blobs/{m['config']['digest']}", "*/*")
    _cache[image] = (cfg.get("config") or {}).get("User") or ""
    return _cache[image]


def non_root_problem(container, pod, user_of):
    """Why the kubelet will refuse to start this container under runAsNonRoot, or None."""
    csc, psc = container.get("securityContext") or {}, pod.get("securityContext") or {}
    non_root = csc.get("runAsNonRoot", psc.get("runAsNonRoot"))
    uid = csc.get("runAsUser", psc.get("runAsUser"))
    if not non_root:
        return None
    if uid is not None:
        return "runAsUser: 0 with runAsNonRoot: the container would run as root" if int(uid) == 0 else None
    user = user_of(container["image"]).split(":", 1)[0]
    if user in ("", "0", "root"):
        return f"image {container['image']} runs as root and no runAsUser is set"
    if not user.isdigit():
        return (f"image {container['image']} has USER {user!r} (a name): with runAsNonRoot the kubelet cannot verify it is "
                f"not root and will not start the container; set runAsUser to its numeric id")
    return None


def check_helm_pods(plan, run=None, render=helm, user_of=image_user, workdir="."):
    errors = []
    for addr, rtype, after, _ in changes(plan):
        if rtype != "helm_release" or not isinstance(after.get("chart"), str):
            continue
        if any(not isinstance(v, str) for v in after.get("values") or []):
            print(f"::warning::{addr}: values not known at plan time, pods not checked")
            continue
        manifests, err = render_release(after, workdir, render)
        if manifests is None:
            errors.append((addr, "helm-pods", f"helm template failed: {err}"))
            continue
        for workload, pod in pod_specs(manifests):
            for c in (pod.get("initContainers") or []) + (pod.get("containers") or []):
                try:
                    why = non_root_problem(c, pod, user_of)
                except Exception as e:   # registry unreachable: report, do not block
                    print(f"::warning::{addr}: {workload}: cannot read the USER of {c.get('image')}: {e}")
                    continue
                if why:
                    errors.append((addr, "helm-pods", f"{workload} container {c.get('name')}: {why}"))
    return errors


CHECKS = [check_db_parameters, check_sg_descriptions, check_event_patterns, check_names_taken, check_helm_pods]


def main(argv):
    ap = argparse.ArgumentParser(description="Terraform plan preflight (see the module docstring)")
    ap.add_argument("plan", help="terraform show -json tfplan > tfplan.json")
    ap.add_argument("--adopt", metavar="FILE", help="write import blocks for existing objects missing from the state")
    ap.add_argument("--remove-failed-releases", action="store_true", help="uninstall releases left by a failed install")
    args = ap.parse_args(argv)
    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    if args.adopt:
        print(f"preflight: {write_imports(plan, args.adopt)} existing object(s) to adopt")
        return 0
    if args.remove_failed_releases:
        return remove_failed_releases(plan)
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
    sys.exit(main(sys.argv[1:]))
