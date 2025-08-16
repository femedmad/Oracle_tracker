#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).parent.resolve()
REPO_GIT_URL  = os.environ.get("ORACLE_REPO_GIT_URL", "https://github.com/femedmad/defillama-server.git")
REPO_WEB_URL  = os.environ.get("ORACLE_REPO_WEB_URL", "https://github.com/femedmad/defillama-server")
REPO_CLONE_PATH = BASE_DIR / "defillama-server"
DATA_SUBDIR     = "defi/src/protocols"
DATA_ABS_PATH   = REPO_CLONE_PATH / DATA_SUBDIR

# ▶️ This should now point to your Tree-sitter tracker with HTML print_human
TRACKER_PATH    = BASE_DIR / "track_oracles_ts.py"

RUN_INTERVAL_S = int(os.environ.get("ORACLE_RUN_INTERVAL_S", "30"))

TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()
TG_PARSE_MODE = os.environ.get("TG_PARSE_MODE", "HTML")   # ⬅️ HTML mode for rich formatting
SEND_IF_NO_CHANGES = os.environ.get("ORACLE_SEND_IF_NO_CHANGES", "1") == "1"

ALLOWED_DATA_FILES = {"data.ts", "data1.ts", "data2.ts", "data3.ts", "data4.ts"}

def load_env_file(path: Path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        os.environ[k] = v
    global TG_BOT_TOKEN, TG_CHAT_ID, TG_PARSE_MODE
    TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
    TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()
    TG_PARSE_MODE = os.environ.get("TG_PARSE_MODE", "HTML")

def run(cmd, cwd=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nExit: {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result

def ensure_repo():
    if not TRACKER_PATH.exists():
        raise FileNotFoundError(f"TRACKER_PATH missing: {TRACKER_PATH}")
    if not REPO_CLONE_PATH.exists():
        print(f"✅ Cloning {REPO_GIT_URL} -> {REPO_CLONE_PATH}")
        run(["git", "clone", REPO_GIT_URL, str(REPO_CLONE_PATH)], check=True)
    elif not (REPO_CLONE_PATH / ".git").exists():
        raise RuntimeError(f"{REPO_CLONE_PATH} is not a git repo")

def git_pull():
    run(["git", "fetch", "--all"], cwd=REPO_CLONE_PATH)
    run(["git", "reset", "--hard", "origin/HEAD"], cwd=REPO_CLONE_PATH)

def last_commit_for_file_rel_to_repo(file_rel_to_repo):
    r = run(["git", "log", "-n", "1", "--pretty=format:%H", "--", file_rel_to_repo],
            cwd=REPO_CLONE_PATH, check=False)
    return (r.stdout or "").strip() or None

def add_commit_link_to_output(text: str) -> str:
    """
    For lines like:
      🛠️ <b>Protocol …</b> (id …) on <i>file.ts</i> has the following changes:
    append (Commit) as an HTML link.
    """
    lines, out = text.splitlines(), []
    for line in lines:
        if "has the following changes:" in line and "Protocol " in line:
            try:
                file_part = line.split(" on ", 1)[1].split(" has the following changes:", 1)[0]
                file_part_clean = file_part.replace("<i>", "").replace("</i>", "").strip()
            except Exception:
                file_part_clean = None
            if file_part_clean and file_part_clean in ALLOWED_DATA_FILES:
                sha = last_commit_for_file_rel_to_repo(f"{DATA_SUBDIR}/{file_part_clean}")
                if sha:
                    link = f'<a href="{REPO_WEB_URL}/commit/{sha}">Commit</a>'
                    if line.rstrip().endswith("changes:"):
                        line = line.rstrip()[:-1] + f" ({link}):"
                    else:
                        line += f" ({link})"
        out.append(line)
    return "\n".join(out)

def run_tracker_and_collect_output(dry_run: bool) -> str:
    cmd = ["python3", str(TRACKER_PATH), "--repo", str(DATA_ABS_PATH), "--out", "human"]
    if dry_run:
        cmd.append("--dry-run")
    res = run(cmd, check=False)
    stdout, stderr = (res.stdout or "").strip(), (res.stderr or "").strip()
    if stdout:
        return add_commit_link_to_output(stdout)
    if stderr:
        return f"(tracker stderr)\n{stderr}"
    return "(tracker produced no output)"

def send_telegram(text: str, tg_ready: bool):
    if not tg_ready:
        print("⚠️  Telegram disabled: missing TG_BOT_TOKEN or TG_CHAT_ID.\n")
        print(text)
        return
    try:
        run([
            "curl","-sS",f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            "-d",f"chat_id={TG_CHAT_ID}","-d",f"parse_mode={TG_PARSE_MODE}",
            "--data-urlencode",f"text={text}"
        ], check=True)
    except Exception as e:
        print(f"⚠️  Failed to send Telegram message: {e}")
        print("Message was:\n", text)

def do_one_cycle(dry_run: bool, tg_ready: bool):
    ensure_repo()
    print("✅ Repo ready:", REPO_CLONE_PATH)
    print("✅ Data subdir:", DATA_ABS_PATH)
    git_pull()
    txt = run_tracker_and_collect_output(dry_run=dry_run)

    print("\n=== Tracker Output ===")
    print(txt or "(no output)")

    if dry_run:
        print("ℹ️  DRY-RUN: not sending to Telegram and not updating snapshot.")
        return

    if txt.startswith("(tracker stderr)"):
        send_telegram(txt, tg_ready)
    elif "Initialized snapshot" in txt:
        print("ℹ️  Snapshot initialized. Next run will show changes.")
        if SEND_IF_NO_CHANGES: send_telegram("✨ No oracle changes today!", tg_ready)
    elif "No oracle changes today!" in txt:
        print("ℹ️  No changes.")
        if SEND_IF_NO_CHANGES: send_telegram("✨ No oracle changes today!", tg_ready)
    elif "Protocol " in txt:
        print("✅ Changes detected. Sending to Telegram.")
        send_telegram(txt, tg_ready)
    else:
        print("ℹ️  No actionable output detected.")

def main():
    parser = argparse.ArgumentParser(description="Oracle change detector runner.")
    parser.add_argument("--once", action="store_true", help="Run a single cycle then exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not update snapshot and do not send Telegram.")
    parser.add_argument("--env-file", default=".env", help="Path to .env with TG_BOT_TOKEN, TG_CHAT_ID, etc.")
    args = parser.parse_args()

    env_path = (BASE_DIR / args.env_file) if not os.path.isabs(args.env_file) else Path(args.env_file)
    load_env_file(env_path)
    tg_ready = bool(os.environ.get("TG_BOT_TOKEN", "").strip() and os.environ.get("TG_CHAT_ID", "").strip())
    if not tg_ready:
        print("⚠️  Telegram disabled: missing TG_BOT_TOKEN or TG_CHAT_ID.")

    if args.once:
        do_one_cycle(dry_run=args.dry_run, tg_ready=tg_ready)
    else:
        while True:
            print(f"\n[{datetime.now(timezone.utc).isoformat()}] 🔁 Pulling & checking…")
            try:
                do_one_cycle(dry_run=args.dry_run, tg_ready=tg_ready)
            except Exception as e:
                err = f"⚠️ Runner error: {e}"
                print(err)
                if tg_ready: send_telegram(err, tg_ready)
            time.sleep(RUN_INTERVAL_S)

if __name__ == "__main__":
    main()
