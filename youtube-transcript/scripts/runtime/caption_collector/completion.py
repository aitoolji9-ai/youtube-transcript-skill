"""Every extraction checks alternate captions and confirms payload cleanup."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from .formats import CaptionError,digest,timed_text
from .providers import classify
from .retention import finalize
from .storage import atomic,json_bytes,now,verify_record

MEDIA_PROVIDERS={'whisper_medium','screen_ocr','vision_ocr','embedded_subtitle'}


def caption_body(record):
    text=Path(record['files']['note']['path']).read_text()
    match=re.search(r'(?m)^(`{3,})text\n([\s\S]*?)\n\1(?:\n|$)',text)
    if not match:raise CaptionError('invalid_saved_transcript','저장된 시간별 대본 블록이 없습니다.')
    return match.group(2)


def alternate_captions(record,state,runner,attempts):
    base={'checked_at':now(),'note_sha256':record['files']['note']['sha256'],
          'scope':'all_provider_caption_text_and_timestamps; not audio speech accuracy'}
    if record['video_id'].startswith('local-') or record['provider'] in MEDIA_PROVIDERS or record['provider'] not in {'yt_dlp','yt_dlp_authenticated','youtube_transcript_api','browser_export','provided_file'}:
        return {**base,'status':'not_applicable','reason':'source_not_youtube_caption_copy','source_file_alignment':record.get('source_verification',{}).get('ok',False)}
    provider='yt_dlp' if record['provider']=='youtube_transcript_api' else 'youtube_transcript_api'
    base['provider']=provider
    cooldown=state/'youtube-cooldown.json'
    if cooldown.exists() and json.loads(cooldown.read_bytes())['retry_not_before']>time.time():
        return {**base,'status':'pending','reason':'rate_limited_cooldown'}
    failed=[a for a in attempts if a.get('provider')==provider and a.get('outcome')!='captured']
    if failed:
        return {**base,'status':'pending','reason':'reference_route_already_failed','route_error':failed[-1]['outcome']}
    try:
        reference=runner(provider,record['video_id'],record.get('language'))
        if reference.provider!=provider:
            raise CaptionError('reference_provider_mismatch','다른 수집 경로의 자막이 아닙니다.')
        if reference.video_id!=record['video_id'] or reference.language!=record.get('language'):
            raise CaptionError('reference_track_mismatch','대조 자막의 영상·언어가 다릅니다.')
        if record.get('is_generated') is not None and reference.is_generated!=record['is_generated']:
            raise CaptionError('reference_track_mismatch','수동·자동 자막 종류가 다릅니다.')
        saved=caption_body(record);expected=timed_text(reference.segments)
        saved_count=len(re.findall(r'^\[\d+:\d\d:\d\d\.\d{3}\]$',saved,re.M))
        equal=saved==expected and saved_count==record['segment_count']==len(reference.segments)
        return {**base,'status':'passed' if equal else 'mismatch','all_text_and_timestamps_match':equal,
                'saved_segment_count':saved_count,'reference_segment_count':len(reference.segments),
                'saved_body_sha256':digest(saved.encode()),'reference_body_sha256':digest(expected.encode()),
                'reference_raw_sha256':digest(reference.raw),'reference_stored_on_disk':False}
    except Exception as exc:
        error=classify(exc)
        if error.kind=='rate_limited':
            previous=json.loads(cooldown.read_bytes()) if cooldown.exists() else {}
            atomic(cooldown,json_bytes({'retry_not_before':max(previous.get('retry_not_before',0),time.time()+error.details.get('retry_after_seconds',60)),
                                       'retry_after_basis':error.details.get('retry_after_basis','local_default')}))
        return {**base,'status':'pending','reason':error.kind}


def cleanup_check(record,state):
    folder=Path(record['receipt']).parent
    capture_left=[p for p in folder.iterdir() if p.name!='receipt.json']
    downloads=state/'downloads'/record['video_id']
    download_files=[p for p in downloads.rglob('*') if p.is_file() or p.is_symlink()]
    request=state/'requests'/(record['video_id']+'.json')
    managed=record.get('retention',{}).get('managed_targets',[])
    managed_left=[t for t in managed if Path(t['path']).exists() or Path(t['path']).is_symlink()]
    ok=record.get('retention',{}).get('status')=='complete' and not capture_left and not download_files and not request.exists() and not managed_left
    return {'ok':ok,'checked_at':now(),'capture_payload_files':len(capture_left),'download_files':len(download_files),
            'request_payload_files':int(request.exists()),'managed_paths_remaining':len(managed_left),
            'managed_paths_checked':len(managed),'reference_stored_on_disk':False}


def complete(record,state,runner):
    if record.get('status') not in {'saved','already_saved'}:return record
    attempts=record.get('attempts',[])
    # The original-to-MD comparison is already verified by publish. The alternate
    # reference is then fetched only into memory; no new media payload is created.
    result=finalize(record,state)
    if result.get('status') not in {'saved','already_saved'}:return result
    audit=alternate_captions(result,state,runner,attempts)
    result.pop('caption_completeness_audit',None)
    result['caption_audit']=audit
    result['temporary_data_audit']=cleanup_check(result,state)
    result['verification']=verify_record(result)
    ok=audit['status'] in {'passed','not_applicable'} and result['temporary_data_audit']['ok'] and result['verification']['ok']
    result['completion']={'ok':ok,'caption_audit':audit['status'],'cleanup_confirmed':result['temporary_data_audit']['ok']}
    atomic(Path(result['receipt']),json_bytes(result))
    if not ok:
        reason='verification_failed' if not result['verification']['ok'] else 'cleanup_pending' if not result['temporary_data_audit']['ok'] else 'caption_audit_'+audit['status']
        return {**result,'status':'partial','reason':reason,
                'message':'MD는 저장했습니다. 별도 자막 대조 또는 임시 자료 정리 확인이 끝나지 않아 전체 완료로 처리하지 않았습니다.'}
    return result
