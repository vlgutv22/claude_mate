# The command line

Claude Mate installs **four** commands, and until now nothing said so in one
place. They are small and they do not overlap; the confusion is that their names
all start the same way. Here is the whole surface.

| Command | What it is for | You run it |
|---|---|---|
| **`claude-mate`** | The app, and the device's buttons from a terminal | Constantly |
| **`claude-mate-wrap`** | The thing that actually runs `claude` and reports it | Never directly — see below |
| **`claude-mate-connect`** | Getting a device linked, and saying why it is not | When a device will not connect |
| **`claude-mate-switch`** | Continue this conversation on another account | When one login hits its limit |

The installer symlinks the first, third and fourth onto your `PATH`. The wrapper
is deliberately **not** linked: it is reached through a shell alias, because its
whole job is to stand in for `claude`.

---

## `claude-mate` — the app

Run it with no arguments in a terminal:

```sh
claude-mate
```

You get a full-screen app. Arrow keys move, Enter chooses, `q` quits.

```
 claude-mate   ● daemon running · 14:22:31

 SESSIONS
  ▸• api-server   working    1:23   Opus 5  xhigh  work
    web-ui        waiting    0:05   Opus 5         needs you
    docs          idle      12:40

  ▸  Start new session
     Start new session · skip permissions
     Device · link status and pairing
     Accounts · switch or review
     Quit

 ↑↓ move   ⏎ raise this session   a ack   f follow   m mirror   c continue
 r restart daemon   q quit
```

Every level under the top one works the same way: **Accounts** and **Device**
are arrow-driven menus, not a dumped `--help`, and `esc` always means back.

**Start new session** is the reason the app exists. It runs `claude` through the
wrapper in this terminal — the app hands the terminal over and gets out of the
way, exactly as if you had typed the command yourself. The second entry adds
`--dangerously-skip-permissions`. There are two entries rather than one setting
because skipping permission prompts is a decision, and a menu should say which
one it is about to make. Anything else you want on the command line goes in
`CLAUDE_MATE_NEW_ARGS`.

**On a pipe it is not an app.** Redirect it anywhere — a hook, cron, a script,
a test — and `claude-mate` is the same one-shot queue print it always was, with
no colour and no escape codes. The app appears only when stdin *and* stdout are
both terminals. `claude-mate status` gets you the plain print on purpose.

### Everything it accepts

| Command | Device equivalent | What it does |
|---|---|---|
| `claude-mate` | — | the app (on a terminal), else the queue |
| `claude-mate status` | — | the queue, printed once. Aliases: `queue`, `ls`, `list` |
| `claude-mate watch` | — | reprint it every second until Ctrl-C. Aliases: `-w`, `--watch` |
| `claude-mate app` | — | the app, by name. Aliases: `ui`, `menu` |
| `claude-mate next` / `prev` | NEXT / PREV | step the selection (or scroll the mirror) |
| `claude-mate go` | GO | acknowledge **and raise that session's terminal** |
| `claude-mate ack` | GO, held | acknowledge only — leave your windows alone |
| `claude-mate follow` | ACK, held | toggle FOLLOW: `next`/`prev` then also raise |
| `claude-mate mirror` | 4th, tapped | toggle the live terminal mirror on the device |
| `claude-mate continue` | — | type `continue` into that session |
| `claude-mate new-terminal` | — | open a terminal in that session's directory |
| `claude-mate select <n\|name>` | *(none)* | point at a row instead of stepping to it |
| `claude-mate accounts` | — | the saved logins. Alias: `account` |
| `claude-mate accounts rm <n\|name>` | — | delete one, after typing its name back |
| `claude-mate daemon` | — | start, or restart, the daemon through launchd |
| `claude-mate --version` | — | which build this is. Aliases: `-V`, `version` |
| `claude-mate --help` | — | all of the above. Aliases: `-h`, `help` |

`accounts rm` takes an **index** as well as a name, and that is not a
convenience: profile names come from whatever was typed at the picker, and an
arrow key types an escape sequence — so a real accounts directory can contain a
profile literally called `\033`, which prints as nothing and cannot be typed
back at a prompt. The index is how you delete it.

### The menus underneath

| Menu | What is in it |
|---|---|
| **Accounts** | every saved login with its email; open one to use it for the next session or delete it |
| **Device** | the connection report, pairing, the shared token, and a daemon restart |

Deleting from the Accounts menu asks with a chooser rather than by making you
type the name, and the cursor starts on **No**. At a shell prompt typing the
name is the right gate — it proves you mean *that* profile — but in a menu the
cursor has already proved which row you are on, and the account most likely to
need deleting is the one whose name **cannot be typed**: an arrow key at the old
picker typed an escape sequence, so a profile literally called `\033` could get
created, and it renders as a blank row. Those show as `'\x1b'` and are marked
*unprintable name*.

---

## `claude-mate-wrap` — the wrapper

You do not run this by name. It replaces `claude`:

```sh
echo "alias claude='$HOME/Projects/claude_mate/bin/claude-mate-wrap'" >> ~/.zshrc
```

It runs the real `claude` inside a pseudo-terminal, watches the screen, and
reports the session's state to the daemon — which is what puts it on the device
and in `claude-mate`. Everything you pass goes straight through to `claude`,
so `claude --dangerously-skip-permissions` works exactly as before.

**The account picker is a chooser now.** When you start a session and profiles
exist, the wrapper asks which one with the same arrow-key menu the app uses —
including a *New account…* entry, which is the one place a name is still typed,
because there it is the actual answer. It falls back to the old numbered prompt
whenever there is no terminal on both ends, so scripts and tests are unaffected.

It owns exactly one flag of its own, `--account <name>`, which it strips before
handing the rest over. `claude-mate-wrap --help` shows *claude's* help, not
its own, because the help you want when you type that is almost always claude's.

**A session started any other way is invisible.** Bare `claude` runs untracked:
nothing appears on the device, nothing appears in `claude-mate`. That is the one
thing to get right, and it is why the app's *Start new session* goes through the
wrapper rather than running `claude` directly.

---

## `claude-mate-connect` — the device

```sh
claude-mate-connect            # report every link and what to do next
claude-mate-connect --pair     # enrol an unprovisioned device over BLE
claude-mate-connect --token    # print the shared token
```

With no arguments it checks the daemon, the USB port, the BLE state and the
token, and for whichever one is broken it names the shortest fix. It is the
answer to "the device will not connect", which previously had no answer you
could find from the Mac — the device said so on its own glass and the daemon
said so in a log file, neither of which is in front of you.

`--pair` puts a `PAIR?` prompt on the device; press GO within 45 seconds and the
daemon hands it a token over the link it is already using. No access point, no
phone, no secret typed by hand.

`--token` prints the shared secret, for the one case where you do have to type
it: a cordless board being set up through its own Wi-Fi portal.

---

## `claude-mate-switch` — accounts

```sh
claude-mate-switch                 # pick from the saved logins
claude-mate-switch work            # continue on 'work'
claude-mate-switch --best          # whichever has the most headroom
claude-mate-switch --dry-run       # say what it would do, do nothing
claude-mate-switch --tty /dev/ttys004   # act on another terminal's session
```

It copies the current conversation into another account's profile and re-execs
the wrapper there, so the session keeps running and keeps being reported. Use it
when a login hits its five-hour limit mid-task.

`--tty` is for driving a session from a *different* terminal than the one it is
running in — which is what the device does when you pick **Switch account** on
its ACTIONS sheet.

---

## Starting and stopping the daemon

```sh
claude-mate daemon        # start it, or restart it if it is already up
```

This goes through the LaunchAgent that the installer sets up, and it is the
only supported way. The daemon has no single-instance guard and unlinks its
socket before binding it, so a second copy started by hand does not fail — it
silently takes the socket from the first while the first keeps the serial port,
and you get a daemon the CLI can reach with no device and a device driven by a
daemon nothing can reach.

By hand, if you must:

```sh
launchctl kickstart -k gui/$UID/com.claudemate.daemon    # restart
launchctl bootout gui/$UID/com.claudemate.daemon         # stop
tail -f ~/Library/Logs/claude-mate.err.log               # what it is doing
```

Note `kickstart`, not `bootstrap`. `bootstrap` fails with `Bootstrap failed: 5:
Input/output error` when the job is already loaded — which it is, on any machine
that ran the installer.

---

## Environment

| Variable | Used by | Meaning |
|---|---|---|
| `CLAUDE_MATE_SOCK` | all | the daemon's control socket (default `/tmp/claude-mate.sock`) |
| `CLAUDE_MATE_NEW_ARGS` | `claude-mate` | extra arguments for *Start new session* |
| `CLAUDE_MATE_ACCOUNT` | wrapper, switch | run as this profile, skipping the picker |
| `CLAUDE_MATE_ACCOUNTS_DIR` | wrapper, switch | where profiles live (default `~/.claude-accounts`) |
| `CLAUDE_MATE_TOKEN_FILE` | daemon, connect | the shared secret's path (default `~/.config/claude-mate/token`) |
| `CLAUDE_CONFIG_DIR` | claude itself | the active profile; set by the wrapper, and always wins |

---

## The socket, if you are scripting

Everything above is a line on a Unix socket at `$CLAUDE_MATE_SOCK`, mode 0600.
The full command surface is six verbs — see
[PROTOCOL.md](PROTOCOL.md#the-control-socket) for the replies. Deletion is
deliberately *not* one of them: a hook in any of your shells can write to this
socket, and a hook firing a malformed line should not be able to remove a login,
so `accounts rm` deletes in the CLI's own process.
