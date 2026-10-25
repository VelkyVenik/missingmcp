#!/bin/sh
# Roll the gateway to one image tag (a full commit SHA) and roll back if it
# doesn't come up healthy — see deploy/README.md.
#
# GitHub Actions calls this over SSH as a forced command
#   (authorized_keys: command="/opt/missingmcp/deploy.sh",restrict ssh-ed25519 ...)
# so its only input is SSH_ORIGINAL_COMMAND. By hand: ./deploy.sh <sha>
set -eu
cd "$(dirname "$0")"

tag="${SSH_ORIGINAL_COMMAND:-${1:-}}"
case "$tag" in
	*[!0-9a-f]* | "") echo "usage: deploy.sh <40-char commit sha>" >&2; exit 2 ;;
esac
[ "${#tag}" -eq 40 ] || { echo "usage: deploy.sh <40-char commit sha>" >&2; exit 2; }

prev=$(sed -n 's/^IMAGE_TAG=//p' .env 2>/dev/null || true)

healthy() {
	i=0
	while [ "$i" -lt 60 ]; do
		curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:8080/healthz && return 0
		i=$((i + 1))
		sleep 2
	done
	return 1
}

IMAGE_TAG="$tag" docker compose pull --quiet gateway
echo "IMAGE_TAG=$tag" > .env

if docker compose up -d gateway && healthy; then
	echo "deployed $tag"
	# Keep a week of images for manual rollback (./deploy.sh <older sha>).
	docker image prune -af --filter until=168h > /dev/null
	exit 0
fi

echo "unhealthy after deploy: $tag" >&2
docker compose logs --tail 50 gateway >&2 || true
if [ -n "$prev" ] && [ "$prev" != "$tag" ]; then
	echo "rolling back to $prev" >&2
	echo "IMAGE_TAG=$prev" > .env
	docker compose up -d gateway
	healthy && echo "rolled back to $prev" >&2
fi
exit 1
