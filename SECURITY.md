# Security Policy

## Attack surface

Claude Mate is deliberately small and **local-only**. It has **no network
exposure**:

- The daemon listens on a **local Unix domain socket** (default
  `/tmp/claude-mate.sock`). It does not open any TCP/UDP port and does not listen
  on any network interface. Only local processes that can access that socket path
  can send it messages.
- The daemon opens a **USB serial device** (e.g. `/dev/cu.usbserial*` or
  `/dev/cu.usbmodem*`) to talk to the Arduino. This is local hardware I/O.
- The Claude Code **hooks** are fire-and-forget writers to the local socket. They
  run as your user, never block a Claude turn, and always exit 0.

Because there is no listening network port, Claude Mate is not remotely
reachable. The relevant trust boundaries are therefore the local machine and the
physical USB device.

### Things to be aware of

- **Socket permissions.** Any local process running as your user (or with access
  to the socket path) can write session messages to the daemon. The socket lives
  under a world-writable directory (`/tmp`) by default; set `CLAUDE_MATE_SOCK` to
  a path in a directory only you can access if you want to restrict this.
- **FOCUS actions.** A GO (FOCUS) button press causes the daemon to raise a
  window: for a wrapped session it connects to that session's local control
  socket and the wrapper raises **its own terminal** (AppleScript / `open`);
  hook-only sessions fall back to a VS Code deep link, then a window-raise for
  the workspace folder. These act on locally-known session data only, and the
  operation is **raise-only** — nothing is ever moved, resized, or closed.
- **Untrusted serial input.** The daemon treats input from the serial device as
  untrusted and parses it defensively; the firmware likewise tolerates and
  discards malformed lines.

## Supported versions

Claude Mate is a hobby/hardware project. Security fixes are made on a best-effort
basis against the `main` branch. Please run a recent version.

## Reporting a vulnerability

If you discover a security issue, please report it **privately** rather than
opening a public issue:

- Open a [GitHub Security Advisory][advisory] on the repository, **or**
- Email the maintainers (see the repository's profile / about page for the
  current contact).

Please include:

- A description of the issue and its impact.
- Steps to reproduce, if possible.
- Any relevant logs, configuration, or proof-of-concept.

We will acknowledge your report as soon as we can and keep you updated on the
fix. Please give us a reasonable opportunity to address the issue before any
public disclosure. Thank you for helping keep Claude Mate users safe.

[advisory]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability
