from __future__ import annotations

from types import SimpleNamespace as NS

from agent.tools.base import ToolRegistry
from agent.tools.kubernetes_tools import KubernetesTools
from tests.fakes import FakeApps, FakeCore, k8s_factory, k8s_pod


def reg(core: FakeCore, apps: FakeApps | None = None):
    apps = apps or FakeApps()
    r = ToolRegistry(5)
    r.register(KubernetesTools(k8s_factory(core, apps), ["mlops-agent"], "mlops-agent", "app=inference-api", "inference-api", 5).tools())
    return r, apps


def test_workloads_and_healthy_state():
    r, _ = reg(FakeCore([k8s_pod("a"), k8s_pod("b")]))
    d = r.execute("list_k8s_workloads", {}).data
    assert d["deployments"][0]["desired"] == 2 and len(d["pods"]) == 2
    assert r.execute("find_unhealthy_pods", {}).data["unhealthy"] == []


def test_crashloop_and_oom_detected_with_events_and_logs():
    ev = [NS(type="Warning", reason="BackOff", message="Back-off restarting failed container", count=9)]
    pods = [k8s_pod("good"), k8s_pod("bad", ready=False, restarts=7, waiting="CrashLoopBackOff", last_term="OOMKilled")]
    r, _ = reg(FakeCore(pods, logs="ERROR MODEL LOAD FAILED: FileNotFoundError\n", events=ev))
    u = r.execute("find_unhealthy_pods", {}).data
    assert u["unhealthy"][0]["pod"] == "bad" and any("OOMKilled" in x for x in u["unhealthy"][0]["reasons"])
    d = r.execute("get_pod_status_and_logs", {}).data  # defaults to least healthy pod
    assert d["pod"]["name"] == "bad" and "MODEL LOAD FAILED" in d["log_tail"][0] and d["events"][0]["reason"] == "BackOff"


def test_readiness_failure_detected():
    r, _ = reg(FakeCore([k8s_pod("p", ready=False)]))
    assert any("readiness" in x for x in r.execute("find_unhealthy_pods", {}).data["unhealthy"][0]["reasons"])


def test_service_without_endpoints_reported():
    r, _ = reg(FakeCore([k8s_pod("p", ready=False)], endpoints=0))
    s = r.execute("inspect_k8s_services", {}).data["services"][0]
    assert s["type"] == "ClusterIP" and s["ready_endpoints"] == 0


def test_deployed_version_from_image_tag_and_label():
    r, _ = reg(FakeCore([k8s_pod("p")]), FakeApps(image="registry/app:2.3.1"))
    d = r.execute("get_deployed_version", {}).data["deployments"][0]
    assert d["image_tags"] == ["2.3.1"] and d["app_version_label"] == "1.0.0"


def test_namespace_outside_approved_list_is_denied_for_reads_and_writes():
    r, apps = reg(FakeCore([k8s_pod("p")]))
    assert r.execute("list_k8s_workloads", {"namespace": "kube-system"}).status == "denied"
    res = r.execute("restart_k8s_deployment", {"namespace": "kube-system", "deployment": "coredns"}, confirmed=True)
    assert res.status == "denied" and not apps.patches


def test_state_change_needs_confirmation_and_obeys_replica_cap():
    r, apps = reg(FakeCore([k8s_pod("p")]))
    assert r.execute("restart_k8s_deployment", {}).status == "needs_confirmation" and not apps.patches
    assert r.execute("restart_k8s_deployment", {}, confirmed=True).ok and apps.patches[0][0] == "patch"
    assert r.execute("scale_k8s_deployment", {"replicas": 99}, confirmed=True).status == "denied"
    assert r.execute("scale_k8s_deployment", {"replicas": 3}, confirmed=True).ok and apps.patches[-1][2] == {"spec": {"replicas": 3}}


def test_api_failure_is_unavailable():
    r, _ = reg(FakeCore([], fail=ConnectionError("cluster down")))
    assert r.execute("list_k8s_workloads", {}).status == "unavailable"
