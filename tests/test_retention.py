import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from caption_collector.cli import parser, run
from caption_collector.formats import Capture
from caption_collector.retention import finalize
from caption_collector.storage import cached, publish, verify_record


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.state=self.root/'state';self.output=self.root/'output'
        self.global_root=self.root/'global';self.models=self.global_root/'stt/models'
        self.models.mkdir(parents=True);(self.models/'medium.bin').write_bytes(b'model keep')
        self.source=self.root/'user-original.mp4';self.source.write_bytes(b'user original keep')
        self.work=self.state/'media'/'owned-work';self.work.mkdir(parents=True)
        (self.work/'stt.json').write_text('{}')
        self.job=self.global_root/'stt/jobs/job-current';self.job.mkdir(parents=True)
        self.engine=self.job/'engine.json';self.engine.write_text('{"transcription":"private"}')
        (self.job/'engine.log').write_text('private log')
        self.other=self.global_root/'stt/jobs/job-unrelated';self.other.mkdir();(self.other/'keep').write_text('other active job')
        self.download=self.state/'downloads'/'jKupp4CYJAc';self.download.mkdir(parents=True)
        (self.download/'media.webm').write_bytes(b'download delete')
        from caption_collector.formats import digest
        rows=[{'text':'원문 반복 반복','start':0,'duration':1}]
        self.capture=Capture('jKupp4CYJAc','whisper_medium',json.dumps({'segments':rows}).encode(),'json',rows,'ko',True,'정리 검증',details={'local_source':{'path':str(self.source)},'processing_work':str(self.work),'evidence_files':[{'path':str(self.engine),'sha256':digest(self.engine.read_bytes())}]})
        self.record=publish(self.capture,self.state,self.output,[])
    def tearDown(self):self.tmp.cleanup()
    def finish(self):
        with patch('caption_collector.retention.STATE',self.global_root):return finalize(self.record,self.state)

    def test_verified_job_keeps_only_md_minimal_receipt_and_reusable_models(self):
        note=Path(self.record['files']['note']['path']);before=note.read_bytes()
        done=self.finish()
        self.assertEqual(done['status'],'saved');self.assertEqual(done['retention']['status'],'complete')
        self.assertEqual(done['schema_version'],4);self.assertEqual(set(done['files']),{'note'})
        self.assertFalse(self.download.exists());self.assertFalse(self.job.exists());self.assertFalse(self.work.exists())
        self.assertEqual(list(Path(done['receipt']).parent.iterdir()),[Path(done['receipt'])])
        self.assertEqual(note.read_bytes(),before);self.assertEqual(self.source.read_bytes(),b'user original keep')
        self.assertTrue((self.models/'medium.bin').exists());self.assertTrue((self.other/'keep').exists())
        self.assertEqual([p.suffix for p in self.output.iterdir()],['.md'])
        self.assertTrue(verify_record(done)['ok'])
        again=cached(self.state,'jKupp4CYJAc','ko',self.output)
        self.assertEqual(again['status'],'already_saved')
        self.assertNotIn('원문 반복',Path(done['receipt']).read_text())
        note.write_text('changed');self.assertFalse(verify_record(done)['ok'])

    def test_edited_older_md_does_not_block_cleanup_of_new_verified_edition(self):
        first=self.finish();old_note=Path(first['files']['note']['path']);old_note.write_text('user edited note')
        self.work.mkdir();(self.work/'checkpoint').write_text('new job')
        self.job.mkdir();self.engine.write_text('{"transcription":"private"}')
        self.capture.title='새로운 검증 판본'
        second=publish(self.capture,self.state,self.output,[])
        with patch('caption_collector.retention.STATE',self.global_root):done=finalize(second,self.state)
        self.assertEqual(done['status'],'saved');self.assertEqual(done['retention']['status'],'complete')
        self.assertEqual(old_note.read_text(),'user edited note')
        self.assertFalse(self.work.exists());self.assertFalse(self.job.exists())

    def test_failed_verification_does_not_delete_payloads(self):
        Path(self.record['files']['note']['path']).write_text('changed')
        done=self.finish();self.assertEqual(done['status'],'partial')
        self.assertTrue(self.engine.exists());self.assertTrue(self.download.exists());self.assertTrue(self.work.exists())
        self.assertEqual(json.loads(Path(self.record['receipt']).read_text())['schema_version'],3)

    def test_cleanup_failure_is_not_success_and_retry_finishes_without_retranscribing(self):
        with patch('caption_collector.retention.STATE',self.global_root),patch('caption_collector.retention.shutil.rmtree',side_effect=PermissionError('fixture')):
            done=finalize(self.record,self.state)
        self.assertEqual(done['status'],'partial');self.assertEqual(done['reason'],'cleanup_pending')
        pending=cached(self.state,'jKupp4CYJAc','ko',self.output)
        self.assertEqual(pending['retention']['status'],'pending')
        with patch('caption_collector.retention.STATE',self.global_root):done=finalize(pending,self.state)
        self.assertEqual(done['status'],'already_saved');self.assertFalse(self.download.exists());self.assertFalse(self.job.exists())

    def test_symlink_work_never_deletes_external_target(self):
        import shutil
        shutil.rmtree(self.work);self.work.symlink_to(self.other,target_is_directory=True)
        done=self.finish();self.assertEqual(done['status'],'partial')
        self.assertTrue((self.other/'keep').exists());self.assertTrue(self.engine.exists())

    def test_cli_import_reuses_md_and_rechecks_reference_without_primary_collection(self):
        imported=self.root/'input.vtt';imported.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n가져온 원문\n')
        args=parser().parse_args(['https://youtu.be/zfIGHqnpPyE','--import-file',str(imported),'--language','ko','--state-dir',str(self.state),'--output-dir',str(self.output)])
        rows=[{'text':'가져온 원문','start':0,'duration':1}]
        reference=Capture('zfIGHqnpPyE','youtube_transcript_api',json.dumps({'segments':rows}).encode(),'json',rows,'ko',True)
        with patch('caption_collector.cli.isolated',return_value=reference) as api:
            first=run(args);self.assertEqual(first['retention']['status'],'complete');self.assertTrue(imported.exists())
            args.import_file=None
            with patch('caption_collector.cli.collect') as network:
                second=run(args);network.assert_not_called()
            args.resume=True
            third=run(args)
            self.assertEqual(third["caption_audit"]["status"],"passed")
            self.assertEqual(api.call_count,3)
        self.assertEqual(second['status'],'already_saved');self.assertTrue(second['verification']['ok'])
        self.assertEqual(second['caption_audit']['status'],'passed');self.assertTrue(second['temporary_data_audit']['ok'])

    def test_explicit_refresh_of_identical_capture_cleans_recreated_work_too(self):
        self.finish()
        self.work.mkdir();(self.work/'stt.json').write_text('{}')
        self.job.mkdir();self.engine.write_text('{"transcription":"private"}')
        self.download.mkdir();(self.download/'media.webm').write_bytes(b'download delete')
        refreshed=publish(self.capture,self.state,self.output,[])
        with patch('caption_collector.retention.STATE',self.global_root):done=finalize(refreshed,self.state)
        self.assertEqual(done['status'],'already_saved');self.assertTrue(done['verification']['ok'])
        self.assertFalse(self.work.exists());self.assertFalse(self.job.exists());self.assertFalse(self.download.exists())
        self.assertEqual(list(Path(done['receipt']).parent.iterdir()),[Path(done['receipt'])])
        self.assertEqual(len(list(self.output.iterdir())),1)

    def test_interrupted_refresh_metadata_is_cleaned_on_next_cached_request(self):
        done=self.finish();folder=Path(done['receipt']).parent
        (folder/'capture.json').write_text(json.dumps(self.capture.metadata(),ensure_ascii=False))
        self.work.mkdir();(self.work/'checkpoint.json').write_text('{}')
        with patch('caption_collector.retention.STATE',self.global_root):recovered=finalize(done,self.state)
        self.assertEqual(recovered['status'],'saved');self.assertTrue(recovered['verification']['ok'])
        self.assertFalse(self.work.exists());self.assertEqual(list(folder.iterdir()),[Path(done['receipt'])])

    def test_tampered_pending_cleanup_cannot_target_model_folder(self):
        from caption_collector.retention import prepare,erase_pending
        with patch('caption_collector.retention.STATE',self.global_root):
            pending=prepare(self.record,self.state)
            pending['retention']['targets']=[{'path':str(self.models/'medium.bin'),'root':str(self.models)}]
            from caption_collector.formats import CaptionError
            with self.assertRaises(CaptionError):erase_pending(pending)
        self.assertEqual((self.models/'medium.bin').read_bytes(),b'model keep')


if __name__=='__main__':unittest.main()
