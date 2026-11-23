"""Inspect only synthetic sentinels and attempt bounded operations."""

import errno
import json
import mmap
import os
from pathlib import Path
import resource
import socket


def refused(call, *args):
    try:
        call(*args)
    except OSError as exc:
        return exc.errno
    return None


fixture = json.loads(Path("/input/job.json").read_text())
Path("scratch-test").write_text("scratch")
deny_reads = {key: refused(Path(value).read_bytes) for key, value in SENTINELS.items()}
traversal = "../../../../" + SENTINELS["state"].lstrip("/")
Path("link").symlink_to(SENTINELS["private"])
host_write = Path(SENTINELS["outside"]) / "must-not-be-written"
descriptors = [fd for fd in range(64) if refused(os.fstat, fd) is None]
network = [refused(socket.socket, family, socket.SOCK_STREAM)
           for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX)]
print(json.dumps({
    "fixture": fixture["phase"], "scratch": Path("scratch-test").read_text(),
    "readonly": refused(Path("/input/job.json").write_text, "modified"),
    "denied_reads": deny_reads,
    "traversal": refused(Path(traversal).read_bytes),
    "symlink": refused(Path("link").read_bytes),
    "outside_write": refused(host_write.write_text, "escape"),
    "root_write": refused(Path("/new-directory").mkdir),
    "network": network, "connection": refused(socket.create_connection, ("192.0.2.1", 9), 0.1),
    "descriptors": descriptors, "environment": sorted(os.environ),
    "pid": os.getpid(), "memory_budget": resource.getrlimit(resource.RLIMIT_AS),
    "process_budget": resource.getrlimit(resource.RLIMIT_NPROC),
    "memory_denied": refused(mmap.mmap, -1, 300 * 1024 * 1024),
}))
