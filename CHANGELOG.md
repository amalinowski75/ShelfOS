# Changelog

Notable changes, newest group first. Inside a group the order is whichever reads
best rather than a strict chronology — where one change builds on the last, they
run oldest first. This file starts at **#112 (2026-08-28)**; the 111 pull requests
before it are in `git log`, and the entries are grouped by the work rather than by
release — the project has no releases yet.

Each entry says what changed and, where it is not obvious, why. Numbers link to the
pull request, which carries the reasoning and the verification.

## CI says how many tests ran, not just whether they passed

A finished run reported one word per job. Learning that the Python suite is 1733
tests, or which of them failed, meant opening the log and reading to the end —
and a red run is exactly when nobody wants to. Both runners now write JUnit XML
and a summary step renders it into the table at the top of the run: counts,
wall time, and every test that failed with the first line of its assertion.

Vitest's built-in `github-actions` reporter puts a failure on the offending line
in the diff, and `pytest-github-actions-annotate-failures` does the same for
Python, so a red run is readable from *Files changed* without opening a log at
all. Coverage of `app` is measured and printed beside the table; nothing is
gated on the number, because a threshold turns an honest refactor into an
argument with CI. `--durations=10` names the slowest tests, which is how a suite
that has quietly grown an eight-second test gets noticed before it is the run.

The renderer is one stdlib file, `scripts/ci_summary.py`, rather than a
marketplace action: a reporter action wants write permission on checks or pull
requests, and pinning one safely means chasing its commit SHA at every bump.
Reading a file the job just wrote needs neither. Runs are also now cancelled
when a newer push supersedes them, and the workflow declares read-only
permissions.

There is no progress bar, and there is no way to build one: job summaries render
only after a step ends. What the split into parallel jobs already gives is the
job list itself, which fills in as each suite lands.

## Successful sign-ins are logged where sign-ins are looked for

uvicorn configures its own three loggers and leaves the root one with nothing
below WARNING, so every `_logger.info` in ShelfOS was written to a logger that
discarded it. Failed sign-ins are WARNING and reached the journal; successful
ones are INFO and did not — which is backwards for the question an operator
actually asks. It never showed up in the suite, because pytest installs a
handler of its own and the lines appear there perfectly.

`create_app` now installs a root handler and sets the level of the `shelfos`
logger — the one flat logger every module here uses — from `SHELFOS_LOG_LEVEL`
(default INFO), so `journalctl -u shelfos | grep "Login for"` answers who
signed in, from where, and when. The tests for it run in a subprocess
configured the way uvicorn configures one, because that is the only place the
difference is visible, and they drive the modules' own logger objects rather
than naming one: the first version of them named a logger that nothing uses,
and passed against it while the production line stayed silent. A separate test
now asserts that every `getLogger` in the tree names the logger this
configures.

It came up on a live server where a tab signed in as a `user` account showed an
admin after a service restart. A browser keeps one session cookie per site,
shared by every tab, so signing in as somebody else in a second tab replaces it
and the first tab shows the new account the moment it next loads a page. That
reads exactly like a session changing hands on its own, and the log is what
tells the two apart — `deploy/README.md` now says so.

## A scanned location landed in the quantity column

Filing a bag from the invoice's scan panel wrote the shelf into the Qty cell of
the matched line and the count into the Unit price cell — each one column to the
right of where it belonged, and the picker that actually holds the location was
left untouched. The page had been telling this lie since the two invoice tables
were given the same five columns; a reload (any *Edit line* → *Save*) put every
row right again, which is why it looked like a display glitch rather than a
wrong write.

The adapter reached for its cells by index, so the reshuffle moved the ground
under it. It now finds the count by class and the location by its picker — the
same picker a person clicks. Two of the things it keeps in step, the row's
`data-location-id` and the picker's last-value, are what an inline pick in
`invoices.js` already does; the picker's `title` is new here, because nothing
updated it after the server rendered it. The placeholder option is dropped on a
real line once it has a shelf (that endpoint only assigns) and kept on a staged
one (which can still be cleared).

The test fixture had drifted from `invoice_detail.html`, which is how a wrong
column stayed green: its rows now mirror the template, and the three assertions
that pinned the old indices fail against the old code.

## Settings moved off the nav, into one button

*Label printer*, *Users*, *API* and *Change password* each sat in the top bar
beside the pages, so the bar mixed places to work with things you set up once and
then forget. They now live in a menu behind a gear button on the right of the
bar, next to the account it belongs to. The nav is what is left: Components,
Locations, Invoices, BOMs and, for an admin, Types, Match rules and Audit.

The menu closes on a second press of the button, on a click anywhere outside it,
on Escape, on the focus leaving it, and as soon as an entry in it is chosen.
Escape takes the focus back to the gear only if the menu still had it — the
invoice page blurs the focused control on Escape to hand the keyboard back to
the barcode collector, and grabbing it back would disarm the scanner. *Users* is
still admin-only; the gear itself shows the current-page highlight while one of
its pages is open, so nothing is hidden that used to say where you are.

The entries are ordinary links and one button, with no ARIA menu roles: the menu
pattern would promise arrow keys and a roving tabindex that nothing here
implements, and would take Tab — the thing that does work — away from a screen
reader.

## A backup every night, without being asked

`deploy` now installs `shelfos-backup.service` and `shelfos-backup.timer` and
enables the timer, so an install has backups from its first day instead of from
the day somebody remembers to arrange them. It runs at 03:15 local time, catches
up a night the machine spent switched off, writes into
`/var/lib/shelfos/backups` as `root:root` 0700, and sweeps archives older than
thirty days. The app keeps serving throughout — the snapshot goes through
SQLite's online backup API, so there was never anything to stop.

`status` now reports the pair that fails apart: when the timer next fires, and
the newest archive with its date. An enabled timer firing every night into a
service that has been erroring since a disk filled up looks perfectly healthy
until somebody asks what it has produced — so it also exits non-zero on a timer
that was stopped, on one that has fired and left nothing behind, and on a newest
archive older than a week.

The archives are `root`'s, and the deploy now keeps them that way: handing
`/var/lib/shelfos` to the service user used to be a recursive `chown` over the
whole tree, which reached the archives and gave every password hash in the
database to the account the web app runs as.

The hour and the retention are the operator's, so a re-deploy that finds an
edited unit shows the difference and asks before replacing it, and `update`
never touches either file. `deploy --no-backup-timer` installs without the
schedule.

Two things the schedule still does not do, both written down in
`deploy/README.md`: it does not copy anything off the machine, and it does not
carry `/etc/shelfos/env` — the archives are passed around far too casually to
hold the signing secret and the shop keys.

## What the printer's own machine has to be

Said out loud rather than discovered: the machine with the printer has to be
Linux with systemd, because the bridge writes to a `/dev` node and reads status
frames back from it, and both halves are user units. The setup page says so
before anybody downloads a script they cannot run.

The alternatives are written down with it — a network-capable printer (nothing to
install anywhere), a small Linux box beside the printer, or a Windows client,
which is real work: the tunnel half ports easily, and the bridge needs libusb via
`brother_ql`'s pyusb backend, because printing through the Windows spooler is
one-way and this design leans on the status frame for tape detection, the
two-colour refusal, faults and confirmation.

## Setting up the label printer from the browser

*(Seven fixes from review, folded into the entry below: the port on the machine
with the printer and the port on the server are now kept apart — choosing one on
the form asked the server to bind it, which its sshd refuses, after the page had
said the printer was registered; `--dry-run` no longer reaches for sudo to read a
world-readable file; `tunnel-key` run as an ordinary user no longer reads the
root-only settings file as "unset" and writes keys to the wrong place or for the
wrong port; a settings change during a deploy restarts the service like every
other change; the same key under a new comment replaces its entry rather than
adding a second; the connection probe asks `configured_device()` like everything
else; and the enrolment token's fingerprint is compared with `compare_digest`.)*

Printing to a printer on someone else's desk worked, and standing it up meant
reading the README, cloning this repository onto a laptop for one file, and
writing two systemd units and a udev rule without a typo. Nobody has a clone:
the deployment is a server and a browser.

- **`/label-printer`**, open to anyone signed in — read-only accounts included,
  because it concerns the machine in front of the person rather than what they
  may change in ShelfOS. Answer three questions, download one script with the
  answers already in it, read it, run it.
- The script carries the bridge inside it as base64 and checks its sha256 after
  decoding, so what lands on the laptop is this repository's file byte for byte
  and a truncated download says so instead of half-installing. `--show-bridge`
  prints it, `--dry-run` prints what it would do and never calls sudo.
- It checks ssh **before** it changes anything, and separates the two failures
  that look identical from the outside: an unaccepted host key and a key the
  server does not know both leave the tunnel restarting for ever, saying
  nothing. Each gets the command that fixes it.
- It refuses to run as root. Under sudo it would set up root's user services and
  root's linger — two services nobody would think to look for, while the user's
  never start.
- **Test connection** asks the printer what tape it holds, over the same path
  printing uses, and names the failure rather than shrugging. The one worth
  having is "the connection was accepted and then went quiet": the bridge is up
  and the printer behind it is not — unplugged, in Editor Lite mode, or held by
  CUPS. Everywhere else that collapses into "the printer is not saying what it
  holds", which sends people to the wrong machine.
- The page says nothing whatever about the server. What ShelfOS itself needs is
  the administrator's, set elsewhere; the person with the printer has no way to
  act on it and no reason to see it.
- A deploy now creates **`shelfos-tunnel`**, an account for this and nothing
  else, and the page fills its name in. Not the service account: that one has no
  shell and a root-owned home, so sshd would refuse it, and giving it those would
  turn a confined service account into a login account.
- **A deploy that changed the code restarts the service.** `enable --now` starts
  a stopped service and does nothing to a running one, so a re-deploy left the
  old process serving the old code while the new templates sat on disk — which is
  a service half-changed: a new link in the navigation, and a 404 behind it,
  because routes are registered at import and templates are read per request. Now
  anything that changes what a running service executes — the code, the
  virtualenv, the unit — restarts it, and nothing else does.
- **`deploy --reinstall` moves the installed code.** It used to skip that step
  outright as "already a checkout", so an install cloned from `main` stayed on
  `main` through an update and a re-deploy — every step reporting success, with
  the only symptom a feature that never appeared. It now brings `/opt/shelfos` to
  what the clone being deployed has checked out, branch included, and refuses
  over hand-edited files there. A plain `update` says which branch it is on and
  points at `--ref`, since an update that changes nothing looks the same as one
  that had nothing to do.
- A second `deploy --reinstall` **keeps the domain the first one was given**,
  read back from the Caddy config it wrote. Asked again without `--domain` it
  used to fall back to no TLS, which on a working HTTPS server drops the proxy
  from the plan and loosens the session cookie — a re-run quietly undoing the
  thing it is re-running. Only our own site counts: a Caddyfile serving somebody
  else's site names their domain, not ours.
- The deploy asks sshd what it will **actually do** for the tunnel account
  (`sshd -T -C user=…`), rather than trusting that a file it wrote is a file that
  applies. An `AllowUsers` list it cannot extend, or somebody's own `Match` block
  further down, would otherwise refuse every printer in exactly the words an
  unauthorised key produces — and the installer's message now names that
  possibility too.
- A deploy installs **fonts-dejavu-core**. A label is a bitmap and drawing text
  into one needs a TTF on the host, which a server has no desktop to have brought
  — so every preview and every print failed with "no label font found" until
  somebody worked out that a font was the missing piece. No setting goes with it:
  DejaVu is the first family the renderer looks for.
- **A registered machine is a configured printer.** Test connection went green
  and the Print buttons still were not there, because `SHELFOS_LABEL_DEVICE` was
  unset — so the page's promise stopped one step short of an administrator
  editing a settings file and restarting the service, with nothing left to
  decide. A registration now answers that question by itself; the setting still
  wins where it is set, and withdrawing the last key takes the buttons away
  again.
- The tunnel asks for `127.0.0.1:<port>` by name rather than for a bare port,
  and the server permits both spellings. sshd matches `PermitListen` against what
  the client *asked* for, and a bare port carries no address at all — so a rule
  naming only the address it would resolve to could refuse the exact forward it
  was written to allow, with the client reporting it in the same words it uses
  for a port that is genuinely in use. The message now names both possibilities.
- **Signing in works on a deployment served without TLS.** The session cookie is
  marked `Secure` in production, and no browser sends one of those back over
  plain HTTP — so the session was dropped, the sign-in form's token had nothing
  to match, and every attempt said the form had expired, with nothing in the log
  because nothing had failed. `SHELFOS_COOKIE_SECURE` decides it now, a deploy
  with `--no-tls` sets it, and the app says so at startup.
- **`deploy --listen ADDRESS`**, so a server without a proxy can be reached at
  its own address instead of through a forwarded port. Still 127.0.0.1 by
  default — a plain-HTTP port on a network interface carries sign-ins in the
  clear, and the summary says so when one is asked for. `status` and `update`
  read the address back out of the unit, so they stop reporting "no answer" for
  a healthy service that simply is not on loopback.
- The ssh target offered is no longer whatever the browser's address bar says.
  A loopback address there means a proxy or a tunnel in between — a container's
  proxy device, a published port, an `ssh -L` — and ssh from the machine with
  the printer would come back to that machine. The page offers the address the
  server sees itself at instead, and says why.
- **Nothing to do on the server.** The script registers its own public key with
  ShelfOS over the session the person is already signed in with — no account
  here, no ssh, nothing typed. sshd reads that account's keys from a command
  (`AuthorizedKeysCommand`) printing a file ShelfOS owns, so the service needs no
  privileges to authorise a machine, and a `Match User` block caps what any key
  in it may do: one remote forward of one loopback port, no shell, nothing
  outbound. The deploy validates the block with `sshd -t` before reloading, and
  withdraws it if it does not pass — a bad sshd config that gets reloaded is how
  people lose the only way into their own server.
- The registration token lives in the downloaded script, is good for a week, and
  is **not a sign-in**: tokens carrying a scope are refused everywhere else in
  the app, so a shell script in somebody's Downloads can never be presented as
  the account that downloaded it.
- **The key is made on the machine with the printer and stays there.** ShelfOS
  hands out a script, never a credential — a page that handed out a private key
  would make "can open this page" mean "has ssh access to the server", and a key
  fetched by ten people is nobody's. `./shelfos.sh tunnel-key add "<public key>"`
  remains for what the browser cannot cover — a server without the sshd block, or
  a read-only account, which may read the page but not change what this server
  accepts — with `list` and `remove` beside it.
- `sshd -t` will not test a configuration at all while its run directory is
  missing, and on a machine where ssh has never started it is — systemd makes it
  when the service comes up. The check now makes it first, and when sshd still
  refuses, asks again **without** the new file: only a rejection that goes away
  with the file removed is the file's fault. A configuration that was already
  broken is reported rather than blamed on the deploy.
- An install made before any of this **learns the two new settings** even though
  its `/etc/shelfos/env` is never replaced (it holds the signing secret and the
  shop keys). Only what is missing or empty is filled in; an answer already
  there is somebody's decision.
- **A deploy no longer dies because udev would not re-apply a rule.** In a
  container `/sys` is not writable even for root, so `udevadm trigger` reports
  "Permission denied" for every device and exits non-zero — which ended the
  deploy at step 10 of 12, with everything installed and nothing started. The
  rule is written either way and takes effect at the next replug, so this is now
  a warning that says so. The same for reloading sshd, which may not be running
  yet on a machine being set up.
- The ssh check is now the connection the unit actually makes, held open for a
  moment. `ssh host true` tested a session, which a forwarding-only key refuses
  on purpose: the check would have failed on a setup that works. The new one
  tells apart an unaccepted host key, a key nobody has authorised, and a port
  already taken on the server — and each gets the command that fixes it.

## A bridge of our own for the network printer

The `socat` line the last entry recommended turns out to be the wrong tool, and
it fails in the way that wastes the most time: intermittently. A Brother QL
answers a question when it is ready, and a read taken before then returns zero
bytes — which `socat` reads as end of stream and hangs up on. So the tape was
read "sometimes", which looks like a flaky printer or a flaky network and is
neither.

- **`scripts/label_bridge.py`** replaces it: the same polling loop the app's own
  reader has always used, so a printer at the far end behaves exactly like a
  local one rather than nearly. Measured on a QL-800: the device directly
  answered 10 out of 10, `socat` 2 in 8 (7 in 8 with `ignoreeof`), this bridge
  20 out of 20.
- It also writes in a loop, which `socat` did for us and a first draft of this
  did not: `os.write` may accept less than it is given, and on a printer behind
  a small kernel buffer it routinely does — losing the tail of every label while
  a three-byte status request still worked perfectly.
- The transport tests now drive the shipped script instead of an in-process
  imitation of it, so they cannot go on passing while the thing people actually
  run drifts away from them.

## The label printer can be somewhere other than the server

ShelfOS writes raster bytes straight to a device, so the printer had to be on the
machine running the service. That is the wrong shape for the ordinary case: the
service is on a server and the printer is on the desk of the person printing.

- `SHELFOS_LABEL_DEVICE` now takes `tcp://host:port` as well as a device path.
  On the machine holding the printer, `socat` relays one connection to it and a
  reverse SSH tunnel carries the port; both are user services, so they come up
  on their own and nothing is exposed to the network. README has the two unit
  files.
- Nothing downstream changes. The transport was already a file descriptor, so
  the tape is still read off the printer, a fault still stops the job before any
  tape moves, and each label is still confirmed — tested end to end through a
  real bridge in front of the existing fake printer, not against a stub that
  would have proved only that a socket works.
- The failure modes a network brings are answered rather than inherited: the
  connect is bounded by the caller's own budget, so a sleeping machine cannot
  stall every preview for the length of a print timeout while holding the print
  lock; a timeout says what happened rather than "None"; and a peer that accepts
  and never speaks ends the read instead of being taken for a quiet printer,
  which would have burned the whole budget before the useful error.
- The tunnel is what keeps the server's setting stable: it always talks to its
  own `127.0.0.1`, so the printer's machine can move networks or sit behind NAT
  with nothing on the server to change.

## Restoring a backup could leave a service that would not start

An archive carries its own accounts. Restore one taken from a laptop onto a
production install and it brings that laptop's admin — usually still on
`admin`/`admin` — so the next start refuses, correctly, over a password nobody
on that machine ever chose. The service was left down, three steps after the
cause, and the way out did not work either: the refusal said to run
`scripts/set_password.py`, which falls back to a database path relative to the
working directory. On a server that is not where the database is, so anyone
following the advice literally got "No database at /opt/shelfos/data/shelfos.db".

- **`./shelfos.sh password [USERNAME]`** sets an account's password on whichever
  install is here, with the database named and as the right user. It offers to
  start the service afterwards, since the reason to be there is usually one that
  will not start.
- **`backup restore` checks before starting.** If the restored database has an
  admin on the default password it says so while the service is still stopped —
  the only moment the fix is one command away — and offers to set a new one there
  and then.
- The refusal message now names the wrapper first, and says that running the
  script directly needs `DATABASE_URL`.
- Fixes a crash shipped in the previous entry: the handback after a restore read
  two variables nothing assigned, which under `set -u` ends the script — after
  the database had been replaced and before the service was started. shellcheck
  finds that only with `check-unassigned-uppercase`, which is off by default; CI
  turns it on, and a test asserts every global the script reads is one it sets.

## A backup you could not restore

`./shelfos.sh backup restore ~/snap.tar.gz` failed with `Permission denied` on an
archive that was plainly world-readable, and no `chmod` on it helped — because
the file was never the problem. The wrapper stepped down to the service user with
`sudo -u shelfos`, giving up root's right to traverse directories, and a home
directory on Ubuntu is `0750`. The service user could not enter it whatever the
archive's own mode was.

- `backup` runs as root on a deployed install now. Root can read an archive
  wherever the operator left it, and the data is chowned back to the service user
  after a restore — always, not only on success, since a restore that fails part
  way is exactly when root-owned data is left behind. Symlinks are resolved
  first, because `chown -R` on a symlinked operand changes the link rather than
  the tree behind it, and an attachments directory pointing at external storage
  is something backup.py deliberately supports.
- **`/opt/shelfos` stays root-owned.** It was chowned to the service user, which
  bought nothing — `ProtectSystem=strict` already makes it read-only to the
  unit — and became a real hazard the moment root started running code out of
  it: a bug in the web app would have been a path to root on the operator's next
  `sudo ./shelfos.sh backup`.
- A relative archive path is made absolute before it is handed on, since it is
  read by a process that need not share the working directory it was typed in.
- Archives go to a `root`-owned `0700` directory: each one carries every password
  hash in the database, and the service has no reason to read them back.
- The gzip header no longer records the temporary name. The archive is built as
  `<name>.part` and renamed, so `file` reported a finished backup as having been
  `something.tar.gz.part` — which reads like a truncated download and is what
  first made a perfectly good archive look broken.

## The "system" account belonged to the demo data all along

A fresh install had two accounts in its users table: the admin somebody asked
for, and a second admin-role row called `system` that nothing would ever use.
It was a leftover of decision D2, when there was no authentication and one
account owned every recorded action; D11 gave people real accounts and every
action has been attributed to whoever took it ever since, but the seeding stayed
in the startup path. It looked like an account someone had forgotten about,
which is exactly what it was.

- It is created by the demo data now, which is the only thing that needs it —
  stock movements and audit entries take a `user_id` that is a foreign key, and
  nobody is signed in while demo data is generated. A production install has one
  account.
- New databases call it **`demo`**. An older database's `system` row is adopted
  as it stands rather than renamed: its name is what the audit log shows against
  everything that account ever did, and renaming it now would make old entries
  claim something that was never on screen. Adoption goes by the property, not
  the name — only an account that cannot sign in is taken, so a person's account
  that happens to be called `demo` never ends up with hundreds of demo actions
  recorded against it.
- Giving it a password is refused everywhere, not only by
  `scripts/set_password.py`. The Password button on the users page could turn it
  into an ordinary admin account, quietly, while the account was documented as
  one that cannot sign in.

## httpx was a test dependency that the app imports to start

`url_fetch` and all four shop providers import `httpx` at module scope, but it
was declared in the `dev` extra rather than among the runtime dependencies. That
worked everywhere it was ever tried — `run.sh`, the manual instructions and CI
all install `.[dev]` — and failed the first time anything installed the runtime
set alone, which is exactly what a real deploy does. The service came up in a
restart loop with `ModuleNotFoundError: No module named 'httpx'`.

- `httpx` is a runtime dependency now.
- CI grows a **Runtime dependencies** job that installs without the extra and
  starts the app, because nothing else in the suite could have caught this:
  every other job runs in an environment where the missing package is present
  for another reason. Importing alone would not do either, so the job starts
  uvicorn and asks `/health`.

## One command, whichever thing you are doing

`run.sh` only ever did one of the two things anyone does with this repository.
Installing it on a server was a dozen commands from `deploy/README.md`, several
of which fail silently when got wrong — an `export` systemd cannot parse, a
relative data path under a read-only filesystem, a database moved after the
first start rather than before it. `shelfos.sh` replaces it with one entry
point.

- **`devel`** is the old `run.sh` plus the conveniences it never had: a
  `--port`, a `--reset` that hands off to `reset_db.py` and its own
  confirmation, and an offer of demo data for an empty database. It still runs
  in the clone and still keeps its database at `data/shelfos.db`; separate
  instances are separate clones, and nothing under `data/` or `attachments/` is
  ever moved, renamed or deleted by the script.
- **`deploy`** does the whole of `deploy/README.md`: packages, the system user,
  `/opt/shelfos`, `/var/lib/shelfos`, a generated signing secret, the unit,
  Caddy and its certificate. It asks everything up front, then works, and every
  step says `ok` or `skipped` — so re-running it is how a half-finished install
  is repaired rather than something to be careful of. It refuses to overwrite
  `/etc/shelfos/env`, refuses to overwrite a `Caddyfile` that serves anything
  besides the site it wrote there itself, refuses to invent a first admin
  password when there is no terminal to ask at, and does not roll back on
  failure, because removing a half-made user or a directory that may hold data
  is the more dangerous thing to do.
- **`update`** takes a backup first, refuses to pull over hand-edited files, and
  restarts only after it has checked the app answers. **`status`** says what is
  installed and whether it is healthy without ever printing a setting's value.
  **`backup`** wraps `scripts/backup.py` with the right paths and the right user.
- **`--dry-run`** works on all of them: it prints what would happen, changes
  nothing, and never calls `sudo` — which is also what makes a root-privileged
  installer testable. Secrets are blanked even there.
- **A behaviour change worth reading twice**: the settings file is now *parsed*
  rather than sourced, and fills in only what is not already set. So
  `PORT=8080 ./shelfos.sh devel` finally wins over a `PORT` line in the file, as
  the README always claimed it did — and shell in that file (`$(…)`, backticks,
  references to other variables) no longer runs. The script warns when it sees
  one. This also stops `deploy` from executing a root-owned file in `/etc` as
  code.

## The startup check was asking the wrong question

`SHELFOS_ENV=production` refused to start on the default admin password, and it
was checking `SHELFOS_ADMIN_PASSWORD` — the environment variable. But the
bootstrap admin is seeded **only when no login-capable admin exists**, so on any
database past its first run that variable says nothing about the account. Set a
strong one on an install seeded months ago, and every check passed, the log said
nothing, and `admin`/`admin` still signed you in. That is precisely the state
the check exists to prevent, and it was reached by configuring the instance the
way the README asked.

- Startup now asks the accounts: any active admin whose password is still the
  public default refuses the boot in production, and warns otherwise. The
  message names the account, says how to fix it, and says why setting the
  variable did not.
- **`scripts/set_password.py`** is the way out, because there had to be one — the
  refusal is unfixable through the UI, which needs the app to be running. It
  prompts without echoing (or takes `SHELFOS_NEW_PASSWORD` when unattended),
  applies the same password policy the app does, and says which database it
  opened before changing anything — it refuses one that is not there rather than
  creating an empty database and then reporting the account missing, which on a
  box that will not boot reads as the accounts having been lost. It doubles as
  the answer to an admin locked out of their own account, which had none.
- `SHELFOS_ADMIN_PASSWORD` is now judged only where it is about to be read: on a
  database with no admin yet. It was refusing production whenever it was unset,
  which after the paragraph above would have met the operator who fixed the real
  account and then dropped the variable the README calls pointless — told to set
  a decoy value that no account uses.
- Enabling an account that still has the default password is refused. Startup
  looks at active admins, so a disabled one holding it is invisible there, and
  turning it back on from the users page would put the instance on the public
  default at once while arming a boot failure for the next restart — over an
  account nobody touched at that moment.
- The README now says plainly that those two variables seed a first admin and
  do nothing afterwards.

## Taking a BOM off the shelves

Building a board meant walking the BOM by hand — find the part, find where it
lives, remove the right count, sixty times — and nothing afterwards recorded what
was taken. Now one button does the walk in one transaction and leaves a snapshot
behind.

- **#133** — *Take parts…* on a BOM report. Set the board count, say which
  temporary location the parts were gathered into, adjust a quantity where the
  BOM is wrong, and confirm. The gathering branch is drained before any ordinary
  shelf; what it cannot cover falls back — silently when there is one place, as a
  question when there are several. A part that is short does not stop the run: it
  is taken as far as it goes and the shortfall is recorded.
- **#133** — Every run leaves a snapshot named after the BOM and the moment, and
  every stock movement's note carries that name and links to it. The link lives in
  a table of its own rather than a column on the ledger, because the schema has no
  migrations and a new column would mean recreating the database.
- **#133** — A take can be undone with a written reason, which is required. The
  parts go back exactly where they came from and the snapshot stays on record,
  marked as reversed — a double-clicked Undo cannot return the same stock twice.

## Three small things the security review left open

None of them was going to be how an instance fell over, which is why they came
after the throttle and the session invalidation. They are also cheap, and each
one was a door left open for no reason.

- **The API docs need an admin now.** `/docs`, `/redoc` and `/openapi.json`
  were public: an inventory of every endpoint, its parameters and its shapes,
  which is as useful to someone looking for a way in as to whoever runs the
  instance. They sit behind the same session every other page does, and still
  follow an ASGI `root_path`, so they keep working under a proxy that mounts
  ShelfOS at a prefix.
- **The sign-in and sign-out forms carry a CSRF token.** They were the two
  plain HTML posts, with no header for the existing check to look at. A forged
  sign-out is a small thing to be able to do to someone — dropped work, and a
  login form to phish at the end of it — and a forged sign-in lands them in an
  account the attacker controls. Signing in also starts a fresh session rather
  than adopting the one the browser arrived with (session fixation). The login
  page is sent `no-store`: it now carries a per-session token, so a copy the
  browser kept is a stale one, and a Back-button form would otherwise reject
  the first sign-in typed into it.
- **A sign-in takes the same time whether or not the username exists.** A
  wrong password for a real account cost a bcrypt round and a guess at a name
  nobody had cost none, and the difference is readable off the response time —
  which made the form a way to ask whether an account exists. A rejection now
  verifies against a hash of a random value when there is no account to verify
  against.

## Changing a password now ends the sessions made with the old one

Until this, it did not. Both ways in outlive the password they were issued
against — an access token is stateless and a session cookie is signed, so
neither has a server-side record to delete — which made "change your password"
no answer at all to the situation it exists for: someone else has your
credentials. A leaked token stayed good for its full 24 hours, and a stolen
cookie indefinitely.

- Each sign-in now carries a fingerprint of the password it was made with, and
  it is checked on every request. Setting a new password changes the hash,
  which changes the fingerprint, which retires every sign-in issued before it.
  The fingerprint is an HMAC keyed by the app secret, not the bcrypt hash: it
  travels in a JWT payload, which is signed but readable. (Django calls the
  same mechanism the session auth hash.) No schema change and no session store,
  which matters in a project with no migrations.
- An admin resetting an account's password signs that account out everywhere.
  A user changing their own in the browser stays signed in there — the change
  would otherwise sign them out of the request making it — and loses their
  other sessions and tokens.
- The admin route now refuses to reset **your own** password, and the Users
  table drops the action from your own row; *Change password* in the top bar is
  the way. That route asks for the current password first, which is what stops
  a bystander at an unlocked browser from taking the account over — a
  protection an admin was until now the only person unable to have, by
  resetting themselves through the admin route instead.
- **Everyone signs in once more after this deploy**: a session or token from
  before carries no fingerprint, and treating a missing one as acceptable would
  make the check optional at the caller's choosing.
- `report.json`, a BOM report dumped while working on the report code, is no
  longer tracked. It is one project's parts list; the repo need not carry it or
  keep its history.

## Sign-in hardening

A public instance is only as safe as its weakest password and the number of
guesses an attacker gets at it. Both now have a floor.

- **Sign-ins are throttled** per client address: ten failures in fifteen minutes
  and the address is refused (429, `Retry-After`) until the oldest failure has
  aged out — before the password is checked, so a locked-out address costs no
  bcrypt work either. Per address rather than per account, so guessing at a name
  cannot lock its owner out; per /64 for IPv6, where one host owns the whole
  allocation. An attempt counts from the moment it starts and is uncounted only
  on success, so concurrent connections cannot each pass a check none of them
  has recorded yet. Every failure is logged with the name and the address, in a
  shape no username can alter, which is what a fail2ban filter matches.
  `SHELFOS_LOGIN_MAX_FAILURES` and `SHELFOS_LOGIN_FAILURE_WINDOW_SECONDS` tune
  it; 0 turns it off.
- **Passwords must be at least 8 characters** — created, reset, or changed by
  their owner; the dialogs say so up front with `minlength`. The bootstrap
  admin's password comes from the environment and is judged at startup instead:
  fatal in production, a warning otherwise, so a development install still comes
  up on `admin`/`admin`.

## A BOM line has to name its part

The BOM report used to resolve a line by looking its MPN up in inventory. An MPN
is not unique across manufacturers, so that answer can be the wrong part — and it
was good enough to make a BOM read as ready to build while nobody had ever
confirmed half its lines. The report now describes stock only for a line someone
has assigned a component to, which is the groundwork for taking a whole BOM off
the shelves in one go.

- **#131** — Lines are `unresolved` until a person assigns a component to them;
  `in stock` / `short` / `out` are reserved for lines that have one, and one
  unresolved line caps *buildable boards* at 0. The statuses `not in inventory`
  and `no MPN` are gone: they said different things about the same situation, and
  the answer to all of them was the same — assign a component. **Existing BOMs
  will look worse before they look better**, which is the point.
- **#131** — *Assign the obvious ones* settles, in one request, every line whose
  MPN admits exactly one component and whose manufacturer does not contradict it,
  leaving a person only the lines that genuinely need judging. Beside it, *Show
  only unresolved* drives the Status column's own filter.

## Manufacturer names — one part, however it is spelled

An MPN does not identify a part: two companies really do print the same number on
different components, and one company is printed several ways. ShelfOS now asks
rather than guesses, and remembers the answer. All four ways a part enters the
catalog ask the same question, through one shared rule.

- **#116** — Importing a part whose number is already in stock under a different
  maker's name shows what it found and lets you say which, if any, is the same
  part. Answering also records that spelling, so the question is asked once.
- **#118** — Match rules page grows a *Manufacturer names* section: an admin can
  see every recorded spelling and forget one. Before this the recording was a
  one-way door — not even editing the component could undo it, because that write
  path canonicalises too.
- **#119** — Scan putaway stopped matching on the part number alone. A bag label
  states its maker in the `1V` field; a scan is a putaway only when that maker is
  the one the part is stored under. Silence on either side is not disagreement —
  most 1D barcodes name nobody, and a Farnell invoice prints no manufacturer
  column at all.
- **#120** — The invoice review asks it too. A staged line whose number is already
  in stock carries an *Already in stock?* marker; *This is it* files the line
  against that component instead of creating a second one.
- **#127** — The dialog's promise ("picking this also records that spelling") is
  now answered per candidate by the server, which is the only side that can: the
  test folds accents and punctuation, so a client comparing lowercased strings
  promised rules that would never be created.

## Matchers within reach

Teaching the import engine a word meant leaving whatever you were doing, going to
the admin match-rules page, and re-picking domain → type → parameter from scratch.

- **#98** — A parameter carries its own **Matchers** panel: the rules scoped to it,
  listed in two sections (a shop's word for one of the allowed values, and a shop's
  label for the parameter itself), editable and deletable in place, with a quick-add
  row that stays open so a run of aliases goes in one after another rather than one
  modal per word. The new-type builder gets the same reach — its "Create matcher"
  saves the type first, then opens the matcher scoped to the fresh parameter.

  Aliases are now written as one comma-separated list per target, so a value that
  six shop spellings map onto is one row and one field rather than six rows. One
  rule per alias is still what is stored — that is what keeps the duplicate guard,
  the ordering and the audit trail per alias — and an alias may no longer contain a
  comma, since it would be read as two.

  The create dialog moved out of the admin page into `openMatcherDialog`, so all
  three callers open the same one.

## Farnell

- **#115** — A Farnell bag is recognised by the `3P` order code on its label.
- **#114** — Farnell invoice PDFs import, with the supplier's own line numbers
  and customs-code count used as integrity checks.
- **#113** — Parts import from the element14 API by URL or part number.

## Invoice review

The draft page holds rows at two stages of the same thing. They differed in what
you could do with them, and in what they were called.

- **#121** — A staged row and a real line now offer the same edits: an inline
  location picker on both, and an *Edit line* dialog over quantity, unit price and
  supplier part number on both. The three editable things on the page are named
  apart — *Edit invoice*, *Edit component*, *Edit line* — since two of them were
  called "Edit".
- **#122** — Both tables read the same: part, location, quantity, unit price,
  total, in that order, on one shared column grid. The component type and the
  supplier's code are no longer columns; a *missing* type still shows, because it
  blocks finalizing and nothing else would say so.

## Failures that used to be silent

Three `/web/api/*` readers mapped their payload with no usable catch around it.
An HTTP failure parses as JSON perfectly well and simply has no data, so the map
threw — usually out of an async event handler with nobody to catch it.

- **#123** — A failed component list no longer makes *Add line* a dead button.
- **#124** — The components table empties and says so rather than leaving the
  previous type's rows under the new filter, and the BOM picker's catch no longer
  ends one line early.
- **#125** — Both modal dialogs offer a **Retry** button instead of advice about
  retrying. Three fixes running had got the wording wrong; a button beside the
  message cannot be.

Throughout: an empty list and an unreadable one are now said differently. "You
have no components" is a claim, and a failure must not make it.

## Elsewhere

- **#129** — HTMX is gone. It was fetched from a CDN on every page load and no
  template ever carried an `hx-` attribute; what the pages actually use is
  server-rendered HTML plus small vanilla-JS modules talking to the JSON API.
- **#128** — This file. It starts at #112 because reconstructing the 111 before it
  from commit subjects would read as authoritative without being so.
- **#126** — Nine bespoke `[hidden]` resets become one rule, with the `until-found`
  exception. Author `display` beat the UA rule, so an element could be hidden as
  far as its `hidden` property and every test were concerned, and visible on screen.
- **#117** — `run.sh` sets the project up on a fresh clone and starts it thereafter.
- **#112** — The Components header carries live statistics that follow the table's
  filters.
