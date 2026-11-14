"""Task 4 - Kubernetes operations tools.

Safety: every call is restricted to approved namespaces; state-changing tools (restart/scale) additionally
need explicit user confirmation (enforced by ToolRegistry) and obey a replica ceiling."""
from __future__ import annotations

from typing import Any, Callable

from agent.schemas import K8sNsArgs, PodArgs, RestartArgs, ScaleArgs
from agent.tools.base import Tool, ToolDenied, ToolError, ToolUnavailable


def load_apis() -> tuple[Any, Any]:
    """Create (CoreV1Api, AppsV1Api); raises ToolUnavailable when no cluster is reachable/configured."""
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001
            config.load_kube_config()
        return client.CoreV1Api(), client.AppsV1Api()
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(f"Kubernetes API not configured/reachable: {exc}") from exc


def _container_states(p: Any) -> list[dict[str, Any]]:
    out = []
    for cs in (p.status.container_statuses or []):
        st: dict[str, Any] = {"container": cs.name, "ready": cs.ready, "restarts": cs.restart_count, "image": cs.image}
        if cs.state and cs.state.waiting:
            st["waiting_reason"] = cs.state.waiting.reason
            st["waiting_message"] = (cs.state.waiting.message or "")[:200]
        if cs.state and cs.state.terminated:
            st["terminated_reason"] = cs.state.terminated.reason
            st["exit_code"] = cs.state.terminated.exit_code
        lt = cs.last_state.terminated if cs.last_state else None
        if lt:
            st["last_terminated_reason"] = lt.reason
            st["last_exit_code"] = lt.exit_code
        out.append(st)
    return out


def _pod(p: Any) -> dict[str, Any]:
    cs = _container_states(p)
    return {"name": p.metadata.name, "phase": p.status.phase,
            "ready": bool(cs) and all(c["ready"] for c in cs),
            "restarts": sum(c["restarts"] for c in cs), "containers": cs, "node": p.spec.node_name,
            "labels": dict(p.metadata.labels or {})}


def _unhealthy_reason(pod: dict[str, Any]) -> list[str]:
    r = []
    if pod["phase"] == "Pending":
        r.append("Pending (not scheduled or containers not created)")
    if pod["phase"] == "Failed":
        r.append("Failed")
    for c in pod["containers"]:
        if c.get("waiting_reason") in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError", "InvalidImageName"):
            r.append(f"{c['container']}: {c['waiting_reason']}")
        if c.get("last_terminated_reason") in ("OOMKilled", "Error"):
            r.append(f"{c['container']}: last termination {c['last_terminated_reason']} (exit {c.get('last_exit_code')})")
        if not c["ready"] and not c.get("waiting_reason") and pod["phase"] == "Running":
            r.append(f"{c['container']}: running but NOT ready (readiness probe failing)")
    if pod["restarts"] >= 3:
        r.append(f"{pod['restarts']} restarts")
    return r


class KubernetesTools:
    def __init__(self, api_factory: Callable[[], tuple[Any, Any]], approved_namespaces: list[str],
                 default_namespace: str, app_label: str, default_deployment: str, max_replicas: int = 5) -> None:
        self._factory = api_factory
        self.approved = approved_namespaces
        self.ns_default = default_namespace
        self.label = app_label
        self.dep_default = default_deployment
        self.max_replicas = max_replicas

    def _apis(self) -> tuple[Any, Any]:
        return self._factory()

    def _ns(self, ns: str | None) -> str:
        ns = ns or self.ns_default
        if ns not in self.approved:
            raise ToolDenied(f"Namespace '{ns}' is not approved. Approved namespaces: {self.approved}")
        return ns

    def _call(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (ToolError, ToolDenied, ToolUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            if status in (403, 404):
                raise ToolError(f"Kubernetes API returned {status}: {getattr(exc, 'reason', exc)}") from exc
            raise ToolUnavailable(f"Kubernetes API call failed: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def _pods(self, core: Any, ns: str) -> list[dict[str, Any]]:
        return [_pod(p) for p in core.list_namespaced_pod(ns, label_selector=self.label).items]

    # ---- tools -------------------------------------------------------------
    def list_workloads(self, a: K8sNsArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)

        def go() -> dict[str, Any]:
            core, apps = self._apis()
            deps = apps.list_namespaced_deployment(ns, label_selector=self.label).items
            return {"namespace": ns,
                    "deployments": [{"name": d.metadata.name, "desired": d.spec.replicas, "ready": d.status.ready_replicas or 0,
                                     "available": d.status.available_replicas or 0, "updated": d.status.updated_replicas or 0}
                                    for d in deps],
                    "pods": [{k: p[k] for k in ("name", "phase", "ready", "restarts")} for p in self._pods(core, ns)]}
        return self._call(go)

    def _events(self, core: Any, ns: str, pod: str) -> list[dict[str, Any]]:
        try:
            ev = core.list_namespaced_event(ns, field_selector=f"involvedObject.name={pod}").items
        except Exception:  # noqa: BLE001
            return []
        return [{"type": e.type, "reason": e.reason, "message": (e.message or "")[:250], "count": e.count} for e in ev][-10:]

    def pod_status_logs(self, a: PodArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)

        def go() -> dict[str, Any]:
            core, _ = self._apis()
            pods = self._pods(core, ns)
            if not pods:
                raise ToolError(f"No pods match label '{self.label}' in namespace '{ns}'")
            if a.pod_name:
                pod = next((p for p in pods if p["name"] == a.pod_name), None)
                if pod is None:
                    raise ToolError(f"Pod '{a.pod_name}' not found in '{ns}'")
            else:
                pod = max(pods, key=lambda p: (len(_unhealthy_reason(p)), p["restarts"]))
            logs, note = "", None
            try:
                logs = core.read_namespaced_pod_log(pod["name"], ns, tail_lines=a.tail_lines)
            except Exception as exc:  # noqa: BLE001
                note = f"current logs unavailable ({getattr(exc, 'status', type(exc).__name__)}); trying previous container"
                try:
                    logs = core.read_namespaced_pod_log(pod["name"], ns, tail_lines=a.tail_lines, previous=True)
                except Exception:  # noqa: BLE001
                    logs = ""
            return {"pod": pod, "problems": _unhealthy_reason(pod), "events": self._events(core, ns, pod["name"]),
                    "log_tail": logs.splitlines()[-a.tail_lines:] if isinstance(logs, str) else [], "note": note}
        return self._call(go)

    def inspect_services(self, a: K8sNsArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)

        def go() -> dict[str, Any]:
            core, _ = self._apis()
            svcs = core.list_namespaced_service(ns).items
            out = []
            for s in svcs:
                ep_ready = None
                try:
                    ep = core.read_namespaced_endpoints(s.metadata.name, ns)
                    ep_ready = sum(len(sub.addresses or []) for sub in (ep.subsets or []))
                except Exception:  # noqa: BLE001
                    pass
                out.append({"name": s.metadata.name, "type": s.spec.type, "cluster_ip": s.spec.cluster_ip,
                            "selector": s.spec.selector, "ports": [{"port": p.port, "target_port": str(p.target_port)} for p in (s.spec.ports or [])],
                            "ready_endpoints": ep_ready})
            return {"namespace": ns, "services": out}
        return self._call(go)

    def unhealthy_pods(self, a: K8sNsArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)

        def go() -> dict[str, Any]:
            core, _ = self._apis()
            bad = []
            pods = self._pods(core, ns)
            for p in pods:
                reasons = _unhealthy_reason(p)
                if reasons:
                    ev = [e for e in self._events(core, ns, p["name"]) if e["type"] == "Warning"]
                    bad.append({"pod": p["name"], "phase": p["phase"], "restarts": p["restarts"], "reasons": reasons, "warning_events": ev})
            return {"namespace": ns, "total_pods": len(pods), "unhealthy": bad, "healthy_count": len(pods) - len(bad)}
        return self._call(go)

    def deployed_version(self, a: K8sNsArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)

        def go() -> dict[str, Any]:
            _, apps = self._apis()
            deps = apps.list_namespaced_deployment(ns, label_selector=self.label).items
            if not deps:
                raise ToolError(f"No deployment with label '{self.label}' in '{ns}'")
            out = []
            for d in deps:
                images = [c.image for c in d.spec.template.spec.containers]
                labels = d.spec.template.metadata.labels or {}
                out.append({"deployment": d.metadata.name, "images": images,
                            "image_tags": [i.rsplit(":", 1)[-1] if ":" in i.split("/")[-1] else "latest(implicit)" for i in images],
                            "app_version_label": labels.get("app.kubernetes.io/version"),
                            "generation": d.metadata.generation, "observed_generation": d.status.observed_generation,
                            "desired": d.spec.replicas, "ready": d.status.ready_replicas or 0})
            return {"namespace": ns, "deployments": out}
        return self._call(go)

    def restart(self, a: RestartArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)
        name = a.deployment or self.dep_default

        def go() -> dict[str, Any]:
            import datetime
            _, apps = self._apis()
            body = {"spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restartedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}}}}}
            apps.patch_namespaced_deployment(name, ns, body)
            return {"restarted": name, "namespace": ns}
        return self._call(go)

    def scale(self, a: ScaleArgs) -> dict[str, Any]:
        ns = self._ns(a.namespace)
        name = a.deployment or self.dep_default
        if a.replicas > self.max_replicas:
            raise ToolDenied(f"Requested replicas {a.replicas} exceeds the allowed maximum {self.max_replicas}")

        def go() -> dict[str, Any]:
            _, apps = self._apis()
            apps.patch_namespaced_deployment_scale(name, ns, {"spec": {"replicas": a.replicas}})
            return {"scaled": name, "namespace": ns, "replicas": a.replicas}
        return self._call(go)

    def tools(self) -> list[Tool]:
        return [
            Tool("list_k8s_workloads", "List Deployments (desired/ready replicas) and Pods of the ML application.", K8sNsArgs, self.list_workloads, "kubernetes"),
            Tool("get_pod_status_and_logs", "Get pod status, events and log tail (default: least healthy pod).", PodArgs, self.pod_status_logs, "kubernetes"),
            Tool("inspect_k8s_services", "Inspect Services: type, selector, ports and ready endpoints.", K8sNsArgs, self.inspect_services, "kubernetes"),
            Tool("find_unhealthy_pods", "Find unhealthy pods, restarts and warning events.", K8sNsArgs, self.unhealthy_pods, "kubernetes"),
            Tool("get_deployed_version", "Report the currently deployed application version (image tags, labels, rollout state).", K8sNsArgs, self.deployed_version, "kubernetes"),
            Tool("restart_k8s_deployment", "Rolling-restart a deployment in an approved namespace.", RestartArgs, self.restart, "kubernetes", mutating=True),
            Tool("scale_k8s_deployment", "Scale a deployment in an approved namespace (bounded replica count).", ScaleArgs, self.scale, "kubernetes", mutating=True),
        ]
