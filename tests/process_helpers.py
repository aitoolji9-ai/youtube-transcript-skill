"""Disposable process fixtures; no connection to a local application."""
import subprocess
import time


def alive(pid):
    p = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
    return bool(p.stdout.strip()) and not p.stdout.strip().startswith('Z')


def wait_file(path):
    for _ in range(100):
        if path.exists(): return
        time.sleep(.05)
    raise AssertionError('child readiness timeout')


def wait_dead(pid):
    for _ in range(80):
        if not alive(pid): return
        time.sleep(.05)
    raise AssertionError('orphan process remains')
