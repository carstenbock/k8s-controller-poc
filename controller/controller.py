# controller.py (robust with PDNS retries + periodic reconcile)
import os
import time
import json
import threading
import traceback
from typing import Dict, List

import requests
from kubernetes import client, config, watch
from kubernetes.client import V1ObjectMeta, V1ConfigMap
from kubernetes.client.rest import ApiException

PDNS_API_URL          = os.getenv("PDNS_API_URL", "http://powerdns-api.default.svc.cluster.local:8081")
PDNS_API_KEY          = os.getenv("PDNS_API_KEY", "changeme")
PDNS_SERVER_ID        = os.getenv("PDNS_SERVER_ID", "localhost")
DNS_ZONE              = os.getenv("DNS_ZONE", "example.com.")
DNS_TTL               = int(os.getenv("DNS_TTL", "30"))
DNS_RECORD_PREFIX     = os.getenv("DNS_RECORD_PREFIX", "")
SOURCE_LABEL_SELECTOR = os.getenv("SOURCE_LABEL_SELECTOR", "dns=true")
CONFIGMAP_NAMESPACE   = os.getenv("CONFIGMAP_NAMESPACE", "default")
CONFIGMAP_NAME        = os.getenv("CONFIGMAP_NAME", "pod-peers")
CONFIG_FILE_JSON      = os.getenv("CONFIG_FILE_JSON", "peers.json")
CONFIG_FILE_LIST      = os.getenv("CONFIG_FILE_LIST", "peers.txt")
CONFIG_ANNOTATION_BUMP= os.getenv("CONFIG_ANNOTATION_BUMP", "peers.lastUpdate")
VERIFY_SSL            = os.getenv("VERIFY_SSL", "false").lower() == "true"
RECONCILE_INTERVAL    = int(os.getenv("RECONCILE_INTERVAL_SEC", "30"))

WATCH_NAMESPACES      = [ns.strip() for ns in os.getenv("WATCH_NAMESPACES", "").split(",") if ns.strip()]

def _pdns_headers():
    return {"X-API-Key": PDNS_API_KEY, "Content-Type": "application/json"}

def _zone_url():
    return f"{PDNS_API_URL}/api/v1/servers/{PDNS_SERVER_ID}/zones/{DNS_ZONE}"

def fqdn_for_pod(pod_name: str) -> str:
    name = f"{DNS_RECORD_PREFIX}{pod_name}".strip(".")
    return f"{name}.{DNS_ZONE}"

def pdns_upsert_a_record(name_fqdn: str, ip: str, ttl: int = DNS_TTL):
    payload = {
        "rrsets": [{
            "name": name_fqdn,
            "type": "A",
            "ttl": ttl,
            "changetype": "REPLACE",
            "records": [{"content": ip, "disabled": False}]
        }]
    }
    r = requests.patch(_zone_url(), headers=_pdns_headers(), data=json.dumps(payload), verify=VERIFY_SSL, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"PDNS upsert failed {r.status_code}: {r.text}")

def pdns_delete_a_record(name_fqdn: str):
    payload = {"rrsets": [{"name": name_fqdn, "type": "A", "changetype": "DELETE"}]}
    r = requests.patch(_zone_url(), headers=_pdns_headers(), data=json.dumps(payload), verify=VERIFY_SSL, timeout=10)
    if r.status_code >= 400 and "not found" not in r.text.lower():
        raise RuntimeError(f"PDNS delete failed {r.status_code}: {r.text}")

def pdns_ready(timeout=0):
    url = f"{PDNS_API_URL}/api/v1/servers/{PDNS_SERVER_ID}"
    try:
        r = requests.get(url, headers=_pdns_headers(), verify=VERIFY_SSL, timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def pdns_upsert_with_retry(name_fqdn: str, ip: str, ttl: int = DNS_TTL, attempts: int = 6, backoff: float = 2.0):
    last = None
    for i in range(attempts):
        try:
            pdns_upsert_a_record(name_fqdn, ip, ttl)
            return True
        except Exception as e:
            last = e
            time.sleep(backoff * (i + 1))
    print(f"[pdns] upsert failed after retries for {name_fqdn}: {last}")
    return False

def load_kube_config():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

core = None
def ensure_clients():
    global core
    if core is None:
        core = client.CoreV1Api()

def list_source_pods() -> List[client.V1Pod]:
    ensure_clients()
    if WATCH_NAMESPACES:
        pods = []
        for ns in WATCH_NAMESPACES:
            pods.extend(core.list_namespaced_pod(ns, label_selector=SOURCE_LABEL_SELECTOR).items)
        return pods
    else:
        return core.list_pod_for_all_namespaces(label_selector=SOURCE_LABEL_SELECTOR).items

def build_peers(pods: List[client.V1Pod]) -> List[Dict]:
    peers = []
    for p in pods:
        if not p.status or not p.status.pod_ip:
            continue
        peers.append({"name": p.metadata.name, "namespace": p.metadata.namespace, "ip": p.status.pod_ip})
    peers.sort(key=lambda x: (x["namespace"], x["name"]))
    return peers

def upsert_configmap(peers: List[Dict]):
    ensure_clients()
    data = {
        CONFIG_FILE_JSON: json.dumps(peers, indent=2),
        CONFIG_FILE_LIST: "\n".join([x["ip"] for x in peers]) + ("\n" if peers else ""),
    }
    cm_meta = V1ObjectMeta(name=CONFIGMAP_NAME, namespace=CONFIGMAP_NAMESPACE)
    cm = V1ConfigMap(metadata=cm_meta, data=data)
    try:
        existing = core.read_namespaced_config_map(CONFIGMAP_NAME, CONFIGMAP_NAMESPACE)
        existing.data = data
        if existing.metadata.annotations is None:
            existing.metadata.annotations = {}
        existing.metadata.annotations[CONFIG_ANNOTATION_BUMP] = str(int(time.time()))
        core.replace_namespaced_config_map(CONFIGMAP_NAME, CONFIGMAP_NAMESPACE, existing)
    except ApiException as e:
        if e.status == 404:
            cm.metadata.annotations = {CONFIG_ANNOTATION_BUMP: str(int(time.time()))}
            core.create_namespaced_config_map(CONFIGMAP_NAMESPACE, cm)
        else:
            raise

def reconcile_all(reason: str):
    print(f"[reconcile] start ({reason})")
    pods = list_source_pods()
    peers = build_peers(pods)
    if pdns_ready():
        for peer in peers:
            fqdn = fqdn_for_pod(peer['name'])
            pdns_upsert_with_retry(fqdn, peer['ip'])
    else:
        print("[pdns] API not ready; will retry later")
    upsert_configmap(peers)
    print(f"[reconcile] done peers={len(peers)}")

def handle_pod_added(pod: client.V1Pod):
    if not pod or not pod.metadata:
        return
    labels = pod.metadata.labels or {}
    if not _match_selector(labels, SOURCE_LABEL_SELECTOR):
        return
    ip = (pod.status and pod.status.pod_ip) or None
    if not ip:
        return
    fqdn = fqdn_for_pod(pod.metadata.name)
    try:
        pdns_upsert_with_retry(fqdn, ip)
        print(f"[pdns] UPSERT A {fqdn} -> {ip}")
    except Exception as e:
        print(f"[pdns] upsert failed for {fqdn}: {e}")
    try:
        reconcile_all("pod_added")
    except Exception:
        traceback.print_exc()

def handle_pod_deleted(pod: client.V1Pod):
    if not pod or not pod.metadata:
        return
    labels = pod.metadata.labels or {}
    if not _match_selector(labels, SOURCE_LABEL_SELECTOR):
        return
    fqdn = fqdn_for_pod(pod.metadata.name)
    try:
        pdns_delete_a_record(fqdn)
        print(f"[pdns] DELETE A {fqdn}")
    except Exception as e:
        print(f"[pdns] delete failed for {fqdn}: {e}")
    try:
        reconcile_all("pod_deleted")
    except Exception:
        traceback.print_exc()

def _match_selector(labels: Dict[str, str], selector: str) -> bool:
    if not selector:
        return True
    for clause in selector.split(","):
        clause = clause.strip()
        if not clause:
            continue
        if "=" in clause:
            k, v = clause.split("=", 1)
            if labels.get(k.strip()) != v.strip():
                return False
        else:
            if clause not in labels:
                return False
    return True

def _periodic_reconciler():
    while True:
        try:
            if pdns_ready():
                reconcile_all("periodic")
        except Exception:
            traceback.print_exc()
        time.sleep(RECONCILE_INTERVAL)

def watch_pods():
    while True:
        ensure_clients()
        w = watch.Watch()
        try:
            if WATCH_NAMESPACES:
                for ns in WATCH_NAMESPACES:
                    threading.Thread(target=_watch_ns, args=(ns,), daemon=True).start()
            else:
                for event in w.stream(core.list_pod_for_all_namespaces, timeout_seconds=0):
                    _dispatch_event(event)
        except Exception:
            traceback.print_exc()

def _watch_ns(ns: str):
    while True:
        ensure_clients()
        w = watch.Watch()
        try:
            for event in w.stream(core.list_namespaced_pod, ns, timeout_seconds=0):
                _dispatch_event(event)
        except Exception:
            traceback.print_exc()

def _dispatch_event(event):
    etype = event.get("type")
    pod: client.V1Pod = event.get("object")
    if etype in ("ADDED", "MODIFIED"):
        handle_pod_added(pod)
    elif etype == "DELETED":
        handle_pod_deleted(pod)

if __name__ == "__main__":
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    threading.Thread(target=_periodic_reconciler, daemon=True).start()
    try:
        reconcile_all("startup")
    except Exception:
        traceback.print_exc()
    watch_pods()
    while True:
        time.sleep(60)
