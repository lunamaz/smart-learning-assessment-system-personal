// static/script.js — 最終版：雙狀態獨立顯示 + 移除暫停
(() => {
  // 防止重複初始化
  if (window.__SCRIPT_JS_LOADED__) {
    console.log('[script.js] 已載入，跳過重複初始化');
    return;
  }
  window.__SCRIPT_JS_LOADED__ = true;
  
  console.log('[script.js] 開始載入');

  /* ========= 全域變數 ========= */
  let video, canvas, ctx;
  let faceDetectionModel = null;
  let emotionModel = null;
  let lastFaceDetectionResult = null;

  let isDetecting = false;
  let detectionInterval = null;

  let cameraReady = false;
  let cameraError = false;
  let modelsLoaded = false;

  let emotionData = [];
  let detectionCount = 0;
  let validDetections = 0;
  let noFaceWarningCount = 0;
  let multipleFaceWarningCount = 0;

  const EMOTION_LABELS = ['anger', 'disgust', 'fear', 'happy', 'neutral', 'sad', 'surprise', 'no_emotion'];
  const EMOTION_LABELS_ZH = {
    anger: '生氣', disgust: '厭惡', fear: '恐懼', happy: '開心',
    neutral: '中性', sad: '難過', surprise: '驚訝', no_emotion: '無情緒'
  };
  const EMOTION_ICONS = {
    anger: { icon: 'fas fa-angry', color: '#E74C3C' },
    disgust: { icon: 'fas fa-grimace', color: '#8E44AD' },
    fear: { icon: 'fas fa-dizzy', color: '#9B59B6' },
    happy: { icon: 'fas fa-smile', color: '#F39C12' },
    neutral: { icon: 'fas fa-meh', color: '#2ECC71' },
    sad: { icon: 'fas fa-sad-tear', color: '#3498DB' },
    surprise: { icon: 'fas fa-surprise', color: '#E74C3C' },
    no_emotion: { icon: 'fas fa-question', color: '#95A5A6' }
  };

  /* ========= 工具函式 ========= */
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  function addScriptOnce(src, readyTest) {
    try {
      if (typeof readyTest === 'function' && readyTest()) return;
    } catch (_) { }
    if ([...document.scripts].some(s => s.src === src)) return;
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    document.head.appendChild(s);
  }

  function getFaceDetectionCtor() {
    const g = window;
    if (g.FaceDetection && typeof g.FaceDetection.FaceDetection === 'function')
      return g.FaceDetection.FaceDetection;
    if (g.faceDetection && typeof g.faceDetection.FaceDetection === 'function')
      return g.faceDetection.FaceDetection;
    if (typeof g.FaceDetection === 'function')
      return g.FaceDetection;
    return null;
  }

  /* ========= 初始化路由 ========= */
  function initRouter() {
    const p = window.location.pathname;
    if (p.includes('/study/')) {
      console.log('[script.js] 偵測到學習頁面');
      initStudyPage();
    }
  }

  /* ========= 學習頁初始化 ========= */
  async function initStudyPage() {
    // 載入外部庫
    addScriptOnce('https://cdn.jsdelivr.net/npm/@mediapipe/face_detection/face_detection.js',
      () => !!(window.FaceDetection || window.faceDetection));
    addScriptOnce('https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@latest/dist/tf.min.js',
      () => !!window.tf);

    // 等待 DOM 元素
    await waitForElements();

    // 載入 AI 模型（不等相機）
    loadModels();  // 不 await，讓它在背景載入

    // 初始化 UI
    initEmotionLights();

    // 暴露控制函式
    window.startDetection = startDetection;
    window.stopDetection = stopDetection;
    
    // ✅ 新增：暴露相機回調函式給 study.html
    window.onCameraReady = onCameraReady;
    window.onCameraError = onCameraError;

    console.log('[script.js] 初始化完成');
  }

  /* ========= 等待 DOM 元素 ========= */
  async function waitForElements() {
    let tries = 0;
    while (tries < 50) {
      video = document.getElementById('video');
      canvas = document.getElementById('canvas');
      if (video && canvas) {
        ctx = canvas.getContext('2d');
        return;
      }
      await sleep(100);
      tries++;
    }
    console.error('[script.js] DOM 元素載入超時');
  }

  /* ========= 相機回調函式（由 study.html 呼叫）========= */
  function onCameraReady() {
    cameraReady = true;
    cameraError = false;
    console.log('[script.js] ✓ 相機已就緒');
  }

  function onCameraError() {
    cameraReady = false;
    cameraError = true;
    console.log('[script.js] ✗ 相機啟動失敗');
  }

  /* ========= 更新模型狀態顯示（獨立）========= */
  function updateModelStatus(status, message) {
    const statusEl = document.getElementById('modelStatus');
    if (!statusEl) return;

    const configs = {
      loading: {
        className: 'alert alert-info mb-0',
        html: '<i class="fas fa-spinner fa-spin me-2"></i>' + message
      },
      success: {
        className: 'alert alert-success mb-0',
        html: '<i class="fas fa-robot me-2"></i>' + message
      },
      error: {
        className: 'alert alert-danger mb-0',
        html: '<i class="fas fa-exclamation-triangle me-2"></i>' + message
      }
    };

    const config = configs[status] || configs.loading;
    statusEl.className = config.className;
    statusEl.innerHTML = config.html;
  }

  /* ========= 載入 AI 模型 ========= */
  async function loadModels() {
    if (modelsLoaded) {
      console.log('[script.js] 模型已載入，跳過');
      return;
    }

    console.log('[script.js] 開始載入 AI 模型');
    updateModelStatus('loading', '正在載入 AI 模型...');

    // 等待外部庫載入
    let tries = 0;
    while ((!getFaceDetectionCtor() || typeof tf === 'undefined') && tries < 200) {
      await sleep(50);
      tries++;
    }

    try {
      if (!getFaceDetectionCtor()) throw new Error('MediaPipe Face Detection 未載入');
      if (typeof tf === 'undefined') throw new Error('TensorFlow.js 未載入');

      // 載入人臉偵測模型
      const FD = getFaceDetectionCtor();
      faceDetectionModel = new FD({
        locateFile: f => `https://cdn.jsdelivr.net/npm/@mediapipe/face_detection/${f}`
      });
      if (faceDetectionModel.setOptions) {
        faceDetectionModel.setOptions({
          model: 'short',
          minDetectionConfidence: 0.5
        });
      }
      if (faceDetectionModel.onResults) {
        faceDetectionModel.onResults(res => {
          lastFaceDetectionResult = res;
        });
      }
      console.log('[script.js] ✓ 人臉偵測模型已載入');

      // 載入情緒辨識模型
      const modelPath = '/static/models/emotion_model.json';
      const ok = await fetch(modelPath);
      if (!ok.ok) throw new Error(`模型不存在: ${modelPath}`);
      emotionModel = await tf.loadLayersModel(modelPath);
      console.log('[script.js] ✓ 情緒辨識模型已載入');

      modelsLoaded = true;
      updateModelStatus('success', 'AI 模型已就緒');

    } catch (err) {
      console.error('[script.js] ✗ 模型載入失敗：', err);
      updateModelStatus('error', 'AI 模型載入失敗');
    }
  }

  /* ========= 初始化情緒燈 ========= */
  function initEmotionLights() {
    const box = document.getElementById('emotionLights');
    if (!box) return;

    box.innerHTML = '';
    EMOTION_LABELS.forEach(em => {
      const d = document.createElement('div');
      d.className = 'emotion-light-item';
      d.id = `emotion-${em.replace('_', '-')}`;

      const i = document.createElement('i');
      i.className = `${EMOTION_ICONS[em].icon} fa-2x`;
      i.style.color = EMOTION_ICONS[em].color;

      const l = document.createElement('div');
      l.className = 'emotion-label';
      l.textContent = EMOTION_LABELS_ZH[em];

      d.appendChild(i);
      d.appendChild(l);
      box.appendChild(d);
    });

    console.log('[script.js] ✓ 情緒燈已初始化');
  }

  function resetEmotionLights() {
    EMOTION_LABELS.forEach(e => {
      const el = document.getElementById(`emotion-${e.replace('_', '-')}`);
      if (el) {
        el.style.opacity = '0.3';
        el.style.background = 'transparent';
        el.style.boxShadow = 'none';
        el.classList.remove('active');
      }
    });
  }

  function updateEmotionLights(emotion, confidence) {
    resetEmotionLights();
    const el = document.getElementById(`emotion-${emotion.replace('_', '-')}`);
    if (!el) return;

    const opacity = 0.5 + (confidence * 0.5);
    el.style.opacity = opacity.toString();
    el.style.backgroundColor = EMOTION_ICONS[emotion].color + '20';
    el.style.boxShadow = `0 0 20px ${EMOTION_ICONS[emotion].color}`;
    el.classList.add('active');
  }

  /* ========= 控制函式（移除 toggleDetection）========= */
  function startDetection() {
    console.log('[script.js] 開始偵測');

    if (!faceDetectionModel || !emotionModel) {
      console.error('[script.js] ✗ AI 模型尚未載入');
      alert('AI 模型尚未載入完成，請稍候');
      return;
    }

    if (!cameraReady) {
      console.error('[script.js] ✗ 相機尚未就緒');
      alert('相機尚未就緒，請確認權限');
      return;
    }

    if (isDetecting) {
      console.warn('[script.js] ⚠ 偵測已在進行中');
      return;
    }

    isDetecting = true;
    noFaceWarningCount = 0;
    multipleFaceWarningCount = 0;
    emotionData = [];
    detectionCount = 0;
    validDetections = 0;

    startFaceDetection();
  }

  function stopDetection() {
    isDetecting = false;
    if (detectionInterval) {
      clearInterval(detectionInterval);
      detectionInterval = null;
    }
    console.log('[script.js] 偵測已停止');
  }

  /* ========= 人臉偵測主迴圈 ========= */
  function startFaceDetection() {
    if (detectionInterval) {
      clearInterval(detectionInterval);
    }

    detectionInterval = setInterval(async () => {
      if (!isDetecting || !video || !canvas) return;

      try {
        detectionCount++;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        if (faceDetectionModel && typeof faceDetectionModel.send === 'function') {
          await faceDetectionModel.send({ image: video });
        }
        await sleep(100);

        const faces = (lastFaceDetectionResult && lastFaceDetectionResult.detections)
          ? lastFaceDetectionResult.detections : [];

        if (!faces || faces.length === 0) {
          if (++noFaceWarningCount >= 2) {
            showDetectionWarning('未檢測到人臉，請正對鏡頭');
          }
          return;
        }

        if (faces.length > 1) {
          if (++multipleFaceWarningCount >= 3) {
            showDetectionWarning('檢測到多人，請保持單人');
          }
          return;
        }

        noFaceWarningCount = 0;
        multipleFaceWarningCount = 0;

        const emo = await performEmotionDetection(faces[0]);

        if (emo.emotion === 'no_emotion') {
          showDetectionWarning('檢測到無情緒，請不要離開鏡頭');
          updateEmotionLights(emo.emotion, emo.confidence);
          return;
        }

        validDetections++;
        updateEmotionLights(emo.emotion, emo.confidence);
        await recordEmotion(emo);
        updateStatistics();

      } catch (err) {
        console.error('[script.js] 偵測錯誤：', err);
      }
    }, 1000);
  }

  /* ========= 取出人臉框 ========= */
  function extractBox(face) {
    if (face && face.boundingBox && typeof face.boundingBox.xCenter === 'number') {
      const b = face.boundingBox;
      return {
        x: b.xCenter - b.width / 2,
        y: b.yCenter - b.height / 2,
        w: b.width,
        h: b.height
      };
    }
    if (face && face.locationData && face.locationData.relativeBoundingBox) {
      const r = face.locationData.relativeBoundingBox;
      return {
        x: r.xmin,
        y: r.ymin,
        w: r.width,
        h: r.height
      };
    }
    return null;
  }

  /* ========= 情緒偵測 ========= */
  async function performEmotionDetection(face) {
    if (!emotionModel || typeof tf === 'undefined') {
      throw new Error('情緒模型未就緒');
    }

    const box = extractBox(face);
    if (!box || !video.videoWidth || !video.videoHeight) {
      return { emotion: 'no_emotion', confidence: 0 };
    }

    const x = Math.max(0, Math.min(1, box.x));
    const y = Math.max(0, Math.min(1, box.y));
    const w = Math.max(0.01, Math.min(1 - x, box.w));
    const h = Math.max(0.01, Math.min(1 - y, box.h));

    const fc = document.createElement('canvas');
    fc.width = 112;
    fc.height = 112;
    const fctx = fc.getContext('2d');
    fctx.drawImage(
      video,
      x * video.videoWidth, y * video.videoHeight,
      w * video.videoWidth, h * video.videoHeight,
      0, 0, 112, 112
    );

    let input = tf.browser.fromPixels(fc);
    if (emotionModel.inputs[0].shape[3] === 1) {
      input = input.mean(2, true);
    }

    const out = await emotionModel.predict(input.div(255).expandDims(0)).data();
    const idx = out.indexOf(Math.max(...out));
    const emotion = EMOTION_LABELS[idx] || 'no_emotion';
    const confidence = out[idx] ?? 0;

    input.dispose();
    return { emotion, confidence };
  }

  /* ========= 專注度計算 ========= */
  function calcAttention(emotion) {
    const map = {
      anger: 1, disgust: 1, fear: 1,
      happy: 2, neutral: 3, sad: 1,
      surprise: 2, no_emotion: 0
    };
    return map[emotion] ?? 2;
  }

  /* ========= 顯示警告訊息 ========= */
  function showDetectionWarning(msg) {
    const w = document.getElementById('detectionWarning');
    const m = document.getElementById('warningMessage');
    if (!w || !m) return;

    m.textContent = msg;
    w.style.display = 'block';

    setTimeout(() => {
      w.style.display = 'none';
    }, msg.includes('無情緒') ? 3000 : 5000);
  }

  /* ========= 記錄情緒到後端 ========= */
  async function recordEmotion(emo) {
    if (emo.emotion === 'no_emotion') return;

    emotionData.push({
      timestamp: new Date(),
      emotion: emo.emotion,
      attention: calcAttention(emo.emotion),
      confidence: emo.confidence
    });

    try {
      await fetch('/record_emotion', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          emotion: emo.emotion,
          attention_level: calcAttention(emo.emotion),
          confidence: emo.confidence
        })
      });
    } catch (err) {
      console.error('[script.js] 記錄情緒失敗：', err);
    }
  }

  /* ========= 更新統計數據 ========= */
  function updateStatistics() {
    const avgEl = document.getElementById('avgAttention');
    const detEl = document.getElementById('detectionCount');
    const valEl = document.getElementById('validDetections');

    if (avgEl && emotionData.length) {
      const avg = emotionData.reduce((s, d) => s + d.attention, 0) / emotionData.length;
      avgEl.textContent = Math.round(avg * 100 / 3) + '%';
    }
    if (detEl) detEl.textContent = detectionCount;
    if (valEl) valEl.textContent = validDetections;
  }

  /* ========= 啟動路由 ========= */
  ready(initRouter);

  console.log('[script.js] 載入完成');
})();