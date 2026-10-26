"""One durable result file per registration run, updated after each success."""
import csv
import io
import json
import threading
import time
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

FIELDS=['email','password','name','status','verify','quota_left','quota_text','cookies']

class BatchResults:
    def __init__(self,data_dir):
        self.lock=threading.RLock()
        self.directory=Path(data_dir)/'exports'
        self.directory.mkdir(exist_ok=True)
        self.journal=Path(data_dir)/'current-batch.json'
        self.logs=Path(data_dir)/'logs'
        self.logs.mkdir(exist_ok=True)
        self.batch={'id':'','requested':0,'done':0,'failed':0,'state':'idle','rows':[],'file':''}
        if self.journal.exists():
            self.batch=json.loads(self.journal.read_text(encoding='utf-8'))
            if self.batch['file']:self.batch['file']=str(self.directory/Path(self.batch['file']).name)
            if 'rows' not in self.batch:
                with Path(self.batch['file']).open(encoding='utf-8',newline='') as f:self.batch['rows']=list(csv.DictReader(f,delimiter='\t'))
            if self.batch['state']=='running':
                self.batch['state']='interrupted'
                self.save()

    def start(self,count):
        with self.lock:
            ident=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:6]
            self.batch=dict(id=ident,requested=count,done=0,failed=0,state='running',rows=[],file=str(self.directory/f'dot-{ident}.txt'),started_at=time.time(),last_progress_at=time.time())
            Path(self.batch['file']).write_text(self.text(),encoding='utf-8',newline='')
            self.save()
            self.event('start')

    def text(self):
        with self.lock:
            out=io.StringIO(newline='')
            writer=csv.DictWriter(out,fieldnames=FIELDS,delimiter='\t',lineterminator='\n')
            writer.writeheader();writer.writerows(self.batch['rows'])
            return out.getvalue()

    def resume(self):
        with self.lock:
            if not self.batch['id'] or self.batch['done']>=self.batch['requested']:
                raise ValueError('Không còn lượt đăng ký để tiếp tục.')
            self.batch['state']='running';self.batch['error']=''
            self.save();self.event('resume')

    def save(self):
        temp=self.journal.with_suffix('.tmp')
        temp.write_text(json.dumps({k:v for k,v in self.batch.items() if k!='rows'},ensure_ascii=False),encoding='utf-8');temp.replace(self.journal)

    def event(self,event,**extra):
        if not self.batch['id']:return
        now=time.time()
        record=dict(at=datetime.now().isoformat(timespec='seconds'),event=event,batch=self.batch['id'],requested=self.batch['requested'],done=self.batch['done'],success=len(self.batch['rows']),failed=self.batch['failed'],state=self.batch['state'],elapsed_s=round(now-self.batch.get('started_at',now),1),no_progress_s=round(now-self.batch.get('last_progress_at',now),1),**extra)
        with (self.logs/f"dot-{self.batch['id']}.jsonl").open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')

    def heartbeat(self,state,threads):
        with self.lock:
            if self.batch['state']=='running':self.event('heartbeat',phase=state,threads=threads)

    def completed(self,result,pool):
        with self.lock:
            self.batch['done']+=1
            if result.success:
                account=pool.get(result.email)
                self.batch['rows'].append(dict(email=result.email,password=result.password,name=result.name,
                    status=account['status'],verify='verified',quota_left=result.quota_left,
                    quota_text=result.quota_text,cookies=account['cookies']))
            else:self.batch['failed']+=1
            if result.success:
                with Path(self.batch['file']).open('a',encoding='utf-8',newline='') as f:
                    csv.DictWriter(f,fieldnames=FIELDS,delimiter='\t',lineterminator='\n').writerow(self.batch['rows'][-1]);f.flush();os.fsync(f.fileno())
            self.batch['last_progress_at']=time.time()
            self.save()
            error=str(getattr(result,'error','')).lower()
            category='rate_limit' if ('429' in error or 'too many' in error) else 'timeout' if 'timeout' in error else 'proxy' if 'proxy' in error else 'registration_error' if not result.success else ''
            self.event('account_done',ok=bool(result.success),error_category=category)

    def finish(self,stopped=False,error=''):
        with self.lock:
            if not self.batch['id']:return
            self.batch['state']='rate_limited' if self.batch.get('stop_reason')=='rate_limit' else ('stopped' if stopped else ('error' if error else 'completed'))
            self.batch['error']=self.batch.get('error','') if self.batch.get('stop_reason') else error
            self.save()
            self.event('finish')

    def stop_for_rate_limit(self):
        with self.lock:
            self.batch['stop_reason']='rate_limit'
            self.batch['error']='Dịch vụ báo giới hạn. Đã dừng toàn bộ; không tự tiếp tục hoặc đổi IP.'
            self.save()
            self.event('rate_limit_stop',action='cancel_all_workers',auto_resume=False,rotate=False)

    def snapshot(self):
        with self.lock:return json.loads(json.dumps(self.batch))
