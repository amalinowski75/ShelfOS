# Running ShelfOS on a server

One command, from a clone, on the machine that will run it:

```bash
sudo ./shelfos.sh deploy
```

It asks for a hostname and a first admin password, generates the signing secret
itself, and performs everything this file describes. Every step reports `ok` or
`skipped`, so re-running it is how you repair a half-finished install rather than
something to be afraid of. `--dry-run` prints the whole plan and touches nothing
— it does not even call `sudo`.

What it deliberately leaves to you: DNS, the firewall, off-host copies of
`/etc/shelfos/env` (the backups do not contain it), and any schedule for the
backups themselves.

This directory holds what it installs, each file commented in full:

| File | What it is |
| --- | --- |
| `shelfos.service` | The unit: one uvicorn worker on the loopback, sandboxed |
| `shelfos.env.example` | Settings and secrets, for `/etc/shelfos/env` |
| `Caddyfile` | TLS, the certificate, and the headers the app cannot always set |

## Why a proxy at all

Everything that authenticates a person crosses the wire on every request: the
password in the sign-in form, the session cookie after it, the bearer token an
API client sends. Without TLS all of it is readable by anything on the path, and
the sign-in throttle, the bcrypt cost and the password floor stop mattering —
none of them is an obstacle to someone who can simply read the password.

In production ShelfOS also marks its session cookie `Secure`, so a browser will
not send it back over plain HTTP. Without TLS the app does not merely become
insecure, it stops working: you sign in and are immediately signed out again.

Uvicorn can terminate TLS itself, so the proxy is not strictly required. It earns
its place on renewal: a Let's Encrypt certificate lasts ninety days and uvicorn
will not pick up a new one without a restart, where Caddy obtains and renews it
with no help. The proxy also covers a gap the app documents in `app/main.py` —
an unhandled exception becomes a 500 from Starlette's error middleware, which
wraps the app's own header middleware, so that one response goes out without the
security headers. Caddy adds them to everything.

## Doing it by hand

The script does exactly this, and this is the only path on a host it refuses —
anything that is not Debian or Ubuntu. A system user that owns nothing else, the
code in one place and the data in another, so replacing the code never touches
the database:

```bash
sudo useradd --system --home-dir /opt/shelfos --shell /usr/sbin/nologin shelfos
sudo mkdir -p /opt/shelfos /var/lib/shelfos /etc/shelfos
sudo git clone https://github.com/amalinowski75/ShelfOS.git /opt/shelfos
sudo python3 -m venv /opt/shelfos/.venv
sudo /opt/shelfos/.venv/bin/pip install --editable /opt/shelfos
sudo chown -R shelfos:shelfos /opt/shelfos /var/lib/shelfos
```

A second account, for a label printer plugged into somebody else's machine (see
`README.md`). It logs in over ssh and does one thing: bind the printer's port on
this host. Not the service user — that one has no shell and a root-owned home, so
sshd would refuse it, and giving it those would turn a confined service account
into a login account:

```bash
sudo useradd --system --create-home --home-dir /var/lib/shelfos-tunnel \
     --shell /usr/sbin/nologin --comment "ShelfOS label-printer tunnel" shelfos-tunnel
sudo chmod 0700 /var/lib/shelfos-tunnel
```

It can do nothing until a key is authorised. Keys are not kept in its home: sshd
refuses a key file owned by a third account, and ShelfOS has to write it so that a
machine with a printer can register itself from the browser. So sshd is pointed at
a command instead:

```bash
sudo install -D -m 0644 -o shelfos -g shelfos /dev/null /var/lib/shelfos/tunnel-keys
sudo install -D -m 0755 -o root -g root /opt/shelfos/deploy/tunnel-keys.sh \
     /usr/local/lib/shelfos/tunnel-keys     # replace @TUNNEL_USER@ / @TUNNEL_KEYS@
sudoedit /etc/ssh/sshd_config.d/60-shelfos-tunnel.conf   # the Match block, see README.md
sudo sshd -t && sudo systemctl reload ssh                # never reload an untested config
```

Put both names in `/etc/shelfos/env` (`SHELFOS_TUNNEL_USER`, `SHELFOS_TUNNEL_KEYS`)
and the setup page fills the form in and registers keys by itself. Without them
everything still works, with a key authorised by hand
(`./shelfos.sh tunnel-key add`).

Settings, readable by the service and nobody else — it holds the signing secret
and every shop key:

```bash
sudo cp /opt/shelfos/deploy/shelfos.env.example /etc/shelfos/env
sudo chown root:shelfos /etc/shelfos/env && sudo chmod 640 /etc/shelfos/env
sudoedit /etc/shelfos/env          # at minimum: the secret and the admin password
```

Then the unit and the proxy:

```bash
sudo cp /opt/shelfos/deploy/shelfos.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now shelfos
sudo journalctl -u shelfos -f     # it says which setting is wrong, if one is

sudo cp /opt/shelfos/deploy/Caddyfile /etc/caddy/Caddyfile   # edit the hostname
sudo systemctl reload caddy
```

## Things that catch people out

**`export` in the environment file.** systemd's `EnvironmentFile` does not
understand the prefix: `export SHELFOS_ENV=production` sets a variable named
`export SHELFOS_ENV`, and ShelfOS never sees it. Plain `KEY=value` lines work in
both places, because `shelfos.sh` parses the file the same way systemd does, so
one file can serve the laptop and the server. Drop the `export`s rather than
keeping two copies that will drift.

**Relative data paths under a read-only filesystem.** The unit sets
`ProtectSystem=strict`, so everything outside `/var/lib/shelfos` is read-only —
the checkout included. `DATABASE_URL` and `SHELFOS_ATTACHMENTS_DIR` both default
to paths relative to the working directory, which would put the database inside
that read-only tree. Set both absolute, as the example does.

**Moving an existing database.** Copy it in before the first start, or the app
seeds a fresh one and you will wonder where the parts went:

```bash
sudo systemctl stop shelfos
sudo cp /path/to/old/data/shelfos.db /var/lib/shelfos/shelfos.db
sudo cp -r /path/to/old/attachments /var/lib/shelfos/attachments
sudo chown -R shelfos:shelfos /var/lib/shelfos
```

**The first start refusing.** With `SHELFOS_ENV=production` ShelfOS will not run
on the default signing secret, and will not run while any admin still has the
default password — including a database carried over from a laptop. The journal
names the account and the fix:

```bash
sudo ./shelfos.sh password admin
```

The same thing happens to a **restored backup**: an archive carries its own
accounts, so one taken from a laptop brings that laptop's admin with it. The
restore says so while the service is still stopped and offers to set a new
password there and then, which is the only moment the fix is one command away.

**The label printer, if there is one.** The unit's printer block is written for
the udev rule in the main README, which is not optional here: without it the
device is `/dev/usb/lpN` with an N that changes on replug, owned `root:lp 0660`.
Install it, then keep three things agreeing with each other — the rule's `GROUP`,
the unit's `SupplementaryGroups`, and `SHELFOS_LABEL_DEVICE`:

```
# /etc/udev/rules.d/99-brother-ql.rules
SUBSYSTEM=="usbmisc", ATTRS{idVendor}=="04f9", MODE="0660", GROUP="plugdev", SYMLINK+="shelfos-label"
```

```bash
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
# then in /etc/shelfos/env:
#   SHELFOS_LABEL_DEVICE=/dev/shelfos-label
```

The unit allows the device by class (`char-usb`, major 180) rather than by path,
so a replug that renumbers the node changes nothing. A wrong group here fails
with `EACCES`, which reads exactly like the printer being unplugged; the main
README's printer section covers the rest, including that CUPS must not own the
same printer and that a QL-800 out of the box is in Editor Lite mode and
enumerates as a USB disk rather than a printer.

**One worker.** The sign-in throttle counts failures in one process's memory and
the label printer's job lock is per process, so a second worker doubles the
allowance and lets two prints reach the printer at once. Both need shared state
before `--workers` is worth raising.

**Everyone signs in again after an upgrade** that changes `SHELFOS_SECRET_KEY`,
and once more after the release that tied sessions to the current password.

**The Caddyfile.** `deploy` takes over `/etc/caddy/Caddyfile` only when it is
absent, empty, or one it wrote itself and still the only site in it — it leaves a
marker comment on the first line to know. Anything else gets
`/etc/caddy/sites/shelfos.caddy` and a printed `import` line to add, because
overwriting a file that serves somebody else's site takes that site off the air.

**Backups.** `./shelfos.sh backup` wraps `scripts/backup.py` with the right paths
and the right user; it takes the database and attachments together and verifies
checksums on restore. Neither carries the environment file, which is where the
secret lives — back that up separately, or a restore comes back with every
session invalid. `./shelfos.sh update` takes one before it changes anything.

## Checking a change to this by hand

Most of `deploy` cannot be tested in CI: it installs packages, adds an apt
source, creates a user and talks to systemd and to a certificate authority. On a
throwaway Ubuntu 24.04 container or VM, in order:

1. `sudo ./shelfos.sh deploy` from a fresh clone, answering the prompts.
2. `sudo ./shelfos.sh deploy` again — it must recognise the install, say what it
   found, and point at `update` and `--reinstall` rather than doing anything.
3. `sudo ./shelfos.sh deploy --reinstall` — *this* is the run where every step
   reports `skipped`, and where a half-finished install would be repaired.
4. `./shelfos.sh status` — service active, health answering, no `replace-me`.
5. `./shelfos.sh backup create`, then `restore` of that archive. A restore whose
   archive carries an admin on the default password must say so and offer to fix
   it **before** the service is started.
6. `sudo ./shelfos.sh update` with nothing new upstream — must say so and stop.
7. Reboot; the service comes back on its own.
8. With a Brother QL attached: `/dev/shelfos-label` exists and a test label prints.

## Behind a different proxy

nginx works as well. Two things to carry over: raise `client_max_body_size`
above `SHELFOS_MAX_ATTACHMENT_MB` (the default 1 MB rejects attachments the app
would accept), and pass `X-Forwarded-For` and `X-Forwarded-Proto`. If the proxy
is not on this host, its address has to go in the unit's
`--forwarded-allow-ips`, or every visitor arrives as the proxy and they all
share one sign-in allowance.
