"""A child-side watchdog also fences orphaned training processes."""
import os
import signal
import threading
import time

def expired(path,now=None,seconds=110):
    try:return (time.time() if now is None else now)-path.stat().st_mtime>seconds
    except FileNotFoundError:return True

def watch(path):
    def loop():
        while True:
            time.sleep(5)
            if expired(path):
                # Runner was started in a new session. The parent agent is outside this group.
                os.killpg(os.getpgrp(),signal.SIGTERM)
                return
    threading.Thread(target=loop,daemon=True).start()
