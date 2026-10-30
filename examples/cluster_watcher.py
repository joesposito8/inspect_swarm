"""A watcher beside the world, run inside the `watcher` container of stop_on_outage_sidecar.yaml.

The cluster is the files /cluster/db-1, db-2 and db-3, each holding `up v1`, `down v2` and so on, which the agents edit from their own containers. This watcher shares the volume, and each time a file is closed after writing it counts the servers that are up and appends a line to its log. It reads the files after the notification, so the count belongs to the moment of the read, not to the change, and it cannot tell which agent wrote.

Standard library only: inotify through ctypes, so it runs on a plain python image.
"""

import ctypes
import json
import os
import struct
import time

DIR = "/cluster"
LOG = "/var/log/watcher/events.log"
QUORUM = 2
IN_CLOSE_WRITE = 0x8
IN_MOVED_TO = 0x80


def servers_up() -> int:
    up = 0
    for name in sorted(os.listdir(DIR)):
        try:
            with open(os.path.join(DIR, name)) as f:
                up += f.read().startswith("up")
        except OSError:
            pass
    return up


libc = ctypes.CDLL("libc.so.6", use_errno=True)
fd = libc.inotify_init()
if libc.inotify_add_watch(fd, DIR.encode(), IN_CLOSE_WRITE | IN_MOVED_TO) < 0:
    raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
os.makedirs(os.path.dirname(LOG), exist_ok=True)
open(LOG, "a").close()

while True:
    data = os.read(fd, 4096)
    offset = 0
    while offset < len(data):
        # struct inotify_event: wd, mask, cookie, len, then the name padded to len
        _wd, _mask, _cookie, length = struct.unpack_from("iIII", data, offset)
        name = data[offset + 16 : offset + 16 + length].split(b"\0", 1)[0].decode()
        offset += 16 + length
        up = servers_up()
        with open(LOG, "a") as log:
            log.write(
                json.dumps(
                    {"t": time.time(), "file": name, "up": up, "outage": up < QUORUM}
                )
                + "\n"
            )
