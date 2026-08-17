"""Gunicorn settings for the household ledger.

Read by `gunicorn -c deploy/gunicorn.conf.py "app:create_app()"`.

The numbers here are small on purpose. This serves a household — four phones,
none of them looking at the same page at the same moment — off a Raspberry Pi
with 1GB of RAM and an SD card. The failure mode worth designing against is not
load; it is a worker count that quietly triples the memory footprint until the
kernel kills something, on a machine nobody is watching.
"""

import multiprocessing
import os

# ---------------------------------------------------------------- listening
#
# Loopback only. Nothing on the network reaches gunicorn directly: `tailscale
# serve` terminates TLS and proxies to this port, so binding 0.0.0.0 would put
# a plaintext copy of the app on the LAN alongside the encrypted one.
bind = os.environ.get("BIND", "127.0.0.1:8000")

# ----------------------------------------------------------------- workers
#
# Two processes, threaded. SQLite is the reason for the shape: writes take a
# lock on the whole file, so more processes do not buy more write throughput,
# they buy more processes waiting on each other. Two means a slow request (a
# receipt being resized) does not block the other person's page load, and the
# threads soak up the reads.
#
# `sync` rather than `gthread` would serialise a photo upload against everything
# else; `gevent` would need the whole app to be non-blocking, which Pillow and
# sqlite3 are not.
workers = int(os.environ.get("WEB_WORKERS", 2))
threads = int(os.environ.get("WEB_THREADS", 4))
worker_class = "gthread"

# A receipt from a modern phone camera is a few megabytes and the Pi resizes it
# in-process; 30 seconds is not enough on a cold SD card.
timeout = 120
graceful_timeout = 30
keepalive = 5

# Restart a worker after this many requests, with jitter so both do not go at
# once. Cheap insurance against a slow leak in a process meant to run for
# months without a deploy.
max_requests = 2000
max_requests_jitter = 200

# --------------------------------------------------------------- preloading
#
# Off. `preload_app` would open the database in the parent and hand the same
# connection to both workers — a shared sqlite3 handle across a fork is how you
# get "database is locked" and, worse, silent corruption. `db.py` opens per
# app-context; leave the fork before that happens.
preload_app = False

# ----------------------------------------------------------------- logging
#
# To stdout/stderr, which under systemd means the journal — one place to look,
# rotated by the system rather than by a file we would have to remember to
# prune on a 32GB card.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")

# The proxy is on loopback, so the remote address in every log line would be
# 127.0.0.1 without this. Tailscale sets X-Forwarded-For.
forwarded_allow_ips = "127.0.0.1"
access_log_format = '%({x-forwarded-for}i)s %(t)s "%(r)s" %(s)s %(b)s %(M)sms'

if multiprocessing.cpu_count() < 2:
    # A single-core Pi Zero would spend more time context-switching between two
    # workers than serving. Printed rather than silently adjusted: a config that
    # quietly disagrees with what it says is the reason nobody trusts config.
    import sys

    workers = 1
    print("gunicorn.conf.py: one CPU, so one worker rather than two",
          file=sys.stderr)
