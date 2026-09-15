# ShelfOS — Location labels and the label printer

The print-ready page, the Brother QL path, pointing ShelfOS at a printer that is
on another machine, and the handful of things that catch people out. The decision
behind writing to the device rather than going through CUPS is D12 in
[`DECISIONS.md`](DECISIONS.md).

## Printing a location label

Every location can be printed as a label — a QR holding `SL<id>`, which scanning
puts straight into the "Set location" flow — in two ways. `/labels/locations` is a
print-ready page for an ordinary browser print dialog (`?root=<id>` for one branch,
`?sheet=1` to flow onto A4). Alongside it, ShelfOS renders the same label onto a
Brother QL's own 300 dpi grid:

```bash
# the tape in the printer, as a brother_ql identifier
export SHELFOS_LABEL_TAPE="62red"       # DK-22251, the black/red roll in the box
# how long each label is, on a CONTINUOUS tape (a die-cut label's length is its die's)
export SHELFOS_LABEL_LENGTH_MM="30"
export SHELFOS_LABEL_MARGIN_MM="2"
# only if the host has none of DejaVu, Liberation or Noto (a deploy installs DejaVu)
export SHELFOS_LABEL_FONT="/path/to/Sans.ttf"
export SHELFOS_LABEL_FONT_BOLD="/path/to/Sans-Bold.ttf"
```

Pick the roll in the app: the per-location **Print** button opens a dialog with
the tapes you stock, the bitmap that tape would produce, and — when the printer
is holding something else — the question of whether to print on what is loaded
or go and change the roll. List the rolls you own so the picker is short:

```bash
export SHELFOS_LABEL_TAPES="62red,29x90,12,17x54,62x29"
```

**The size mostly settles itself.** A connected printer is asked what tape it
holds, and the label is laid out for that: swapping a 62 mm roll for a 29 mm one
changes the labels, not the settings. `SHELFOS_LABEL_TAPE` then matters for the
one thing the printer will not say — whether the roll is the black/red kind —
and as the fallback when no printer is answering. The layout follows the tape's
proportions too: a wide label puts the QR beside the name and path, a squarer one
stacks them, and a label longer than it is wide — a 12 mm roll, a 29 × 90 mm
address label — is composed along its length and turned a quarter turn, which is
how such a roll is read anyway. The code stops growing at 25 mm, where it is
already readable across a room, and on a narrow tape the margin gives way before
the code does.

`GET /api/labels/locations/<id>/preview.png` returns exactly the bitmap the printer
would receive, and takes `?tape=` / `?length=` to try a roll without touching the
environment — so the layout can be settled by looking, rather than by feeding tape
through a printer. A setting that would fail is named in a startup warning.

## Pointing ShelfOS at a printer

To print for real, point ShelfOS at the printer's device:

```bash
export SHELFOS_LABEL_DEVICE="/dev/usb/lp0"   # empty (default) = no printer; see the udev rule below
export SHELFOS_LABEL_PRINTER_MODEL="QL-800"
export SHELFOS_LABEL_MAX_JOB="50"            # labels per job
```

The raster bytes go straight to that device, so three things about the host matter.

**The device number is not stable.** `usblp` hands out the next free index, so a
printer that was `/dev/usb/lp0` can come back as `lp4` after a few replugs. A udev
rule fixes both that and the permissions (the node is `root:lp 0660`, and the
ShelfOS user is usually in neither group):

```
# /etc/udev/rules.d/99-brother-ql.rules
SUBSYSTEM=="usbmisc", ATTRS{idVendor}=="04f9", MODE="0660", GROUP="plugdev", SYMLINK+="shelfos-label"
```

```bash
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
export SHELFOS_LABEL_DEVICE="/dev/shelfos-label"   # stable across replugs
```

## When the printer is on another machine

**The printer can be somewhere else.** ShelfOS writes raster bytes straight to a
device, so the printer has to be on the machine running the service — unless you
point it at one over the network:

```bash
export SHELFOS_LABEL_DEVICE="tcp://127.0.0.1:9100"
```

**The machine with the printer has to be Linux with systemd.** That is what the
setup script and the bridge assume: a `/dev` node to write raster bytes to and
read status frames back from, and user units to keep the bridge and the tunnel
running. Windows and macOS are not supported, and the script will not run there.

If the printer is on a Windows machine, three ways out, best first:

1. **A network-capable printer** (QL-810W, QL-1110NWB and the like). Point
   `SHELFOS_LABEL_DEVICE` at `tcp://<printer>:9100` and there is no client
   software, no tunnel and no setup page in the picture at all.
2. **A small Linux box beside the printer** — a Pi, an old laptop. Everything
   below works unchanged, and the person at the Windows machine never touches it.
3. **A Windows client**, which is real work rather than a setting. The tunnel
   half ports easily: Windows 10/11 ship OpenSSH, so the key, the enrolment POST
   and `ssh -N -R` all have equivalents, with Task Scheduler in place of systemd.
   The bridge is the problem. Printing through the spooler in RAW mode
   (`WritePrinter`) is one-way, and this design leans on the status frame —
   tape detection, the two-colour refusal, fault reporting, print confirmation —
   so that route gives up most of what makes the printer usable. Reading back
   needs libusb: `brother_ql` already has a `pyusb` backend, but on Windows the
   device must be bound to WinUSB with Zadig, which takes it away from Brother's
   own driver. WSL2 needs `usbipd-win` and an attach per replug. Any of those
   would want a PowerShell installer and the bridge packaged as an executable,
   since Python is not a given there — and none of it touches this server: the
   enrolment endpoint, the key store and the sshd block are transport-agnostic.

## Setting a remote printer up

**The quick way is `/label-printer`** (*Settings → Label printer* in the top
bar), a page open to anyone signed in: answer three questions, download a script
with your answers already in it, read it and run it on the machine holding the
printer. It carries the bridge below, sets up both services, and checks the ssh
connection before it changes anything — then the page's Test connection button
asks the printer what tape it holds. The rest of this page is what that
script does, for setting it up by hand, for checking its work, and for the
reasons behind each step, which are the part a script cannot carry. The one
thing it deliberately does not do is touch the server: the setting above is the
administrator's to make.

If the server has no proxy in front of it — a test container, say — deploy it
with `--no-tls --listen 0.0.0.0` and reach it at its own address; the service otherwise
binds loopback and needs a forwarded port to be reachable at all, which is what
makes the address in the browser useless as an ssh target. `--no-tls` also
writes `SHELFOS_COOKIE_SECURE=0`, without which signing in cannot work at all:
the session cookie would be marked `Secure`, no browser sends one of those back
over plain HTTP, and every attempt would say the sign-in form had expired.

The page proposes the ssh target from the address you are reading it at, with
one exception that matters in a container: when that address is loopback, the
browser has come through a proxy or a forwarded port, and ssh from the machine
with the printer cannot follow it back — so what is offered instead is the
address this server sees itself at. Both are proposals in a field you can
change, and the port beside it is there for the same reason (an `lxc proxy`
device in front of ssh, say).

**The key stays on the machine with the printer, and registers itself.** The
script makes its own ssh key there and hands the *public* half to ShelfOS over
the session the person is already signed in with — so setting up a printer needs
no account on this server, no ssh, and nothing typed here. ShelfOS never hands
out a credential: what comes down is a script, and what goes up is a public key.

That works because sshd does not read the tunnel account's keys from its home. A
deploy points `AuthorizedKeysCommand` at a small root-owned script that prints a
file ShelfOS owns (`SHELFOS_TUNNEL_KEYS`), so the service writes ordinary data
and needs no privileges of its own. What a key in that file may then do is capped
by the server, not by trusting whatever wrote it:

```
Match User shelfos-tunnel
    AuthorizedKeysCommand /usr/local/lib/shelfos/tunnel-keys %u
    AuthorizedKeysCommandUser root
    AllowTcpForwarding remote        # -R only: no outbound connections
    PermitListen 127.0.0.1:9100 localhost:9100   # and only this port
    PermitTTY no
    ForceCommand /usr/sbin/nologin
Match all
```

On a server somebody else set up, two things can quietly undo all of this, and
the deploy checks for both by asking sshd what it will actually do
(`sshd -T -C user=shelfos-tunnel,…`) rather than trusting that a file it wrote
is a file that applies: an `AllowUsers`/`AllowGroups` list, which cannot go in a
`Match` block and so cannot be extended from ours, and a `Match` block of theirs
further down that wins on whatever it repeats. Either one refuses the tunnel in
exactly the words an unauthorised key produces.

`Match all` closes the block: drop-ins are included at the *top* of `sshd_config`,
and a Match left open would swallow every global setting after it. The deploy
validates with `sshd -t` before reloading and withdraws the file if it does not
pass, and reloads rather than restarts, so the session running it survives.

The upshot is that the worst a registered key can do — even one written by a
ShelfOS that had been compromised — is bind that one loopback port, which is to
say pretend to be a label printer. There is no shell, no other port, and nothing
outbound.

**A registered machine is a configured printer.** With `SHELFOS_LABEL_DEVICE`
unset, a machine that has registered through the page answers the question that
setting exists to answer: somebody said, with a key, that they have a printer at
the other end of a tunnel ending on this server's loopback. So the Print buttons
appear when the first machine registers, and go when the last one is withdrawn —
no settings file to edit, no restart. Setting `SHELFOS_LABEL_DEVICE` still wins,
for a printer plugged into the server itself or one pointed at by hand.

Registrations are visible and revocable on the server:

```bash
./shelfos.sh tunnel-key list              # which machines may connect
./shelfos.sh tunnel-key remove goofy      # withdraw one
./shelfos.sh tunnel-key add "ssh-..."     # authorise one by hand
```

`add` is for the cases the browser cannot cover: a server without the sshd block,
or a read-only account, which may read the page but not change what this server
accepts. The page says which of the two it is and prints the line to run.

The service account is deliberately not usable for this: it has no shell and a
root-owned home, so sshd would refuse it, and giving it those would turn a
confined service account into a login one. Whoever holds the tunnel makes no
difference to ShelfOS — a reverse forward binds this machine's loopback whoever
opened it, and the service just connects to `127.0.0.1`.

Everything else is unchanged: the tape is still read off the printer, a fault
still stops the job before any tape moves, and each label is still confirmed.
Only the last hop is different.

## The bridge and the tunnel, by hand

On the machine holding the printer, one bridge and one tunnel, both as user
services so they come up on their own:

```ini
# ~/.config/systemd/user/shelfos-label.service
[Unit]
Description=Expose the label printer on 127.0.0.1:9100

[Service]
ExecStart=/usr/bin/python3 /path/to/ShelfOS/scripts/label_bridge.py --device /dev/shelfos-label --port 9100
Restart=always

[Install]
WantedBy=default.target
```

`scripts/label_bridge.py` rather than a line of `socat`, and the reason is worth
knowing if you are tempted to substitute one. A QL answers a question when it is
ready, and a read taken before then returns **zero bytes** — which `socat` takes
for the end of the conversation and hangs up on, usually before the answer
arrives. Measured on a QL-800: reading the device directly answered 10 times out
of 10, `socat` 2 times in 8 (7 in 8 with `ignoreeof`), and this bridge 20 out of
20. That is the difference between "the printer is not saying what it holds"
appearing at random and not at all.

One connection is served at a time, which is what `usblp` allows anyway — so the
bridge never lets a single connection become permanent. A printer that stops
accepting bytes (a QL waiting for its cover to be closed) gives up after 30
seconds, and a caller that goes silent for two minutes is disconnected; both are
logged, and the next caller is served. `--write-timeout` and `--idle-timeout`
change those, and `--idle-timeout 0` turns the second one off.

```ini
# ~/.config/systemd/user/shelfos-label-tunnel.service
[Unit]
Description=Reverse tunnel for the label printer
After=shelfos-label.service

[Service]
ExecStart=/usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 9100:127.0.0.1:9100 CHANGE-ME-user@server
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**Before enabling the tunnel, ssh into the server by hand once.** A systemd
service cannot answer either of the two questions ssh asks on a first
connection, and both failures look the same from the outside — a unit stuck in
`activating`, restarting every five seconds for ever, saying nothing:

```bash
ssh-copy-id user@server     # your key in the server's authorized_keys
ssh user@server             # accept the host key, and check it lets you in
```

Then enable both, and look at what happened — `Restart=always` means a wrong
address or an unaccepted host key retries silently rather than stopping:

```bash
systemctl --user enable --now shelfos-label shelfos-label-tunnel
loginctl enable-linger "$USER"       # so it runs without a graphical session
systemctl --user status shelfos-label shelfos-label-tunnel
```

Both must say `active (running)`. `activating (auto-restart)` means the tunnel is
failing; `journalctl --user -u shelfos-label-tunnel` says why, and the usual
answers are the `CHANGE-ME` above still being there, a host key never accepted,
or a key the server does not know. Note `--user` throughout: these are user
units, and `sudo systemctl status` looks in the system manager and reports that
they do not exist.

Recreating the machine at the far end invalidates both halves at once — its host
key changes and its `authorized_keys` goes with it.

The tunnel is what keeps the server's setting stable: it always talks to its own
`127.0.0.1`, so the machine with the printer can change address, move networks or
sit behind NAT without anything on the server changing — and no port is exposed.
`ServerAliveInterval` and `Restart=always` matter more than they look: a laptop
that sleeps otherwise leaves the server with a port that accepts connections and
does nothing with them.

The port on the machine with the printer and the port on the server are two
different answers: the first is free to be anything not already taken there, the
second is fixed by the server's `PermitListen` and by what ShelfOS connects to.
The tunnel joins them, which is what a tunnel is for — so a laptop with something
on 9100 changes only its own end, on the setup page, and the server is untouched.

**9100 is a convention, not a requirement.** It is the port HP JetDirect used for
raw printing, so anyone who has set up a network printer recognises what this is —
but nothing in ShelfOS knows the number. It appears in three places, and any free
port works as long as all three agree: the bridge unit, the `-R` argument in the
tunnel unit, and `SHELFOS_LABEL_DEVICE` on the server. Change it if something on
either machine already listens there; `ExitOnForwardFailure=yes` in the tunnel unit
means a port already taken on the server fails loudly rather than leaving you
printing into nothing.

CUPS still must not hold the same printer, exactly as when it is plugged in
locally.

Joining the `lp` group works too (`sudo usermod -aG lp $USER`), but needs a fresh
login and leaves the unstable device number.

## What catches people out

**A QL-800 fresh out of the box is in Editor Lite mode**, in which it enumerates as
a 2 MB USB *mass storage* device holding Brother's Windows editor — no printer
interface, so no `/dev/usb/lp*` at all. Hold the Editor Lite button on the printer
for about a second until its LED goes out; it re-enumerates as a printer
(`04f9:209b`, "QL-800 Label Printer") and the node appears.

And **CUPS must not own the same printer**. Ubuntu creates a queue automatically
the moment a USB printer appears, and CUPS's `usb` backend *detaches the kernel's
`usblp` driver* whenever it touches the device. So the conflict does not show up
as a clean "device busy": the node disappears and comes back (`usblp4: removed`
in `dmesg`, then re-added a second later) and a job in flight dies mid-stream.
Remove the queue with `sudo lpadmin -x QL-800` and print through ShelfOS, or keep
the queue and print through the browser page instead — but not both.

**Two-colour tape is not optional to get right.** With DK-22251 (62 mm black/red)
loaded, a QL-800 *refuses* a one-colour job: it reports an error with no error
bits set, which looks exactly like a broken printer. Name the tape and the job is
built with two raster planes (the red one empty, unless a label uses red):

```bash
export SHELFOS_LABEL_TAPE="62red"    # DK-22251; "62" is the plain white roll
```

Labels go to the printer **one at a time**, and the dialog shows how far a run
has got with a Stop beside it. That matters for the case it was built for: a
whole cabinet started by mistake is hundreds of labels, and a job handed over in
one piece lives in the printer's buffer where nothing can reach it — the power
switch, mid-label, would be the only way out. Stopping takes effect after the
label being printed, so expect one or two more to come out.

ShelfOS talks to the printer both ways: before a job it asks what tape is loaded
and refuses when that is not the configured one ("the printer has 62 mm tape
loaded, but SHELFOS_LABEL_TAPE is '29'"), or when the printer reports an open
cover, a jam or an empty roll. After the job it waits for the printer to confirm
the print. A printer that stays silent is not treated as a failure — the job is
then reported as *sent* rather than printed.
