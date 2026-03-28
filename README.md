# Friendly text notifications

```bash
podman run --interactive --tty --rm \
  --name friendly-reminder \
  --userns keep-id \
  --user "$(id -u):$(id -g)" \
  --env HOME=/home/user \
  --tmpfs /home/user:rw \
  --volume "$(pwd)":/workspace:Z \
  --workdir /workspace \
  docker.io/docker/sandbox-templates:claude-code \
  bash
```
