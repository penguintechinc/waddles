# Beta Helm Standardization + Translate Fix Implementation Plan

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Clean-slate redeploy beta cluster using Helm only, add `interactive-translate` to the Helm chart, set beta replicas to 1 per module, restore the kustomize beta overlay, and document the Helm/kustomize split by environment.

**Architecture:** Delete the `waddlebot` namespace to eliminate the Helm+kustomize mix, add translate to the Helm chart following the existing `alias.yaml` pattern, override replicas to 1 in `values-beta.yaml`, then `helm install` fresh. Kustomize stays intact for alpha/local use.

**Tech Stack:** Helm 3, Kustomize 5, kubectl, dal2-beta cluster (`--kube-context dal2-beta`), registry `registry-dal2.penguintech.io/waddlebot`

---

### Task 1: Restore kustomize beta overlay

The `kustomize edit set image` command during debugging mangled `k8s/kustomize/overlays/beta/kustomization.yaml` — it stripped `includeSelectors: false` from the labels block and reformatted. Restore it to clean format.

**Files:**
- Modify: `k8s/kustomize/overlays/beta/kustomization.yaml`

**Step 1: Overwrite with correct content**

Replace the entire file with this content (preserving `interactive-translate` which was correctly added, and restoring `includeSelectors: false`):

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: waddlebot

resources:
  - ../../base

components:
  - ../../components/infrastructure

images:
  # Core Services
  - name: hub-api
    newName: registry-dal2.penguintech.io/waddlebot/hub-api
    newTag: latest
  - name: hub-webui
    newName: registry-dal2.penguintech.io/waddlebot/hub-webui
    newTag: latest
  - name: core-router
    newName: registry-dal2.penguintech.io/waddlebot/core-router
    newTag: latest
  - name: core-identity
    newName: registry-dal2.penguintech.io/waddlebot/core-identity
    newTag: latest
  - name: core-labels
    newName: registry-dal2.penguintech.io/waddlebot/core-labels
    newTag: latest
  - name: core-browser-source
    newName: registry-dal2.penguintech.io/waddlebot/core-browser-source
    newTag: latest
  - name: core-reputation
    newName: registry-dal2.penguintech.io/waddlebot/core-reputation
    newTag: latest
  - name: core-community
    newName: registry-dal2.penguintech.io/waddlebot/core-community
    newTag: latest
  - name: core-ai-researcher
    newName: registry-dal2.penguintech.io/waddlebot/core-ai-researcher
    newTag: latest
  - name: core-video-proxy
    newName: registry-dal2.penguintech.io/waddlebot/core-video-proxy
    newTag: latest
  - name: core-engagement
    newName: registry-dal2.penguintech.io/waddlebot/core-engagement
    newTag: latest
  - name: core-module-rtc
    newName: registry-dal2.penguintech.io/waddlebot/core-module-rtc
    newTag: latest

  # Collectors
  - name: collector-twitch
    newName: registry-dal2.penguintech.io/waddlebot/collector-twitch
    newTag: latest
  - name: collector-discord
    newName: registry-dal2.penguintech.io/waddlebot/collector-discord
    newTag: latest
  - name: collector-slack
    newName: registry-dal2.penguintech.io/waddlebot/collector-slack
    newTag: latest
  - name: collector-youtube-live
    newName: registry-dal2.penguintech.io/waddlebot/collector-youtube-live
    newTag: latest
  - name: collector-kick
    newName: registry-dal2.penguintech.io/waddlebot/collector-kick
    newTag: latest

  # Interactive Modules
  - name: interactive-ai
    newName: registry-dal2.penguintech.io/waddlebot/interactive-ai
    newTag: latest
  - name: interactive-alias
    newName: registry-dal2.penguintech.io/waddlebot/interactive-alias
    newTag: latest
  - name: interactive-shoutout
    newName: registry-dal2.penguintech.io/waddlebot/interactive-shoutout
    newTag: latest
  - name: interactive-inventory
    newName: registry-dal2.penguintech.io/waddlebot/interactive-inventory
    newTag: latest
  - name: interactive-calendar
    newName: registry-dal2.penguintech.io/waddlebot/interactive-calendar
    newTag: latest
  - name: interactive-memories
    newName: registry-dal2.penguintech.io/waddlebot/interactive-memories
    newTag: latest
  - name: interactive-youtube-music
    newName: registry-dal2.penguintech.io/waddlebot/interactive-youtube-music
    newTag: latest
  - name: interactive-spotify
    newName: registry-dal2.penguintech.io/waddlebot/interactive-spotify
    newTag: latest
  - name: interactive-loyalty
    newName: registry-dal2.penguintech.io/waddlebot/interactive-loyalty
    newTag: latest
  - name: interactive-translate
    newName: registry-dal2.penguintech.io/waddlebot/interactive-translate
    newTag: latest

  # Action/Pushing Services
  - name: action-discord
    newName: registry-dal2.penguintech.io/waddlebot/action-discord
    newTag: latest
  - name: action-slack
    newName: registry-dal2.penguintech.io/waddlebot/action-slack
    newTag: latest
  - name: action-twitch
    newName: registry-dal2.penguintech.io/waddlebot/action-twitch
    newTag: latest
  - name: action-youtube
    newName: registry-dal2.penguintech.io/waddlebot/action-youtube
    newTag: latest

  # Infrastructure Services
  - name: infra-postgres
    newName: registry-dal2.penguintech.io/waddlebot/infra-postgres
    newTag: latest
  - name: infra-redis
    newName: registry-dal2.penguintech.io/waddlebot/infra-redis
    newTag: latest
  - name: infra-minio
    newName: registry-dal2.penguintech.io/waddlebot/infra-minio
    newTag: latest
  - name: infra-qdrant
    newName: registry-dal2.penguintech.io/waddlebot/infra-qdrant
    newTag: latest
  - name: infra-ollama
    newName: registry-dal2.penguintech.io/waddlebot/infra-ollama
    newTag: latest

  # Migrations
  - name: waddlebot-migrations
    newName: registry-dal2.penguintech.io/waddlebot/waddlebot-migrations
    newTag: latest

labels:
  - pairs:
      environment: beta
    includeSelectors: false

patches:
  # Configure ingress for beta environment
  - patch: |-
      - op: replace
        path: /spec/rules/0/host
        value: waddlebot.penguintech.cloud
      - op: replace
        path: /spec/tls
        value:
          - hosts:
              - waddlebot.penguintech.cloud
            secretName: waddlebot-tls
    target:
      kind: Ingress
      name: waddlebot-ingress
```

**Step 2: Validate kustomize builds cleanly**

```bash
kustomize build k8s/kustomize/overlays/beta 2>&1 | tail -5
```

Expected: no errors, YAML output ends cleanly.

**Step 3: Commit**

```bash
git add k8s/kustomize/overlays/beta/kustomization.yaml
git commit -m "fix(kustomize): restore beta overlay - includeSelectors and clean format"
```

---

### Task 2: Add translate Helm template

Add `k8s/helm/waddlebot/templates/interactive/translate.yaml` modeled on `alias.yaml`. Translate has two ports (HTTP 8033, gRPC 50033).

**Files:**
- Create: `k8s/helm/waddlebot/templates/interactive/translate.yaml`

**Step 1: Validate dry-run fails (translate not yet in values)**

```bash
helm template waddlebot k8s/helm/waddlebot \
  -f k8s/helm/waddlebot/values.yaml \
  -f k8s/helm/waddlebot/values-beta.yaml 2>&1 | grep -i "translate"
```

Expected: no translate output (it doesn't exist yet).

**Step 2: Create the template**

```yaml
{{- if .Values.modules.interactive.translate.enabled }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "waddlebot.fullname" . }}-translate-interaction
  labels:
    {{- include "waddlebot.labels" . | nindent 4 }}
    app.kubernetes.io/component: translate-interaction
spec:
  replicas: {{ .Values.modules.interactive.translate.replicas }}
  selector:
    matchLabels:
      {{- include "waddlebot.selectorLabels" . | nindent 6 }}
      app.kubernetes.io/component: translate-interaction
  template:
    metadata:
      labels:
        {{- include "waddlebot.selectorLabels" . | nindent 8 }}
        app.kubernetes.io/component: translate-interaction
    spec:
      serviceAccountName: {{ include "waddlebot.serviceAccountName" . }}
      securityContext:
        {{- toYaml .Values.podSecurityContext | nindent 8 }}
      initContainers:
      {{- include "waddlebot.dbMigrateInitContainer" . | nindent 6 }}
      containers:
      - name: translate-interaction
        image: "{{ .Values.global.imageRegistry }}/{{ .Values.modules.interactive.translate.image }}:{{ .Values.modules.interactive.translate.imageTag | default .Values.global.imageTag }}"
        imagePullPolicy: {{ .Values.global.imagePullPolicy }}
        securityContext:
          {{- toYaml .Values.securityContext | nindent 10 }}
        ports:
        - name: http
          containerPort: 8033
          protocol: TCP
        - name: grpc
          containerPort: 50033
          protocol: TCP
        envFrom:
        - configMapRef:
            name: {{ include "waddlebot.fullname" . }}-config
        - configMapRef:
            name: {{ include "waddlebot.fullname" . }}-translate-interaction-config
            optional: true
        - secretRef:
            name: {{ include "waddlebot.fullname" . }}-secrets
        - secretRef:
            name: {{ include "waddlebot.fullname" . }}-translate-interaction-secret
            optional: true
        env:
        - name: MODULE_NAME
          value: "translate-interaction"
        - name: MODULE_PORT
          value: "8033"
        - name: GRPC_PORT
          value: "50033"
        livenessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /health
            port: http
          initialDelaySeconds: 10
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 3
        resources:
          {{- toYaml .Values.modules.interactive.translate.resources | nindent 10 }}
        volumeMounts:
        - name: logs
          mountPath: /var/log/waddlebotlog
      volumes:
      - name: logs
        emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "waddlebot.fullname" . }}-translate-interaction
  labels:
    {{- include "waddlebot.labels" . | nindent 4 }}
    app.kubernetes.io/component: translate-interaction
spec:
  type: ClusterIP
  ports:
  - port: 8033
    targetPort: http
    protocol: TCP
    name: http
  - port: 50033
    targetPort: grpc
    protocol: TCP
    name: grpc
  selector:
    {{- include "waddlebot.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: translate-interaction
{{- end }}
```

---

### Task 3: Add translate entry to values.yaml

Add `translate:` block inside `modules.interactive` after the `loyalty:` block (around line 695).

**Files:**
- Modify: `k8s/helm/waddlebot/values.yaml`

**Step 1: Add the translate block after `loyalty:` section**

Insert after the closing line of the `loyalty:` block:

```yaml
    translate:
      enabled: true
      name: translate-interaction
      image: waddlebot-translate-interaction
      tag: latest
      port: 8033
      grpcPort: 50033
      replicas: 1
      resourcePreset: medium
      env:
        MODULE_NAME: "translate_interaction_module"
        MODULE_VERSION: "1.0.0"
        PORT: "8033"
        GRPC_PORT: "50033"
        LOG_LEVEL: "INFO"
```

**Step 2: Validate helm template renders translate**

```bash
helm template waddlebot k8s/helm/waddlebot \
  -f k8s/helm/waddlebot/values.yaml 2>&1 | grep -A5 "translate-interaction"
```

Expected: Deployment and Service for `waddlebot-translate-interaction` appear in output.

**Step 3: Commit**

```bash
git add k8s/helm/waddlebot/templates/interactive/translate.yaml \
        k8s/helm/waddlebot/values.yaml
git commit -m "feat(helm): add interactive-translate module to chart"
```

---

### Task 4: Update values-beta.yaml — translate image + replicas: 1 overrides

Two changes: add translate image override, and set `replicas: 1` for all modules currently at 2 (so beta doesn't over-provision).

**Files:**
- Modify: `k8s/helm/waddlebot/values-beta.yaml`

**Step 1: Add translate image to the interactive section** (after `loyalty:` entry, around line 105)

```yaml
    translate:
      image: interactive-translate
```

**Step 2: Add replicas: 1 overrides for modules with replicas: 2 in values.yaml**

Add a `# Beta: single replica per module` section. Append to `values-beta.yaml` before the `sharedEnv:` block:

```yaml
# Beta: single replica per module to stay within node capacity
replicaOverrides:
  collectors:
    twitch:
      replicas: 1
    discord:
      replicas: 1
    slack:
      replicas: 1
  interactive:
    ai:
      replicas: 1
    alias:
      replicas: 1
    shoutout:
      replicas: 1
    inventory:
      replicas: 1
    calendar:
      replicas: 1
    memories:
      replicas: 1
    loyalty:
      replicas: 1
    translate:
      replicas: 1
  pushing:
    discordAction:
      replicas: 1
    slackAction:
      replicas: 1
    twitchAction:
      replicas: 1
    youtubeAction:
      replicas: 1
```

> **Note:** Check how replica overrides propagate in the Helm chart templates. The `alias.yaml` template uses `.Values.modules.interactive.alias.replicas` directly. If the templates don't read from `replicaOverrides`, add the overrides directly inside the existing `modules:` block in `values-beta.yaml` instead — e.g. under `modules.interactive.alias.replicas: 1`.

**Step 3: Validate replicas render correctly**

```bash
helm template waddlebot k8s/helm/waddlebot \
  -f k8s/helm/waddlebot/values.yaml \
  -f k8s/helm/waddlebot/values-beta.yaml 2>&1 | grep -A2 "replicas:"
```

Expected: all Deployment replicas show `1` (not `2`).

**Step 4: Commit**

```bash
git add k8s/helm/waddlebot/values-beta.yaml
git commit -m "feat(helm): add translate to beta values, set replicas=1 for all modules"
```

---

### Task 5: Update deploy-beta.sh — add environment comment

**Files:**
- Modify: `scripts/deploy-beta.sh`

**Step 1: Add comment block near top of file, after the `KUBE_CONTEXT` variable (around line 24)**

```bash
# Deployment tool by environment:
#   Beta  (dal2-beta cluster) → Helm   [this script]
#   Alpha (local/minikube)   → Kustomize (k8s/kustomize/overlays/alpha/)
```

**Step 2: Commit**

```bash
git add scripts/deploy-beta.sh
git commit -m "docs(deploy): document Helm=beta / Kustomize=alpha split in deploy-beta.sh"
```

---

### Task 6: Update docs/KUBERNETES.md — document tool split

**Files:**
- Modify: `docs/KUBERNETES.md`

**Step 1: Add section after the Overview section**

Find the `## Prerequisites` heading and insert before it:

```markdown
## Deployment Tool by Environment

| Environment | Tool | Config Path |
|---|---|---|
| **Beta** (`dal2-beta`) | **Helm** | `k8s/helm/waddlebot/values-beta.yaml` |
| **Alpha** (local/minikube) | **Kustomize** | k8s/kustomize/overlays/alpha |

**Rule:** Do not mix tools within an environment. Running both Helm and kustomize against the same namespace creates duplicate resources with different name prefixes (`waddlebot-*` vs unprefixed), exhausts node pod limits, and makes rollback ambiguous.

Use `scripts/deploy-beta.sh` for beta (Helm). For alpha, use `kustomize build k8s/kustomize/overlays/alpha | kubectl apply -n waddlebot --context local-alpha -f -`.

---
```

**Step 2: Commit**

```bash
git add docs/KUBERNETES.md
git commit -m "docs(k8s): document Helm/kustomize environment split"
```

---

### Task 7: Delete waddlebot namespace (clean slate)

This removes all resources — Helm pods, kustomize pods, PVCs, secrets, services. Beta data is disposable.

**Step 1: Confirm context before deleting**

```bash
kubectl config current-context
```

Expected: `dal2-beta`. If not, run `kubectl config use-context dal2-beta` first.

**Step 2: Delete namespace**

```bash
kubectl delete namespace waddlebot --context dal2-beta
```

Expected output: `namespace "waddlebot" deleted`

This takes 30–60 seconds as pods terminate.

**Step 3: Confirm namespace is gone**

```bash
kubectl get namespace waddlebot --context dal2-beta 2>&1
```

Expected: `Error from server (NotFound): namespaces "waddlebot" not found`

---

### Task 8: Helm install — fresh deploy

**Step 1: Verify translate image is in registry**

```bash
docker manifest inspect registry-dal2.penguintech.io/waddlebot/interactive-translate:latest 2>&1 | head -3
```

Expected: valid JSON manifest (image was pushed during the debugging session).

**Step 2: Run helm install**

```bash
helm upgrade --install waddlebot k8s/helm/waddlebot \
  --kube-context dal2-beta \
  --namespace waddlebot \
  --create-namespace \
  -f k8s/helm/waddlebot/values.yaml \
  -f k8s/helm/waddlebot/values-beta.yaml \
  --timeout 10m \
  --wait 2>&1
```

Expected: `Release "waddlebot" has been upgraded. Happy Helming!`

> If `--wait` times out, run without it and check pod status manually in the next task.

---

### Task 9: Verify deployment

**Step 1: Check all pods Running**

```bash
kubectl --context dal2-beta get pods -n waddlebot
```

Expected: All pods show `Running` with `1/1` or `2/2` ready. No `Pending` or `CrashLoopBackOff`. All pod names should have the `waddlebot-` prefix (Helm-managed).

**Step 2: Specifically verify translate is running**

```bash
kubectl --context dal2-beta get pods -n waddlebot | grep translate
```

Expected: `waddlebot-translate-interaction-<hash>   1/1   Running`

**Step 3: Check translate service endpoints**

```bash
kubectl --context dal2-beta get endpoints waddlebot-translate-interaction -n waddlebot
```

Expected: one endpoint IP listed (pod is behind the service).

**Step 4: Smoke test translation config API through the LB**

```bash
curl -sk -H "Host: waddlebot.penguintech.cloud" \
  https://dal2.penguintech.io/health 2>&1
```

Expected: `{"status": "ok"}` or similar healthy response.

**Step 5: Check no duplicate pods (no unprefixed pods)**

```bash
kubectl --context dal2-beta get pods -n waddlebot --no-headers | grep -v "^waddlebot-"
```

Expected: empty output (all pods are Helm-managed with `waddlebot-` prefix).

---

### Task 10: Update deploying-to-beta skill

**Files:**
- Modify: `~/.claude/skills/deploying-to-beta` or wherever the skill file lives — check `~/code/.claude/skills/`

**Step 1: Find the skill file**

```bash
ls ~/code/.claude/skills/ 2>/dev/null || ls ~/.claude/skills/ 2>/dev/null
```

**Step 2: Add a note in the "When to Use" section**

Add after the existing bullet points:

```markdown
- **Kustomize method is alpha-only** — Do not use `--method kustomize` for beta. Beta exclusively uses Helm. Kustomize is for `local-alpha` context only.
```

**Step 3: Commit**

```bash
git add ~/code/.claude/skills/deploying-to-beta  # adjust path as needed
git commit -m "docs(skills): note kustomize=alpha-only in deploying-to-beta skill"
```

---

## Notes for Executor

- Always verify `kubectl config current-context` returns `dal2-beta` before any cluster operation
- If `helm upgrade --wait` times out, the install may still be progressing — check `kubectl get pods -n waddlebot --context dal2-beta` directly
- If any pod is in `ImagePullBackOff`, the image wasn't pushed to the registry — use `docker manifest inspect` to verify before retrying
- The `values-beta.yaml` replica overrides in Task 4 may need adjustment depending on how the Helm templates reference `.Values.modules` — check `helm template` output in Step 3 before deploying
- Do NOT run `kustomize edit set image` against the beta overlay — it reformats the file destructively
