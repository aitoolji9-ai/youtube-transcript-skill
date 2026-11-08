import errno,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from caption_collector.formats import CaptionError,digest
from caption_collector.resources import run_managed
from caption_collector.storage import create_file
from caption_collector.media import transcribe


class ResilienceTests(unittest.TestCase):
    def test_failed_fsync_leaves_no_final_filename_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'result.json'
            with patch('caption_collector.storage.os.fsync',side_effect=OSError(errno.ENOSPC,'full')):
                with self.assertRaises(OSError):create_file(target,b'complete')
            self.assertFalse(target.exists());self.assertEqual(list(root.iterdir()),[])
            create_file(target,b'complete');self.assertEqual(target.read_bytes(),b'complete')

    def test_existing_verified_file_can_be_reused_without_temporary_disk_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'source.json';target.write_bytes(b'complete')
            with patch('caption_collector.storage.tempfile.mkstemp',side_effect=OSError(errno.ENOSPC,'full')):
                create_file(target,b'complete')
            self.assertEqual(target.read_bytes(),b'complete')

    def test_conflicting_new_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'original';target.write_bytes(b'original')
            with patch('caption_collector.storage.os.fsync',side_effect=OSError(errno.ENOSPC,'full')):
                with self.assertRaises(CaptionError):create_file(target,b'new')
            self.assertEqual(target.read_bytes(),b'original')

    def test_managed_tool_preserves_stdout_stderr_and_exit_code(self):
        result=run_managed([sys.executable,'-c','import sys;print("가나다");print("diagnostic",file=sys.stderr);sys.exit(3)'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,3);self.assertEqual(result.stdout,'가나다\n');self.assertEqual(result.stderr,'diagnostic\n')

    def test_managed_tool_stops_descendants_even_after_leader_exits(self):
        from process_helpers import wait_dead,alive
        with tempfile.TemporaryDirectory() as tmp:
            pidfile=Path(tmp)/'pid'
            child='import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('+repr(str(pidfile))+').write_text(str(os.getpid()));time.sleep(120)'
            leader='import subprocess,sys,time;from pathlib import Path;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);p=Path('+repr(str(pidfile))+')\nwhile not p.exists():time.sleep(.01)'
            result=run_managed([sys.executable,'-c',leader],capture_output=True,text=True,timeout=8)
            self.assertEqual(result.returncode,0)
            pid=int(pidfile.read_text())
            try:wait_dead(pid)
            finally:
                if alive(pid):
                    import signal
                    os.kill(pid,signal.SIGKILL)

    def test_guard_keeps_worker_lock_until_orphaned_tool_is_stopped(self):
        import fcntl,signal,subprocess,time
        from process_helpers import wait_file,wait_dead,alive
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pidfile=root/'pid';lockroot=root/'lock'
            tool='import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path('+repr(str(pidfile))+').write_text(str(os.getpid()));time.sleep(120)'
            code='import sys;from pathlib import Path;from caption_collector.resources import media_slot;from caption_collector.media import command\nwith media_slot(Path('+repr(str(lockroot))+')):command([sys.executable,"-c",'+repr(tool)+'])'
            parent=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            child=None
            try:
                wait_file(pidfile);child=int(pidfile.read_text());os.kill(parent.pid,signal.SIGKILL);parent.wait()
                with (lockroot/'media-worker.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    wait_dead(child)
                    for _ in range(40):
                        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                        except BlockingIOError:time.sleep(.05)
                    else:self.fail('worker lock not released')
                    fcntl.flock(lock,fcntl.LOCK_UN)
            finally:
                if parent.poll() is None:parent.kill();parent.wait()
                if child and alive(child):os.kill(child,signal.SIGKILL)

    def test_interrupted_stt_job_is_removed_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.wav';source.write_bytes(b'source')
            state=root/'state';(state/'stt/jobs').mkdir(parents=True)
            (state/'stt/config.json').write_text(json.dumps({'sha256':'model'}))
            work=root/'work';work.mkdir();old=state/'stt/jobs/job-interrupted';old.mkdir();(old/'audio.wav').write_bytes(b'old audio')
            (work/'stt-pending.json').write_text(json.dumps({'job':str(old)}))
            class Engine:
                def transcribe(self,path,language,output_note,on_job_created):
                    assert not old.exists()
                    job=state/'stt/jobs/job-new';job.mkdir();on_job_created(job)
                    raw=job/'engine.json';raw.write_text(json.dumps({'transcription':[{'text':'전사','offsets':{'from':0,'to':1000}}],'result':{'language':'ko'}}))
                    return {'status':'transcribed','source_sha256':digest(source.read_bytes()),'source_files':{'engine_json':{'path':str(raw),'sha256':digest(raw.read_bytes())}},'elapsed_seconds':1,'peak_child_rss_bytes':1024}
            with patch('caption_collector.media.STATE',state),patch('caption_collector.media.stt_rows',return_value=Engine()):
                rows,saved=transcribe(source,'ko',work)
            self.assertEqual(rows,[{'text':'전사','start':0,'duration':1}]);self.assertFalse((work/'stt-pending.json').exists())
            self.assertTrue(Path(saved['engine_json']).exists())

    def test_interrupted_stt_foreign_directory_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';(state/'stt').mkdir(parents=True)
            (state/'stt/config.json').write_text(json.dumps({'sha256':'model'}))
            source=root/'source';source.write_bytes(b'source');foreign=root/'important';foreign.mkdir();(foreign/'keep').write_text('keep')
            work=root/'work';work.mkdir();(work/'stt-pending.json').write_text(json.dumps({'job':str(foreign)}))
            with patch('caption_collector.media.STATE',state):
                with self.assertRaises(CaptionError):transcribe(source,'ko',work)
            self.assertTrue((foreign/'keep').exists())


if __name__=='__main__':unittest.main()
