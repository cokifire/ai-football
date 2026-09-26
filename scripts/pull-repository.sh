#!/usr/bin/env bash
set -Eeuo pipefail

# Pull the configured Git repository and make the working tree match the remote
# branch. Ignored files such as backend/.env are left in place.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
REMOTE_NAME="${REMOTE_NAME:-origin}"
BRANCH="${BRANCH:-main}"
KEY_FILE="${GIT_SSH_KEY:-/home/ubuntu/.ssl/id_ed25519}"
FINGERPRINT_FILE="${GIT_SSH_FINGERPRINT_FILE:-/home/ubuntu/.ssl/authorized_key}"
KNOWN_HOSTS_FILE="${GIT_KNOWN_HOSTS:-${HOME}/.ssh/known_hosts}"

usage() {
  cat <<'USAGE'
用法: scripts/pull-repository.sh [--yes]

从 origin 拉取 BRANCH，并将当前工作区重置为远程版本。
环境变量:
  REMOTE_NAME                 远程名称，默认 origin
  BRANCH                      分支名称，默认 main
  GIT_SSH_KEY                 GitHub SSH 私钥，默认 /home/ubuntu/.ssl/id_ed25519
  GIT_SSH_FINGERPRINT_FILE    可选，保存 SHA256 指纹的文件
  GIT_KNOWN_HOSTS             SSH known_hosts 文件

--yes                         确认丢弃本地未提交修改和未跟踪文件
USAGE
}

CONFIRM=false
case "${1:-}" in
  --yes) CONFIRM=true ;;
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 2 ;;
esac

if [[ ! -d "$ROOT_DIR/.git" ]]; then
  echo "错误：不是 Git 仓库：$ROOT_DIR" >&2
  exit 1
fi
if [[ ! -r "$KEY_FILE" ]]; then
  echo "错误：找不到 GitHub SSH 私钥：$KEY_FILE" >&2
  echo "SHA256 指纹不能替代私钥，请设置 GIT_SSH_KEY。" >&2
  exit 1
fi

if [[ -f "$FINGERPRINT_FILE" ]]; then
  expected="$(sed -nE 's/.*(SHA256:[A-Za-z0-9+/=]+).*/\1/p' "$FINGERPRINT_FILE" | head -n 1)"
  if [[ -n "$expected" ]]; then
    actual="$(ssh-keygen -y -f "$KEY_FILE" 2>/dev/null | ssh-keygen -lf - -E sha256 | awk '{print $2}')"
    if [[ "$actual" != "$expected" ]]; then
      echo "错误：私钥指纹不匹配。" >&2
      echo "期望：$expected" >&2
      echo "实际：$actual" >&2
      exit 1
    fi
  fi
fi

if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" && "$CONFIRM" != true ]]; then
  echo "当前工作区有本地修改或未跟踪文件；覆盖操作会丢弃它们。" >&2
  echo "如确认覆盖，请重新执行：$0 --yes" >&2
  exit 1
fi

if [[ ! -r "$KNOWN_HOSTS_FILE" ]]; then
  echo "错误：找不到 SSH 主机密钥文件：$KNOWN_HOSTS_FILE" >&2
  exit 1
fi

temp_key="$(mktemp)"
cleanup() { rm -f -- "$temp_key"; }
trap cleanup EXIT
chmod 600 "$temp_key"
cp -- "$KEY_FILE" "$temp_key"

ssh_cmd=(ssh -i "$temp_key" -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile="$KNOWN_HOSTS_FILE" -o StrictHostKeyChecking=yes)
git_cmd=(git -C "$ROOT_DIR" -c core.sshCommand="${ssh_cmd[*]}")

echo "验证 GitHub SSH 连接..."
GIT_SSH_COMMAND="${ssh_cmd[*]}" git ls-remote --exit-code "$REMOTE_NAME" HEAD >/dev/null

echo "拉取 $REMOTE_NAME/$BRANCH..."
"${git_cmd[@]}" fetch --prune "$REMOTE_NAME" "$BRANCH"
"${git_cmd[@]}" checkout -B "$BRANCH" "$REMOTE_NAME/$BRANCH"
"${git_cmd[@]}" reset --hard "$REMOTE_NAME/$BRANCH"
"${git_cmd[@]}" clean -fd -e "scripts/pull-repository.sh"

echo "完成：$(git -C "$ROOT_DIR" rev-parse --short HEAD) $REMOTE_NAME/$BRANCH"
