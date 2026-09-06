#!/bin/bash
# Install Docker Engine (rootful) on Debian 12 (bookworm)
# Usage: sudo bash install-docker.sh

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo bash install-docker.sh"
  exit 1
fi

TARGET_USER="${SUDO_USER:-$USER}"
echo "==> Target user: $TARGET_USER"

echo "==> [1/7] Removing podman-docker alias to avoid conflict"
apt-get remove -y podman-docker || true

echo "==> [2/7] Installing prerequisites"
apt-get update
apt-get install -y ca-certificates curl gnupg

echo "==> [3/7] Adding Docker official GPG key"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "==> [4/7] Adding Docker apt source"
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
  > /etc/apt/sources.list.d/docker.list

echo "==> [5/7] Installing Docker Engine"
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "==> [6/7] Adding $TARGET_USER to docker group"
usermod -aG docker "$TARGET_USER"

echo "==> [7/7] Enabling and starting docker service"
systemctl enable --now docker

echo "==> Verifying"
docker version
echo
echo "Done. To use docker without sudo, run:  newgrp docker   (or re-login)"
