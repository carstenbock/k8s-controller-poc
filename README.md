# PowerDNS Pod DNS Controller — GKE v5

**Fixes included:**
- Initialize SQLite DB via **curl → schema → sqlite3** in initContainers (no guessing paths).
- Pre-create `pod-peers` ConfigMap so the web server doesn’t crash before the controller writes files.
- PDNS listens on **10053** in the pod; Service maps **53 → 10053**.
- Controller: PDNS retries + periodic reconcile + unbuffered logs + SA token automount.

## Deploy / Update
```bash
kubectl apply -f k8s/
```

### Test
```bash
kubectl get pods
kubectl logs deploy/pdns-dns-controller
kubectl port-forward deploy/pod-peers-web 8080:8080
# open http://localhost:8080/peers.txt and /peers.json
```

Scale to see updates:
```bash
kubectl scale deploy/hello-world --replicas=5
```

Cleanup:
```bash
kubectl delete -f k8s/
```
