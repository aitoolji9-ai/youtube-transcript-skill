import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from caption_collector.completion import complete
from caption_collector.formats import Capture,CaptionError
from caption_collector.storage import publish,cached,verify_record

VID='jKupp4CYJAc'


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.state=self.root/'state';self.output=self.root/'output'
        self.rows=[{'text':'  원문 그대로\n반복  ','start':.125,'duration':1.25},{'text':'마지막 구간','start':2,'duration':.5}]
    def tearDown(self):self.tmp.cleanup()
    def capture(self,provider='yt_dlp',rows=None,vid=VID):
        rows=rows or self.rows
        return Capture(vid,provider,json.dumps({'segments':rows},ensure_ascii=False).encode(),'json',rows,'ko',True,'API 대조 시험')
    def record(self,provider='yt_dlp',attempts=None,vid=VID):return publish(self.capture(provider,vid=vid),self.state,self.output,attempts or [])
    def reference(self,provider='youtube_transcript_api',rows=None):return self.capture(provider,rows)

    def test_other_api_compares_every_character_and_timestamp_then_confirms_cleanup(self):
        for provider in ['yt_dlp','yt_dlp_authenticated']:
            runner=Mock(return_value=self.reference());r=complete(self.record(provider),self.state,runner)
            runner.assert_called_once_with('youtube_transcript_api',VID,'ko')
            self.assertTrue(r['completion']['ok']);self.assertEqual(r['caption_audit']['status'],'passed')
            self.assertTrue(r['temporary_data_audit']['ok']);self.assertEqual(set(r['files']),{'note'})
            self.assertEqual([p.name for p in Path(r['receipt']).parent.iterdir()],['receipt.json'])
            self.assertNotIn('마지막 구간',Path(r['receipt']).read_text())

    def test_primary_api_is_checked_using_ytdlp(self):
        runner=Mock(return_value=self.reference('yt_dlp'));r=complete(self.record('youtube_transcript_api'),self.state,runner)
        self.assertTrue(r['completion']['ok']);runner.assert_called_once_with('yt_dlp',VID,'ko')

    def test_cache_reuse_refetches_and_detects_a_missing_segment(self):
        runner=Mock(return_value=self.reference());first=complete(self.record(),self.state,runner)
        saved=Path(first['files']['note']['path']).read_bytes()
        runner.return_value=self.reference(rows=self.rows[:1])
        second=complete(cached(self.state,VID,'ko',self.output),self.state,runner)
        self.assertEqual(runner.call_count,2);self.assertEqual(second['status'],'partial')
        self.assertEqual(second['reason'],'caption_audit_mismatch');self.assertFalse(second['completion']['ok'])
        self.assertEqual(Path(second['files']['note']['path']).read_bytes(),saved);self.assertTrue(second['temporary_data_audit']['ok'])

    def test_time_or_whitespace_change_is_not_normalized_away(self):
        changed=[dict(x) for x in self.rows];changed[0]['text']=changed[0]['text'].strip()
        r=complete(self.record(),self.state,Mock(return_value=self.reference(rows=changed)))
        self.assertEqual(r['caption_audit']['status'],'mismatch')

    def test_access_restriction_does_not_become_verified(self):
        r=complete(self.record(),self.state,Mock(side_effect=CaptionError('request_blocked','blocked')))
        self.assertEqual(r['status'],'partial');self.assertEqual(r['caption_audit']['reason'],'request_blocked')
        self.assertTrue(r['temporary_data_audit']['ok']);self.assertFalse(r['completion']['ok'])

    def test_rate_limit_cooldown_prevents_repeated_reference_requests(self):
        runner=Mock(side_effect=CaptionError('rate_limited','limited',retry_after_seconds=60))
        first=complete(self.record(),self.state,runner)
        second=complete(cached(self.state,VID,'ko',self.output),self.state,runner)
        self.assertEqual(runner.call_count,1);self.assertEqual(second['caption_audit']['reason'],'rate_limited_cooldown')
        self.assertEqual(first['status'],'partial')

    def test_local_generated_output_is_not_labelled_api_verified(self):
        runner=Mock();r=complete(self.record('whisper_medium',vid='local-example'),self.state,runner)
        runner.assert_not_called();self.assertEqual(r['caption_audit']['status'],'not_applicable')
        self.assertTrue(r['completion']['ok']);self.assertTrue(r['temporary_data_audit']['ok'])

    def test_track_mismatch_is_unverified(self):
        for wrong in ['language','provider']:
            reference=self.reference()
            if wrong=='language':reference.language='en'
            else:reference.provider='yt_dlp'
            r=complete(self.record(),self.state,Mock(return_value=reference))
            expected='reference_track_mismatch' if wrong=='language' else 'reference_provider_mismatch'
            self.assertEqual(r['caption_audit']['reason'],expected);self.assertEqual(r['status'],'partial')

    def test_a_failed_reference_route_is_not_retried_in_the_same_request(self):
        runner=Mock();r=complete(self.record(attempts=[{'provider':'youtube_transcript_api','outcome':'request_blocked'}]),self.state,runner)
        runner.assert_not_called();self.assertEqual(r['caption_audit']['reason'],'reference_route_already_failed')

    def test_cleanup_postcheck_catches_a_false_complete_flag(self):
        from caption_collector.retention import finalize
        record=finalize(self.record(),self.state);(Path(record['receipt']).parent/'leftover.log').write_text('temporary')
        with patch('caption_collector.completion.finalize',return_value=record):
            r=complete(record,self.state,Mock(return_value=self.reference()))
        self.assertEqual(r['reason'],'cleanup_pending');self.assertFalse(r['completion']['ok'])

    def test_md_change_during_reference_fetch_is_reported_as_integrity_failure(self):
        record=self.record();note=Path(record['files']['note']['path'])
        def reference(*args):
            note.write_text('user edited header\n'+note.read_text())
            return self.reference()
        result=complete(record,self.state,reference)
        self.assertEqual(result['status'],'partial');self.assertEqual(result['reason'],'verification_failed')
        self.assertFalse(result['completion']['ok']);self.assertTrue(note.read_text().startswith('user edited header'))

    def test_managed_scratch_path_is_rechecked_and_removed_on_reuse(self):
        capture=self.capture();work=self.state/'media/work';work.mkdir(parents=True);(work/'chunk').write_text('temporary');capture.details['processing_work']=str(work)
        first=complete(publish(capture,self.state,self.output,[]),self.state,Mock(return_value=self.reference()))
        self.assertEqual(first['temporary_data_audit']['managed_paths_checked'],1)
        work.mkdir();(work/'late.log').write_text('temporary')
        second=complete(cached(self.state,VID,'ko',self.output),self.state,Mock(return_value=self.reference()))
        self.assertTrue(second['completion']['ok']);self.assertFalse(work.exists())


if __name__=='__main__':unittest.main()
