#!/bin/sh
# What sshd asks for the label-printer tunnel account's authorized keys.
#
# Installed by shelfos.sh as root and named by AuthorizedKeysCommand, which is
# what lets ShelfOS authorise a machine without any privileges of its own: the
# service writes an ordinary file it owns, and this prints it. sshd will not read
# a key file owned by a third account, so without this the app could not do it at
# all — and giving the app a way to write root's files, or a sudo rule, would put
# far more than a printer within reach of a bug in it.
#
# Deliberately does nothing else. It takes the account sshd is asking about,
# answers for one, and prints a file. What those keys may then DO is decided by
# the Match block in /etc/ssh/sshd_config.d/60-shelfos-tunnel.conf, which holds
# whatever ends up in this file to one remote forward of one loopback port.
set -eu

account="${1:-}"
[ "$account" = "@TUNNEL_USER@" ] || exit 0

# Missing is not an error: no printer has registered yet, and sshd reads "no
# keys" from empty output. An error here would be logged as a broken server.
cat "@TUNNEL_KEYS@" 2>/dev/null || true
