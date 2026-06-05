#!/usr/bin/env python3
"""cc-fasrc PreToolUse guard — confine writes to the sandbox tree.

Reads the tool-call JSON on stdin. Exit 0 = allow, exit 2 = block (reason -> stderr,
which Claude Code surfaces back to the model).

Guarantees:
  - Write / Edit / NotebookEdit : HARD. The structured file_path is resolved and
    must sit under an allowed root, else blocked. This covers the bulk of CC edits.
  - Bash                        : BEST-EFFORT. The shell is arbitrary, so this scans
    for the common destructive mistakes (rm -rf /, redirects, mv/cp/rm/chmod/... to
    a path outside the sandbox). It is defense-in-depth, not a kernel boundary.
    For a hard guarantee, use the Apptainer mode (see README).
"""
import json, os, re, sys

SANDBOX = os.environ.get("CC_SANDBOX_DIR") or "@SANDBOX@"
HOME = os.path.expanduser("~")
WRITABLE = [SANDBOX, f"{HOME}/.local", f"{HOME}/.cache", f"{HOME}/.config",
            "/tmp", os.environ.get("TMPDIR", "")]
PASS_DEVS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def _real(p):
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def allowed(path):
    if path in PASS_DEVS:
        return True
    rp = _real(path)
    for root in WRITABLE:
        if not root:
            continue
        rr = _real(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def block(msg):
    sys.stderr.write(
        f"[cc-fasrc guard] BLOCKED: {msg}\n"
        f"Writable roots: {SANDBOX} (+ ~/.local ~/.cache ~/.config /tmp)\n"
    )
    sys.exit(2)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail-open on unparseable input; CC's own perms still apply
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool in ("Write", "Edit", "NotebookEdit"):
        p = ti.get("file_path") or ti.get("notebook_path")
        if p and not allowed(p):
            block(f"{tool} -> {p} is outside the sandbox")
        sys.exit(0)

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        flat = cmd.replace(" ", "")
        # 1) hard foot-guns
        if "rm-rf/" in flat or "rm-fr/" in flat or re.search(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f", cmd) and re.search(r"\s/(?:\s|$)", cmd):
            block("recursive removal targeting /")
        if ":(){" in flat:
            block("fork bomb")
        # 2) output redirections to an absolute/home path
        for m in re.finditer(r"(?:>>?|\btee\b(?:\s+-a)?)\s*([~/][^\s;|&)<>]+)", cmd):
            if not allowed(m.group(1)):
                block(f"redirect writes to {m.group(1)}")
        # 3) mutating verbs naming an absolute/home path outside the sandbox
        if re.search(r"\b(rm|mv|cp|dd|truncate|chmod|chown|chgrp|ln|mkdir|rmdir|shred|install|rsync)\b", cmd):
            for tok in re.findall(r"(?<![\w=])(/[^\s;|&)<>'\"]+|~/[^\s;|&)<>'\"]+)", cmd):
                if not allowed(tok):
                    block(f"{tok} (mutating command touches a path outside the sandbox)")
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
