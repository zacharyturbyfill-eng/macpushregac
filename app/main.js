const {app,BrowserWindow,Menu,Tray,nativeImage,session,dialog}=require('electron');
const path=require('node:path');
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const crypto=require('node:crypto');
const spec=require('./app-config.json');
const isMac=process.platform==='darwin';
// Windows bản portable: mọi thứ cạnh .exe. Mac: lõi/trình duyệt nằm trong .app (Contents/Resources),
// dữ liệu ra ~/Library/Application Support/<tên app> vì bên trong .app không ghi được.
const root=isMac?process.resourcesPath:path.dirname(process.execPath);
const stateRoot=isMac?path.join(app.getPath('appData'),spec.title):root;
const dataDir=path.join(stateRoot,'data');
fs.mkdirSync(dataDir,{recursive:true});
app.setName(spec.title);
app.setPath('userData',path.join(stateRoot,'desktop-state'));
const ownsLock=app.requestSingleInstanceLock();
let win,tray,backend,origin='',quitting=false,finished=false;
const token=crypto.randomBytes(32).toString('hex');
const log=fs.createWriteStream(path.join(dataDir,'desktop.log'),{flags:'a'});
function show(){if(win){win.show();win.focus();}}
app.on('second-instance',show);

async function shutdown(){
  if(quitting)return;
  quitting=true;
  if(origin){try{await fetch(origin+'/shutdown',{method:'POST',headers:{'X-NoteGPT-Token':token,'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(3000)});}catch{}}
  if(backend&&backend.exitCode===null){
    await Promise.race([new Promise(resolve=>backend.once('exit',resolve)),new Promise(resolve=>setTimeout(resolve,25000))]);
    if(backend.exitCode===null){
      if(isMac){
        // Lõi chạy detached nên có nhóm tiến trình riêng: giết cả nhóm để Chromium con cũng tắt.
        try{process.kill(-backend.pid,'SIGKILL');}catch{try{backend.kill('SIGKILL');}catch{}}
      }else{
        const kill=spawn('taskkill',['/PID',String(backend.pid),'/T','/F'],{windowsHide:true});
        await new Promise(resolve=>{kill.once('exit',resolve);kill.once('error',resolve);});
      }
    }
  }
  finished=true;log.end();app.quit();
}

async function startBackend(){
  const ext=isMac?'':'.exe';
  const executable=path.join(root,'backend','NoteGPTBackend'+ext);
  backend=spawn(executable,['--role',spec.role,'--port','0','--data-dir',dataDir,
    '--ui-dir',path.join(app.getAppPath(),'ui'),'--ffmpeg',path.join(root,'tools','ffmpeg'+ext)],
    {cwd:stateRoot,windowsHide:true,detached:isMac,env:{...process.env,NOTEGPT_DESKTOP_TOKEN:token,
      PLAYWRIGHT_BROWSERS_PATH:path.join(root,'browsers'),PYTHONUTF8:'1'}});
  backend.stderr.pipe(log,{end:false});
  await new Promise((resolve,reject)=>{
    let buffer='';
    const timer=setTimeout(()=>reject(Error('Khởi động lõi xử lý quá thời gian. Xem data/desktop.log.')),45000);
    backend.once('error',error=>{clearTimeout(timer);reject(error);});
    backend.once('exit',code=>{clearTimeout(timer);reject(Error('Lõi xử lý dừng, mã '+code));});
    backend.stdout.on('data',chunk=>{
      log.write(chunk);buffer+=chunk.toString('utf8');
      const lines=buffer.split(/\r?\n/);buffer=lines.pop();
      for(const line of lines){if(line.startsWith('DESKTOP_READY ')){
        const ready=JSON.parse(line.slice(14));
        if(ready.role!==spec.role){clearTimeout(timer);reject(Error('Sai vai trò ứng dụng'));return;}
        origin='http://127.0.0.1:'+ready.port;clearTimeout(timer);resolve();
      }}
    });
  });
  backend.on('exit',()=>{if(!quitting){dialog.showErrorBox(spec.title,'Lõi xử lý đã dừng. Mở lại ứng dụng để tiếp tục.');shutdown();}});
}

async function launch(){
  await startBackend();
  const browserSession=session.fromPartition('persist:'+spec.role);
  browserSession.setPermissionRequestHandler((contents,permission,callback)=>callback(permission==='clipboard-sanitized-write' && contents.getURL().startsWith(origin+'/')));
  browserSession.webRequest.onBeforeSendHeaders({urls:[origin+'/*']},(details,callback)=>{
    callback({requestHeaders:{...details.requestHeaders,'X-NoteGPT-Token':token}});
  });
  browserSession.on('will-download',(_event,item)=>{
    item.setSaveDialogOptions({defaultPath:path.join(app.getPath('downloads'),item.getFilename())});
  });
  win=new BrowserWindow({width:1180,height:870,minWidth:820,minHeight:620,
    title:spec.title,backgroundColor:'#0d1218',icon:path.join(app.getAppPath(),'icon.png'),
    webPreferences:{session:browserSession,nodeIntegration:false,contextIsolation:true,sandbox:true}});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
  win.webContents.on('will-navigate',(event,url)=>{if(!url.startsWith(origin+'/'))event.preventDefault();});
  win.webContents.on('console-message',(_event,details)=>{
    // Keep renderer errors useful without logging API response bodies or credentials.
    if(details&&details.level==='error')log.write('Renderer error: '+details.message+'\n');
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate([{label:'Ứng dụng',submenu:[
    {label:'Hiện cửa sổ',click:show},{label:'Thu nhỏ',click:()=>win.minimize()},
    {type:'separator'},{label:'Thoát và dừng tác vụ',click:shutdown}]},
    {label:'Soạn thảo',submenu:[{role:'undo'},{role:'redo'},{type:'separator'},{role:'cut'},{role:'copy'},{role:'paste'},{role:'selectAll'}]}]));
  if(spec.role==='accounts'){
    tray=new Tray(nativeImage.createFromPath(path.join(app.getAppPath(),'icon.png')).resize({width:24,height:24}));
    tray.setToolTip(spec.title+' — đăng ký tài khoản');
    tray.setContextMenu(Menu.buildFromTemplate([{label:'Mở '+spec.title,click:show},
      {type:'separator'},{label:'Thoát và dừng đăng ký',click:shutdown}]));
    tray.on('double-click',show);
    win.on('close',event=>{if(!quitting){event.preventDefault();win.hide();}});
  }else win.on('close',event=>{if(!quitting){event.preventDefault();shutdown();}});
  await win.loadURL(origin+'/');
}

app.on('activate',show);
app.on('before-quit',event=>{if(!finished){event.preventDefault();shutdown();}});
app.on('window-all-closed',()=>{if(spec.role!=='accounts')shutdown();});
if(!ownsLock){finished=true;app.quit();}
else app.whenReady().then(launch).catch(error=>{dialog.showErrorBox(spec.title,String(error.message||error));shutdown();});
