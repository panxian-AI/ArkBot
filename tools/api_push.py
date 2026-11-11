"""api_push.py — 通过 GitHub Git Data API 推送当前 HEAD 到远程仓库。

背景：本机网络对 github.com 的 TLS 被选择性阻断（git push 不可用），
但 api.github.com 可达，故用 REST 完成 blob→tree→commit→ref 全流程。
全量上传当前 HEAD 的所有文件（约 40 个，体量小，无需 diff 逻辑）。

用法::

    python tools/api_push.py <token> [commit_message]

不传 message 时使用最后一次 git commit 的 message。
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = "panxian-AI/ArkBot"
API = f"https://api.github.com/repos/{REPO}"


def api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "arkbot-api-push",
        },
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {body}") from e


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout.strip()


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        print("用法: python tools/api_push.py <token> [message]")
        return 2
    message = sys.argv[2] if len(sys.argv) > 2 else git("log", "-1", "--format=%B")

    # 1) 远程当前 main
    remote = api("GET", "/commits/main", token)
    print(f"远程 main: {remote['sha'][:7]}")

    # 2) 全量上传 blob（本地 HEAD 的全部文件；.github/workflows 需要
    #    workflow scope 的令牌，无权限时自动剔除并提示）
    entries = []
    skipped: list[str] = []
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, path = line.split("\t", 1)
        mode, otype, sha = meta.split()
        assert otype == "blob", f"跳过非 blob 条目: {path}"
        if path.startswith(".github/"):
            skipped.append(path)
            continue
        content = Path(path).read_bytes()
        blob = api("POST", "/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        assert blob["sha"] == sha, f"blob SHA 不一致 {path}: {blob['sha']} != {sha}"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    print(f"blob 上传完成: {len(entries)} 个（跳过需 workflow scope: {skipped or '无'}）")

    # 3) 建树 → 提交 → 更新 ref
    tree = api("POST", "/git/trees", token, {"tree": entries})
    commit = api("POST", "/git/commits", token, {
        "message": message, "tree": tree["sha"], "parents": [remote["sha"]],
    })
    api("PATCH", "/git/refs/heads/main", token, {"sha": commit["sha"], "force": False})
    print(f"推送成功: {commit['sha'][:7]} (parent {remote['sha'][:7]}, tree {tree['sha'][:7]})")
    if skipped:
        print("提醒: .github/workflows 变更未推送（token 需含 workflow scope），"
              "换令牌后重跑本脚本即可补上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
