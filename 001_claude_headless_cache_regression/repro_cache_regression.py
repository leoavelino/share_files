#!/usr/bin/env python3
"""
Minimal repro: Claude Code >= 2.1.203 emits SessionStart-hook context as a
trailing `role:"system"` message placed AFTER the final cache_control
breakpoint in headless (-p) sessions, so it re-bills as uncached input tokens
on every request. Through 2.1.202 the same content was embedded in the first
user message, inside the final breakpoint, and cached normally.

No API key required: the script points ANTHROPIC_BASE_URL at a local capture
sink, so the CLI's outgoing /v1/messages request body is recorded before any
network auth matters (the API call itself intentionally fails afterwards).

Usage:
  python3 repro_cache_regression.py \
      --old-bin "npx -y @anthropic-ai/claude-code@2.1.202" \
      --new-bin "npx -y @anthropic-ai/claude-code@2.1.203"

  (any way of invoking the two versions works; pass explicit binary paths
   if you have them installed side by side)
"""

import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

MARKER = "SYNTHETIC-HOOK-CONTEXT"
HOOK_CONTEXT_BYTES = 8000
PROMPT = "Reply with just: ok"


def make_workspace(root: str) -> dict:
    """Create a throwaway HOME, project dir, hook script, and settings file."""
    home = os.path.join(root, "home")
    project = os.path.join(root, "project")
    os.makedirs(home, exist_ok=True)
    os.makedirs(project, exist_ok=True)

    filler = (MARKER + ": all agents must consider synthetic rule %d. ")
    context = "".join(filler % i for i in range(1, 400))[:HOOK_CONTEXT_BYTES]

    hook_path = os.path.join(root, "session_start_hook.py")
    with open(hook_path, "w") as f:
        f.write(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"ctx = {context!r}\n"
            "print(json.dumps({'hookSpecificOutput': {"
            "'hookEventName': 'SessionStart',"
            "'additionalContext': ctx}}))\n"
        )
    os.chmod(hook_path, 0o755)

    settings_path = os.path.join(root, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {hook_path}",
                                }
                            ]
                        }
                    ]
                }
            },
            f,
        )
    return {"home": home, "project": project, "settings": settings_path}


class _Sink(http.server.BaseHTTPRequestHandler):
    capture_path = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if not os.path.exists(self.capture_path):
            with open(self.capture_path, "wb") as f:
                f.write(body)
        resp = json.dumps(
            {"error": {"type": "api_error", "message": "capture sink"}}
        ).encode()
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


def capture_request(cli_cmd: str, ws: dict, capture_path: str) -> dict:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    handler = type("H", (_Sink,), {"capture_path": capture_path})
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env.update(
        {
            "HOME": ws["home"],
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_API_KEY": "sk-ant-dummy-capture-only",
            "CLAUDE_CODE_MAX_RETRIES": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    cmd = cli_cmd.split() + [
        "-p", PROMPT,
        "--model", "claude-sonnet-5",
        "--settings", ws["settings"],
        "--strict-mcp-config",
    ]
    log_path = capture_path + ".cli-output.txt"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            cmd, cwd=ws["project"], env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 120
        while time.time() < deadline and not os.path.exists(capture_path):
            if proc.poll() is not None:
                time.sleep(2)  # grace period for a request already in flight
                break
            time.sleep(0.5)
        proc.terminate()
    server.shutdown()
    if not os.path.exists(capture_path):
        with open(log_path, errors="replace") as f:
            tail = "".join(f.readlines()[-15:]).strip()
        exit_note = (
            f"CLI exited early with code {proc.returncode}"
            if proc.returncode is not None
            else "CLI did not exit (timed out waiting for a request; "
            "check that the CLI command can actually run, e.g. npx has "
            "network access)"
        )
        raise RuntimeError(
            f"no request captured for: {cli_cmd}\n"
            f"{exit_note}. Last CLI output:\n{tail or '(no output)'}"
        )
    with open(capture_path) as f:
        return json.load(f)


def analyze(req: dict) -> None:
    def blocks():
        for i, b in enumerate(req.get("system") or []):
            yield f"system[{i}]", b
        for mi, m in enumerate(req.get("messages") or []):
            content = m.get("content")
            if isinstance(content, str):
                yield f"messages[{mi}] role={m['role']} (plain string)", {
                    "type": "text", "text": content,
                }
                continue
            for bi, b in enumerate(content):
                yield f"messages[{mi}][{bi}] role={m['role']}", b

    entries = list(blocks())
    last_cc = max(
        (i for i, (_, b) in enumerate(entries) if isinstance(b, dict) and "cache_control" in b),
        default=None,
    )
    marker_pos = None
    for i, (label, b) in enumerate(entries):
        text = b.get("text", "") if isinstance(b, dict) else ""
        cc = " [cache_control]" if isinstance(b, dict) and "cache_control" in b else ""
        has_marker = MARKER in text
        marker_note = "  <-- HOOK CONTEXT" if has_marker else ""
        if has_marker:
            marker_pos = i
        print(f"    {label}: {b.get('type', '?')} len={len(text)}{cc}{marker_note}")

    if marker_pos is None:
        print("    VERDICT: hook context not found in request (hook did not fire?)")
    elif last_cc is not None and marker_pos <= last_cc:
        print("    VERDICT: hook context is INSIDE the final cache_control prefix (cacheable)")
    else:
        print("    VERDICT: hook context is AFTER the final cache_control breakpoint (uncacheable, re-billed every request)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-bin", default="npx -y @anthropic-ai/claude-code@2.1.202")
    ap.add_argument("--new-bin", default="npx -y @anthropic-ai/claude-code@2.1.203")
    ap.add_argument(
        "--keep-captures", action="store_true",
        help="save the raw captured request bodies to the current directory "
             "(note: they include your local hook/skills content)",
    )
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix="cc_cache_repro_")
    try:
        ws = make_workspace(root)
        for name, cli in (("OLD", args.old_bin), ("NEW", args.new_bin)):
            print(f"\n== {name}: {cli}")
            capture_path = os.path.join(root, f"req_{name}.json")
            req = capture_request(cli, ws, capture_path)
            analyze(req)
            if args.keep_captures:
                dest = os.path.join(os.getcwd(), f"captured_request_{name}.json")
                shutil.copyfile(capture_path, dest)
                print(f"    saved raw request body to {dest}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
