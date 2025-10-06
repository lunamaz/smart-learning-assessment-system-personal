// static/script.js
(() => {
// ========= 小朋友學習系統：前端整合腳本（鏡頭 + AI 偵測 + 學習控制 + 帳號） =========
// 本檔以 IIFE 包裹，避免任何全域變數與其他檔案撞名。

// ---------- 共同狀態 ----------
let video, canvas, ctx;
let faceDetectionModel = null;   // MediaPipe Face Detection
let emotionModel = null;         // TensorFlow.js 情緒模型
let lastFaceDetectionResult = null;

let isDetecting = false;
let isPaused = false;
let detectionInterval = null;

let cameraReady = false;
let cameraError = false;

let currentSessionId = null;

// 右側「已學習 mm:ss」— 改名避免撞名
let elapsedTicker = null;
let elapsedStartAt = 0;

// 統計資料
let emotionData = [];       // { timestamp, emotion, attention, confidence }
let detectionCount = 0;
let validDetections = 0;
let noFaceWarningCount = 0;
let multipleFaceWarningCount = 0;

// 八類情緒（含 no_emotion）
const EMOTION_LABELS = ['anger','disgust','fear','happy','neutral','sad','surprise','no_emotion'];
const EMOTION_LABELS_ZH = {
  anger:'生氣', disgust:'厭惡', fear:'恐懼', happy:'開心',
  neutral:'中性', sad:'難過', surprise:'驚訝', no_emotion:'無情緒'
};
const EMOTION_ICONS = {
  anger:{icon:'fas fa-angry',color:'#E74C3C'},
  disgust:{icon:'fas fa-grimace',color:'#8E44AD'},
  fear:{icon:'fas fa-dizzy',color:'#9B59B6'},
  happy:{icon:'fas fa-smile',color:'#F39C12'},
  neutral:{icon:'fas fa-meh',color:'#2ECC71'},
  sad:{icon:'fas fa-sad-tear',color:'#3498DB'},
  surprise:{icon:'fas fa-surprise',color:'#E74C3C'},
  no_emotion:{icon:'fas fa-question',color:'#95A5A6'}
};
let currentEmotionCounts = { anger:0, disgust:0, fear:0, happy:0, neutral:0, sad:0, surprise:0, no_emotion:0 };

// ---------- 入口 ----------
document.addEventListener('DOMContentLoaded', () => {
  const p = window.location.pathname;
  if (p.includes('/register')) initRegisterPage();
  if (p.includes('/login')) initLoginPage();
  if (p.includes('/study/')) initStudyPage();
});

// ---------- 小工具 ----------
const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));
function mmss(sec){ const m=(sec/60|0).toString().padStart(2,'0'); const s=(sec%60|0).toString().padStart(2,'0'); return `${m}:${s}`; }
function startElapsedClock(){
  stopElapsedClock();
  elapsedStartAt = Date.now();
  const lab = document.getElementById('elapsedText');
  if (lab) lab.textContent = '00:00';
  elapsedTicker = setInterval(()=>{ if(lab) lab.textContent = mmss(((Date.now()-elapsedStartAt)/1000)|0); }, 1000);
}
function stopElapsedClock(){ if(elapsedTicker){ clearInterval(elapsedTicker); elapsedTicker=null; } }

function showMessage(msg,type='error'){
  const modal=document.getElementById('messageModal');
  const title=document.getElementById('modalTitle');
  const body=document.getElementById('modalMessage');
  if(modal && body){
    body.textContent = msg;
    if(title){
      title.textContent = (type==='success')?'成功':(type==='error')?'錯誤':'訊息';
      title.className = 'modal-title ' + (type==='success'?'text-success':type==='error'?'text-danger':'');
    }
    new bootstrap.Modal(modal).show();
  }else{ alert(msg); }
}

// ---------- 註冊 / 登入 ----------
function initRegisterPage(){
  const form=document.getElementById('registerForm'); if(!form) return;
  form.addEventListener('submit', async e=>{
    e.preventDefault();
    const username=document.getElementById('username').value;
    const email=document.getElementById('email').value;
    const password=document.getElementById('password').value;
    const confirm=document.getElementById('confirmPassword').value;
    const age=parseInt(document.getElementById('age').value,10);
    if(isNaN(age)||age<6||age>18){ showMessage('年齡必須在6-18歲之間'); return; }
    if(password!==confirm){ showMessage('密碼確認不一致'); return; }

    try{
      const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,email,password})});
      const j=await r.json();
      if(j.success){ showMessage('註冊成功！即將跳轉到登入頁面...','success'); setTimeout(()=>location.href='/login',1500); }
      else showMessage(j.message||'註冊失敗');
    }catch{ showMessage('註冊失敗，請稍後再試'); }
  });

  const age=document.getElementById('age');
  if(age){
    age.addEventListener('blur',function(){ const v=parseInt(this.value,10); this.setCustomValidity(isNaN(v)||v<6||v>18?'年齡必須在6-18歲之間':''); });
    age.addEventListener('keypress',e=>{ if(e.which!==8 && e.which!==0 && (e.which<48 || e.which>57)) e.preventDefault(); });
  }
}

function initLoginPage(){
  const form=document.getElementById('loginForm'); if(!form) return;
  form.addEventListener('submit', async e=>{
    e.preventDefault();
    const username=document.getElementById('username').value;
    const password=document.getElementById('password').value;
    const btn=document.getElementById('loginBtn'); if(btn){ btn.disabled=true; btn.innerHTML='<i class="fas fa-spinner fa-spin me-2"></i>登入中...';}
    try{
      const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const j=await r.json();
      if(j.success){ showMessage('登入成功！即將跳轉...','success'); setTimeout(()=>location.href='/child_selection',1200); }
      else{ showMessage(j.message||'登入失敗'); if(btn){ btn.disabled=false; btn.innerHTML='<i class="fas fa-sign-in-alt me-2"></i>登入'; } }
    }catch{ showMessage('登入失敗，請稍後再試'); if(btn){ btn.disabled=false; btn.innerHTML='<i class="fas fa-sign-in-alt me-2"></i>登入'; } }
  });
}

// ---------- 學習頁 ----------
async function initStudyPage(){
  // 只在學習頁載入外部庫
  addScript('https://cdn.jsdelivr.net/npm/chart.js');
  addScript('https://cdn.jsdelivr.net/npm/@mediapipe/face_detection/face_detection.js');
  addScript('https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@latest/dist/tf.min.js');

  await initCamera();
  bindStudyControls();
  await loadModels();       // 等外部庫可用才載
  initEmotionLights();
  updateStartButtonState();
}

function addScript(src){ const s=document.createElement('script'); s.src=src; document.head.appendChild(s); }

// 相機
async function initCamera(){
  try{
    video=document.getElementById('video'); canvas=document.getElementById('canvas');
    if(!video||!canvas) return;
    ctx=canvas.getContext('2d');

    updateCameraStatus('正在啟動攝影機...','info');
    const stream=await navigator.mediaDevices.getUserMedia({ video:{width:640,height:480,facingMode:'user'} });
    video.srcObject=stream;
    video.addEventListener('loadedmetadata',()=>{ cameraReady=true; cameraError=false; updateCameraStatus('攝影機已就緒','success'); updateStartButtonState(); });
    video.addEventListener('error',()=>{ cameraReady=false; cameraError=true; updateCameraStatus('攝影機載入失敗','error'); updateStartButtonState(); });
  }catch(err){
    cameraReady=false; cameraError=true;
    let msg='無法存取攝影機';
    if(err.name==='NotAllowedError') msg='請允許瀏覽器存取攝影機權限';
    else if(err.name==='NotFoundError') msg='未找到可用的攝影機設備';
    else if(err.name==='NotReadableError') msg='攝影機正被其他應用程式使用';
    updateCameraStatus(msg,'error'); updateStartButtonState();
  }
}
function updateCameraStatus(message,type='info'){
  const el=document.getElementById('cameraStatus'); if(!el) return;
  el.className=`alert alert-${type==='error'?'danger':type==='success'?'success':'info'}`;
  el.innerHTML=`<i class="fas fa-${type==='error'?'times-circle':type==='success'?'check-circle':'info-circle'} me-2"></i>${message}`;
}
function updateStartButtonState(){
  const btn=document.getElementById('startButton'); if(!btn) return;
  if(cameraReady && !cameraError && faceDetectionModel && emotionModel){
    btn.disabled=false; btn.className='btn btn-success btn-sm'; btn.innerHTML='<i class="fas fa-play me-1"></i>開始';
  }else{
    btn.disabled=true; btn.className='btn btn-warning btn-sm';
    btn.innerHTML=!cameraReady||cameraError?'<i class="fas fa-exclamation-triangle me-1"></i>請先開啟攝影機':'<i class="fas fa-hourglass-half me-1"></i>AI 模型載入中...';
  }
}

// 等外部庫載入 → 載模型
async function loadModels(){
  // 等待外部庫可用
  let tries=0;
  while((typeof FaceDetection==='undefined' || typeof tf==='undefined') && tries<80){ await sleep(125); tries++; }
  try{
    if(typeof FaceDetection==='undefined') throw new Error('MediaPipe Face Detection 未載入');
    if(typeof tf==='undefined') throw new Error('TensorFlow.js 未載入');

    // Face Detection
    faceDetectionModel = new FaceDetection({ locateFile: f => `https://cdn.jsdelivr.net/npm/@mediapipe/face_detection/${f}` });
    faceDetectionModel.setOptions({ model:'short', minDetectionConfidence:0.5 });
    faceDetectionModel.onResults(res=>{ lastFaceDetectionResult=res; });
    console.log('MediaPipe Face Detection 模型載入成功');

    // Emotion model
    const modelPath='/static/models/emotion_model.json';
    const ok=await fetch(modelPath); if(!ok.ok) throw new Error(`模型不存在: ${modelPath}`);
    emotionModel = await tf.loadLayersModel(modelPath);
    console.log('情緒模型載入成功');

    if(cameraReady) updateCameraStatus('AI 模型已就緒，可以開始學習','success');
    updateStartButtonState();
  }catch(err){
    console.error('模型載入失敗：',err);
    updateCameraStatus(`模型載入失敗：${err.message}`,'error');
    updateStartButtonState();
  }
}

// 情緒燈
function initEmotionLights(){
  const box=document.getElementById('emotionLights'); if(!box) return; box.innerHTML='';
  EMOTION_LABELS.forEach(em=>{
    const d=document.createElement('div'); d.className='emotion-light-item'; d.id=`emotion-${em.replace('_','-')}`;
    const i=document.createElement('i'); i.className=`${EMOTION_ICONS[em].icon} fa-2x`;
    const l=document.createElement('div'); l.className='emotion-label'; l.textContent=EMOTION_LABELS_ZH[em];
    d.appendChild(i); d.appendChild(l); box.appendChild(d);
  });
}
function resetEmotionLights(){
  EMOTION_LABELS.forEach(e=>{
    const el=document.getElementById(`emotion-${e.replace('_','-')}`);
    if(el){ el.style.opacity='0.3'; el.style.background='transparent'; el.style.boxShadow='none'; el.style.animation='none'; el.classList.remove('active'); }
  });
}
function updateEmotionLights(e,conf){
  resetEmotionLights();
  const el=document.getElementById(`emotion-${e.replace('_','-')}`); if(!el) return;
  const opacity=0.5+(conf*0.5);
  el.style.opacity = opacity;
  el.style.backgroundColor = EMOTION_ICONS[e].color+'20';
  el.style.boxShadow = `0 0 18px ${EMOTION_ICONS[e].color}`;
  el.classList.add('active');
}

// 偵測
function startFaceDetection(){
  clearInterval(detectionInterval);
  detectionInterval = setInterval(async ()=>{
    if(!isDetecting || isPaused || !video || !canvas) return;

    try{
      detectionCount++;
      ctx.drawImage(video,0,0,canvas.width,canvas.height);

      await faceDetectionModel.send({image:video});
      await sleep(80);
      const faces = (lastFaceDetectionResult && lastFaceDetectionResult.detections) ? lastFaceDetectionResult.detections : [];

      if(!faces || faces.length===0){
        if(++noFaceWarningCount>=2) showDetectionWarning('未檢測到人臉，請正對鏡頭');
        return;
      }
      if(faces.length>1){
        if(++multipleFaceWarningCount>=3) showDetectionWarning('檢測到多人，請保持單人');
        return;
      }
      noFaceWarningCount=0; multipleFaceWarningCount=0;

      const emo = await performEmotionDetection(faces[0]);  // {emotion, confidence}
      if(emo.emotion==='no_emotion'){
        showDetectionWarning('檢測到無情緒，請不要離開鏡頭');
        updateEmotionCounts(emo.emotion);
        updateEmotionLights(emo.emotion, emo.confidence);
        return; // 不納入統計、不上報
      }

      validDetections++;
      updateEmotionCounts(emo.emotion);
      updateEmotionLights(emo.emotion, emo.confidence);
      updateAttentionIndicator(calcAttention(emo.emotion));
      await recordEmotion(emo);
      updateStatistics();

    }catch(err){
      console.error(err);
      showDetectionWarning('偵測錯誤，請重新開始');
    }
  }, 1000);
}

async function performEmotionDetection(face){
  if(!emotionModel || typeof tf==='undefined') throw new Error('情緒模型未就緒');
  const fc=document.createElement('canvas'); fc.width=112; fc.height=112; const fctx=fc.getContext('2d');
  const b=face.boundingBox, x=b.xCenter-b.width/2, y=b.yCenter-b.height/2;
  fctx.drawImage(video, x*video.videoWidth, y*video.videoHeight, b.width*video.videoWidth, b.height*video.videoHeight, 0,0,112,112);

  let input=tf.browser.fromPixels(fc);
  if(emotionModel.inputs[0].shape[3]===1) input=input.mean(2,true); // 灰階
  const out = await emotionModel.predict(input.div(255).expandDims(0)).data();
  const idx = out.indexOf(Math.max(...out));
  const emotion = EMOTION_LABELS[idx]; const confidence = out[idx];

  input.dispose();
  return {emotion, confidence};
}

function calcAttention(e){
  const map={ anger:1, disgust:1, fear:1, happy:2, neutral:3, sad:1, surprise:2, no_emotion:0 };
  return map[e] ?? 2;
}
function updateEmotionCounts(e){ if(e in currentEmotionCounts) currentEmotionCounts[e]++; }
function updateAttentionIndicator(level){
  const L=document.getElementById('lowAttention'), M=document.getElementById('mediumAttention'), H=document.getElementById('highAttention');
  [L,M,H].forEach(x=>x&&x.classList.remove('active'));
  if(level===1 && L) L.classList.add('active');
  if(level===2 && M) M.classList.add('active');
  if(level===3 && H) H.classList.add('active');
}
function showDetectionWarning(msg){
  const w=document.getElementById('detectionWarning'), m=document.getElementById('warningMessage');
  if(!w||!m) return; m.textContent=msg; w.style.display='block'; setTimeout(()=>{ w.style.display='none'; }, msg.includes('無情緒')?3000:5000);
}
async function recordEmotion(emo){
  if(emo.emotion==='no_emotion') return; // 不上報
  emotionData.push({ timestamp:new Date(), emotion:emo.emotion, attention:calcAttention(emo.emotion), confidence:emo.confidence });
  try{
    await fetch('/record_emotion',{ method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ emotion:emo.emotion, attention_level:calcAttention(emo.emotion), confidence:emo.confidence }) });
  }catch(_){}
}
function updateStatistics(){
  const avgEl=document.getElementById('avgAttention');
  const detEl=document.getElementById('detectionCount');
  const valEl=document.getElementById('validDetections');
  if(avgEl && emotionData.length){ const avg=emotionData.reduce((s,d)=>s+d.attention,0)/emotionData.length; avgEl.textContent=Math.round(avg*100/3)+'%'; }
  if(detEl) detEl.textContent=detectionCount;
  if(valEl) valEl.textContent=validDetections;
}

// ---------- 學習控制（開始 / 暫停 / 結束 / 產生報告 / 刪除） ----------
function bindStudyControls(){
  const startBtn=document.getElementById('startButton');
  const pauseBtn=document.getElementById('pauseButton');
  const endBtn=document.getElementById('endButton');
  const reportBtn=document.getElementById('generateReportBtn');

  if(startBtn) startBtn.addEventListener('click', startStudySession);
  if(pauseBtn) pauseBtn.addEventListener('click', togglePauseStudySession);
  if(endBtn) endBtn.addEventListener('click', endStudySession);
  if(reportBtn) reportBtn.addEventListener('click', ()=>location.href='/smart_suggestions');

  // 若頁面上有刪除學習記錄的按鈕（data-session-id）
  document.querySelectorAll('[data-delete-session-id]').forEach(btn=>{
    btn.addEventListener('click', ()=>deleteStudySession(btn.getAttribute('data-delete-session-id')));
  });
}

async function startStudySession(){
  if(isDetecting) return; // 防重入
  if(!cameraReady || cameraError){ showMessage('請先開啟攝影機！','error'); return; }
  if(!video || !video.srcObject || video.readyState<2){ showMessage('攝影機尚未就緒，請稍候','error'); return; }
  if(!faceDetectionModel || !emotionModel){ showMessage('AI 模型尚未載入完成，請稍候','error'); return; }

  try{
    const res=await fetch('/api/session/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subject: (typeof SUBJECT!=='undefined'?SUBJECT:null)})});
    const data=await res.json(); if(!data.ok){ showMessage(data.error||'開始學習失敗','error'); return; }
    currentSessionId=data.session_id;

    // 重置統計
    noFaceWarningCount=0; multipleFaceWarningCount=0; emotionData=[]; detectionCount=0; validDetections=0;
    Object.keys(currentEmotionCounts).forEach(k=>currentEmotionCounts[k]=0);

    // 顯示右側卡片
    const stats=document.getElementById('statsCard'); if(stats) stats.style.display='block';
    const emo=document.getElementById('emotionCard'); if(emo) emo.style.display='block';
    const startBtn=document.getElementById('startButton'); const endBtn=document.getElementById('endButton');
    if(startBtn) startBtn.disabled=true; if(endBtn) endBtn.disabled=false;

    startElapsedClock();
    startFaceDetection();
    isDetecting=true;
  }catch{ showMessage('開始學習失敗，請稍後再試','error'); }
}

function togglePauseStudySession(){
  const btn=document.getElementById('pauseButton');
  isPaused=!isPaused;
  if(btn){
    if(isPaused){ btn.innerHTML='<i class="fas fa-play me-2"></i>繼續'; btn.classList.remove('btn-warning'); btn.classList.add('btn-success'); }
    else{ btn.innerHTML='<i class="fas fa-pause me-2"></i>暫停'; btn.classList.remove('btn-success'); btn.classList.add('btn-warning'); }
  }
}

async function endStudySession(){
  isDetecting=false; isPaused=false; clearInterval(detectionInterval); stopElapsedClock();
  try{
    const res=await fetch('/api/session/end',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id: currentSessionId})});
    const data=await res.json(); if(!data.ok){ showMessage(data.error||'結束學習失敗','error'); return; }
    location.href='/dashboard';
  }catch{ showMessage('結束學習失敗，請稍後再試','error'); }
}

async function deleteStudySession(sessionId){
  if(!confirm('確定要刪除這次學習記錄嗎？')) return;
  try{
    const r=await fetch(`/delete_session/${sessionId}`,{method:'POST'}); const j=await r.json();
    if(j.success) location.reload(); else alert('刪除失敗：'+(j.message||'未知錯誤'));
  }catch{ alert('刪除失敗，請稍後再試'); }
}

})(); // IIFE end
