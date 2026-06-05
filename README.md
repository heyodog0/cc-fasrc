# cc-fasrc

Run Claude Code **perpetually on FASRC**, sandboxed so it can only write inside
your lab tree — while `sbatch`, `squeue`, `uv`, and `node` all work natively.

Clone it once, run `./install.sh`, and from then on every `cc` / `claude` you
launch in any tmux pane is sandboxed by default.

```
ssh you@login.rc.fas.harvard.edu
git clone <this-repo> ~/code/cc-fasrc
cd ~/code/cc-fasrc
CC_SANDBOX_DIR=/n/holylabs/<your_lab>/Users/$USER ./install.sh
cc-doctor      # verify every assumption on this node (run this first)
cc-up          # perpetual sandboxed CC in a pinned tmux
```

## Verify before you trust it

`cc-doctor` checks login-node status, python3/tmux/claude/sbatch, sandbox
writability, valid settings, and runs the guard self-test. The **one thing it
can't check** is whether your CC build honors `CLAUDE_CONFIG_DIR` — so after
`cc` starts, run `/hooks` inside the session (the PreToolUse guard must be
listed), or ask CC to write to `~/.bashrc` and confirm it's blocked. That's the
real proof the sandbox is live.

**Defaults:** sessions start in `auto` permission mode (a safety classifier
auto-handles prompts — good for unattended runs; the guard hook is still a hard
pre-filter underneath). Pass `CC_REMOTE_CONTROL=1 ./install.sh` to also turn on
remote control (steer the session from claude.ai / the mobile app) at startup.
Both live in the rendered `settings.json`; change them there (from a plain shell)
or re-run the installer. Note the guard protects its own `guard-write.py` from
being edited by CC — config changes are a human action, not an agent one.

## What it gives you

- **One writable tree.** All edits are confined to `$CC_SANDBOX_DIR` (your lab
  dir), plus the package/install caches (`~/.local`, `~/.cache`, `~/.config`,
  `/tmp`) — your "unless it needs to download / pip install" carve-out.
- **`sbatch` just works.** CC runs *directly on the login node* (no container),
  so it submits jobs, polls `squeue`/`sacct`, and tails `logs/<id>.out` natively.
- **The results bottleneck is gone.** Jobs write `outputs/` and `logs/` into the
  same `/n/holylabs` tree CC sits in, so CC reads progress **live** — no
  `pull_results.sh`, no rsync, no W&B round-trip, no 2FA. CC can even *view* the
  rollout GIFs/plots itself.
- **Sandboxed by default.** `install.sh` aliases `claude` → `cc`, so muscle
  memory stays safe across every pane.

## How the boundary is enforced

A **PreToolUse guard hook** (`hooks/guard-write.py`) gates every tool call:

| Tool | Enforcement |
|------|-------------|
| `Write` / `Edit` / `NotebookEdit` | **Hard.** `file_path` is resolved; outside the sandbox → blocked. Covers ~95% of CC's file changes. |
| `Bash` | **Best-effort, source/dest aware.** Blocks `rm -rf /`, redirects, and `mv`/`cp`/`rm`/`chmod`/… whose *destination* is outside the sandbox — while allowing reads FROM anywhere (`cp /n/shared/data ./` works). Defense-in-depth, *not* a kernel boundary. |

Plus belt-and-suspenders `deny` rules in `settings.json` for `~/.bashrc`,
`~/.zshrc`, `~/.ssh`, `~/.profile` (the files whose corruption would lock you out).

This is tuned for **"don't let CC make a mistake,"** not for confining a
malicious agent. A determined `bash` one-liner could still escape the heuristic —
if you need a hard kernel guarantee, see *Apptainer mode* below.

## Two environments (important)

- **CC's sandbox** — login node. CC edits code and submits jobs. Sandboxed here.
- **The SLURM job** — compute node, *outside* the sandbox, full GPU/CUDA/module
  access. This is correct: you never wanted to sandbox the training run.

Because of this, **bootstrap your toolchain separately, as yourself** (your
project's own setup script — installing node/uv/conda, cloning repos, etc.). That
step legitimately writes the toolchain into `~/.local` and env into `~/.bashrc` so
the *compute-node job* inherits it — let a human run it once, not CC.

## Perpetual session

`tmux` keeps CC alive across disconnects. The catch: FASRC load-balances several
login nodes, and your session lives on **one** of them. `cc-up` prints the
hostname — reconnect with `ssh <you>@<that-host>.rc.fas.harvard.edu` then `cc-up`.
A maintenance reboot kills the tmux; `cc-up` makes the restart one command.

Keep CC a **coordinator**, not a worker: it should `sbatch` + poll, never run
heavy compute on the login node. (Tiny CPU smokes are fine — those are explicitly
login-node-safe.)

## Headless auth

So CC survives reboots with no browser/laptop:

```bash
umask 077; printf %s 'sk-ant-...' > "$CC_SANDBOX_DIR/.cc/.claude/api-key"
```

Or run `cc` once and complete the OAuth URL by hand — the token persists in the
config dir.

## Files

```
install.sh                 one-time setup (idempotent)
config/settings.json.tmpl  deny rules + hook registration (templated per-account)
hooks/guard-write.py       the write-confinement guard
bin/cc                     sandboxed launcher (sets CLAUDE_CONFIG_DIR + guard)
bin/cc-up                  perpetual pinned-tmux wrapper
bin/cc-doctor              preflight verifier (run after install)
config.env                 generated by install.sh — edit CC_SANDBOX_DIR / CC_MODE here
cc.def, lib/cc-sandbox     OPT-IN Apptainer hardening (see below)
bin/cc-iso                 launch sealed CC + host submit proxy (Apptainer mode)
bin/cc-proxyd              start/stop/status/tail the host-side Slurm proxy
bin/cc-approve             approve/deny a held sbatch (CC_PROXY_MODE=hold)
lib/slurm-proxy/proxyd     the host-side daemon (allowlisted Slurm broker)
lib/slurm-proxy/shim       in-container fake sbatch/squeue/... -> proxy
```

## Why not Docker?

Docker needs a root daemon, which shared HPC clusters (FASRC included) don't
allow — one `docker run -v /:/host` is a root-on-node escape. The rootless
equivalent is **Apptainer**, already provided in *Apptainer mode* below. There's
no Docker path on FASRC, by design.

## Apptainer mode (opt-in, hard kernel boundary)

For a boundary that survives *any* command (not just the heuristic guard), run CC
sealed inside Apptainer with only the sandbox tree bound read-write. Slurm still
works — via a **host-side submit proxy**, so you never bind host `sbatch`/munge
into the container (which fails on the glibc mismatch).

```bash
cd ~/code/cc-fasrc
apptainer build cc.sif cc.def      # ~once; needs --fakeroot or build elsewhere & scp
cc-iso                             # starts the proxy, launches sealed CC
```

### How Slurm crosses the boundary

```
   ┌─ Apptainer container (CC, sealed) ──────┐        ┌─ host login shell (tmux) ─┐
   │  sbatch run.sh                          │        │                           │
   │   └─ shim writes requests/<id>.json ────┼──┐     │   cc-proxyd (proxyd)      │
   │      then waits for results/<id>.json   │  │     │    polls requests/        │
   └─────────────────────────────────────────┘  │     │    validates + runs REAL  │
              shared sandbox tree (/n/holylabs)  └────▶│    /usr/bin/sbatch        │
                  ...results/<id>.json ◀───────────────┤    writes results/<id>    │
                                                       └───────────────────────────┘
```

The container ships fake `sbatch`/`squeue`/`sacct`/`scancel`/`sinfo`/`scontrol`
that drop a request file in the shared tree; `cc-proxyd` (running **outside**, in
your real login-shell env, so submitted jobs inherit the correct `PATH`/`NODE_GYM`/
`UV_CACHE_DIR`) runs the real command and writes the result back. Pure file IPC —
works on Lustre/NFS where sockets/inotify don't.

### The proxy is the trust boundary

`cc-proxyd` is the one privileged hole, so it is deliberately narrow:

- **Allowlist only** — `sbatch squeue sacct scancel sinfo scontrol`, nothing else.
- **No shell** — args go straight to the real binary as an argv list (no injection).
- **`sbatch` scripts must live inside the sandbox**; `--wrap` inline commands are
  refused. (A submitted job still runs as you on a compute node — this confines
  the login-node filesystem, not what a job you wrote chooses to do.)
- **Audited** — every request logged to `slurm-proxy/proxyd.log` (`cc-proxyd tail`).
- **Optional human gate** — `CC_PROXY_MODE=hold` in `config.env` holds every
  `sbatch` until you run `cc-approve <id>` (id printed by `cc-proxyd tail`). Good
  for fully-unattended runs.

### Controls

```bash
cc-proxyd start|stop|status|tail    # manage the daemon (cc-iso auto-starts it)
cc-approve <id> [--deny]            # release/reject a held submission (hold mode)
```

Permission-guard mode (`cc`/`cc-up`) and iso mode (`cc-iso`) can coexist: same
sandbox tree, same config dir. Use `cc` for day-to-day, `cc-iso` when you want the
hard wall.
