"""One low-priority media worker shared by STT, OCR and media download."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import contextmanager

from .formats import CaptionError

ACTIVE_LOCK_FDS = []

ROOT = Path.home() / ".local/share/youtube-transcript"


def lower_priority():
    current = os.getpriority(os.PRIO_PROCESS, 0)
    if current < 19: os.nice(19 - current)
    if sys.platform == "darwin":
        policy = shutil.which("taskpolicy")
        if not policy: raise CaptionError("resource_policy_unavailable", "백그라운드 자원 정책을 적용할 수 없습니다.")
        result = subprocess.run([policy, "-b", "-p", str(os.getpid())], capture_output=True, timeout=10)
        if result.returncode: raise CaptionError("resource_policy_unavailable", "백그라운드 자원 정책 적용에 실패했습니다.")


@contextmanager
def media_slot(root=None):
    root = root or ROOT
    root.mkdir(parents=True, exist_ok=True)
    with (root / "media-worker.lock").open("a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise CaptionError("busy", "다른 영상·STT·OCR 작업이 실행 중입니다. 한 번에 한 파일만 처리합니다.")
        try:
            lower_priority()
            ACTIVE_LOCK_FDS.append(lock.fileno())
            yield
        finally:
            if lock.fileno() in ACTIVE_LOCK_FDS: ACTIVE_LOCK_FDS.remove(lock.fileno())
            fcntl.flock(lock, fcntl.LOCK_UN)


def child_env():
    return dict(os.environ, VECLIB_MAXIMUM_THREADS="2", OMP_NUM_THREADS="2", OMP_THREAD_LIMIT="2", OPENBLAS_NUM_THREADS="2")


def run_managed(args, *, input=None, text=False, capture_output=False, env=None, timeout=None, stdout=None, stderr=None):
    """A guard survives parent SIGKILL just long enough to stop the tool group."""
    if capture_output:
        if stdout is not None or stderr is not None: raise ValueError("capture_output with output streams")
        stdout, stderr = subprocess.PIPE, subprocess.PIPE
    guard = [sys.executable, str(Path(__file__).with_name("_child_guard.py")), str(os.getpid()), *map(str, args)]
    proc = subprocess.Popen(guard, stdin=subprocess.PIPE if input is not None else None,
                            stdout=stdout, stderr=stderr, text=text, env=env or child_env(),
                            pass_fds=tuple(ACTIVE_LOCK_FDS))
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        proc.terminate()
        try: proc.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.communicate()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)
