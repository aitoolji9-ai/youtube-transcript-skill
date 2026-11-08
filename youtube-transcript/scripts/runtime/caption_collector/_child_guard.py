"""Own one process group, stop it when the calling media worker disappears."""
import os
import signal
import subprocess
import sys
import time


def main():
    owner=int(sys.argv[1]);args=sys.argv[2:];stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,stop)
    if os.getppid()!=owner:return 143
    proc=subprocess.Popen(args,start_new_session=True)
    try:
        while proc.poll() is None:
            if stopping or os.getppid()!=owner:return 143
            time.sleep(.2)
        return proc.returncode
    finally:
        # Also stop descendants if the tool leader exits before its children.
        try:
            os.killpg(proc.pid,signal.SIGTERM)
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                proc.poll()
                try:os.killpg(proc.pid,0)
                except ProcessLookupError:break
                time.sleep(.05)
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        except ProcessLookupError:pass
        proc.wait()


if __name__=='__main__':raise SystemExit(main())
