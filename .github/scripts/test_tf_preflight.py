import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tf_preflight as p  # noqa: E402

PG18 = {"EngineDefaults": {"Parameters": [
    {"ParameterName": "log_connections", "ApplyType": "dynamic", "DataType": "list", "IsModifiable": True,
     "AllowedValues": "receipt, authentication, authorization, setup_durations, all"},
    {"ParameterName": "rds.force_ssl", "ApplyType": "dynamic", "DataType": "boolean", "IsModifiable": True, "AllowedValues": "0,1"},
    {"ParameterName": "log_min_duration_statement", "ApplyType": "dynamic", "DataType": "integer", "IsModifiable": True,
     "AllowedValues": "-1-2147483647"},
    {"ParameterName": "shared_preload_libraries", "ApplyType": "static", "DataType": "list", "IsModifiable": True,
     "AllowedValues": "pg_stat_statements,pgaudit"},
]}}


def plan(rtype, after, actions=("create",), address=None):
    return {"resource_changes": [{"address": address or f"module.x.{rtype}.this", "mode": "managed", "type": rtype,
                                  "change": {"actions": list(actions), "after": after}}]}


def fake_aws(answers):
    """answers: first CLI word after the service -> (code, out, err); records the calls."""
    calls = []

    def run(args, region=None):
        calls.append(args)
        return answers.get(args[1], (254, None, "An error occurred (NotFound)"))
    run.calls = calls
    return run


def pg(*params):
    return plan("aws_db_parameter_group", {"family": "postgres18", "region": "eu-west-2",
                                           "parameter": [dict(name=n, value=v, apply_method=m) for n, v, m in params]})


# --- the four errors of the first infra-aws run, each caught at plan time ------------------------------------
def test_pg18_log_connections_on_off_value_is_rejected():
    run = fake_aws({"describe-engine-default-parameters": (0, PG18, "")})
    [(_, check, msg)] = p.check_db_parameters(pg(("log_connections", "1", "immediate")), run)
    assert check == "db-parameters" and "log_connections = '1'" in msg and "setup_durations" in msg


def test_sg_description_with_an_apostrophe_is_rejected():
    errs = p.check_sg_descriptions(plan("aws_vpc_security_group_ingress_rule",
                                        {"description": "HTTPS from CloudFront's origin-facing servers"}))
    assert len(errs) == 1 and "found '" in errs[0][2]


def test_existing_parameter_group_not_in_state_prints_the_import():
    run = fake_aws({"describe-db-parameter-groups": (0, {"DBParameterGroups": [{}]}, "")})
    [(addr, check, msg)] = p.check_names_taken(
        plan("aws_db_parameter_group", {"name": "agripulse-postgres18"}, address="module.database.aws_db_parameter_group.this"), run)
    assert check == "name-taken" and "terraform import 'module.database.aws_db_parameter_group.this' 'agripulse-postgres18'" in msg


def test_nested_lists_in_an_event_pattern_are_rejected_without_calling_aws():
    bad = {"source": ["aws.rds"], "detail": {"EventCategories": [["failover"], ["failure"]]}}
    run = fake_aws({})
    errs = p.check_event_patterns(plan("aws_cloudwatch_event_rule", {"event_pattern": json.dumps(bad)}), run)
    assert len(errs) == 2 and all("holds values, not lists" in e[2] for e in errs) and run.calls == []


# --- the fixed versions pass ------------------------------------------------------------------------------
def test_fixed_values_pass():
    run = fake_aws({"describe-engine-default-parameters": (0, PG18, ""), "test-event-pattern": (0, {"Result": True}, "")})
    assert p.check_db_parameters(pg(("log_connections", "receipt,authentication,authorization", "immediate"),
                                    ("rds.force_ssl", "1", "immediate"), ("log_min_duration_statement", "500", "immediate")), run) == []
    assert p.check_sg_descriptions(plan("aws_vpc_security_group_ingress_rule",
                                        {"description": "HTTPS from the CloudFront origin-facing servers"})) == []
    good = {"source": ["aws.rds"], "detail-type": ["RDS DB Instance Event"],
            "detail": {"SourceIdentifier": ["agripulse-postgres"], "EventCategories": ["failover", "low storage"]}}
    assert p.check_event_patterns(plan("aws_cloudwatch_event_rule", {"event_pattern": json.dumps(good)}), run) == []
    event = json.loads(run.calls[-1][run.calls[-1].index("--event") + 1])
    assert event["detail"] == {"SourceIdentifier": "agripulse-postgres", "EventCategories": "failover"}
    # generated names (name_prefix) and resources already in the state are never looked up
    assert p.check_names_taken(plan("aws_db_parameter_group", {"name": None}), run) == []
    assert p.check_names_taken(plan("aws_iam_role", {"name": "r"}, actions=("update",)), run) == []


# --- the other rules ----------------------------------------------------------------------------------------
def test_static_parameter_needs_pending_reboot_and_unknown_names_fail():
    run = fake_aws({"describe-engine-default-parameters": (0, PG18, "")})
    msgs = [e[2] for e in p.check_db_parameters(pg(("shared_preload_libraries", "pg_stat_statements", "immediate"),
                                                   ("log_min_duration_statement", "-5", "immediate"),
                                                   ("no_such_param", "1", "immediate")), run)]
    assert any("pending-reboot" in m for m in msgs) and any("outside -1-2147483647" in m for m in msgs)
    assert any("no such parameter" in m for m in msgs)


def test_pattern_rejected_or_unmatched_by_eventbridge_fails():
    good = json.dumps({"source": ["aws.rds"]})
    rejected = fake_aws({"test-event-pattern": (254, None, "InvalidEventPatternException")})
    assert "rejected by EventBridge" in p.check_event_patterns(plan("aws_cloudwatch_event_rule", {"event_pattern": good}), rejected)[0][2]
    unmatched = fake_aws({"test-event-pattern": (0, {"Result": False}, "")})
    assert "does not match" in p.check_event_patterns(plan("aws_cloudwatch_event_rule", {"event_pattern": good}), unmatched)[0][2]


def test_secret_scheduled_for_deletion_says_restore_or_force_delete():
    run = fake_aws({"describe-secret": (0, {"ARN": "arn:aws:secretsmanager:eu-west-2:1:secret:s-AbC", "DeletedDate": "2026-10-01"}, "")})
    [(_, _, msg)] = p.check_names_taken(plan("aws_secretsmanager_secret", {"name": "agripulse/web/x"}), run)
    assert "scheduled for deletion" in msg and "restore-secret" in msg


def test_listing_lookups_need_the_exact_name():
    run = fake_aws({"describe-log-groups": (0, {"logGroups": [{"logGroupName": "/aws/lambda/agripulse-mobile-api-old"}]}, "")})
    assert p.check_names_taken(plan("aws_cloudwatch_log_group", {"name": "/aws/lambda/agripulse-mobile-api"}), run) == []


def test_main_writes_annotations_and_summary(tmp_path, monkeypatch, capsys):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(plan("aws_security_group", {"description": "it's bad", "ingress": [], "egress": []})))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(p, "CHECKS", [p.check_sg_descriptions])
    assert p.main([str(f)]) == 1
    assert "::error title=preflight sg-description::" in capsys.readouterr().out
    assert "the apply would fail" in (tmp_path / "summary.md").read_text()


# --- Helm releases: pods the kubelet refuses (the YACE release of the first platform run) -----------------
YACE_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: yace}
spec:
  template:
    spec:
      containers:
        - name: exporter
          image: quay.io/prometheuscommunity/yet-another-cloudwatch-exporter:v0.68.0
          securityContext: {runAsNonRoot: true%s}
---
apiVersion: v1
kind: Service
metadata: {name: yace}
"""


def helm_plan(values="serviceAccount: {name: yace}"):
    return plan("helm_release", {"name": "yace", "chart": "prometheus-yet-another-cloudwatch-exporter", "version": "0.48.0",
                                 "repository": "https://prometheus-community.github.io/helm-charts", "namespace": "monitoring",
                                 "values": [values]}, address="module.k8s_observability.helm_release.yace")


def fake_helm(manifests, seen):
    def render(args):
        seen.append(args)
        return 0, manifests, ""
    return render


def test_named_image_user_under_run_as_non_root_is_rejected():
    seen = []
    errs = p.check_helm_pods(helm_plan(), render=fake_helm(YACE_DEPLOYMENT % "", seen), user_of=lambda image: "nobody")
    [(addr, check, msg)] = errs
    assert addr.endswith("helm_release.yace") and check == "helm-pods" and "USER 'nobody' (a name)" in msg
    args = seen[0]
    assert args[:4] == ["template", "yace", "prometheus-yet-another-cloudwatch-exporter", "--repo"] and "-f" in args


def test_numeric_run_as_user_passes_and_the_registry_is_not_asked():
    def never(image):
        raise AssertionError("registry asked")
    assert p.check_helm_pods(helm_plan(), render=fake_helm(YACE_DEPLOYMENT % ", runAsUser: 65534", []), user_of=never) == []


def test_root_images_and_uid_zero_are_rejected():
    pod = {"securityContext": {"runAsNonRoot": True}}
    assert "runs as root" in p.non_root_problem({"image": "x"}, pod, lambda i: "")
    assert "runs as root" in p.non_root_problem({"image": "x"}, pod, lambda i: "0:0")
    assert "runAsUser: 0" in p.non_root_problem({"image": "x", "securityContext": {"runAsUser": 0}}, pod, lambda i: "")
    assert p.non_root_problem({"image": "x"}, pod, lambda i: "65532:65532") is None
    assert p.non_root_problem({"image": "x"}, {}, lambda i: "root") is None   # runAsNonRoot not asked for


def test_image_references():
    assert p.image_ref("redis") == ("registry-1.docker.io", "library/redis", "latest")
    assert p.image_ref("bitnami/redis:7") == ("registry-1.docker.io", "bitnami/redis", "7")
    assert p.image_ref("quay.io/a/b:v1") == ("quay.io", "a/b", "v1")
    assert p.image_ref("registry.k8s.io/x/y@sha256:ab") == ("registry.k8s.io", "x/y", "sha256:ab")
    assert p.image_ref("localhost:5000/z:1") == ("localhost:5000", "z", "1")


def test_failed_render_is_an_error_and_unknown_values_are_skipped():
    errs = p.check_helm_pods(helm_plan(), render=lambda args: (1, "", "chart not found"), user_of=lambda i: "")
    assert errs and "helm template failed: chart not found" in errs[0][2]
    unknown = helm_plan(); unknown["resource_changes"][0]["change"]["after"]["values"] = [None]
    assert p.check_helm_pods(unknown, render=lambda a: (_ for _ in ()).throw(AssertionError()), user_of=lambda i: "") == []


# --- existing objects missing from the state: adopted, or (failed Helm installs) removed ---------------------
def fake_kube(answers, calls=None):
    """answers: ('helm'|'kubectl', name) -> (code, stdout, stderr); everything else is NotFound."""
    def kube(args):
        if calls is not None:
            calls.append(args)
        key = (args[0], args[3] if args[0] == "kubectl" else args[2])
        if args[:2] == ["helm", "uninstall"]:
            return 0, "", ""
        return answers.get(key, (1, "", "Error: release: not found" if args[0] == "helm" else "Error from server (NotFound)"))
    return kube


def platform_plan():
    rcs = []
    for addr, rtype, after in [
        ("module.k8s_observability.helm_release.yace", "helm_release", {"name": "yace", "namespace": "monitoring", "chart": "c"}),
        ('module.k8s_gitops.helm_release.this["argocd"]', "helm_release", {"name": "argocd", "namespace": "argocd", "chart": "c"}),
        ("module.k8s_observability.kubernetes_secret_v1.grafana_admin", "kubernetes_secret_v1",
         {"metadata": [{"name": "grafana-admin", "namespace": "monitoring"}]}),
        ("module.k8s_observability.kubernetes_storage_class_v1.gp3", "kubernetes_storage_class_v1", {"metadata": [{"name": "gp3"}]}),
        ("module.k8s_controllers.helm_release.csi", "helm_release", {"name": "csi", "namespace": "kube-system", "chart": "c"}),
    ]:
        rcs.append({"address": addr, "mode": "managed", "type": rtype, "change": {"actions": ["create"], "after": after}})
    return {"resource_changes": rcs}


KUBE = {("helm", "yace"): (0, '{"info": {"status": "failed"}}', ""),
        ("helm", "argocd"): (0, '{"info": {"status": "deployed"}}', ""),
        ("kubectl", "grafana-admin"): (0, "secret/grafana-admin", ""),
        ("kubectl", "gp3"): (0, "storageclass.storage.k8s.io/gp3", "")}


def test_existing_objects_are_adopted_with_import_blocks(tmp_path):
    out = tmp_path / "adopted_imports.tf"
    assert p.write_imports(platform_plan(), str(out), run=fake_aws({}), kube=fake_kube(KUBE)) == 3
    text = out.read_text()
    assert 'to = module.k8s_gitops.helm_release.this["argocd"]\n  id = "argocd/argocd"' in text
    assert 'id = "monitoring/grafana-admin"' in text and 'id = "gp3"' in text
    assert "yace" not in text and "csi" not in text   # failed install: removed, not adopted; csi does not exist


def test_failed_release_is_a_notice_and_unadopted_objects_are_errors(capsys):
    errs = p.check_names_taken(platform_plan(), run=fake_aws({}), kube=fake_kube(KUBE))
    assert sorted(e[0] for e in errs) == ['module.k8s_gitops.helm_release.this["argocd"]',
                                         "module.k8s_observability.kubernetes_secret_v1.grafana_admin",
                                         "module.k8s_observability.kubernetes_storage_class_v1.gp3"]
    assert "the apply job uninstalls it" in capsys.readouterr().out


def test_apply_job_uninstalls_only_releases_left_by_a_failed_install():
    calls = []
    assert p.remove_failed_releases(platform_plan(), kube=fake_kube(KUBE, calls)) == 0
    assert [c for c in calls if c[:2] == ["helm", "uninstall"]] == [
        ["helm", "uninstall", "yace", "-n", "monitoring", "--wait", "--timeout", "5m"]]
