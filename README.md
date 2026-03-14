# ChatGPT → Overseerr Bridge

A lightweight FastAPI service that lets ChatGPT request movies and TV shows directly into your media stack via a custom action.

## Architecture

```
ChatGPT custom action
        │  HTTPS (Bearer token)
        ▼
chatgpt-bridge (FastAPI, k8s)
        │  X-Api-Key  (in-cluster DNS, no Pi-hole needed)
        ▼
overseerr.overseerr.svc.cluster.local:5055
        │
        ├─▶ Radarr  (movies)
        └─▶ Sonarr  (TV shows)
                │
                ▼
              Plex
```

ChatGPT never talks to Overseerr directly — the bridge handles search, deduplication, and season resolution.

## Prerequisites

- Overseerr running in the cluster (namespace `overseerr`, service `overseerr:5055`)
- A container registry to push the image to (GitHub Container Registry, Docker Hub, etc.)
- Cloudflare Tunnel or a public DNS record pointing to your Traefik ingress (ChatGPT must reach the bridge over the internet)
- `cert-manager` + `letsencrypt-production` ClusterIssuer (same as other services in this repo)

## Build and push the image

A GitHub Actions workflow ([.github/workflows/docker-publish.yml](.github/workflows/docker-publish.yml)) builds and pushes to **both** Docker Hub and GHCR automatically on every push to `main`.

### One-time GitHub repo setup

1. Create a new GitHub repo and push this folder to it:

```bash
cd ~/Projects/chatgpt-overseerr-bridge
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/YOUR_GH_USER/chatgpt-overseerr-bridge.git
git push -u origin main
```

2. In the GitHub repo → **Settings → Secrets and variables → Actions**, add:

| Type | Name | Value |
|------|------|-------|
| Secret | `DOCKERHUB_TOKEN` | Docker Hub access token (Hub → Account Settings → Security → New Token) |
| Variable | `DOCKERHUB_USERNAME` | Your Docker Hub username |
| Variable | `GHCR_USERNAME` | Your GitHub username |

3. The workflow runs automatically on the next push. Watch it under **Actions**.

### Manual build (optional)

If you want to build locally before pushing:

```bash
cd ~/Projects/chatgpt-overseerr-bridge
docker build -t YOUR_DOCKERHUB_USER/chatgpt-overseerr-bridge:latest .
docker push YOUR_DOCKERHUB_USER/chatgpt-overseerr-bridge:latest
```

Then update `k8s/deployment.yaml` → `image:` to match.

## Deploy

### 1. Generate a bridge token

This is the secret ChatGPT will send with every request. Keep it safe.

```bash
openssl rand -hex 32
```

### 2. Get your Overseerr API key

Overseerr UI → **Settings → General → API Key**

### 3. Create the secret (do not commit real values)

```bash
kubectl apply -f k8s/namespace.yaml

kubectl -n chatgpt-bridge create secret generic bridge-secrets \
  --from-literal=OVERSEERR_API_KEY=<overseerr-api-key> \
  --from-literal=BRIDGE_TOKEN=<token-from-step-1>
```

### 4. Apply remaining manifests

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml
```

### 5. Verify

```bash
kubectl -n chatgpt-bridge rollout status deploy/chatgpt-bridge

# Health check (from inside the cluster or via ingress)
curl https://chatgpt-bridge.local.phoenixlab.me/health
# → {"status":"ok"}
```

## Expose publicly for ChatGPT

ChatGPT needs an HTTPS URL reachable from the internet. Two options:

### Option A — Cloudflare Tunnel (recommended, already deployed)

In your Cloudflare Zero Trust dashboard add a public hostname:

| Field | Value |
|-------|-------|
| Subdomain | `chatgpt-bridge` |
| Domain | `phoenixlab.me` (or your public domain) |
| Service | `http://chatgpt-bridge.chatgpt-bridge.svc.cluster.local:8000` |

This gives you `https://chatgpt-bridge.phoenixlab.me` without exposing any ports.

### Option B — Traefik ingress with public DNS

Update `k8s/ingress.yaml` with your public hostname and add a DNS record pointing to your WAN IP.

## Configure the ChatGPT custom action

In ChatGPT: **Your profile → My GPTs → (your GPT or use a temp GPT) → Configure → Actions → Create new action**

Paste this OpenAPI schema (update `url` with your actual public hostname):

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "Overseerr Media Request",
    "version": "1.0.0"
  },
  "servers": [
    { "url": "https://chatgpt-bridge.phoenixlab.me" }
  ],
  "paths": {
    "/request": {
      "post": {
        "operationId": "requestMedia",
        "summary": "Request a movie or TV show to be added to Plex",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                  "query": {
                    "type": "string",
                    "description": "Title of the movie or TV show to request"
                  },
                  "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "default": "movie",
                    "description": "Whether this is a movie or a TV show"
                  }
                }
              }
            }
          }
        },
        "responses": {
          "200": {
            "description": "Request result",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "properties": {
                    "message": { "type": "string" },
                    "title":   { "type": "string" },
                    "status":  { "type": "string" }
                  }
                }
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "securitySchemes": {
      "bearerAuth": {
        "type": "http",
        "scheme": "bearer"
      }
    }
  },
  "security": [{ "bearerAuth": [] }]
}
```

Under **Authentication** in the action setup, select **API Key → Bearer** and paste the `BRIDGE_TOKEN` you generated earlier.

### GPT instructions (add to your GPT's system prompt)

```
When the user asks to watch, add, or request a movie or TV show, call the requestMedia action.
Set media_type to "tv" for series and "movie" for films.
Relay the message field from the response back to the user verbatim.
```

## Usage example

> **You:** Add The Equalizer to my watch list  
> **GPT:** 'The Equalizer' has been requested and will be added to Plex shortly.

> **You:** Can you add Breaking Bad?  
> **GPT:** 'Breaking Bad' has been requested and will be added to Plex shortly.

## Lessons learned from this homelab stack

| Issue | Cause | Fix |
|-------|-------|-----|
| Pod can't resolve `overseerr.local.phoenixlab.me` | CoreDNS doesn't know about Pi-hole records | Use in-cluster DNS: `overseerr.overseerr.svc.cluster.local:5055` |
| Secret values overwritten on restart | App reads env at startup from k8s Secret, not config file | Values are safe in k8s Secret — unlike config.ini (see Tautulli README) |
| ChatGPT can't reach the bridge | Bridge on local DNS only | Expose via Cloudflare Tunnel or public ingress hostname |
| Overseerr API key leaks | Committed to YAML | Use `kubectl create secret` — never commit real values to `secret.yaml` |

## Files

```
chatgpt-overseerr-bridge/
├── app/
│   └── main.py              # FastAPI bridge app
├── requirements.txt
├── Dockerfile
├── k8s/
│   ├── namespace.yaml
│   ├── secret.yaml          # template only — do not commit real values
│   ├── configmap.yaml       # OVERSEERR_URL (in-cluster)
│   ├── deployment.yaml
│   ├── service.yaml
│   └── ingress.yaml         # local HTTPS via Traefik + cert-manager
└── README.md
```
# chatgptoverseerrbridge
