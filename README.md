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

## 5. Get Endpoints

### Grafana
```bash
kubectl get svc grafana-lb -n monitoring -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
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

1. **Sync the repo Argo CD uses**  
   Argo CD points at `LetsgoPens87/k8s-argo-monitoring` (see `apps/argocd-apps/prometheus.yaml`). Ensure your `apps/prometheus/values.yaml` (with the `scrapeConfigs.automation-api` job) and `apps/automation-api/deployment.yaml` (Service annotations) are in that repo and synced (e.g. push to main and refresh the Prometheus app in Argo CD).

2. **Open Prometheus config**  
   ```bash
   kubectl -n monitoring port-forward svc/prometheus-server 9090:80
   ```  
   In the browser: http://localhost:9090 → **Status** → **Configuration**. Search for `automation-api`. You should see a scrape job with `job_name: automation-api` and target `automation-api.api.svc.cluster.local:80`.

3. **Check targets**  
   **Status** → **Targets**. Look for job **automation-api** (or a target with label `service="automation-api"`). State should be **UP**.

4. **If the target is missing or DOWN**  
   - Confirm automation-api is running: `kubectl get pods -n api -l app=automation-api`  
   - Confirm the Service exists: `kubectl get svc automation-api -n api`  
   - From inside the cluster, test metrics: `kubectl run -it --rm curl --image=curlimages/curl --restart=Never -- curl -s http://automation-api.api.svc.cluster.local/metrics | head -5`  
   - Re-sync the Prometheus application in Argo CD so the updated scrape config is applied; restart the Prometheus pod if the config changed but targets did not update.

5. **Query in Prometheus**  
   In **Graph**, run: `up{job="automation-api"}`. A value of `1` means the automation-api target is being scraped.

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
