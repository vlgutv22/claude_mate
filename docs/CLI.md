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
     History · resume or delete a past chat
     Device · link status and pairing
     Accounts · switch or review
     Quit

 ↑↓ move   ⏎ raise this session   a ack   f follow   m mirror   c continue
 r restart daemon   q quit
```

Every level under the top one works the same way: **History**, **Device** and
**Accounts** are arrow-driven menus, not a dumped `--help`, and `esc` always
means back.

**Every line fits the window.** The app paints one terminal row per line, so a
line wider than the window would wrap, push every row under it down, and
scroll the header and the cursor's row off the top — and Enter would act on a
row you cannot see. So a session's row that is too wide gives up its limit,
account, effort and model from the end, and `needs you` stays; the workflow
line under it cuts the name first, then drops the tokens, `+N more`, the clock
and the note, so the status word, a failed agent and the counts go last.

**The menus underneath fit too, however long the list.** A menu used to draw
every row and let the terminal drop what did not fit — the cursor's row among
them once you had arrowed far enough — so Enter chose a row nobody could see.
Nothing had that many rows until the history, which keeps every chat until you
delete it. Now a list longer than the window is drawn as a window around the
cursor, with a dim `↑ N more` / `↓ N more` for what is off screen, and every
line is cut to the width by the columns it takes, not its characters. The
number keys still choose a row, but only while the whole list is on the screen
— and never in a yes/no: `1` used to answer **Yes** to a destructive question
without the cursor ever leaving **No**.

**Start new session** is the reason the app exists. It runs `claude` through the
wrapper in this terminal — the app hands the terminal over and gets out of the
way, exactly as if you had typed the command yourself. The second entry adds
`--dangerously-skip-permissions`. There are two entries rather than one setting
because skipping permission prompts is a decision, and a menu should say which
one it is about to make. Anything else you want on the command line goes in
`CLAUDE_MATE_NEW_ARGS`.

**On a pipe it is not an app.** Redirect it anywhere — a hook, cron, a script,
a test — and `claude-mate` is the same one-shot queue print it always was (plus
a sub-line per workflow the daemon has, [below](#what-status-prints)), with no
colour and no escape codes. The app appears only when stdin *and* stdout are
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
| `claude-mate new-terminal` | — | open a terminal in that session's directory — a wrapped session's; a hook-only one sends no directory, so it opens in `~` |
| `claude-mate select <n\|name>` | *(none)* | point at a row instead of stepping to it |
| `claude-mate accounts` | — | the saved logins. Alias: `account` |
| `claude-mate accounts rm <n\|name>` | — | delete one, after typing its name back |
| `claude-mate history` | — | the chats the daemon has kept, newest first: index, short id, status, age, name, account, title |
| `claude-mate history resume <n\|id>` | — | pick one up again — in its directory, under its login, with the flags it was started with. `--skip-permissions` or `--ask-permissions` changes the permission mode and nothing else |
| `claude-mate history rm <n\|id>` | — | forget one, after typing its short id back. Aliases: `remove`, `delete` |
| `claude-mate history clear` | — | forget every chat that is not open, after typing `clear` |
| `claude-mate daemon` | — | start, or restart, the daemon through launchd |
| `claude-mate --version` | — | which build this is. Aliases: `-V`, `version` |
| `claude-mate --help` | — | all of the above. Aliases: `-h`, `help` |

`accounts rm` takes an **index** as well as a name, and that is not a
convenience: profile names come from whatever was typed at the picker, and an
arrow key types an escape sequence — so a real accounts directory can contain a
profile literally called `\033`, which prints as nothing and cannot be typed
back at a prompt. The index is how you delete it.

### What `status` prints

One row per session, in the device's order: its state and time in state, then
model, effort, account and remaining limit, with `>` on the row that is on the
glass — with no device linked, on the selected row, which is what a press then
acts on — and `<-- needs you` on an unacknowledged alert. Under a session
running a **dynamic workflow** comes an indented sub-line per workflow the
daemon has for it: its name, its status word — `running`, `paused`, `done`,
`ended` — and whatever progress is known, with the run's clock and tokens
written the way the band writes them: the clock in Claude's own units, `2m39s`
or `1h02m` — never the session rows' minutes and seconds, where a run of an
hour would read `62:01` — and the tokens compact, `543k`. The app puts one such
line under a session however many it runs: the workflow the band shows for
that session — or would, were it on the glass — and `+N more`.

The first sub-line under a session is the record the device's
[workflow band](USING.md#the-workflow-band) shows for it; the others are what
the band counts as `+N`. The glass shows one session, the `>` row, so its first
sub-line is a diagnosis: listed there, but no band on the glass, points at the
firmware. Look where the band is drawn first: on neither device for the ~4 s a
notice is up; on the S3 only on the main screen, not under the menu, the
mirror, the pad or the game; and on the Nano there is no name to look for, only
a bar or a word, with the info, in r2's place. A session running a workflow
with no sub-line at all points at the wrapper (or a session that is not
wrapped). Workflows under other rows, and the second and third under `>`, are
not drawn by design. A script that wants the numbers should not parse the
print; the `queue` JSON has them ([below](#the-socket-if-you-are-scripting)).

### The menus underneath

| Menu | What is in it |
|---|---|
| **History** | every kept chat, newest first: its title, then its name, status, age and account. Open one to raise its terminal if it is open, resume it if it is not, or delete it; *Clear all history* is at the bottom |
| **Device** | the connection report, pairing, the shared token, and a daemon restart |
| **Accounts** | every saved login with its email; open one to use it for the next session or delete it |

Deleting from the Accounts menu asks with a chooser rather than by making you
type the name, and the cursor starts on **No**. At a shell prompt typing the
name is the right gate — it proves you mean *that* profile — but in a menu the
cursor has already proved which row you are on, and the account most likely to
need deleting is the one whose name **cannot be typed**: an arrow key at the old
picker typed an escape sequence, so a profile literally called `\033` could get
created, and it renders as a blank row. Those show as `'\x1b'` and are marked
*unprintable name*.

### History

**Closing a chat used to destroy it**, as far as anything here knew. The session
left the queue, and which conversation it was — where, and as whom — went with
it; a crash or a reboot did the same. Claude Code still had the transcript, for
thirty days by default, but finding it meant knowing its id, its directory and
its login, and resuming it *as it was* meant remembering how it had been
started. So the daemon now keeps a record of every chat a wrapped session has
run a turn in, and a hard link to its transcript, until you delete it
([PROTOCOL.md](PROTOCOL.md#chat-history) has the record and the rules).

```sh
claude-mate history
```
```
  0  3f2a9c1e  open          2m  claude_mate  work     add a history log to the daemon
  1  8c41d07a  closed        3h  claude_mate  default  fix the flaky wrapper test
  2  b7e05f93  interrupted  41d  api-server   work     why does the healthcheck 502 after deploy

/Users/you/.config/claude-mate/history · 3 chats · 1.2 MB kept only here
```

One line per chat, newest first: the index, the first eight characters of its
id, its status, how long since its last turn started, its directory's name, the
account it ran as, and its title — the name you gave it (`-n`, `/rename`), else
the one Claude gave it, else the first line of the last thing you asked. The
footer says where the records are, how many there are, and how much of what is
kept exists nowhere else. On a terminal each line is cut to the window, a name
past 24 columns and an account past 16 first, ending in `…`. On a pipe there is
no colour and no cut to a width: names and accounts come whole, padded to line
up. A title stops at 200 characters everywhere, a pipe included, with no mark.

- **`open`** — a terminal has it now. That is asked of the daemon's queue *and*,
  separately, of every wrapper's per-terminal context file whose process is
  alive and that was refreshed in the last fifteen minutes — so it holds while
  the daemon is down, or has just restarted and the wrappers have not
  re-registered yet. The list says so when the context files are all it had.
  A context file counts by the conversation its wrapper is sure of *and* by the
  transcript it found, so a chat opened through claude's own picker reads
  `open` once its terminal has found that transcript, named or not. Only files
  you own count, in a directory that is yours alone: where the temp directory
  is a shared `/tmp`, another user could otherwise plant files holding any chat
  open. The other way round, a queue row counts only while its wrapper is still
  reporting: a wrapper killed without a word (`kill -9`) stays in the queue
  until the daemon prunes it, but its chat stops reading `open` within a minute
  — once it has missed three keepalives (the queue's `seen`).
- **`closed`** — its wrapper said it closed.
- **`interrupted`** — it never said: a crash, a killed wrapper, a machine that
  went down with it. The chats this exists for.

**Resume** puts one back as it was:

```sh
claude-mate history resume 2              # or by id: resume b7e0
claude-mate history resume 2 --skip-permissions
```

It runs through the wrapper, so the resumed chat is on the device like any
other: in the directory the transcript itself records — where claude was really
running, which under `-w` is the worktree — under the login it ran as
(`CLAUDE_CONFIG_DIR` set exactly as it was, or unset for the default login,
whatever your shell exports), and with the flags it was started with, put
through the wrapper's [resume policy](PROTOCOL.md#resume-flags) again rather
than replayed from the file. The permission mode, the model and the effort come
back; the opening prompt, which a resume would send again, and `-w`, which would
make a new worktree, do not. A deny list, a tool list and `--restricted` come
back whatever the size budget, and when one cannot, no flag does, the
permission bypass included. The same goes for an `--agent` whose definition
was inline `--agents`, which a record never keeps: the chat comes back with no
flags rather than as claude's default agent. `--skip-permissions` swaps
whatever permission mode it had for `--dangerously-skip-permissions` — refused
for a chat started `--restricted`, which refuses exactly that — and
`--ask-permissions` takes every way of skipping them away. When Claude Code's
own copy of the transcript is gone, the kept link is put back where `--resume`
looks for it. Given no chat at all, it offers a chooser.

**Every check comes before the terminal is handed over**, because nothing can be
said after it, and claude's own answer to most of these — *No conversation found
with session ID* — names none of the causes. So it refuses, saying what it
checked, when stdin or stdout is not a terminal (the wrapper would run claude
unwrapped, untracked); when the chat is open in a terminal (raise it instead:
two processes appending to one transcript fork it, and a later resume follows
one branch) — looked at again right before anything is put back or run, since
a chooser or a question may have waited on you meanwhile; when its account
profile no longer exists (the login went with it — if the conversation was
kept, it says so: make the profile again and log in, and the resume puts the
kept copy back); when its directory no longer exists (claude finds a
conversation through the directory it runs in — recreate it, or delete the
record); or when there is no transcript at all, with where it looked and
Claude's cleanup, `claude purge` and a deleted profile named as possible causes,
not as fact.

**A transcript written in the last two minutes is a question.** "Open" is only
what claude-mate can see. claude run without the wrapper, the VS Code panel, a
session opened by `-r <name>` or the picker that has not run a turn yet — none
says which chat it has, and a second claude on it forks the transcript all the
same.
What each does leave is a fresh write. So when no terminal claude-mate can see
has the chat but its transcript was written under two minutes ago — right after
a clean close too — resume says so and asks: at a shell, type `yes` (anything
else is `not resumed.`, exit 1); in the app, a yes/no that starts on **No**.

**Forgetting one is typed**, the way `accounts rm` is:

```sh
claude-mate history rm 1        # or 4+ characters of its id: rm 8c41
```

It prints the record — id, title, name, directory, account, status — and
whether it holds the only copy, then asks you to **type its id back**: the eight
characters the list shows. `accounts rm` asks for the name, which works there
because account names are unique; every chat in a project has the project's
name, and an index shifts as chats run, so only the id proves which chat you
mean. Anything else prints `not deleted.` and exits 1. With no argument it opens
a chooser on a terminal, and asks which off one. `history clear` forgets every
chat that is not open, behind typing `clear`, and says first how many of them
are the only copy.

**Deleting a record deletes its kept link.** While Claude Code still has its
own copy of the transcript that costs nothing — the conversation is still there
for `claude --resume`. When the kept link is the **only** copy — Claude's was
cleaned up, purged, or went with a deleted profile — the conversation goes with
it, and there is no undo. Every confirmation says which of the two it is. An
open chat is never deleted: it would come back on its next turn, so close it
first.

**Addressing.** An all-digit argument smaller than the list is the index the
list printed; anything else is the start of an id, at least four hex digits,
and must match exactly one chat — an ambiguous prefix is refused, with the
candidates' short ids. The index is the list as it was when it printed, and
that reorders as chats run, so `history resume <n>` by number first shows which
chat that is now — its short id, its title, its directory — and wants Enter
(anything typed first is `not resumed.`, exit 1). By id, or from the chooser,
it goes straight on.

**What is not kept.** Only wrapped sessions are kept: bare `claude`, the
VS Code panel and every `.pkg` install, which ships no wrapper, are not
recorded. Nor is a chat that never ran a turn. Only the daemon writes records,
and only for a turn it saw start, so a chat whose turns all ran while the
daemon was down — or while a daemon from before the history was still running —
is not kept either. A session that has to find its conversation for itself —
the first after a Claude Code update, `--fork-session`, and `-c`, `-r <name>`,
the picker and `--from-pr`, which carry on one that is already there — looks
after its own first turn, and gives up rather than guess when another terminal
in that directory started or carried on a chat of its own meanwhile
([PROTOCOL.md](PROTOCOL.md#hist-field)). A chat switched with `/clear` or
`/resume` inside the TUI stays recorded under the conversation it started with.
There is no switch to stop recording; `history clear` is how to empty it.

**In the app**, *History* is the row under the two Start rows, read when you
open it. Open a chat and the choices depend on it:

- **Raise its terminal** when it is open — by its row's exact key, so it cannot
  raise another window that happens to share the name, and only once the daemon
  has said that row is selected. Only a row still reporting counts, the most
  recently heard first: a terminal killed outright keeps its row for minutes,
  and the same chat resumed since has a second one under the same name. When
  every row for the chat has gone quiet, it says so rather than raise one. With
  no device linked it works the same — the press acts on the row it selected.
- **Resume**, with the permission mode in the label rather than in a detail the
  width cuts first: a chat started skipping prompts offers *Resume · skip
  permissions* (as it was started) and *Resume · ask permissions*; any other
  offers *Resume* and *Resume · skip permissions*.
- **Delete from history**, behind a yes/no that starts on **No** and says
  whether this is the only copy. *Clear all history* on the list works the same
  way, for every chat that is not open.

The number keys do nothing in a chat's menu: one would resume — perhaps
skipping permissions — or reach Delete in a single keystroke.

Deleting an account profile, from `accounts rm` or the Accounts menu, now says
how many kept chats ran on it: their kept transcripts are links of their own, so
they outlive the profile until you delete them from History. Resuming one waits
for the profile, though — make it again and log in, and the kept copy goes back
where `--resume` looks.

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
nothing appears on the device, nothing appears in `claude-mate`, and nothing is
kept in its [History](#history). That is the one thing to get right, and it is
why the app's *Start new session* — and every resume from History — goes
through the wrapper rather than running `claude` directly.

**It also says which conversation it is running** — Claude Code's id for it,
the directory, the login and the flags a resume would replay — once it is sure
which one that is: its transcript exists, and for a session that carries on an
older one (`-c`, `-r <name>`, the picker, `--from-pr`), it is the one
transcript there that grew during the session's own first turn. Then it goes
on saying the same one for as long as it runs
([field 9](PROTOCOL.md#hist-field)). That is what the History is built from.

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
| `CLAUDE_MATE_ACCOUNTS_DIR` | wrapper, switch, `claude-mate history` | where profiles live (default `~/.claude-accounts`); History also looks there for a chat whose login it never learned |
| `CLAUDE_MATE_TOKEN_FILE` | daemon, connect | the shared secret's path (default `~/.config/claude-mate/token`) |
| `CLAUDE_CONFIG_DIR` | claude itself | the active profile; set by the wrapper, and always wins — except on a history resume, which sets it to the login the chat ran under, or unsets it for the default |

---

## The socket, if you are scripting

Everything above is a line on a Unix socket at `$CLAUDE_MATE_SOCK`, mode 0600 —
everything but History's reading, resuming and deleting, which it does on its
files directly and which works with the daemon down. History asks the daemon
only for `queue`, to tell which chats are open, and the app's *Raise its
terminal* sends `select|<key>` then `press|G`. The full command surface is
seven verbs — see [PROTOCOL.md](PROTOCOL.md#the-control-socket) for the
replies. Deletion is deliberately *not* one of them: a hook in any of your
shells can write to this socket, and a hook firing a malformed line should not
be able to remove a login or a chat, so `accounts rm` deletes in the CLI's own
process, and so do `history rm` and `history clear` — a `queue` reply can only
keep a chat from being deleted, never name one.

`queue` is the one to read. Every session in its JSON carries a `workflows`
list — `[]` when there are none — with each visible workflow's name, status
letter and word, counts, percentage, clock in seconds, tokens and usage-limit
note ([PROTOCOL.md §3](PROTOCOL.md#per-session-workflows)), and a
`conversation`: the Claude Code conversation a wrapped session is running, `""`
until its wrapper names one and always for a hook session. `select|<key>`
takes a session's exact `key` from that JSON, for a script that knows which row
it means.
