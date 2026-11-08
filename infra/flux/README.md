# infra/flux

Flux sync config, applied by hand (no `flux bootstrap`, so Flux never gets write access to the repo).
The repo is public, so Flux pulls over HTTPS with no credentials.

    flux install
    kubectl apply -f infra/flux/

One Kustomization per app, each pointing at `apps/<app>/overlays/healthy`.
Switching an app to its `broken` overlay is a Git change to the `path` here, or an edit of the overlay itself.
