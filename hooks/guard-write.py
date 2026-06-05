#!/usr/bin/env python3
"""cc-fasrc PreToolUse guard — confine writes to the sandbox tree.

Reads the tool-call JSON on stdin. Exit 0 = allow, exit 2 = block (reason -> stderr,
which Claude Code surfaces back to the model).

Guarantees:
  - Write / Edit / NotebookEdit : HARD. The structured file_path is resolved and
    must sit under an allowed root, else blocked. Covers the bulk of CC edits.
  - Bash                        : BEST-EFFORT, source/dest aware. Blocks redirects,
    rm/chmod/mkdir/... and copy/move *destinations* that land outside the sandbox,
    while allowing reads FROM anywhere (e.g. `cp /n/shared/data ./`). The shell is
    arbitrary, so this is defense-in-depth, not a kernel boundary. For a hard
    guarantee use the Apptainer mode (see README).

Run `guard-write.py --selftest` to verify the rules (used by install.sh / cc-doctor).
"""
import json, os, re, shlex, sys

SANDBOX = os.environ.get("CC_SANDBOX_DIR") or "@SANDBOX@"
HOME = os.path.expanduser("~")
WRITABLE = [SANDBOX, f"{HOME}/.local", f"{HOME}/.cache", f"{HOME}/.config",
            "/tmp", os.environ.get("TMPDIR", "")]
PASS_DEVS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero"}

# verbs where EVERY path arg is written/destroyed
ALL_ARGS = {"rm", "rmdir", "shred", "truncate", "chmod", "chown", "chgrp",
            "mkdir", "touch", "unlink"}
# verbs of form `CMD [opts] SRC... DEST` — only the final arg (DEST) is written;
# sources may be read from anywhere.
SRC_DEST = {"cp", "mv", "ln", "rsync", "install"}
# wrappers to skip past to find the real command
WRAPPERS = {"sudo", "command", "env", "nice", "nohup", "time", "stdbuf", "xargs"}


def _real(p):
    p = os.path.expanduser(os.path.expandvars(p))
    return os.path.realpath(os.path.abspath(p))


# The guard script lives inside the sandbox (writable), so without this CC could
# edit it to disable its own boundary. Protect it — changing it is a human-only
# action from a plain shell. (settings.json is left editable so in-session mode
# toggles still work.)
PROTECTED = {_real(os.path.join(SANDBOX, ".cc", ".claude", "guard-write.py"))}


def allowed(path):
    if path in PASS_DEVS:
        return True
    rp = _real(path)
    if rp in PROTECTED:
        return False
    for root in WRITABLE:
        if not root:
            continue
        rr = _real(root)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def is_abs(tok):
    return tok.startswith(("/", "~")) or tok.startswith("$HOME")


class Blocked(Exception):
    pass


def block(msg):
    raise Blocked(msg)


def check_segment(seg):
    # 1) output redirections (>, >>, N>, tee) to an absolute/home path
    for m in re.finditer(r"(?:\d?>>?|\btee\b(?:\s+-a)?)\s*([^\s;|&)<>]+)", seg):
        t = m.group(1)
        if is_abs(t) and not allowed(t):
            block(f"redirect writes to {t}")
    # 2) command-aware path checks
    try:
        toks = shlex.split(seg, comments=True)
    except ValueError:
        toks = seg.split()
    i = 0
    while i < len(toks) and ((("=" in toks[i]) and not toks[i].startswith(("/", "~")))
                             or toks[i] in WRAPPERS):
        i += 1
    if i >= len(toks):
        return
    verb = os.path.basename(toks[i])
    args = toks[i + 1:]
    if verb == "dd":
        for a in args:
            if a.startswith("of=") and is_abs(a[3:]) and not allowed(a[3:]):
                block(f"dd of={a[3:]}")
        return
    if verb in ALL_ARGS:
        for a in args:
            if is_abs(a) and not allowed(a):
                block(f"{verb} {a} (outside the sandbox)")
        return
    if verb in SRC_DEST and args:
        dest = args[-1]                      # only the destination is a write
        if is_abs(dest) and not allowed(dest):
            block(f"{verb} ... -> {dest} (destination outside the sandbox)")
        return


def evaluate(tool, ti):
    """Return None if allowed, or a reason string if it should be blocked."""
    try:
        if tool in ("Write", "Edit", "NotebookEdit"):
            p = ti.get("file_path") or ti.get("notebook_path")
            if p and _real(p) in PROTECTED:
                return f"{tool} -> {p} is the cc-fasrc guard; edit it from a plain shell, not via CC"
            if p and not allowed(p):
                return f"{tool} -> {p} is outside the sandbox"
            return None
        if tool == "Bash":
            cmd = ti.get("command", "") or ""
            if ":(){" in cmd.replace(" ", ""):
                return "fork bomb"
            for seg in re.split(r"&&|\|\||[;\n|&]", cmd):
                check_segment(seg)
            return None
    except Blocked as b:
        return str(b)
    return None


def selftest():
    cases = [
        ("Write", {"file_path": f"{SANDBOX}/analogen/x.py"}, False),
        ("Write", {"file_path": f"{SANDBOX}/.cc/.claude/guard-write.py"}, True),
        ("Write", {"file_path": f"{HOME}/.bashrc"}, True),
        ("Edit",  {"file_path": "/etc/passwd"}, True),
        ("Bash",  {"command": "sbatch run.sh configs/smokes/smoke.json"}, False),
        ("Bash",  {"command": "rm -rf /"}, True),
        ("Bash",  {"command": f"rm -rf {HOME}/important"}, True),
        ("Bash",  {"command": f"echo hi > {SANDBOX}/log.txt"}, False),
        ("Bash",  {"command": f"echo hi > {HOME}/.bashrc"}, True),
        # read FROM outside, write INTO sandbox — must be ALLOWED
        ("Bash",  {"command": f"cp /n/shared/data.txt {SANDBOX}/data.txt"}, False),
        ("Bash",  {"command": "cp /n/shared/data.txt ./local.txt"}, False),
        # move OUT of sandbox — must be BLOCKED
        ("Bash",  {"command": f"mv {SANDBOX}/a /n/holylabs/gershman_lab/Users/other/"}, True),
        ("Bash",  {"command": "ln -s /n/sw/lib mylink"}, False),
        ("Bash",  {"command": "module load cuda && uv run python train.py"}, False),
    ]
    bad = 0
    for tool, ti, want_block in cases:
        got_block = evaluate(tool, ti) is not None
        ok = got_block == want_block
        if not ok:
            bad += 1
        print(f"  [{'ok ' if ok else 'FAIL'}] {'block' if want_block else 'allow'}: "
              f"{tool} {ti.get('command', ti.get('file_path',''))}")
    print(f"{'ALL PASS' if bad == 0 else str(bad) + ' FAILED'} ({len(cases)} cases)")
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail-open on unparseable input; CC's own perms still apply
    reason = evaluate(data.get("tool_name", ""), data.get("tool_input", {}) or {})
    if reason:
        sys.stderr.write(
            f"[cc-fasrc guard] BLOCKED: {reason}\n"
            f"Writable roots: {SANDBOX} (+ ~/.local ~/.cache ~/.config /tmp)\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
