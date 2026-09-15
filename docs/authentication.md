# ShelfOS — Authentication and accounts

Signing in, the bootstrap admin, password rules, the sign-in throttle, and what
each role may do. The decision behind all of it is D11 in
[`DECISIONS.md`](DECISIONS.md).

## How you sign in

The UI and API require login (decision D11).

- **Web UI:** sign in at `/login` (session cookie).
- **API:** `POST /api/auth/token` with `{"username", "password"}` returns a JWT;
  send it as `Authorization: Bearer <token>`.
- Roles: `read-only` (GET only), `user` (read + write), `admin` (+ delete and
  user management under `/api/admin/users`).

## The bootstrap admin

On first startup a bootstrap admin is seeded from the environment (defaults
`admin` / `admin`):

```bash
export SHELFOS_SECRET_KEY="a-long-random-secret-at-least-32-bytes"
export SHELFOS_ADMIN_USERNAME="admin"
export SHELFOS_ADMIN_PASSWORD="change-me"
```

## Changing a password, and old sessions

Changing a password ends every sign-in made with the old one. Access tokens are
stateless and session cookies are signed, so there is no server-side record to
delete; instead each carries a fingerprint of the password it was issued against,
which is checked on every request. So an admin resetting an account's password
signs that account out everywhere, immediately — which is the point of resetting
it. Changing your own password in the browser keeps that browser signed in, and
retires your other sessions and any API tokens; an API client that changes its own
password asks for a new token.

One consequence at upgrade time: sessions and tokens issued before this existed
carry no fingerprint and are refused, so everyone signs in once more.

The bootstrap admin is seeded **only when the database has no admin who can sign
in** — so on any install past its first run, changing `SHELFOS_ADMIN_USERNAME` or
`SHELFOS_ADMIN_PASSWORD` does nothing to the account that already exists. Change
that account's password in the app (*Settings → Change password*, top bar), or
with the app stopped:

```bash
python scripts/set_password.py admin        # prompts, without echoing
python scripts/set_password.py --list       # which accounts exist
```

With `SHELFOS_ENV=production`, ShelfOS refuses to start while any admin still has
the default password — checked against the account, not against the variable, so
setting the variable is no way to satisfy it.

## Password rules

Passwords set through ShelfOS — an admin creating or resetting an account, a user
changing their own — must be at least 8 characters. The bootstrap admin's comes
from the environment instead, so that rule is applied at startup: with
`SHELFOS_ENV=production` a shorter `SHELFOS_ADMIN_PASSWORD` (or the default) refuses
to start; otherwise it is a warning.

## The sign-in throttle

Sign-ins are throttled per client address: after 10 failed attempts within
15 minutes, further attempts from that address get a 429 (with `Retry-After`) until
the oldest failure is 15 minutes old. The password is not checked while throttled,
so a locked-out address costs no bcrypt work either. An attempt counts from the
moment it starts, and its slot is given back only if the password turns out right,
so opening many connections at once does not buy more guesses than the limit.
Counting is per address, not per account, so nobody can lock a real user out by
guessing at their name. IPv6 addresses share an allowance per /64, since a single
host is normally given a whole one and could otherwise use a fresh address per
request.

```bash
export SHELFOS_LOGIN_MAX_FAILURES="10"           # 0 turns the throttle off
export SHELFOS_LOGIN_FAILURE_WINDOW_SECONDS="900"
```

Every failed attempt is logged as `Failed login for 'name' from <address>` (and a
throttled one as `Login refused for 'name' from <address>: …`), which is the line a
fail2ban filter can match to block the source at the firewall. That shape is fixed:
control characters and quotes are stripped from the name first, so no username can
break the delimiter a filter anchors on and slip past it.

The address is what uvicorn reports: behind a reverse proxy on the same host that is
the real client only because uvicorn trusts `X-Forwarded-For` from 127.0.0.1 by
default (`--forwarded-allow-ips`); a proxy elsewhere needs that option set to its
address, or every visitor shares one allowance. A transport that reports no client
address at all (a unix socket, say) turns the throttle off and says so once at
startup, rather than putting every visitor in one bucket where ten failures from
anyone would lock out everybody.

The counters are in memory and per process, which is right for the single-worker
uvicorn ShelfOS runs under.
