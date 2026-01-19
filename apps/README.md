# Monitoring Stack Deployment

Quick setup guide for deploying the monitoring stack on EKS with ArgoCD.

## 1. Deploy EKS with Terraform

```bash
cd apps/terraform

terraform init
terraform plan
terraform apply
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

# Patch ArgoCD server to use LoadBalancer
kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "LoadBalancer"}}'
```

Get ArgoCD URL:
```bash
kubectl get svc argocd-server -n argocd -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

Get ArgoCD admin password:
```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
```

## 4. Deploy App of Apps

1. Open ArgoCD UI in browser (use URL from above)
2. Login with `admin` and password from above
3. Click **+ NEW APP** and configure:
   - **Application Name:** `argocd-apps`
   - **Project:** `default`
   - **Sync Policy:** `Automatic`
   - **Repository URL:** `https://github.com/YOUR_USERNAME/k8s-argo-monitoring.git`
   - **Path:** `apps/argocd-apps`
   - **Cluster URL:** `https://kubernetes.default.svc`
   - **Namespace:** `argocd`
4. Click **CREATE**

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

### ArgoCD
```bash
kubectl get svc argocd-server -n argocd -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```
Credentials: `admin` / (password from step 3)

### Grafana
```bash
kubectl get svc grafana-lb -n monitoring -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```
Default credentials: `admin` / `admin`

### FastAPI
```bash
kubectl get svc fastapi-lb -n fastapi -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
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
