# Monitoring Stack Deployment

Quick setup guide for deploying the monitoring stack on EKS with ArgoCD.

## 1. Deploy EKS with AWS eksctl

```bash
ssh-keygen -t rsa -b 2048 -f ~/.ssh/gitops-eks-key

aws ec2 import-key-pair \
  --region us-east-1 \
  --key-name gitops-eks-key \
  --public-key-material fileb://~/.ssh/gitops-eks-key.pub

eksctl create cluster \
  --name monitoring-lab \
  --region us-east-1 \
  --nodes 3 \
  --node-type t3.large \
  --with-oidc \
  --ssh-access \
  --ssh-public-key gitops-eks-key \
  --managed

  eksctl create addon \
  --name aws-ebs-csi-driver \
  --cluster monitoring-lab \
  --region us-east-1
```

## 2. Configure kubectl

```bash
aws eks update-kubeconfig --name monitoring-lab --region us-east-1
```

Verify:
```bash
kubectl get nodes
```

## 3. Install ArgoCD

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Wait for ArgoCD to be ready
kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n argocd
```

Get ArgoCD admin password:
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

## 4. Deploy App of Apps

```bash
kubectl apply -f apps/argocd-apps.yaml
```

This deploys all applications:
- Prometheus, Loki, Grafana, Tempo (monitoring)
- k8s-monitoring (Alloy collectors)
- MySQL, Redis (databases)
- FastAPI (sample app)

Watch sync status:
```bash
kubectl get applications -n argocd
```

**Convention:** Each Argo CD app deploys to a namespace that **matches its name** (e.g. app `grafana` → namespace `grafana`, app `prometheus` → namespace `prometheus`). So when you see an app in Argo CD, use that same name with `kubectl -n <app-name>`.

## 5. Get Endpoints

### Prometheus
```bash
kubectl get svc -n prometheus -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'
```
Or list services and use the one of type LoadBalancer: `kubectl get svc -n prometheus`. Open `http://<hostname>/` for the UI; **Status → Configuration** shows the running config.  
**Note:** The LoadBalancer hostname can change after a redeploy or namespace change (e.g. switching from `monitoring` to `prometheus`). If the old URL returns "site can't be reached" or DNS_PROBE_FINISHED_NXDOMAIN, run the command above to get the current URL.

### Grafana
Argo CD app **grafana** deploys to namespace **grafana** (app name = namespace).
```bash
kubectl get svc -n grafana -o wide | grep -i grafana
# or get LoadBalancer hostname:
kubectl get svc -n grafana -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'
```
Default credentials: `admin` / `admin`

### FastAPI
```bash
kubectl get svc fastapi-lb -n fastapi -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

### ArgoCD UI
```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
```
Open: https://localhost:8080

## Verify automation-api in Prometheus

The automation-api is configured so Prometheus scrapes its `/metrics` endpoint. To confirm it appears:

1. **If the automation-api job is missing from the live config**  
   If `kubectl get configmap -n prometheus -l app.kubernetes.io/name=prometheus -o yaml | grep -A 20 automation-api` returns nothing, the job is not in the live Prometheus config. Check in order:

   - **Repo and sync**  
     Argo CD uses `LetsgoPens87/k8s-argo-monitoring` at `apps/prometheus` (see `apps/argocd-apps/prometheus.yaml`). Ensure that repo’s `apps/prometheus/values.yaml` contains the `scrapeConfigs.automation-api` block (or the `extraScrapeConfigs` block from the same file). Push from this (capstone) repo if needed, then in Argo CD: **Refresh** the Prometheus application and **Sync**.

   - **Confirm rendered config locally**  
     From this repo root:  
     `cd apps/prometheus && helm dependency update && helm template prometheus .`  
     In the output, search for `automation-api`. The ConfigMap should include a scrape job with `job_name: automation-api`. If it appears here but not in the cluster, the issue is sync or which repo/branch Argo CD is using.

   - **Fallback: use extraScrapeConfigs**  
     In `apps/prometheus/values.yaml`, the chart also supports `prometheus.extraScrapeConfigs` (a YAML string). If the job still doesn’t appear, comment out the `scrapeConfigs.automation-api` block and uncomment the `extraScrapeConfigs` block in that file, then push and sync again.

   After a successful sync, the Prometheus ConfigMap should include the `automation-api` job and **Status → Targets** should show it.

2. **Open Prometheus config**  
   ```bash
   kubectl -n prometheus port-forward svc/prometheus-server 9090:80
   ```  
   In the browser: http://localhost:9090 → **Status** → **Configuration**. Search for `automation-api`.

   - **If you see it:** There will be a scrape block like `job_name: automation-api` with `metrics_path: /metrics` and `static_configs` targeting `automation-api.api.svc.cluster.local:80`. You’re good.
   - **If you don’t:** The config will only list default jobs (`prometheus`, `kubernetes-apiservers`, `kubernetes-nodes`, `kubernetes-service-endpoints`, `kubernetes-pods`, etc.) and no `automation-api`. That means the scrape config from `apps/prometheus/values.yaml` is not in the repo Argo CD syncs from, or the app hasn’t synced. Follow **§ Debugging & verification commands** below (especially steps 1–3).

3. **Check targets**  
   **Status** → **Targets**. Look for job **automation-api** (or a target with label `service="automation-api"`). State should be **UP**.

4. **If the target is missing or DOWN**  
   - Confirm automation-api is running: `kubectl get pods -n api -l app=automation-api`  
   - Confirm the Service exists: `kubectl get svc automation-api -n api`  
   - From inside the cluster, test metrics: `kubectl run -it --rm curl --image=curlimages/curl --restart=Never -- curl -s http://automation-api.api.svc.cluster.local/metrics | head -5`  
   - Re-sync the Prometheus application in Argo CD so the updated scrape config is applied; restart the Prometheus pod if the config changed but targets did not update.

5. **Query in Prometheus**  
   In **Graph**, run: `up{job="automation-api"}`. A value of `1` means the automation-api target is being scraped.

## Where is the Prometheus config? (no node changes)

You do **not** need to change any config on the actual node. The flow is:

1. **Git repo** (e.g. `LetsgoPens87/k8s-argo-monitoring`) → `apps/prometheus/values.yaml` is the source of truth.
2. **Argo CD** syncs the Prometheus app: it runs Helm with that repo’s `apps/prometheus` (Chart + values) and produces Kubernetes manifests.
3. **One of those manifests is a ConfigMap** in the **prometheus** namespace. That ConfigMap holds the rendered `prometheus.yml` (including `scrape_configs`).
4. The **Prometheus server pod** mounts that ConfigMap as a file inside the container. Prometheus reads the file from the mount; nothing is stored on the host node.

So: fix the **values in the repo** and **sync the Prometheus application**. Argo CD updates the ConfigMap; the pod’s config is whatever is in that ConfigMap (and configmap-reload may reload it, or a pod restart picks it up). No SSH or node-level edits.

If the repo is correct and you’ve synced but the UI still doesn’t show the automation-api job, the next step is to confirm that the **ConfigMap** in the cluster was actually updated by the sync (see below).

## Debugging & verification commands

Use these to confirm you're changing the right repo, the right files, and that the cluster reflects your changes.

### 1. Where does Argo CD get Prometheus from?

```bash
# Show repo, branch, and path for the Prometheus app
kubectl get application prometheus -n argocd -o jsonpath='{.spec.source.repoURL} {.spec.source.targetRevision} {.spec.source.path}' && echo

# Optional: clone that repo and check the file (replace with your fork if different)
git clone --depth 1 https://github.com/LetsgoPens87/k8s-argo-monitoring.git /tmp/k8s-argo-monitoring-check
grep -A 15 "automation-api" /tmp/k8s-argo-monitoring-check/apps/prometheus/values.yaml || echo "automation-api block NOT FOUND in repo"
```

**You must change the repo Argo CD uses** (e.g. `LetsgoPens87/k8s-argo-monitoring`). Changing only this (capstone) repo has no effect until those changes are pushed to the repo and path above.

### 2. Do my local values produce the automation-api job?

Run from **this repo** (capstone) root:

```bash
cd apps/prometheus
helm dependency update
helm template prometheus . 2>/dev/null | grep -A 25 "automation-api" || echo "automation-api NOT in rendered output"
cd ../..
```

If you see a `job_name: automation-api` block, your local `values.yaml` is correct. If not, fix `apps/prometheus/values.yaml` (e.g. `scrapeConfigs.automation-api` or `extraScrapeConfigs`).

### 3. What is actually in the cluster?

```bash
# Argo CD sync status for Prometheus
kubectl get application prometheus -n argocd -o wide

# Full Prometheus config in the cluster (search for automation-api)
kubectl get configmap -n prometheus -l app.kubernetes.io/name=prometheus -o yaml | grep -A 25 "automation-api" || echo "automation-api NOT in live ConfigMap"

# Prometheus server pod (restart if config changed but targets didn’t)
kubectl get pods -n prometheus -l app.kubernetes.io/name=prometheus
```

If the ConfigMap has no `automation-api`, either the synced repo doesn’t have the scrape config or the app hasn’t synced. If the grep above finds nothing, the config Argo CD applied doesn't include the job. Find which ConfigMap holds `prometheus.yml` and inspect it:

```bash
# List ConfigMap names and which have prometheus.yml
kubectl get configmap -n prometheus -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | while read name; do
  if kubectl get configmap "$name" -n prometheus -o jsonpath='{.data}' 2>/dev/null | grep -q prometheus.yml; then
    echo "$name has prometheus.yml"
  fi
done

# Inspect that ConfigMap (replace PROMETHEUS_CM with the name from above)
kubectl get configmap PROMETHEUS_CM -n prometheus -o jsonpath='{.data.prometheus\.yml}' | grep -A 15 "automation-api" || echo "automation-api not in this ConfigMap"
```

If that ConfigMap still has no `automation-api`, the Helm render from the synced repo isn't producing it. In Argo CD, sync the **Prometheus** application specifically (not only the app-of-apps), then re-run the grep. If it still fails, run **§ 2** locally to confirm your values render the job, then ensure those exact values are in the repo and path Argo CD uses.

### 4. Is automation-api reachable from the cluster?

```bash
# Pod and service
kubectl get pods -n api -l app=automation-api
kubectl get svc automation-api -n api

# Hit /metrics from inside the cluster (same way Prometheus scrapes)
kubectl run curl-debug --rm -it --restart=Never --image=curlimages/curl -n api -- \
  curl -s -o /dev/null -w "%{http_code}" http://automation-api.api.svc.cluster.local:80/metrics
# Expect 200
```

### 5. One-shot “am I good?” check

```bash
# From repo root: render config, then check cluster
cd apps/prometheus && helm dependency update -q && helm template prometheus . 2>/dev/null | grep -q "job_name: automation-api" && echo "LOCAL: automation-api in rendered config" || echo "LOCAL: automation-api MISSING"
cd ../..
kubectl get configmap -n prometheus -l app.kubernetes.io/name=prometheus -o yaml 2>/dev/null | grep -q "automation-api" && echo "CLUSTER: automation-api in Prometheus ConfigMap" || echo "CLUSTER: automation-api MISSING"
```

### 6. Find Grafana datasource config

Argo CD app **grafana** deploys to namespace **grafana** (so app name and namespace match). If you still see the old Prometheus URL in Grafana:

```bash
# List ConfigMaps in grafana namespace
kubectl get configmap -n grafana -o name

# Find which ConfigMap has the Prometheus URL
for cm in $(kubectl get configmap -n grafana -o jsonpath='{.items[*].metadata.name}'); do
  if kubectl get configmap "$cm" -n grafana -o yaml | grep -q 'prometheus-server.monitoring'; then
    echo "FOUND: $cm"
    kubectl get configmap "$cm" -n grafana -o yaml | grep -E 'url:|prometheus|monitoring' | head -15
  fi
done
```

Edit that ConfigMap and change `prometheus-server.monitoring.svc.cluster.local` to `prometheus-server.prometheus.svc.cluster.local`, then restart Grafana:

```bash
kubectl edit configmap <CONFIGMAP_NAME> -n grafana
kubectl rollout restart deployment -n grafana -l app.kubernetes.io/name=grafana
# or: kubectl rollout restart statefulset -n grafana -l app.kubernetes.io/name=grafana
```

## Build, test with Docker, and deploy (GitHub Actions → Argo CD)

Use this flow to test the **automation-api** image locally, then push so GitHub Actions builds and pushes to Docker Hub; Argo CD (and Image Updater) will pick up the new image.

### 1. Build and run with Docker (local test)

From the repo root:

```bash
# Build the image (use your Docker Hub username or any tag for local test)
docker build -t dhricko9/automation-api:test -f apps/automation-api/app/Dockerfile apps/automation-api/app

# Run the container
docker run -p 8000:8000 dhricko9/automation-api:test
```

In another terminal, test the API:

```bash
curl http://localhost:8000/health
# Expect: {"status":"ok"}
```

Stop the container with `Ctrl+C` or `docker stop <container_id>`.

### 2. Push to GitHub and trigger the pipeline

The workflow in `.github/workflows/automation-gateway.yaml` builds **automation-api** (not automation-gateway). It runs when:

- You **push a tag** `v*` (e.g. `v1.0.8`), or  
- You **push changes** under `apps/automation-api/app/`, or  
- You run it **manually** (Actions → "CI/CD Pipeline" → Run workflow, optional version input).

**Option A – Push a version tag (recommended for releases):**

```bash
git add .
git commit -m "Your changes"
git push origin main

# Then create and push a tag (this triggers the workflow and uses the tag as image tag)
git tag v1.0.8
git push origin v1.0.8
```

**Option B – Manual run (no tag):**

1. Go to GitHub → **Actions** → **CI/CD Pipeline** → **Run workflow**.
2. Choose branch, optionally set "Docker image tag" (e.g. `v1.0.8`). If left empty, the image tag will be `latest` (or the tag if the run was triggered by a tag).

**Secrets:** In the repo **Settings → Secrets and variables → Actions**, set `DOCKER_USERNAME` and `DOCKER_PASSWORD` (Docker Hub) so the workflow can push.

### 3. How Argo CD gets the new image

- **If Argo CD Image Updater is installed:** It watches the registry, finds newer semver tags (e.g. `v1.0.8`), and writes the new tag into `apps/automation-api/kustomization.yaml` in the Git repo, then Argo CD syncs from Git.
- **If you prefer to pin manually:** After the workflow has pushed (e.g. `dhricko9/automation-api:v1.0.8`), update `apps/automation-api/kustomization.yaml` and set `newTag` to that tag (e.g. `v1.0.8`), then commit and push. Argo CD will sync and deploy the new image.

To confirm what is deployed:

```bash
kubectl get deployment automation-api -n automation-api -o jsonpath='{.spec.template.spec.containers[0].image}'
```

## Directory Structure

```
apps/
├── argocd-apps.yaml      # Parent App of Apps
├── argocd-apps/          # Individual ArgoCD Application manifests
├── terraform/            # EKS infrastructure
├── prometheus/           # Prometheus Helm wrapper
├── loki/                 # Loki Helm wrapper
├── grafana/              # Grafana Helm wrapper
├── tempo/                # Tempo Helm wrapper
├── k8s-monitoring/       # Alloy collectors
├── mysql/                # MySQL + exporter
├── redis/                # Redis + exporter
└── fastapi/              # Sample FastAPI app
```
