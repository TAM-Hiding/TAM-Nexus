"""Fork two descendants in separate sessions, then wait for termination."""

import os
import time


def child(grandchild):
    marker = MARKER + ("-grandchild" if grandchild else "-child")
    os.setsid()
    os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", "-S", "-B", "-c",
                                "import time; time.sleep(90)", marker])


if os.fork() == 0:
    if os.fork() == 0:
        child(True)
    child(False)
time.sleep(90)
