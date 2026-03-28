# Friendly text notifications

```bash
podman run --interactive --tty --rm \
 --name friendly-reminder \
 --uidmap 0:1:1000 \
 --uidmap 1000:0:1 \
 --uidmap 1001:1001:64536 \
 --gidmap 0:1:1000 \
 --gidmap 1000:0:1 \
 --gidmap 1001:1001:64536 \
 --env HOME=/home/agent \
 --volume "$(pwd)":/workspace:Z \
 --workdir /workspace \
 docker.io/docker/sandbox-templates:claude-code \
 bash
```

then run

```
claude update
claude --dangerously-skip-permissions
```
