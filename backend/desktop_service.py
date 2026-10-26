"""Role-separated, authenticated localhost backend for the desktop apps."""
import argparse
import csv
import hmac
import io
import json
import os
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer

from service import Service, Handler, log
from notegpt_quota_tool.quota_scanner import AccountQuotaScanner
from notegpt_quota_tool.proxy_manager import ProxyManager
from process_lock import OperationLock
from batch_results import BatchResults


def export_txt(pool, only_ready=True, minimum=1):
    with pool._lock:
        rows=pool._conn.execute('SELECT * FROM accounts ORDER BY created_at,email').fetchall()
    output=io.StringIO(newline='')
    fields=['email','password','name','status','verify','quota_left','quota_text','cookies']
    writer=csv.DictWriter(output,fieldnames=fields,delimiter='\t',lineterminator='\n')
    writer.writeheader()
    count=0
    for row in rows:
        row=dict(row)
        if only_ready and (row['status']!='ready' or row['quota_left']<minimum):
            continue
        cookies=''
        cookie_path=Path(row.get('cookie_file') or '')
        if cookie_path.is_file():
            try:
                parsed=json.loads(cookie_path.read_text(encoding='utf-8'))
                if isinstance(parsed,list): cookies=json.dumps(parsed,ensure_ascii=False,separators=(',',':'))
            except (OSError,ValueError): pass
        item={key:row.get(key,'') for key in fields}
        item.update(verify='verified',cookies=cookies)
        writer.writerow(item)
        count+=1
    return output.getvalue(),count


class DesktopService(Service):
    def __init__(self,data_dir,role,ffmpeg=''):
        self.role=role
        super().__init__(data_dir,ffmpeg)
        if role=='voice': self.farm=None
        self.operation_lock=OperationLock(lambda:self.resources.get('proxy',{}))
        if role=='accounts':
            self.batch_results=BatchResults(data_dir)
            def heartbeat():
                while True:
                    threading.Event().wait(30)
                    try:self.batch_results.heartbeat(self.farm.state,self.farm.active_threads)
                    except OSError as exc:log('[LOG] Không ghi được log tiến trình: '+str(exc))
            threading.Thread(target=heartbeat,daemon=True).start()

    def health(self):
        return {**super().health(),'service':'notegpt-'+self.role,'role':self.role}

    def scan_quota(self):
        if self.role=='accounts': return self.farm.scan_now()
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('Đang tạo voice hoặc proxy đang được tool tài khoản sử dụng.')
        try:
            self.stop_event.clear()
            accounts=self.pool.ready_accounts()
            result=AccountQuotaScanner(self.config,log=log,should_stop=self.stop_event.is_set,
                proxy_manager=ProxyManager(self.config,log=log,should_stop=self.stop_event.is_set)).scan_accounts(accounts,max_workers=4)
            for item in result.results:
                if item.quota is not None and item.quota.success:
                    self.pool.set_quota(item.email,item.quota_left,item.quota_text)
            return {'ok':True,'scanned':len(result.results)}
        finally:self.operation_lock.release()


class DesktopHandler(Handler):
    auth_token=''
    ui_dir=Path()

    def _allowed(self,method,path):
        shared={'/health','/pool','/pool/list','/resources','/logs','/shutdown','/resources/check','/pool/scan','/pool/import'}
        if path in shared:return True
        if self.service.role=='accounts':return path in {'/farm/status','/farm/stop','/farm/run-once','/pool/export','/batch','/batch/export'}
        return path in {'/voices','/voices/import','/studio/status','/studio/start','/audio','/progress','/tts','/cancel'}

    def _authorize(self):
        if not hmac.compare_digest(self.headers.get('X-NoteGPT-Token',''),self.auth_token):
            self._send({'ok':False,'error':'Phiên ứng dụng không hợp lệ.'},401)
            return False
        return True

    def do_GET(self):
        if not self._authorize():return
        path=self.path.split('?',1)[0]
        if path in {'/','/index.html','/app.js','/style.css'}:
            file=self.ui_dir/('index.html' if path=='/' else path[1:])
            if not file.is_file():return self._send({'ok':False,'error':'Không tìm thấy giao diện'},404)
            content=file.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html','js':'text/javascript','css':'text/css'}[file.suffix[1:]]+'; charset=utf-8')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header('Content-Length',str(len(content)))
            self.end_headers();self.wfile.write(content);return
        if not self._allowed('GET',path):return self._send({'ok':False,'error':'Chức năng không thuộc tool này.'},404)
        if path=='/resources':
            keys=['proxy','mail','farm'] if self.service.role=='accounts' else ['proxy','tts']
            return self._send({'ok':True,**{key:self.service.resources[key] for key in keys}})
        if path=='/batch':return self._send({'ok':True,**self.service.batch_results.snapshot()})
        return super().do_GET()

    def do_POST(self):
        if not self._authorize():return
        if not self._allowed('POST',self.path):return self._send({'ok':False,'error':'Chức năng không thuộc tool này.'},404)
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+self.headers.get('Host',''):
            return self._send({'ok':False,'error':'Origin không hợp lệ'},403)
        try:
            if self.path=='/batch/export':
                batch=self.service.batch_results.snapshot()
                return self._send({'ok':True,'text':self.service.batch_results.text(),'filename':Path(batch['file']).name if batch['file'] else 'ket-qua.txt','count':len(batch['rows'])})
            if self.path=='/pool/export':
                body=self._body()
                content,count=export_txt(self.service.pool,bool(body.get('only_ready',True)),int(body.get('min_quota',1)))
                return self._send({'ok':True,'filename':'notegpt-accounts.txt','text':content,'count':count})
            if self.path=='/pool/scan':return self._send(self.service.scan_quota())
            if self.path=='/resources':
                body=self._body()
                allowed={'proxy','mail','farm'} if self.service.role=='accounts' else {'proxy','tts'}
                if not set(body).issubset(allowed):return self._send({'ok':False,'error':'Thiết lập không thuộc tool này.'},400)
                resources=self.service.save_resources(body)
                return self._send({'ok':True,**{key:resources[key] for key in allowed}})
            return super().do_POST()
        except (ValueError,RuntimeError) as exc:
            return self._send({'ok':False,'error':str(exc)},400)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--role',choices=['accounts','voice'],required=True)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--ui-dir',type=Path,required=True)
    parser.add_argument('--ffmpeg',default='')
    args=parser.parse_args()
    token=os.environ.get('NOTEGPT_DESKTOP_TOKEN','')
    if len(token)<32:raise RuntimeError('Thiếu token phiên desktop.')
    service=DesktopService(args.data_dir,args.role,args.ffmpeg)
    DesktopHandler.service=service
    DesktopHandler.auth_token=token
    DesktopHandler.ui_dir=args.ui_dir.resolve()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),DesktopHandler)
    DesktopHandler.server_ref=server
    print('DESKTOP_READY '+json.dumps({'port':server.server_address[1],'role':args.role}),flush=True)
    try:server.serve_forever()
    finally:
        service.cancel()
        if service.farm:
            service.farm.stop()
            if service.farm._thread:service.farm._thread.join(timeout=20)
        if service.studio._thread:service.studio._thread.join(timeout=20)
        server.server_close()
        service.pool._conn.close()

if __name__=='__main__':main()
