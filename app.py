from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta, timezone

import json
import os
import sqlite3
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
import httpx
import asyncio
from werkzeug.utils import secure_filename

# === 統一的失敗訊息（無 API Key / 客戶端不可用 / 連線錯誤等一律用此訊息） ===
FAILURE_TEXT = "AI建議暫時無法生成，請稍後再試。系統仍可正常提供其他學習建議。"
LEGACY_OFFLINE_PREFIX = "【系統建議（未設定 API Key）】"   # 用於偵測並清除舊字串
MIN_SESSION_MINUTES = 1
def is_eligible_session(s):
    return s.end_time is not None and (s.duration_minutes or 0) >= MIN_SESSION_MINUTES

# --- OpenAI 可用性偵測 ---
try:
    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv()
    OPENAI_AVAILABLE = True
    print("OpenAI API 已載入")
except ImportError:
    OPENAI_AVAILABLE = False
    print("OpenAI API 未安裝，AI建議功能將不可用")

# 管理開關：若要在部署時關閉，設環境變數 AI_SUGGESTIONS_ENABLED=false
AI_SUGGESTIONS_ENABLED = os.environ.get('AI_SUGGESTIONS_ENABLED', 'true').lower() == 'true'

def has_openai_client() -> bool:
    return OPENAI_AVAILABLE and AI_SUGGESTIONS_ENABLED and (client is not None)

# 初始化 OpenAI client（若無 API Key，client=None）
if OPENAI_AVAILABLE and AI_SUGGESTIONS_ENABLED:
    try:
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            from dotenv import load_dotenv
            load_dotenv()
            api_key = os.environ.get('OPENAI_API_KEY')
        if api_key:
            try:
                custom_http_client = httpx.Client(
                    timeout=httpx.Timeout(60.0, connect=20.0, read=40.0),
                    limits=httpx.Limits(max_keepalive_connections=1, max_connections=1),
                    follow_redirects=True,
                    verify=True,
                    headers={'User-Agent': 'OpenAI-Python/1.0', 'Connection': 'close'}
                )
                client = OpenAI(api_key=api_key, http_client=custom_http_client, timeout=60.0, max_retries=2)
                print("✓ OpenAI 客戶端初始化成功（自定義 HTTP 客戶端）")
            except Exception as e1:
                print(f"自定義 HTTP 客戶端失敗: {e1}")
                try:
                    os.environ['OPENAI_API_KEY'] = api_key
                    client = OpenAI(timeout=45.0, max_retries=1)
                    print("✓ OpenAI 客戶端初始化成功（環境變數方式）")
                except Exception as e2:
                    print(f"環境變數方式也失敗: {e2}")
                    client = None
        else:
            client = None
            print("✗ 未找到 OPENAI_API_KEY")
    except Exception as e:
        client = None
        print(f"✗ OpenAI 初始化失敗: {e}")
else:
    client = None

# --- Matplotlib（PDF 圖表用） ---
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.font_manager import FontProperties
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    CHARTS_AVAILABLE = True
except ImportError:
    CHARTS_AVAILABLE = False
    print("Charts功能暫時無法使用，將生成純文字報告")

from io import BytesIO
import base64

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///learning_system.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# -------- 影片根目錄與科目資料夾映射 --------
DEFAULT_VIDEO_ROOT = os.path.join('static', 'video')
LEGACY_VIDEO_ROOT = os.path.join('static', 'videos')
VIDEO_ROOT = DEFAULT_VIDEO_ROOT if os.path.isdir(DEFAULT_VIDEO_ROOT) else LEGACY_VIDEO_ROOT

SUBJECT_DIR_MAP = {
    'math': 'math',
    'science': 'science',
    'language': 'language',
    'social': 'social',
    'art': 'art',
    'cs': 'cs'
}
ALLOWED_VIDEO_EXTS = {'.mp4', '.m4v', '.webm', '.ogg'}

# -------- PDF 字型註冊 --------
try:
    font_paths = [
        './fonts/MSJH.TTC', 'C:/Windows/Fonts/msjh.ttc', 'C:/Windows/Fonts/msjh.ttf',
        '/System/Library/Fonts/PingFang.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        './static/fonts/NotoSansCJK-Regular.ttc'
    ]
    PDF_FONT = None
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont('ChineseFont', fp))
                PDF_FONT = 'ChineseFont'
                print(f"成功載入中文字體: {fp}")
                break
            except Exception as e:
                print(f"載入字體失敗 {fp}: {e}")
    if PDF_FONT is None:
        print("警告: 無法載入中文字體，將使用 Helvetica")
        PDF_FONT = 'Helvetica'
except Exception as e:
    print(f"字體註冊過程發生錯誤: {e}")
    PDF_FONT = 'Helvetica'

# 修正所有時間相關函數
def get_taiwan_now():
    """獲取當前台灣時間（naive datetime），用於記錄和顯示"""
    utc_now = datetime.now(timezone.utc)
    taiwan_time = utc_now + timedelta(hours=8)
    return taiwan_time.replace(tzinfo=None)  # 返回 naive datetime

# --------- Models ---------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(60), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    children = db.relationship('Child', backref='user', lazy=True, cascade='all, delete-orphan')

class Child(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    nickname = db.Column(db.String(80), nullable=False)
    gender = db.Column(db.String(10), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    education_stage = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    ai_suggestion = db.Column(db.Text)
    pdf_report_path = db.Column(db.String(255))
    pdf_generated_at = db.Column(db.DateTime)
    study_sessions = db.relationship('StudySession', backref='child', lazy=True, cascade='all, delete-orphan')

class StudySession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_id = db.Column(db.Integer, db.ForeignKey('child.id'), nullable=False)
    subject = db.Column(db.String(50), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.now)
    end_time = db.Column(db.DateTime)
    avg_attention = db.Column(db.Float)
    avg_emotion_score = db.Column(db.Float)
    emotion_data = db.relationship('EmotionData', backref='study_session', lazy=True, cascade='all, delete-orphan')

class EmotionData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('study_session.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.now)
    emotion = db.Column(db.String(20))
    attention_level = db.Column(db.Integer)
    confidence = db.Column(db.Float)

class VideoWatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('study_session.id'), nullable=False)
    subject = db.Column(db.String(50), nullable=False)
    video_filename = db.Column(db.String(255), nullable=False)
    video_display_name = db.Column(db.String(255), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.now)
    ended_at = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer, default=0)

SUBJECTS = {
    'math': '數學',
    'science': '自然科學',
    'language': '語言文學',
    'social': '社會科學',
    'art': '藝術創作',
    'cs': '電腦科學'
}
EDUCATION_STAGES = {'elementary': '國小', 'middle': '國中', 'high': '高中'}
GENDERS = {'male': '男生', 'female': '女生'}

def compute_overall_avg_attention_percent(sessions):
    """以『場次平均』計算整體平均專注度（0~100%），忽略 0 與 None。"""
    vals = [s.avg_attention for s in sessions if s.avg_attention]  # 0 被排除
    return round(sum(vals) / len(vals) * 100 / 3) if vals else 0

# ----------------- 影片輔助 -----------------
def get_subject_video_dir(subject_key: str) -> str:
    folder = SUBJECT_DIR_MAP.get(subject_key)
    if not folder:
        return ''
    return os.path.join(VIDEO_ROOT, folder)

def list_subject_videos(subject_key: str):
    directory = get_subject_video_dir(subject_key)
    if not directory or not os.path.isdir(directory):
        return []
    items = []
    for f in sorted(os.listdir(directory)):
        path = os.path.join(directory, f)
        if not os.path.isfile(path):
            continue
        _, ext = os.path.splitext(f)
        if ext.lower() in ALLOWED_VIDEO_EXTS:
            display = os.path.splitext(f)[0]
            items.append({'filename': f, 'display_name': display})
    return items

def build_video_static_url(subject_key: str, filename: str) -> str:
    folder = SUBJECT_DIR_MAP.get(subject_key)
    static_root_name = os.path.basename(VIDEO_ROOT)
    rel_path = f"{static_root_name}/{folder}/{filename}"
    return url_for('static', filename=rel_path)

# ----------------- AI 產生建議 -----------------
def generate_ai_suggestions(child, study_sessions):
    """使用 OpenAI 生成個人化學習建議；任一失敗情境皆回 FAILURE_TEXT"""
    if not has_openai_client():
        return FAILURE_TEXT

    try:
        eligible = [s for s in study_sessions if is_eligible_session(s)]
        total_sessions = len(eligible)

        if total_sessions == 0:
            learning_summary = "目前尚無學習記錄"
        else:
            total_minutes = sum(s.duration_minutes or 0 for s in eligible)
            avg_attention_percent = compute_overall_avg_attention_percent(eligible)  # ← 與前台一致

            subject_stats = {}
            for session in eligible:
                d = subject_stats.setdefault(session.subject, {
                    'count': 0, 'total_time': 0,
                    'att_sum': 0.0, 'att_cnt': 0
                })
                d['count'] += 1
                d['total_time'] += (session.duration_minutes or 0)
                if session.avg_attention:  # ← 忽略 0
                    d['att_sum'] += session.avg_attention
                    d['att_cnt'] += 1

            best_subject = ""
            worst_subject = ""
            if subject_stats:
                best_avg = -1
                worst_avg = 10**9
                for subj, st in subject_stats.items():
                    avg_att = (st['att_sum'] / st['att_cnt']) if st['att_cnt'] > 0 else None
                    if avg_att is not None:
                        if avg_att > best_avg:
                            best_avg = avg_att
                            best_subject = SUBJECTS.get(subj, subj)
                        if avg_att < worst_avg:
                            worst_avg = avg_att
                            worst_subject = SUBJECTS.get(subj, subj)
                            
            learning_summary = f"""
            總學習次數: {total_sessions}次
            總學習時間: {total_minutes}分鐘
            平均專注度: {avg_attention_percent}%
            表現最佳科目: {best_subject}
            需要加強科目: {worst_subject}
            """

        prompt = f"""
        你是一位專業的教育顧問，請根據以下學生資訊提供具體且實用的學習建議：

        學生基本資訊：
        - 暱稱: {child.nickname}
        - 年齡: {child.age}歲
        - 性別: {GENDERS.get(child.gender, child.gender)}
        - 教育階段: {EDUCATION_STAGES.get(child.education_stage, child.education_stage)}

        學習狀況摘要：
        {learning_summary}

        請提供以下方面的建議，每個建議都要具體且可執行：
        1. 年齡與階段的學習策略
        2. 專注力提升方法
        3. 時間安排與休息規劃
        4. 基於目前表現的改進
        5. 合適的工具或方法

        請用溫柔、鼓勵的口吻，以一段話呈現（非條列），總長度控制在300字內，繁體中文。
        """

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "你是一位專業的教育顧問，專長於為台灣學生提供個人化學習建議。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.7,
            timeout=30
        )
        ai_suggestion = response.choices[0].message.content.strip() if response else ""
        return ai_suggestion or FAILURE_TEXT

    except Exception as e:
        print(f"AI建議生成過程發生錯誤: {e}")
        return FAILURE_TEXT

# ----------------- Flask Routes -----------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')

        if User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': '使用者名稱已存在'})
        if User.query.filter_by(email=email).first():
            return jsonify({'success': False, 'message': '電子郵件已註冊'})

        password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, email=email, password_hash=password_hash)
        db.session.add(user)
        db.session.commit()
        return jsonify({'success': True, 'message': '註冊成功'})
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')

        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return jsonify({'success': True, 'message': '登入成功'})
        else:
            return jsonify({'success': False, 'message': '使用者名稱或密碼錯誤'})
    return render_template('login.html')

@app.route('/child_selection')
def child_selection():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    children = Child.query.filter_by(user_id=user_id).all()
    return render_template('child_selection.html', children=children)

@app.route('/create_child', methods=['POST'])
def create_child():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    data = request.get_json()
    nickname = data.get('nickname')
    gender = data.get('gender')
    age = data.get('age')
    education_stage = data.get('education_stage')

    try:
        age = int(age)
        if age < 6 or age > 18:
            return jsonify({'success': False, 'message': '年齡必須在6-18歲之間'})
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': '請輸入有效的年齡'})

    existing_children = Child.query.filter_by(user_id=session['user_id']).count()
    if existing_children >= 4:
        return jsonify({'success': False, 'message': '最多只能創建4個小孩檔案'})

    child = Child(
        user_id=session['user_id'],
        nickname=nickname,
        gender=gender,
        age=age,
        education_stage=education_stage
    )
    db.session.add(child)
    db.session.commit()
    return jsonify({'success': True, 'child_id': child.id})

@app.route('/select_child/<int:child_id>')
def select_child(child_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    session['child_id'] = child.id
    session['child_nickname'] = child.nickname
    return redirect(url_for('dashboard'))

'''
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child_id = session['child_id']
    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只統計「已完成（end_time 不為空）」的學習場次
    user_study_sessions = (StudySession.query
                           .filter_by(child_id=child_id)
                           .filter(StudySession.end_time.isnot(None))
                           .all())

    subject_stats = {}
    for s in user_study_sessions:
        if s.subject not in subject_stats:
            subject_stats[s.subject] = {
                'count': 0,
                'total_time': 0,
                'avg_attention_sum': 0.0,
                'attention_count': 0,
                'avg_attention': 0.0  # 供模板使用
            }
        subject_stats[s.subject]['count'] += 1
        subject_stats[s.subject]['total_time'] += (s.duration_minutes or 0)
        if s.avg_attention is not None:
            subject_stats[s.subject]['avg_attention_sum'] += s.avg_attention
            subject_stats[s.subject]['attention_count'] += 1

    for subject in subject_stats:
        cnt = subject_stats[subject]['attention_count']
        subject_stats[subject]['avg_attention'] = (subject_stats[subject]['avg_attention_sum'] / cnt) if cnt else 0.0
        # 清掉內部欄位，避免模板誤用
        del subject_stats[subject]['avg_attention_sum']
        del subject_stats[subject]['attention_count']

    return render_template('dashboard.html',
                           subjects=SUBJECTS,
                           stats=subject_stats,
                           child=child)'''

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child_id = session['child_id']
    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只統計「已完成（end_time 不為空）」的學習場次
    user_study_sessions = (StudySession.query
                           .filter_by(child_id=child_id)
                           .filter(StudySession.end_time.isnot(None))
                           .filter(StudySession.duration_minutes >= MIN_SESSION_MINUTES)  # ★ 新增
                           .all())

    subject_stats = {}
    for s in user_study_sessions:
        d = subject_stats.setdefault(s.subject, {
            'count': 0,
            'total_time': 0,
            'avg_attention_sum': 0.0,
            'attention_count': 0,
            'avg_attention': 0.0
        })
        d['count'] += 1
        d['total_time'] += (s.duration_minutes or 0)
        if s.avg_attention is not None:
            d['avg_attention_sum'] += s.avg_attention
            d['attention_count'] += 1

    for subject, d in subject_stats.items():
        cnt = d['attention_count']
        d['avg_attention'] = (d['avg_attention_sum'] / cnt) if cnt else 0.0
        d.pop('avg_attention_sum', None)
        d.pop('attention_count', None)

    # ★ 供首頁顯示：以「時數最長」當作最常學習科目
    most_by_time_key = max(subject_stats.items(), key=lambda kv: kv[1]['total_time'])[0] if subject_stats else None
    most_by_time_name = SUBJECTS.get(most_by_time_key, most_by_time_key) if most_by_time_key else None

    # ★ 供首頁顯示：整體平均專注度（百分比）
    att_list = [s.avg_attention for s in user_study_sessions if s.avg_attention is not None]
    # overall_avg_attention_percent = round(sum(att_list)/len(att_list) * 100/3) if att_list else 0
    overall_avg_attention_percent = compute_overall_avg_attention_percent(user_study_sessions)

    total_minutes = sum(s.duration_minutes or 0 for s in user_study_sessions)
    total_hours = total_minutes / 60

    return render_template('dashboard.html',
                           subjects=SUBJECTS,
                           stats=subject_stats,
                           child=child,
                           overall_avg_attention=overall_avg_attention_percent,
                           total_sessions=len(user_study_sessions),
                           total_hours=total_hours,
                           most_studied_subject_by_time=most_by_time_name)


@app.route('/video-selection/<subject>')
def video_selection(subject):
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))
    if subject not in SUBJECTS:
        return redirect(url_for('dashboard'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    videos = list_subject_videos(subject)
    return render_template('video_selection.html',
                           subject=subject,
                           subject_name=SUBJECTS[subject],
                           child=child,
                           videos=videos)

@app.route('/study/<subject>')
def study_session(subject):
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))
    if subject not in SUBJECTS:
        return redirect(url_for('dashboard'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    return render_template('study.html', subject=subject, subject_name=SUBJECTS[subject],
                           child=child, video_path=None, video_name=None)

@app.route('/study/<subject>/video/<path:video_filename>')
def study_with_video(subject, video_filename):
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))
    if subject not in SUBJECTS:
        return redirect(url_for('dashboard'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    directory = get_subject_video_dir(subject)
    if not directory:
        return redirect(url_for('video_selection', subject=subject))

    normalized = os.path.normpath(os.path.join(directory, video_filename))
    if not normalized.startswith(os.path.abspath(directory)):
        return redirect(url_for('video_selection', subject=subject))
    if not os.path.isfile(normalized):
        return redirect(url_for('video_selection', subject=subject))

    video_url = build_video_static_url(subject, os.path.basename(video_filename))
    video_name = os.path.splitext(os.path.basename(video_filename))[0]

    return render_template('study.html', subject=subject, subject_name=SUBJECTS[subject],
                           child=child, video_path=video_url, video_name=video_name)

@app.get('/api/videos/<subject>')
def api_list_videos(subject):
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    if subject not in SUBJECTS:
        return jsonify({'ok': False, 'error': 'invalid subject'}), 400

    videos = list_subject_videos(subject)
    for v in videos:
        v['url'] = build_video_static_url(subject, v['filename'])
    return jsonify({'ok': True, 'videos': videos})

@app.post('/api/session/start')
def api_session_start():
    """開始學習階段 - 新版 API（修正版）"""
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    data = request.get_json(force=True) or {}
    subject = data.get('subject')
    if subject not in SUBJECTS:
        return jsonify({'ok': False, 'error': 'invalid subject'}), 400

    now = get_taiwan_now()  # 使用台灣時間
    
    s = StudySession(
        child_id=session['child_id'],
        subject=subject,
        start_time=now,
        duration_minutes=0
    )
    db.session.add(s)
    db.session.commit()

    # ===== 關鍵修正：設定到 Flask session 中，供 /record_emotion 使用 =====
    session['current_session_id'] = s.id
    session['session_start_time'] = now.isoformat()
    
    print(f"[API] 學習階段開始 - ID: {s.id}, 科目: {subject}, 時間: {now}")
    return jsonify({'ok': True, 'session_id': s.id})


@app.post('/api/session/end')
def api_session_end():
    """結束學習階段 - 新版 API（修正版）"""
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    data = request.get_json(force=True) or {}
    session_id = data.get('session_id')
    
    s = StudySession.query.get(session_id)
    if not s or s.child_id != session['child_id']:
        return jsonify({'ok': False, 'error': 'invalid session'}), 400

    now = get_taiwan_now()  # 使用台灣時間
    s.end_time = now
    
    # ===== 關鍵：正確計算時間差 =====
    if s.start_time and s.end_time:
        delta = s.end_time - s.start_time
        minutes = int(delta.total_seconds() / 60)
        s.duration_minutes = max(0, minutes)  # 防止負數
        
        print(f"[API] 學習階段結束 - ID: {s.id}")
        print(f"  開始時間: {s.start_time}")
        print(f"  結束時間: {s.end_time}")
        print(f"  學習時長: {s.duration_minutes} 分鐘")

    # 計算情緒統計
    emotion_records = EmotionData.query.filter_by(session_id=session_id).all()
    if emotion_records:
        avg_attention = sum(r.attention_level for r in emotion_records) / len(emotion_records)
        avg_emotion = sum(r.confidence for r in emotion_records) / len(emotion_records)
        s.avg_attention = avg_attention
        s.avg_emotion_score = avg_emotion
        print(f"  平均專注度: {avg_attention:.2f}")
        print(f"  情緒記錄數: {len(emotion_records)}")

    db.session.commit()
    
    # 清除 session
    session.pop('current_session_id', None)
    session.pop('session_start_time', None)
    
    return jsonify({'ok': True})

@app.post('/api/video/start')
def api_video_start():
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    data = request.get_json(force=True) or {}
    session_id = data.get('session_id')
    subject = data.get('subject')
    filename = data.get('video_filename')
    display_name = data.get('video_display_name')

    s = StudySession.query.get(session_id)
    if not s or s.child_id != session['child_id']:
        return jsonify({'ok': False, 'error': 'invalid session'}), 400
    if subject not in SUBJECTS:
        return jsonify({'ok': False, 'error': 'invalid subject'}), 400
    if not filename or not display_name:
        return jsonify({'ok': False, 'error': 'missing video info'}), 400

    local_now = get_taiwan_now()
    
    vw = VideoWatch(session_id=session_id, subject=subject,
                    video_filename=filename, video_display_name=display_name,
                    started_at=local_now)
    db.session.add(vw)
    db.session.commit()
    return jsonify({'ok': True, 'watch_id': vw.id})

@app.post('/api/video/end')
def api_video_end():
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    data = request.get_json(force=True) or {}
    watch_id = data.get('watch_id')

    vw = VideoWatch.query.get(watch_id)
    if not vw:
        return jsonify({'ok': False, 'error': 'invalid watch id'}), 400

    local_now = get_taiwan_now()
    vw.ended_at = local_now
    
    if vw.started_at and vw.ended_at:
        vw.duration_seconds = int((vw.ended_at - vw.started_at).total_seconds())
    db.session.commit()
    return jsonify({'ok': True, 'duration_seconds': vw.duration_seconds})

# ===== 舊版相容路由（若外部 JS 仍調用） =====
@app.route('/start_session', methods=['POST'])
def start_session():
    """⚠️ 已廢棄：請使用 /api/session/start"""
    print("[警告] 使用了舊版 /start_session API，建議改用 /api/session/start")
    
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'success': False, 'message': '請先登入並選擇小孩'})

    data = request.get_json()
    subject = data.get('subject')
    duration = data.get('duration', 30)

    now = get_taiwan_now()  # 修正：使用台灣時間
    
    new_study_session = StudySession(
        child_id=session['child_id'],
        subject=subject,
        duration_minutes=duration,
        start_time=now
    )
    db.session.add(new_study_session)
    db.session.commit()

    session['current_session_id'] = new_study_session.id
    session['session_start_time'] = now.isoformat()
    return jsonify({'success': True, 'session_id': new_study_session.id})

@app.route('/record_emotion', methods=['POST'])
def record_emotion():
    """記錄情緒數據（保持不變，使用 session['current_session_id']）"""
    if 'current_session_id' not in session:
        return jsonify({'success': False, 'message': '沒有活躍的學習階段'})

    data = request.get_json()
    emotion = data.get('emotion')
    attention_level = data.get('attention_level')
    confidence = data.get('confidence')

    now = get_taiwan_now()  # 使用台灣時間

    emotion_data = EmotionData(
        session_id=session['current_session_id'],
        emotion=emotion,
        attention_level=attention_level,
        confidence=confidence,
        timestamp=now
    )
    db.session.add(emotion_data)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/end_session', methods=['POST'])
def end_session():
    """⚠️ 已廢棄：請使用 /api/session/end"""
    print("[警告] 使用了舊版 /end_session API，建議改用 /api/session/end")
    
    if 'current_session_id' not in session:
        return jsonify({'success': False, 'message': '沒有活躍的學習階段'})

    session_id = session['current_session_id']
    current_study_session = StudySession.query.get(session_id)

    if current_study_session:
        local_now = get_taiwan_now()
        current_study_session.end_time = local_now

        if 'session_start_time' in session:
            start_time = datetime.fromisoformat(session['session_start_time'])
            actual_duration = (local_now - start_time).total_seconds() / 60
            current_study_session.duration_minutes = int(actual_duration)

        emotion_records = EmotionData.query.filter_by(session_id=session_id).all()
        if emotion_records:
            avg_attention = sum(r.attention_level for r in emotion_records) / len(emotion_records)
            avg_emotion = sum(r.confidence for r in emotion_records) / len(emotion_records)
            current_study_session.avg_attention = avg_attention
            current_study_session.avg_emotion_score = avg_emotion

        db.session.commit()
        session.pop('current_session_id', None)
        session.pop('session_start_time', None)
        return jsonify({'success': True, 'session_id': session_id})

    return jsonify({'success': False, 'message': '找不到學習階段'})

'''
@app.route('/end_session', methods=['POST'])
def end_session():
    if 'current_session_id' not in session:
        return jsonify({'success': False, 'message': '沒有活躍的學習階段'})

    session_id = session['current_session_id']
    current_study_session = StudySession.query.get(session_id)

    if current_study_session:
        current_study_session.end_time = datetime.utcnow()

        if 'session_start_time' in session:
            start_time = datetime.fromisoformat(session['session_start_time'])
            actual_duration = (datetime.utcnow() - start_time).total_seconds() / 60
            current_study_session.duration_minutes = int(actual_duration)

        emotion_records = EmotionData.query.filter_by(session_id=session_id).all()
        if emotion_records:
            avg_attention = sum(r.attention_level for r in emotion_records) / len(emotion_records)
            avg_emotion = sum(r.confidence for r in emotion_records) / len(emotion_records)
            current_study_session.avg_attention = avg_attention
            current_study_session.avg_emotion_score = avg_emotion

        db.session.commit()
        session.pop('current_session_id', None)
        session.pop('session_start_time', None)
        return jsonify({'success': True, 'session_id': session_id})

    return jsonify({'success': False, 'message': '找不到學習階段'})'''

def get_best_subject_for_date(child_id, date):
    sessions = StudySession.query.filter(
        StudySession.child_id == child_id,
        db.func.date(StudySession.start_time) == date,
        StudySession.avg_attention.isnot(None)
    ).all()
    if not sessions:
        return None
    best_session = max(sessions, key=lambda x: x.avg_attention)
    return best_session.subject

@app.route('/delete_session/<int:session_id>', methods=['POST'])
def delete_session(session_id):
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'success': False, 'message': '請先登入並選擇小孩'})

    study_session = StudySession.query.filter_by(id=session_id, child_id=session['child_id']).first()
    if study_session:
        child = Child.query.get(session['child_id'])
        if child:
            child.ai_suggestion = None
            child.pdf_report_path = None
            child.pdf_generated_at = None
        db.session.delete(study_session)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': '找不到該學習記錄'})

@app.route('/get_calendar_data')
def get_calendar_data():
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'success': False, 'message': '請先登入並選擇小孩'})

    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)

    start_date = datetime(year, month, 1)
    end_date = datetime(year + (month // 12), (month % 12) + 1, 1)

    sessions = StudySession.query.filter(
        StudySession.child_id == session['child_id'],
        StudySession.start_time >= start_date,
        StudySession.start_time < end_date
    ).all()

    calendar_data = {}
    daily_subjects = {}
    for study_session in sessions:
        date_key = study_session.start_time.strftime('%Y-%m-%d')
        if date_key not in daily_subjects:
            daily_subjects[date_key] = []
        daily_subjects[date_key].append({
            'subject': study_session.subject,
            'attention': study_session.avg_attention or 0,
            'session_data': {
                'id': study_session.id,
                'subject': SUBJECTS.get(study_session.subject, study_session.subject),
                'duration_minutes': study_session.duration_minutes,
                'avg_attention': study_session.avg_attention,
                'start_time': study_session.start_time.strftime('%H:%M')
            }
        })

    subject_colors = {
        'math': '#3498DB', 'science': '#2ECC71', 'language': '#E74C3C',
        'social': '#F39C12', 'art': '#9B59B6', 'cs': '#1ABC9C'
    }

    for date_key, subjects in daily_subjects.items():
        if subjects:
            best_subject_data = max(subjects, key=lambda x: x['attention'])
            best_subject = best_subject_data['subject']
            calendar_data[date_key] = {
                'best_subject': best_subject,
                'color': subject_colors.get(best_subject, '#95A5A6'),
                'sessions': [s['session_data'] for s in subjects]
            }

    return jsonify({'success': True, 'data': calendar_data})

@app.route('/data_analysis')
def data_analysis():
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只取已完成的學習場次（有 end_time）
    study_sessions = (StudySession.query
                     .filter_by(child_id=child.id)
                     .filter(StudySession.end_time.isnot(None))
                     .filter(StudySession.duration_minutes >= MIN_SESSION_MINUTES)  # ★ 新增
                     .order_by(StudySession.start_time.desc())
                     .all())
    
    chart_data = prepare_chart_data(study_sessions)
    overall_avg_attention_percent = compute_overall_avg_attention_percent(study_sessions)
    return render_template('data_analysis.html',
                           child=child,
                           study_sessions=study_sessions,
                           chart_data=chart_data,
                           overall_avg_attention=overall_avg_attention_percent)

''' # 10/07 12:03 AM 註解
@app.route('/data_analysis')
def data_analysis():
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只取已完成的學習場次（有 end_time）
    study_sessions = (StudySession.query
                     .filter_by(child_id=child.id)
                     .filter(StudySession.end_time.isnot(None))
                     .order_by(StudySession.start_time.desc())
                     .all())
    
    chart_data = prepare_chart_data(study_sessions)
    return render_template('data_analysis.html',
                           child=child,
                           study_sessions=study_sessions,
                           chart_data=chart_data)
'''

'''
@app.route('/data_analysis')
def data_analysis():
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    study_sessions = StudySession.query.filter_by(child_id=child.id).order_by(StudySession.start_time.desc()).all()
    chart_data = prepare_chart_data(study_sessions)
    return render_template('data_analysis.html',
                           child=child,
                           study_sessions=study_sessions,
                           chart_data=chart_data)'''

@app.route('/smart_suggestions')
def smart_suggestions():
    """智慧建議頁面：不自動產生；若偵測到舊的離線文案，載入時即清掉並顯示統一失敗訊息。"""
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只取已完成的學習場次（有 end_time）
    study_sessions = (StudySession.query
                     .filter_by(child_id=child.id)
                     .filter(StudySession.end_time.isnot(None))
                     .filter(StudySession.duration_minutes >= MIN_SESSION_MINUTES)  # ★ 新增
                     .order_by(StudySession.start_time.asc())
                     .all())
    
    suggestions = generate_comprehensive_suggestions(child, study_sessions)

    ai_suggestion_db = child.ai_suggestion or ""
    # 清除舊的離線文案
    if ai_suggestion_db.startswith(LEGACY_OFFLINE_PREFIX):
        child.ai_suggestion = None
        db.session.commit()
        ai_suggestion_db = ""

    # 若目前不可產生（無 key / 關閉 / client 不存在），顯示統一的友善訊息（但不寫回 DB）
    if not has_openai_client() and not ai_suggestion_db:
        ai_suggestion_display = FAILURE_TEXT
    else:
        ai_suggestion_display = ai_suggestion_db or None

    performance_data = prepare_performance_data(study_sessions)

    return render_template(
        'smart_suggestions.html',
        child=child,
        suggestions=suggestions,
        ai_suggestion=ai_suggestion_display,
        ai_enabled=True,
        ai_can_generate=has_openai_client(),
        auto_generate=(has_openai_client() and not ai_suggestion_db),
        performance_data=performance_data
    )

''' # 10/07 12:04 AM 註解
@app.route('/smart_suggestions')
def smart_suggestions():
    """智慧建議頁面：不自動產生；若偵測到舊的離線文案，載入時即清掉並顯示統一失敗訊息。"""
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    # 只取已完成的學習場次（有 end_time）
    study_sessions = (StudySession.query
                     .filter_by(child_id=child.id)
                     .filter(StudySession.end_time.isnot(None))
                     .order_by(StudySession.start_time.asc())
                     .all())
    
    suggestions = generate_comprehensive_suggestions(child, study_sessions)

    ai_suggestion_db = child.ai_suggestion or ""
    # 清除舊的離線文案
    if ai_suggestion_db.startswith(LEGACY_OFFLINE_PREFIX):
        child.ai_suggestion = None
        db.session.commit()
        ai_suggestion_db = ""

    # 若目前不可產生（無 key / 關閉 / client 不存在），顯示統一的友善訊息（但不寫回 DB）
    if not has_openai_client() and not ai_suggestion_db:
        ai_suggestion_display = FAILURE_TEXT
    else:
        ai_suggestion_display = ai_suggestion_db or None

    performance_data = prepare_performance_data(study_sessions)

    return render_template(
        'smart_suggestions.html',
        child=child,
        suggestions=suggestions,
        ai_suggestion=ai_suggestion_display,
        ai_enabled=True,
        ai_can_generate=has_openai_client(),
        auto_generate=(has_openai_client() and not ai_suggestion_db),
        performance_data=performance_data
    )
'''

'''
@app.route('/smart_suggestions')
def smart_suggestions():
    """智慧建議頁面：不自動產生；若偵測到舊的離線文案，載入時即清掉並顯示統一失敗訊息。"""
    if 'user_id' not in session or 'child_id' not in session:
        return redirect(url_for('child_selection'))

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('child_selection'))

    study_sessions = StudySession.query.filter_by(child_id=child.id).order_by(StudySession.start_time.asc()).all()
    suggestions = generate_comprehensive_suggestions(child, study_sessions)

    ai_suggestion_db = child.ai_suggestion or ""
    # 清除舊的離線文案
    if ai_suggestion_db.startswith(LEGACY_OFFLINE_PREFIX):
        child.ai_suggestion = None
        db.session.commit()
        ai_suggestion_db = ""

    # 若目前不可產生（無 key / 關閉 / client 不存在），顯示統一的友善訊息（但不寫回 DB）
    if not has_openai_client() and not ai_suggestion_db:
        ai_suggestion_display = FAILURE_TEXT
    else:
        ai_suggestion_display = ai_suggestion_db or None

    performance_data = prepare_performance_data(study_sessions)

    return render_template(
        'smart_suggestions.html',
        child=child,
        suggestions=suggestions,
        ai_suggestion=ai_suggestion_display,
        ai_enabled=True,
        ai_can_generate=has_openai_client(),
        auto_generate=(has_openai_client() and not ai_suggestion_db),
        performance_data=performance_data
    )'''

@app.route('/generate_ai_suggestion', methods=['POST'])
def generate_ai_suggestion_api():
    """
    生成 AI 建議（僅線上；不可用時一律回 FAILURE_TEXT）。
    前端無論如何都能拿到 ai_suggestion，避免「功能未啟用」的冷冰冰錯誤。
    """
    if 'user_id' not in session or 'child_id' not in session:
        return jsonify({'success': False, 'message': '請先登入並選擇小孩'})

    child = Child.query.filter_by(id=session['child_id'], user_id=session['user_id']).first()
    if not child:
        return jsonify({'success': False, 'message': '找不到小孩檔案'})

    study_sessions = StudySession.query.filter_by(child_id=child.id).order_by(StudySession.start_time.asc()).all()

    try:
        if has_openai_client():
            text = generate_ai_suggestions(child, study_sessions).strip()
        else:
            text = FAILURE_TEXT

        # 寫回 DB（讓重新整理仍能看到最新結果）
        child.ai_suggestion = text
        child.pdf_generated_at = None
        db.session.commit()

        return jsonify({'success': True, 'ai_suggestion': text})
    except Exception as e:
        print(f"生成 AI 建議時發生錯誤: {e}")
        # 仍回 200 + 統一友善訊息，讓前端把 spinner 停掉
        return jsonify({'success': True, 'ai_suggestion': FAILURE_TEXT})

# ----------------- 報告/刪除/更新等其餘路由 -----------------
@app.route('/generate_report/<int:child_id>')
def generate_report(child_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if not child:
        return redirect(url_for('dashboard'))

    study_sessions = StudySession.query.filter_by(child_id=child.id).all()

    should_regenerate = False
    if not child.pdf_report_path or not os.path.exists(child.pdf_report_path) or not child.pdf_generated_at:
        should_regenerate = True
    elif child.pdf_generated_at and study_sessions:
        latest = max(study_sessions, key=lambda x: x.start_time)
        if latest.start_time > child.pdf_generated_at:
            should_regenerate = True

    if should_regenerate:
        ai_suggestion = child.ai_suggestion
        pdf_path = create_comprehensive_report(child, study_sessions, ai_suggestion)
        child.pdf_report_path = pdf_path
        child.pdf_generated_at = datetime.utcnow()
        db.session.commit()
        return send_file(pdf_path, as_attachment=True,
                         download_name=f'學習報告_{child.nickname}_{datetime.now().strftime("%Y%m%d")}.pdf')
    else:
        return send_file(child.pdf_report_path, as_attachment=True,
                         download_name=f'學習報告_{child.nickname}_{datetime.now().strftime("%Y%m%d")}.pdf')

@app.route('/delete_child/<int:child_id>', methods=['POST'])
def delete_child(child_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if child:
        if child.pdf_report_path and os.path.exists(child.pdf_report_path):
            try:
                os.remove(child.pdf_report_path)
            except:
                pass
        db.session.delete(child)
        db.session.commit()
        if session.get('child_id') == child_id:
            session.pop('child_id', None)
            session.pop('child_nickname', None)
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': '找不到該小孩檔案'})

@app.route('/reset_learning_history/<int:child_id>', methods=['POST'])
def reset_learning_history(child_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()
    if child:
        StudySession.query.filter_by(child_id=child_id).delete()
        child.ai_suggestion = None
        if child.pdf_report_path and os.path.exists(child.pdf_report_path):
            try:
                os.remove(child.pdf_report_path)
            except:
                pass
        child.pdf_report_path = None
        child.pdf_generated_at = None
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': '找不到該小孩檔案'})

@app.route('/delete_account', methods=['POST'])
def delete_account():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    user = User.query.get(session['user_id'])
    if user:
        children = Child.query.filter_by(user_id=user.id).all()
        for child in children:
            if child.pdf_report_path and os.path.exists(child.pdf_report_path):
                try:
                    os.remove(child.pdf_report_path)
                except:
                    pass
        db.session.delete(user)
        db.session.commit()
        session.clear()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': '找不到該帳號'})

@app.route('/update_user_profile', methods=['POST'])
def update_user_profile():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    data = request.get_json()
    user = User.query.get(session['user_id'])

    if user:
        new_username = data.get('username')
        new_email = data.get('email')
        new_password = data.get('password')

        if new_username != user.username:
            if User.query.filter_by(username=new_username).first():
                return jsonify({'success': False, 'message': '使用者名稱已被使用'})
        if new_email != user.email:
            if User.query.filter_by(email=new_email).first():
                return jsonify({'success': False, 'message': '電子郵件已被使用'})

        user.username = new_username
        user.email = new_email
        if new_password:
            user.password_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')

        db.session.commit()
        session['username'] = new_username
        return jsonify({'success': True, 'message': '資料更新成功'})
    return jsonify({'success': False, 'message': '找不到使用者'})

@app.route('/update_child_profile', methods=['POST'])
def update_child_profile():
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': '請先登入'})

    data = request.get_json()
    child_id = data.get('child_id')
    child = Child.query.filter_by(id=child_id, user_id=session['user_id']).first()

    if child:
        age = data.get('age')
        try:
            age = int(age)
            if age < 6 or age > 18:
                return jsonify({'success': False, 'message': '年齡必須在6-18歲之間'})
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': '請輸入有效的年齡'})

        child.nickname = data.get('nickname')
        child.gender = data.get('gender')
        child.age = age
        child.education_stage = data.get('education_stage')

        child.ai_suggestion = None
        if child.pdf_report_path and os.path.exists(child.pdf_report_path):
            try:
                os.remove(child.pdf_report_path)
            except:
                pass
        child.pdf_report_path = None
        child.pdf_generated_at = None

        db.session.commit()
        if session.get('child_id') == child_id:
            session['child_nickname'] = child.nickname
        return jsonify({'success': True, 'message': '小孩資料更新成功'})
    return jsonify({'success': False, 'message': '找不到小孩檔案'})

def prepare_chart_data(study_sessions):
    chart_data = {
        'subjects': [],
        'attention_scores': [],
        'study_times': [],
        'dates': [],
        'attention_trend': [],
        'subject_colors': []  # 新增：科目對應的顏色
    }
    
    # 科目顏色映射（與圓餅圖一致）
    COLOR_MAP = {
        'math': '#42a5f5',
        'science': '#66bb6a',
        'language': '#ef5350',
        'social': '#ffb74d',
        'art': '#ab47bc',
        'cs': '#26c6da'
    }
    
    subject_stats = {}
    for s in study_sessions:
        if s.subject not in subject_stats:
            subject_stats[s.subject] = {
                'total_time': 0,
                'avg_attention': 0,
                'count': 0,
                'attention_count': 0
            }
        subject_stats[s.subject]['total_time'] += s.duration_minutes
        subject_stats[s.subject]['count'] += 1
        if s.avg_attention:
            subject_stats[s.subject]['avg_attention'] += s.avg_attention
            subject_stats[s.subject]['attention_count'] += 1
        # if s.avg_attention is not None:
        #     subject_stats[s.subject]['avg_attention'] += s.avg_attention
        #     subject_stats[s.subject]['attention_count'] += 1

    for subject, stats in subject_stats.items():
        chart_data['subjects'].append(SUBJECTS.get(subject, subject))
        chart_data['study_times'].append(stats['total_time'])
        chart_data['subject_colors'].append(COLOR_MAP.get(subject, '#95a5a6'))  # 新增顏色
        
        if stats['attention_count'] > 0:
            avg = stats['avg_attention'] / stats['attention_count']
            chart_data['attention_scores'].append(round(avg * 100 / 3))
        else:
            chart_data['attention_scores'].append(0)

    recent = sorted(study_sessions, key=lambda x: x.start_time)[-10:]
    for s in recent:
        chart_data['dates'].append(s.start_time.strftime('%m/%d'))
        chart_data['attention_trend'].append(round(s.avg_attention * 100 / 3) if s.avg_attention else 0)
    
    return chart_data

'''
def prepare_chart_data(study_sessions):
    chart_data = {'subjects': [], 'attention_scores': [], 'study_times': [], 'dates': [], 'attention_trend': []}
    subject_stats = {}
    for s in study_sessions:
        if s.subject not in subject_stats:
            subject_stats[s.subject] = {'total_time': 0, 'avg_attention': 0, 'count': 0}
        subject_stats[s.subject]['total_time'] += s.duration_minutes
        if s.avg_attention:
            subject_stats[s.subject]['avg_attention'] += s.avg_attention
            subject_stats[s.subject]['count'] += 1

    for subject, stats in subject_stats.items():
        chart_data['subjects'].append(SUBJECTS.get(subject, subject))
        chart_data['study_times'].append(stats['total_time'])
        if stats['count'] > 0:
            avg = stats['avg_attention'] / stats['count']
            chart_data['attention_scores'].append(round(avg * 100 / 3))
        else:
            chart_data['attention_scores'].append(0)

    recent = sorted(study_sessions, key=lambda x: x.start_time)[-10:]
    for s in recent:
        chart_data['dates'].append(s.start_time.strftime('%m/%d'))
        chart_data['attention_trend'].append(round(s.avg_attention * 100 / 3) if s.avg_attention else 0)
    return chart_data'''

def prepare_performance_data(study_sessions):
    # data = {
    #     'total_sessions': len(study_sessions),
    #     'total_hours': sum(s.duration_minutes for s in study_sessions) / 60,
    #     'avg_attention': 0,
    #     'best_subject': '',
    #     'improvement_rate': 0
    # }
    eligible = [s for s in study_sessions if is_eligible_session(s)]
    data = {
        'total_sessions': len(eligible),
        'total_hours': sum(s.duration_minutes or 0 for s in eligible) / 60,
        'avg_attention': compute_overall_avg_attention_percent(eligible),  # ← 統一
        'best_subject': '',
        'improvement_rate': 0
    }

    if not study_sessions:
        return data

    # # 平均專注度（百分比）
    # att = [s.avg_attention for s in study_sessions if s.avg_attention is not None]
    # if att:
    #     data['avg_attention'] = round(sum(att) / len(att) * 100 / 3)
    # data['avg_attention'] = compute_overall_avg_attention_percent(study_sessions)

    # ★ 以總學習時間決定「最常學習科目」
    time_by_subject = {}
    for s in study_sessions:
        time_by_subject[s.subject] = time_by_subject.get(s.subject, 0) + (s.duration_minutes or 0)
    if time_by_subject:
        best_key = max(time_by_subject.items(), key=lambda kv: kv[1])[0]
        data['best_subject'] = SUBJECTS.get(best_key, best_key)

    # att = [s.avg_attention for s in study_sessions if s.avg_attention is not None]
    # # 進步幅度（用最近 3 場 vs 最早 3 場）
    # if len(att) >= 5:
    #     early = sum(att[:3]) / 3
    #     recent = sum(att[-3:]) / 3
    #     if early:
    #         data['improvement_rate'] = round((recent - early) / early * 100)
    # att = [s.avg_attention for s in eligible if s.avg_attention is not None]
    att = [s.avg_attention for s in eligible if s.avg_attention]
    if len(att) >= 5:
        early = sum(att[:3]) / 3
        recent = sum(att[-3:]) / 3
        if early:
            data['improvement_rate'] = round((recent - early) / early * 100)

    return data

''' # 10/07 12:06 註解
def prepare_performance_data(study_sessions):
    data = {
        'total_sessions': len(study_sessions),
        'total_hours': sum(s.duration_minutes for s in study_sessions) / 60,
        'avg_attention': 0,
        'best_subject': '',
        'improvement_rate': 0
    }
    
    if study_sessions:
        # 計算平均專注度
        att = [s for s in study_sessions if s.avg_attention]
        if att:
            data['avg_attention'] = round(sum(s.avg_attention for s in att) / len(att) * 100 / 3)
        
        # 找出最常學習的科目（以學習次數為準）
        subject_count = {}
        subject_attention = {}
        for s in study_sessions:
            subject_count[s.subject] = subject_count.get(s.subject, 0) + 1
            if s.avg_attention:
                if s.subject not in subject_attention:
                    subject_attention[s.subject] = []
                subject_attention[s.subject].append(s.avg_attention)
        
        # 找出學習次數最多的科目
        if subject_count:
            best_subject_key = max(subject_count.items(), key=lambda x: x[1])[0]
            data['best_subject'] = SUBJECTS.get(best_subject_key, best_subject_key)
        
        # 計算進步幅度
        if len(att) >= 5:
            early = sum(s.avg_attention for s in att[:3]) / 3
            recent = sum(s.avg_attention for s in att[-3:]) / 3
            if early:
                data['improvement_rate'] = round((recent - early) / early * 100)
    
    return data
'''

'''
def prepare_performance_data(study_sessions):
    data = {'total_sessions': len(study_sessions), 'total_hours': sum(s.duration_minutes for s in study_sessions) / 60,
            'avg_attention': 0, 'best_subject': '', 'improvement_rate': 0}
    if study_sessions:
        att = [s for s in study_sessions if s.avg_attention]
        if att:
            data['avg_attention'] = round(sum(s.avg_attention for s in att) / len(att) * 100 / 3)
        subject_perf = {}
        for s in study_sessions:
            if s.avg_attention:
                subject_perf.setdefault(s.subject, []).append(s.avg_attention)
        if subject_perf:
            best_subject = max(subject_perf.items(), key=lambda x: sum(x[1])/len(x[1]))
            data['best_subject'] = SUBJECTS.get(best_subject[0], best_subject[0])
        if len(att) >= 5:
            early = sum(s.avg_attention for s in att[:3]) / 3
            recent = sum(s.avg_attention for s in att[-3:]) / 3
            if early:
                data['improvement_rate'] = round((recent - early) / early * 100)
    return data'''

def generate_comprehensive_suggestions(child, study_sessions):
    suggestions = {'learning_style': [], 'schedule': [], 'subject_specific': [], 'attention_improvement': [], 'age_appropriate': []}
    suggestions['age_appropriate'].append("建議使用番茄鐘技巧：學習25分鐘，休息5分鐘，有助於維持專注力")

    if child.education_stage == 'elementary':
        if child.age <= 8:
            suggestions['age_appropriate'].append("年齡較小，建議搭配互動式學習活動和獎勵制度增加學習動機")
        else:
            suggestions['age_appropriate'].append("可以鼓勵自主選擇學習主題，提升學習興趣和責任感")
    elif child.education_stage == 'middle':
        suggestions['age_appropriate'].append("國中階段需要更多自主學習空間，建議設定明確的學習目標")
        suggestions['age_appropriate'].append("可以開始培養時間管理和學習計畫的能力")
    else:
        suggestions['age_appropriate'].append("高中生需要更強的自律性，建議制定長期學習計畫")
        suggestions['age_appropriate'].append("可使用思維導圖、康乃爾筆記法等工具提高效率")

    if child.gender == 'female':
        suggestions['learning_style'].append("可以考慮與朋友一起學習，合作學習環境有助於學習效果")
    else:
        suggestions['learning_style'].append("可以設定挑戰性目標，競爭性學習環境較能激發學習動力")

    if study_sessions:
        attention_sessions = [s for s in study_sessions if s.avg_attention]
        if attention_sessions:
            avg_attention = sum(s.avg_attention for s in attention_sessions) / len(attention_sessions)
            if avg_attention < 1.5:
                suggestions['attention_improvement'].append("專注度偏低，建議檢查學習環境是否有干擾因素")
                suggestions['attention_improvement'].append("可以嘗試使用白噪音或輕音樂幫助集中注意力")
                suggestions['schedule'].append("建議縮短每次學習時間至15-20分鐘，增加休息頻率")
            elif avg_attention < 2.5:
                suggestions['attention_improvement'].append("專注度中等，學前做5分鐘伸展與深呼吸")
                suggestions['schedule'].append("維持25分鐘學習、5分鐘休息的節奏")
            else:
                suggestions['attention_improvement'].append("專注度佳，可嘗試更具挑戰的內容")
                suggestions['schedule'].append("可延長單次學習至30-35分鐘，仍需適當休息")

        study_hours = {}
        for s in study_sessions:
            study_hours.setdefault(s.start_time.hour, [])
            if s.avg_attention:
                study_hours[s.start_time.hour].append(s.avg_attention)
        if study_hours:
            best_hour, values = max(study_hours.items(), key=lambda x: sum(x[1])/len(x[1]) if x[1] else 0)
            if 6 <= best_hour < 9:
                suggestions['schedule'].append("早上(6-9點)表現最佳，重要科目安排在這時段")
            elif 9 <= best_hour < 12:
                suggestions['schedule'].append("上午(9-12點)表現最佳，重要科目安排在這時段")
            elif 14 <= best_hour < 17:
                suggestions['schedule'].append("下午(14-17點)表現最佳，重要科目安排在這時段")
            elif 19 <= best_hour < 22:
                suggestions['schedule'].append("晚上(19-22點)表現最佳，重要科目安排在這時段")

            if child.age <= 10:
                suggestions['schedule'].append("避免在晚上8點後進行高度專注的學習")
            elif child.age <= 15:
                suggestions['schedule'].append("建議在晚上9點前完成主要學習任務")
            else:
                suggestions['schedule'].append("可適度延長晚間學習，但務必確保睡眠")

        subject_performance = {}
        for s in study_sessions:
            if s.avg_attention:
                subject_performance.setdefault(s.subject, []).append(s.avg_attention)

        overall_avg = (sum(s.avg_attention for s in attention_sessions) / len(attention_sessions)) if attention_sessions else 2.0
        attention_level = "high" if overall_avg >= 2.5 else "medium" if overall_avg >= 1.5 else "low"

        def get_subject_improvement_suggestion(subject, age, gender, education_stage, attention_level):
            base = {
                'math': {'elementary': "用教具與遊戲幫助理解", 'middle': "先鞏固基礎概念再做題", 'high': "分解大題成小步驟"},
                'science': {'elementary': "多做小實驗增加興趣", 'middle': "用概念圖整理知識", 'high': "連結生活情境找應用"},
                'language': {'elementary': "每日短時閱讀與說故事", 'middle': "從興趣書單建立自信", 'high': "小目標閱讀並寫心得"},
                'social': {'elementary': "故事與遊戲帶入歷地", 'middle': "用時間軸和地圖輔助", 'high': "從興趣主題切入擴展"},
                'art': {'elementary': "短時創作避免疲勞", 'middle': "嘗試多元媒材", 'high': "訂定每週完成一件作品"},
                'cs': {'elementary': "從Scratch等視覺化入門", 'middle': "以小專題實作提升動機", 'high': "選一語言深入練習"}
            }.get(subject, {}).get(education_stage, "增加練習時間並找出卡點")
            return base

        def get_subject_excellence_suggestion(subject, age, gender, education_stage, attention_level):
            adv = {
                'math': {'elementary': "挑戰趣味題培養思維", 'middle': "可接觸競賽題型", 'high': "先修微積分/統計"},
                'science': {'elementary': "做小型觀察研究", 'middle': "參與科展或專題", 'high': "規劃進階課程或實驗"},
                'language': {'elementary': "嘗試演講或戲劇", 'middle': "參與寫作/校刊", 'high': "閱讀經典強化分析"},
                'social': {'elementary': "關注時事培養觀點", 'middle': "參與辯論/模聯", 'high': "做社科小研究"},
                'art': {'elementary': "參加展演累積作品", 'middle': "研究名家與風格", 'high': "整理作品集"},
                'cs': {'elementary': "做小遊戲或動畫", 'middle': "學進階語言與競賽", 'high': "參與開源與實作"}
            }.get(subject, {}).get(education_stage, "加深加廣，協助同儕學習")
            return adv

        for subject, perf in subject_performance.items():
            avg_perf = sum(perf) / len(perf)
            name = SUBJECTS.get(subject, subject)
            if avg_perf < 2:
                suggestions['subject_specific'].append(f"{name}需要加強，{get_subject_improvement_suggestion(subject, child.age, child.gender, child.education_stage, attention_level)}")
            else:
                suggestions['subject_specific'].append(f"{name}表現良好，{get_subject_excellence_suggestion(subject, child.age, child.gender, child.education_stage, attention_level)}")

    return suggestions

def create_comprehensive_report(child, study_sessions, ai_suggestion=None):
    filename = f'report_{child.id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
    filepath = os.path.join('reports', filename)
    os.makedirs('reports', exist_ok=True)

    doc = SimpleDocTemplate(filepath, pagesize=A4, topMargin=0.5*inch, bottomMargin=0.5*inch)
    story = []
    styles = getSampleStyleSheet()
    font_name = PDF_FONT if PDF_FONT and PDF_FONT != 'Helvetica' else 'Helvetica'

    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'],
                                 fontName=font_name, fontSize=24, textColor=colors.HexColor('#2C3E50'),
                                 alignment=TA_CENTER, spaceAfter=30)
    heading_style = ParagraphStyle('CustomHeading', parent=styles['Heading1'],
                                   fontName=font_name, fontSize=16, textColor=colors.HexColor('#34495E'),
                                   spaceAfter=12, spaceBefore=20)
    sub_heading_style = ParagraphStyle('SubHeading', parent=styles['Heading2'],
                                       fontName=font_name, fontSize=14, textColor=colors.HexColor('#34495E'),
                                       spaceAfter=8, spaceBefore=15, alignment=TA_LEFT)
    normal_style = ParagraphStyle('CustomNormal', parent=styles['Normal'],
                                  fontName=font_name, fontSize=12, leading=18)

    story.append(Paragraph('學習評估報告', title_style))
    story.append(Spacer(1, 30))
    basic_info = [
        ['姓名', child.nickname],
        ['性別', GENDERS.get(child.gender, child.gender)],
        ['年齡', str(child.age)],
        ['教育階段', EDUCATION_STAGES.get(child.education_stage, child.education_stage)],
        ['報告日期', datetime.now().strftime('%Y-%m-%d')],
        ['總學習次數', str(len(study_sessions))]
    ]
    info_table = Table(basic_info, colWidths=[2.5*inch, 3.5*inch])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#ECF0F1')),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#2C3E50')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('FONTSIZE', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#BDC3C7'))
    ]))
    story.append(info_table)
    story.append(Spacer(1, 20))

    if ai_suggestion:
        story.append(Paragraph('AI個人化建議', heading_style))
        story.append(Spacer(1, 6))
        story.append(Paragraph(ai_suggestion.replace('\n', '<br/>'), normal_style))
        story.append(Spacer(1, 12))

    story.append(PageBreak())

    story.append(Paragraph('數據分析', heading_style))
    story.append(Spacer(1, 20))

    if study_sessions:
        total_minutes = sum(s.duration_minutes for s in study_sessions)
        total_hours = total_minutes / 60
        attention_sessions = [s for s in study_sessions if s.avg_attention]
        avg_attention_percent = round((sum(s.avg_attention for s in attention_sessions) / len(attention_sessions)) * 100 / 3) if attention_sessions else 0

        stats_data = [
            ['總學習時間', f'{total_hours:.1f} 小時 ({total_minutes} 分鐘)'],
            ['平均專注度', f'{avg_attention_percent}%'],
            ['學習頻率', f'{len(study_sessions)} 次'],
            ['平均學習時長', f'{total_minutes/len(study_sessions):.1f} 分鐘']
        ]
        stats_table = Table(stats_data, colWidths=[3*inch, 3*inch])
        stats_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#E8F4F8')),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#2C3E50')),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#3498DB'))
        ]))
        story.append(stats_table)
        story.append(PageBreak())

        subject_stats = {}
        for s in study_sessions:
            if s.subject not in subject_stats:
                subject_stats[s.subject] = {'count': 0, 'total_time': 0, 'attention_sum': 0, 'attention_count': 0}
            subject_stats[s.subject]['count'] += 1
            subject_stats[s.subject]['total_time'] += s.duration_minutes
            if s.avg_attention:
                subject_stats[s.subject]['attention_sum'] += s.avg_attention
                subject_stats[s.subject]['attention_count'] += 1

        story.append(Paragraph('科目表現分析', heading_style))
        story.append(Spacer(1, 20))

        if subject_stats:
            subject_data = [['科目', '學習次數', '總時間', '平均專注度']]
            for subject, stats in subject_stats.items():
                subject_name = SUBJECTS.get(subject, subject)
                avg_att = round(stats['attention_sum'] / stats['attention_count'] * 100 / 3) if stats['attention_count'] > 0 else 0
                subject_data.append([subject_name, str(stats['count']), f"{stats['total_time']} 分鐘", f"{avg_att}%"])

            subject_table = Table(subject_data, colWidths=[2*inch, 1.5*inch, 1.5*inch, 1.5*inch])
            subject_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3498DB')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, -1), font_name),
                ('FONTSIZE', (0, 0), (-1, -1), 11),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
                ('TOPPADDING', (0, 0), (-1, -1), 10),
                ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ECF0F1')),
                ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#95A5A6'))
            ]))
            story.append(subject_table)
            story.append(Spacer(1, 20))

            pie_data, pie_labels = [], []
            pie_colors = [colors.HexColor('#42A5F5'), colors.HexColor('#66BB6A'), colors.HexColor('#EF5350'),
                          colors.HexColor('#FFB74D'), colors.HexColor('#AB47BC'), colors.HexColor('#26C6DA')]
            for subject, stats in subject_stats.items():
                subject_name = SUBJECTS.get(subject, subject)
                pie_data.append(stats['total_time'])
                pie_labels.append(subject_name)

            story.append(Paragraph('科目學習時間分布', sub_heading_style))
            story.append(Spacer(1, 5))
            drawing1 = Drawing(500, 400)
            pie = Pie()
            pie.x, pie.y, pie.width, pie.height = 80, 70, 250, 250
            pie.data, pie.labels = pie_data, pie_labels
            pie.slices.strokeColor = colors.white
            pie.slices.strokeWidth = 2
            pie.slices.fontName = font_name
            pie.slices.fontSize = 12
            pie.slices.fontColor = colors.black
            pie.slices.labelRadius = 1.15
            pie.slices.popout = 3
            for i, c in enumerate(pie_colors[:len(pie_data)]):
                pie.slices[i].fillColor = c
            drawing1.add(pie)
            story.append(drawing1)
            story.append(Spacer(1, 15))

            story.append(Paragraph('科目專注度比較', sub_heading_style))
            story.append(Spacer(1, 5))
            drawing2 = Drawing(500, 240)
            bar_chart = VerticalBarChart()
            bar_chart.x, bar_chart.y, bar_chart.height, bar_chart.width = 60, 20, 180, 380
            bar_data, bar_labels = [], []
            for subject, stats in subject_stats.items():
                subject_name = SUBJECTS.get(subject, subject)
                avg_attention = round(stats['attention_sum'] / stats['attention_count'] * 100 / 3) if stats['attention_count'] > 0 else 0
                bar_data.append(avg_attention)
                bar_labels.append(subject_name)
            bar_chart.data = [bar_data]
            bar_chart.categoryAxis.categoryNames = bar_labels
            bar_chart.categoryAxis.labels.fontName = font_name
            bar_chart.categoryAxis.labels.fontSize = 12
            bar_chart.valueAxis.valueMin = 0
            bar_chart.valueAxis.valueMax = 100
            bar_chart.valueAxis.labels.fontName = font_name
            bar_chart.valueAxis.labels.fontSize = 10
            bar_chart.bars[0].fillColor = colors.HexColor('#66BB6A')
            bar_chart.bars[0].strokeColor = colors.white
            bar_chart.bars[0].strokeWidth = 1
            drawing2.add(bar_chart)
            story.append(drawing2)
            story.append(Spacer(1, 20))

        story.append(PageBreak())

    story.append(Paragraph('個人化學習建議', heading_style))
    story.append(Spacer(1, 20))
    suggestions = generate_comprehensive_suggestions(child, study_sessions)
    category_names = {
        'age_appropriate': '年齡適性建議',
        'learning_style': '學習風格建議',
        'schedule': '時間規劃優化',
        'attention_improvement': '專注力提升建議',
        'subject_specific': '科目專屬建議'
    }
    suggestion_heading_style = ParagraphStyle('SuggestionHeading', parent=heading_style, fontSize=14, spaceAfter=8, spaceBefore=15)
    for category, items in suggestions.items():
        if items:
            story.append(Paragraph(category_names.get(category, category), suggestion_heading_style))
            story.append(Spacer(1, 8))
            for item in items:
                story.append(Paragraph(f"• {item}", normal_style))
                story.append(Spacer(1, 6))
            story.append(Spacer(1, 15))

    doc.build(story)
    return filepath

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/favicon.ico')
def favicon():
    static_dir = os.path.join(app.root_path, 'static')
    icon_path = os.path.join(static_dir, 'favicon.ico')
    if os.path.exists(icon_path):
        return send_from_directory(static_dir, 'favicon.ico', mimetype='image/vnd.microsoft.icon')
    from flask import Response
    return Response(status=204)

# --- COOP/COEP: 讓 SIMD WASM 可用（必做） ---
@app.after_request
def add_coop_coep(resp):
    # 讓當前頁成為「跨源隔離」的 opener/嵌入者
    resp.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    resp.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
    # 我們自己的靜態資源也允許被跨源嵌入（保險用）
    resp.headers['Cross-Origin-Resource-Policy'] = 'cross-origin'
    return resp

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
