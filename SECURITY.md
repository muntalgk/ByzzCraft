# Security

ByzzCraft controls a game server and the machine it runs on (the console runs any server command, admins can
restore worlds). Please report vulnerabilities privately, not in a public issue:
use [GitHub's private vulnerability reporting](https://github.com/muntalgk/byzzcraft/security/advisories/new).

Please include what an attacker needs (network access, a player account, an admin account), the steps and the
version (bottom of the **Account** page). You will get an answer within a week; fixes go into a new release and
the advisory credits you unless you prefer otherwise.

## What the panel already does

- Passwords: PBKDF2-SHA256, 240,000 rounds, per-user salt. Sessions: random tokens, only their hash on disk,
  `HttpOnly` and `SameSite=Strict` cookies, CSRF token on every change.
- Logins: 5 wrong attempts lock the address for 15 minutes (not the user: a stranger could lock the admin out).
  Changing or resetting a password ends the other sessions of that user.
- First setup: only from the machine itself (opened by IP or `localhost`, same origin) or with the one-time code
  printed at start-up.
- Proxy headers (`X-Forwarded-For`, last hop; Tailscale) are trusted only from a proxy on the same machine.
- Java flags from the panel: only an allowlist (no `-XX:OnError`, `-javaagent`, `@file`...).
- Content-Security-Policy with a per-page nonce: no inline scripts besides the panel's own.
- HSTS behind an HTTPS proxy.
- The login limit also counts IPv6 addresses by /64, and every login is slowed down after many failures in a short time.
- Java flags that write files or load code from a path are refused (`-XX:...File`, `-XX:...Path`, `-Xlog` writing outside the server folder, `-Dfabric.`).
- The setup warns when "My home network" would mean a public address.

## What it does not do

- HTTPS: use Tailscale, nginx or Caddy (see the README). "My home network" is plain HTTP, and so is the first
  setup from another PC: on a network you do not trust, set up from the machine itself or with
  `panel.py setup` over SSH.
- Two-factor login.
- Many users at once: the built-in server takes at most 100 connections and drops one that sends nothing for
  30 seconds, so slow connections cannot use up the machine; but a stranger can still fill those 100 and keep the
  panel busy. Exposed on the internet, put nginx, Caddy or Tailscale in front.
