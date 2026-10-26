"""Coordinate two desktop apps sharing one Kiot key, without sharing account DBs."""
import ctypes
import hashlib
import os
import threading
import time

class OperationLock:
    def __init__(self,proxy_settings):
        self.proxy_settings=proxy_settings
        self.local=threading.RLock()
        self.depth=threading.local()
        if os.name=='nt':
            self.api=ctypes.WinDLL('kernel32',use_last_error=True)
            self.api.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
            self.api.CreateMutexW.restype=ctypes.c_void_p
            self.api.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
            self.api.ReleaseMutex.argtypes=[ctypes.c_void_p]
            self.api.CloseHandle.argtypes=[ctypes.c_void_p]

    def acquire(self,blocking=True,timeout=-1):
        started=time.monotonic()
        acquired=self.local.acquire(blocking,timeout) if blocking else self.local.acquire(False)
        if not acquired:return False
        depth=getattr(self.depth,'count',0)
        if depth:
            self.depth.count+=1;return True
        handle=None
        try:
            settings=self.proxy_settings()
            if os.name=='nt' and settings.get('provider')=='kiot' and settings.get('kiot_key'):
                name='Local\\NoteGPT-Kiot-'+hashlib.sha256(settings['kiot_key'].encode()).hexdigest()
                handle=self.api.CreateMutexW(None,False,name)
                if not handle:raise ctypes.WinError(ctypes.get_last_error())
                milliseconds=0 if not blocking else (0xFFFFFFFF if timeout<0 else max(0,int((timeout-(time.monotonic()-started))*1000)))
                result=self.api.WaitForSingleObject(handle,milliseconds)
                if result==258:
                    self.api.CloseHandle(handle);self.local.release();return False
                if result not in (0,128):raise ctypes.WinError(ctypes.get_last_error())
            self.depth.handle=handle;self.depth.count=1
            return True
        except BaseException:
            if handle:self.api.CloseHandle(handle)
            self.local.release();raise

    def release(self):
        count=getattr(self.depth,'count',0)
        if not count:raise RuntimeError('Lock is not held by this thread')
        self.depth.count=count-1
        if count==1 and self.depth.handle:
            self.api.ReleaseMutex(self.depth.handle)
            self.api.CloseHandle(self.depth.handle)
            self.depth.handle=None
        self.local.release()

    def __enter__(self):self.acquire();return self
    def __exit__(self,*args):self.release()
