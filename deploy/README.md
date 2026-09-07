# Running ShelfOS on a server

`./run.sh` is for a laptop: it rebuilds the virtualenv when needed and runs
uvicorn with `--reload`. This directory is the other thing — one process under
systemd, reachable only through a reverse proxy that holds the certificate.

Three files, each commented in full:

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

## Setting it up

A system user that owns nothing else, the code in one place and the data in
another, so replacing the code never touches the database:

```bash
sudo useradd --system --home-dir /opt/shelfos --shell /usr/sbin/nologin shelfos
sudo mkdir -p /opt/shelfos /var/lib/shelfos /etc/shelfos
sudo git clone https://github.com/amalinowski75/ShelfOS.git /opt/shelfos
sudo python3 -m venv /opt/shelfos/.venv
sudo /opt/shelfos/.venv/bin/pip install --editable /opt/shelfos
sudo chown -R shelfos:shelfos /opt/shelfos /var/lib/shelfos
```

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
both places, because `run.sh` sources the file with `set -a`, so one file can
serve the laptop and the server. Drop the `export`s rather than keeping two
copies that will drift.

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
sudo -u shelfos /opt/shelfos/.venv/bin/python /opt/shelfos/scripts/set_password.py admin
```

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

**Backups.** `scripts/backup.py` takes the database and attachments together and
verifies checksums on restore. It does not carry the environment file, which is
where the secret lives — back that up separately, or a restore comes back with
every session invalid.

## Behind a different proxy

nginx works as well. Two things to carry over: raise `client_max_body_size`
above `SHELFOS_MAX_ATTACHMENT_MB` (the default 1 MB rejects attachments the app
would accept), and pass `X-Forwarded-For` and `X-Forwarded-Proto`. If the proxy
is not on this host, its address has to go in the unit's
`--forwarded-allow-ips`, or every visitor arrives as the proxy and they all
share one sign-in allowance.
