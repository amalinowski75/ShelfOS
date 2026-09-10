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

What it deliberately leaves to you: DNS, the firewall, and off-host copies of
`/etc/shelfos/env` (the backups do not contain it, and never will). The backups
themselves it schedules — nightly, into `/var/lib/shelfos/backups`.

This directory holds what it installs, each file commented in full:

| File | What it is |
| --- | --- |
| `shelfos.service` | The unit: one uvicorn worker on the loopback, sandboxed |
| `shelfos.env.example` | Settings and secrets, for `/etc/shelfos/env` |
| `Caddyfile` | TLS, the certificate, and the headers the app cannot always set |
| `shelfos-backup.service` | One backup, taken as root, with the app still running |
| `shelfos-backup.timer` | When that happens: 03:15 local, catching up a missed night |

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

### Rebuilding the machine, same domain

`deploy` on the new machine does the whole job, and the only thing it cannot
carry over is what was never in the backup. The archive holds the database and
the attachments; carry these across as well, before the first deploy:

```bash
/etc/shelfos/env               # signing secret + shop keys, root:shelfos 640
/var/lib/shelfos/tunnel-keys   # the machines allowed to bring a label printer
```

A new signing secret is not fatal — it signs everyone out and invalidates every
API token — but the shop keys are not recoverable from anywhere, and without the
tunnel keys every printer has to be registered again from its own machine (which
is self-service, so a small thing). Restore the env file with the same
`root:shelfos 640`, and the key file as `shelfos:shelfos 644` — sshd's command
reads it, and the service writes it.

**The certificate.** Caddy asks for a new one and gets it in seconds, so for a
one-off rebuild this needs no thought beyond having ports 80 and 443 reachable
and DNS still pointing here — 80 as well, because that is how the certificate is
issued. To keep the existing one instead, copy Caddy's storage across (its
certificates *and* its ACME account key) and restore the ownership:

Caddy's storage follows the `HOME`/`XDG_DATA_HOME` of the process that runs it,
which for the packaged unit is the `caddy` user with `HOME=/var/lib/caddy` — not
your shell's, so ask the unit and then look rather than asking Caddy from a root
prompt:

```bash
systemctl show caddy -p User -p Environment            # what the unit sets
sudo find /var/lib/caddy -type d \( -name certificates -o -name acme \)
sudo tar -C /var/lib -czf caddy-storage.tar.gz caddy   # on the old machine
sudo tar -C /var/lib -xzf caddy-storage.tar.gz         # on the new one
sudo chown -R caddy:caddy /var/lib/caddy
```

If `find` comes back empty, the unit is keeping it somewhere else and the two
directories it named are what to carry across; the `chown` applies wherever they
land.

That is worth doing when you expect to rebuild often: Let's Encrypt allows 50
certificates a week per registered domain, but only **5 identical ones in 7
days** — which is a limit nobody meets in production and everybody meets while
testing a rebuild. Failed validations are rate-limited too, so a deploy run
before DNS or the firewall is ready costs more than it looks.

### Deploying again over a working install

`deploy` recognises an install and stops; `--reinstall` walks the steps again,
skipping what is already done — including moving `/opt/shelfos` to whatever the
clone you run it from has checked out, branch and all. Without `--reinstall` the
installed code is left where it is, because a plain deploy is not a licence to
move somebody's running service. To follow a branch afterwards without a full
re-deploy, `./shelfos.sh update --ref <branch>`; a plain `update` fast-forwards
whatever the install is already on. It keeps the domain it finds in the Caddy config
it wrote, so `sudo ./shelfos.sh deploy --reinstall` on an HTTPS server stays an
HTTPS server; `--domain` overrides it and `--no-tls` turns it off. The
certificate lives in Caddy's own storage, not in the config, so rewriting the
config does not reissue anything.

Where the unit differs from the one in this checkout — a new version usually
changes it — the diff is shown and installing it is a question, because that
file carries this machine's port and address. Answering no leaves the unit
alone and stops. The two backup units are asked about the same way, and for the
same reason: the hour and the retention in them may be yours.

`--reinstall` is also how an install that predates a new unit gets it — the
backup timer, for one. A plain `update` moves the code and says a unit is new,
but installs nothing into `/etc/systemd/system` itself; the three commands under
"Doing it by hand" are the other way, and touch nothing else. Note that
`--reinstall` re-decides the label printer from what is plugged in at the time,
so pass `--printer` on a machine whose printer is not attached right now, or the
rendered unit comes back without it.

### Reaching it without a proxy

The service binds 127.0.0.1, because with Caddy in front that is the only thing
that should reach it. Without a proxy — a test container, a private bridge —
that leaves it reachable from nowhere but the machine itself, and a forwarded
port is a workaround for a decision rather than the decision:

```bash
sudo ./shelfos.sh deploy --no-tls --listen 0.0.0.0
```

`--no-tls` also writes `SHELFOS_COOKIE_SECURE=0`. A `Secure` session cookie is
never sent back over plain HTTP, so without it the browser drops the session, the
sign-in form's token has nothing to match, and every attempt says the form has
expired — with nothing in the log, because nothing failed. Set it back to 1 the
moment TLS goes in front.

Then it answers at the machine's own address, which is also the address the
label-printer page will offer for ssh. It is plain HTTP: fine on a bridge only
you can reach, not fine anywhere a password matters. `./shelfos.sh status` reads
the address back out of the unit, so it reports on the service that is actually
running rather than on loopback.

## Doing it by hand

The script does exactly this, and this is the only path on a host it refuses —
anything that is not Debian or Ubuntu. A system user that owns nothing else, the
code in one place and the data in another, so replacing the code never touches
the database:

```bash
sudo apt install python3-venv git curl fonts-dejavu-core
sudo useradd --system --home-dir /opt/shelfos --shell /usr/sbin/nologin shelfos
sudo mkdir -p /opt/shelfos /var/lib/shelfos /etc/shelfos
sudo git clone https://github.com/amalinowski75/ShelfOS.git /opt/shelfos
sudo python3 -m venv /opt/shelfos/.venv
sudo /opt/shelfos/.venv/bin/pip install --editable /opt/shelfos
sudo chown -R shelfos:shelfos /opt/shelfos /var/lib/shelfos
```

`fonts-dejavu-core` is the one that looks optional and is not. A label is a
bitmap, and drawing text into it needs a TTF on the host; a server has no desktop
to have brought one, so without it every preview and every print fails with "no
label font found". DejaVu is the first family ShelfOS looks for, so installing it
is the whole of the fix — `SHELFOS_LABEL_FONT` is for a font of your own.

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

And the nightly backup, which is two more files and one `enable`:

```bash
sudo cp /opt/shelfos/deploy/shelfos-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shelfos-backup.timer
systemctl list-timers shelfos-backup.timer   # when it next fires
```

Enabling the **timer**, not the service: enabling the service would run a backup
at every boot and never again. Nothing has to be stopped for it, and there is no
reason to wait until tonight to find out whether it works —
`sudo systemctl start shelfos-backup.service` takes one now and puts whatever
went wrong in `journalctl -u shelfos-backup`.

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
checksums on restore. `./shelfos.sh update` takes one before it changes anything,
and `shelfos-backup.timer` takes one every night at 03:15 into
`/var/lib/shelfos/backups`, keeping thirty days. The archives are `root:root`
0700 and every one of them carries every password hash in the database, so
`sudo` is needed to so much as list them.

Three things about that schedule are worth knowing before the night you need it:

- **It is not an off-host copy.** An archive on the same disk as the database
  survives a bad restore and a deleted row; it does not survive the disk, or the
  machine. Add an `ExecStartPost=` to `shelfos-backup.service` that pushes the
  newest archive to a NAS or to object storage, and the schedule becomes a
  backup rather than a snapshot.
- **The environment file is not in it**, and cannot be: `/etc/shelfos/env` holds
  the signing secret and the shop keys, and the archives are copied around far
  too casually to carry those. Copy it off once, by hand. A lost secret signs
  everyone out and invalidates every API token; lost shop keys are not
  recoverable from anywhere.
- **An enabled timer is not a working backup.** It can fire faithfully every
  night into a service that has been failing since a disk filled up, and nothing
  will say so. `./shelfos.sh status` prints the next firing *and* the newest
  archive with its date, which is the pair worth reading; `systemctl status
  shelfos-backup` has the last run.

To move the hour or the retention, edit the installed
`/etc/systemd/system/shelfos-backup.timer` (or `.service`) and
`systemctl daemon-reload`. A later `deploy --reinstall` finds the difference,
shows it, and asks before replacing it — answering no keeps yours, and the
plain `update` path never touches either file. `deploy --no-backup-timer`
installs without the schedule at all.

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
6. `sudo systemctl start shelfos-backup.service` — an archive appears in
   `/var/lib/shelfos/backups` while the app keeps answering, and
   `./shelfos.sh status` names it under `backups` along with the next firing.
7. `sudo ./shelfos.sh update` with nothing new upstream — must say so and stop.
8. Reboot; the service comes back on its own, and so does the timer.
9. With a Brother QL attached: `/dev/shelfos-label` exists and a test label prints.

## Behind a different proxy

nginx works as well. Two things to carry over: raise `client_max_body_size`
above `SHELFOS_MAX_ATTACHMENT_MB` (the default 1 MB rejects attachments the app
would accept), and pass `X-Forwarded-For` and `X-Forwarded-Proto`. If the proxy
is not on this host, its address has to go in the unit's
`--forwarded-allow-ips`, or every visitor arrives as the proxy and they all
share one sign-in allowance.
