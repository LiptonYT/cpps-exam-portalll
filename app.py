import os, sqlite3, secrets, json, re
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "cpps.sqlite3"))
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                 SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
                 MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))))
VIDEO_UPLOAD_DIR = os.path.join(APP_DIR, "static", "uploads", "lectures")
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "ogg", "mov"}

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'cadet', created_at TEXT NOT NULL,
      full_name TEXT NOT NULL DEFAULT '', cadet_static TEXT NOT NULL DEFAULT '',
      password_set INTEGER NOT NULL DEFAULT 0, academy_passed INTEGER NOT NULL DEFAULT 0, access_blocked INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tests(
      id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
      duration_minutes INTEGER NOT NULL DEFAULT 20, pass_percent INTEGER NOT NULL DEFAULT 70,
      max_attempts INTEGER NOT NULL DEFAULT 2, published INTEGER NOT NULL DEFAULT 0,
      event_type TEXT NOT NULL DEFAULT 'exam',
      created_by INTEGER, created_at TEXT NOT NULL,
      FOREIGN KEY(created_by) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS teacher_presence(
      user_id INTEGER PRIMARY KEY, last_seen TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS questions(
      id INTEGER PRIMARY KEY, test_id INTEGER NOT NULL, prompt TEXT NOT NULL,
      a TEXT NOT NULL, b TEXT NOT NULL, c TEXT NOT NULL, d TEXT NOT NULL,
      correct TEXT NOT NULL CHECK(correct IN ('A','B','C','D')),
      question_type TEXT NOT NULL DEFAULT 'single',
      correct_answers TEXT NOT NULL DEFAULT '[]',
      FOREIGN KEY(test_id) REFERENCES tests(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS attempts(
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, test_id INTEGER NOT NULL,
      started_at TEXT NOT NULL, finished_at TEXT, score INTEGER, total INTEGER,
      passed INTEGER, answers_json TEXT DEFAULT '{}',
      cadet_full_name TEXT NOT NULL DEFAULT '', cadet_static TEXT NOT NULL DEFAULT '',
      examiner_full_name TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(test_id) REFERENCES tests(id));
    CREATE TABLE IF NOT EXISTS internships(
      id INTEGER PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS internship_assignments(
      id INTEGER PRIMARY KEY, internship_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'assigned', notes TEXT DEFAULT '', assigned_at TEXT NOT NULL,
      UNIQUE(internship_id,user_id),
      FOREIGN KEY(internship_id) REFERENCES internships(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS support_chats(
      id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS support_messages(
      id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
      message TEXT NOT NULL, created_at TEXT NOT NULL,
      FOREIGN KEY(chat_id) REFERENCES support_chats(id) ON DELETE CASCADE,
      FOREIGN KEY(sender_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS lessons(
      id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
      video_url TEXT NOT NULL, course INTEGER NOT NULL CHECK(course IN (1,2)),
      published INTEGER NOT NULL DEFAULT 1, created_by INTEGER, created_at TEXT NOT NULL,
      FOREIGN KEY(created_by) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS lesson_views(
      id INTEGER PRIMARY KEY, lesson_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
      viewed_at TEXT NOT NULL, UNIQUE(lesson_id,user_id),
      FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    """)
    # Миграция: статусы академии и блокировки аккаунта без потери существующих данных.
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "academy_passed" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN academy_passed INTEGER NOT NULL DEFAULT 0")
    if "access_blocked" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN access_blocked INTEGER NOT NULL DEFAULT 0")
    if "full_name" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
    if "cadet_static" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN cadet_static TEXT NOT NULL DEFAULT ''")
    if "password_set" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_set INTEGER NOT NULL DEFAULT 0")

    # Миграция: тип мероприятия добавляется без удаления существующих экзаменов.
    test_columns = {row[1] for row in conn.execute("PRAGMA table_info(tests)").fetchall()}
    if "event_type" not in test_columns:
        conn.execute("ALTER TABLE tests ADD COLUMN event_type TEXT NOT NULL DEFAULT 'exam'")
    # Миграция существующей базы: добавляем поддержку нескольких правильных ответов
    # без удаления существующих вопросов, экзаменов или результатов.
    question_columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()}
    if "question_type" not in question_columns:
        conn.execute("ALTER TABLE questions ADD COLUMN question_type TEXT NOT NULL DEFAULT 'single'")
    if "correct_answers" not in question_columns:
        conn.execute("ALTER TABLE questions ADD COLUMN correct_answers TEXT NOT NULL DEFAULT '[]'")
    # Миграция существующей базы: добавляем данные экзамена без удаления старых результатов.
    attempt_columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    for column in ("cadet_full_name", "cadet_static", "examiner_full_name"):
        if column not in attempt_columns:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    admin_name = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    existing = conn.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not existing and admin_password:
        conn.execute("INSERT OR IGNORE INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                     (admin_name, generate_password_hash(admin_password), "admin", now()))
    conn.commit()
    conn.close()

init_db()

def lesson_video_kind(url):
    path = urlparse(url or "").path.lower()
    return "file" if path.endswith((".mp4", ".webm", ".ogg", ".mov")) else "embed"

def lesson_embed_url(url):
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower().split(":")[0]
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0]
        return "https://www.youtube.com/embed/" + video_id if video_id else url
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            return "https://www.youtube.com/embed/" + video_id if video_id else url
        if parsed.path.startswith(("/shorts/", "/live/")):
            video_id = parsed.path.split("/")[2] if len(parsed.path.split("/")) > 2 else ""
            return "https://www.youtube.com/embed/" + video_id if video_id else url
        if parsed.path.startswith("/embed/"):
            return url
    if host.endswith("vimeo.com") and host != "player.vimeo.com":
        video_id = parsed.path.strip("/").split("/")[0]
        return "https://player.vimeo.com/video/" + video_id if video_id.isdigit() else url
    return url

app.jinja_env.globals["lesson_video_kind"] = lesson_video_kind
app.jinja_env.globals["lesson_embed_url"] = lesson_embed_url

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]
app.jinja_env.globals["csrf_token"] = csrf_token

@app.before_request
def enforce_account_block():
    """Завершает сессию, если руководство заблокировало аккаунт."""
    uid = session.get("user_id")
    if not uid:
        return None
    user = db().execute("SELECT access_blocked FROM users WHERE id=?", (uid,)).fetchone()
    if not user or user["access_blocked"]:
        session.clear()
        flash("Доступ к сайту заблокирован руководством ЦППС. Обратитесь к начальству.", "danger")
        return redirect(url_for("login"))
    return None

@app.before_request
def protect_posts():
    if request.method == "POST":
        token = request.form.get("_csrf", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            abort(400, "Сессия формы истекла. Обновите страницу и повторите действие.")

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db().execute("SELECT id,username,role,created_at,full_name,cadet_static,academy_passed,access_blocked FROM users WHERE id=?", (uid,)).fetchone()

@app.context_processor
def inject_user():
    return {"current_user": current_user()}

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Сначала войдите в аккаунт.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            flash("Войдите в аккаунт администратора.", "warning")
            return redirect(url_for("login"))
        if u["role"] != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapped

def safe_int(value, default, low, high):
    try: n = int(value)
    except (TypeError, ValueError): return default
    return max(low, min(high, n))

@app.route("/")
def index():
    tests = db().execute("""SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) qcount
                            FROM tests t WHERE t.published=1 ORDER BY t.created_at DESC LIMIT 8""").fetchall()
    internships = db().execute("SELECT * FROM internships WHERE status='active' ORDER BY id DESC LIMIT 4").fetchall()
    return render_template("index.html", tests=tests, internships=internships)

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        cadet_static = request.form.get("cadet_static", "").strip()
        password = request.form.get("password", "")
        full_name = f"{first_name} {last_name}".strip()
        name_pattern = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё -]{0,39}$")
        static_pattern = re.compile(r"^[0-9]{1,10}(?:-[0-9]{1,10})?$")
        if not name_pattern.fullmatch(first_name) or not name_pattern.fullmatch(last_name):
            flash("Введите имя и фамилию русскими буквами.", "danger")
        elif not static_pattern.fullmatch(cadet_static):
            flash("Введите статик цифрами, например 855-024.", "danger")
        elif len(password) < 8:
            flash("Пароль должен содержать минимум 8 символов.", "danger")
        elif db().execute("SELECT 1 FROM users WHERE cadet_static=? AND cadet_static!=''", (cadet_static,)).fetchone():
            flash("Этот статик уже зарегистрирован.", "danger")
        else:
            internal_username = "cadet_" + cadet_static.replace("-", "_")
            try:
                db().execute("""INSERT INTO users(username,password_hash,role,created_at,full_name,cadet_static,password_set)
                    VALUES(?,?,?,?,?,?,1)""",
                    (internal_username, generate_password_hash(password), "cadet", now(), full_name, cadet_static))
                db().commit()
                flash("Регистрация завершена. Теперь войдите по статику и паролю.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("Не удалось зарегистрировать этот статик. Проверьте, не зарегистрирован ли он уже.", "danger")
    return render_template("auth.html", mode="register")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        cadet_static = request.form.get("cadet_static", "").strip()
        cadet_password = request.form.get("cadet_password", "")
        admin_username = request.form.get("admin_username", "").strip()
        admin_password = request.form.get("admin_password", "")
        user = None
        if admin_username and admin_password:
            user = db().execute("SELECT * FROM users WHERE username=? AND role='admin'", (admin_username,)).fetchone()
            if not user or not check_password_hash(user["password_hash"], admin_password):
                user = None
        elif cadet_static and cadet_password:
            user = db().execute("SELECT * FROM users WHERE cadet_static=? AND role!='admin'", (cadet_static,)).fetchone()
            if user:
                # Старые аккаунты, созданные до добавления пароля, получают пароль при первом входе.
                if not user["password_set"]:
                    if len(cadet_password) < 8:
                        flash("Для первого входа задайте пароль минимум из 8 символов.", "danger")
                        return render_template("auth.html", mode="login")
                    db().execute("UPDATE users SET password_hash=?, password_set=1 WHERE id=?",
                                 (generate_password_hash(cadet_password), user["id"]))
                    db().commit()
                    user = db().execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
                elif not check_password_hash(user["password_hash"], cadet_password):
                    user = None
        if user:
            if user["access_blocked"]:
                flash("Доступ к сайту заблокирован, так как академия пройдена или доступ закрыт руководством ЦППС. Обратитесь к начальству.", "danger")
                return render_template("auth.html", mode="login")
            session.clear()
            session["user_id"] = user["id"]
            csrf_token()
            return redirect(request.args.get("next") if request.args.get("next", "").startswith("/") else url_for("dashboard"))
        flash("Проверьте статик и пароль либо данные администратора.", "danger")
    return render_template("auth.html", mode="login")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("Вы вышли из аккаунта.", "success")
    return redirect(url_for("index"))

@app.route("/teacher/presence")
@login_required
def teacher_presence():
    user = current_user()
    if user["role"] not in ("teacher", "admin"):
        abort(403)
    db().execute("INSERT INTO teacher_presence(user_id,last_seen) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen", (user["id"], now()))
    db().commit()
    return {"ok": True}

def get_teacher_statuses():
    """Возвращает статус преподавателей; онлайн означает активность за последние 75 секунд."""
    teachers = db().execute("""SELECT u.username,p.last_seen FROM users u
        LEFT JOIN teacher_presence p ON p.user_id=u.id
        WHERE u.role='teacher' ORDER BY u.username""").fetchall()
    current_time = datetime.now(timezone.utc)
    statuses = []
    for teacher in teachers:
        online = False
        if teacher["last_seen"]:
            try:
                seen = datetime.fromisoformat(teacher["last_seen"])
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                online = current_time - seen <= timedelta(seconds=75)
            except (ValueError, TypeError):
                pass
        statuses.append({"username": teacher["username"], "online": online})
    return statuses


@app.route("/dashboard")
@login_required
def dashboard():
    u = current_user()
    attempts = db().execute("""SELECT a.*,t.title FROM attempts a JOIN tests t ON t.id=a.test_id
        WHERE a.user_id=? ORDER BY a.id DESC LIMIT 10""", (u["id"],)).fetchall()
    assignments = db().execute("""SELECT ia.*,i.title,i.details FROM internship_assignments ia
        JOIN internships i ON i.id=ia.internship_id WHERE ia.user_id=? ORDER BY ia.id DESC""", (u["id"],)).fetchall()
    return render_template("dashboard.html", attempts=attempts, assignments=assignments, teacher_statuses=get_teacher_statuses())

@app.route("/tests")
def tests_list():
    tests = db().execute("""SELECT t.*, (SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) qcount
       FROM tests t WHERE t.published=1 ORDER BY t.id DESC""").fetchall()
    return render_template("tests.html", tests=tests, teacher_statuses=get_teacher_statuses())

@app.route("/test/<int:test_id>", methods=["GET","POST"])
@login_required
def take_test(test_id):
    conn = db()
    test = conn.execute("SELECT * FROM tests WHERE id=? AND published=1", (test_id,)).fetchone()
    if not test: abort(404)
    user = current_user()
    used = conn.execute("SELECT COUNT(*) n FROM attempts WHERE user_id=? AND test_id=? AND finished_at IS NOT NULL",
                        (user["id"], test_id)).fetchone()["n"]
    if request.method == "GET":
        if used >= test["max_attempts"]:
            flash("Лимит попыток для этого экзамена исчерпан.", "warning")
            return redirect(url_for("tests_list"))
        questions = conn.execute("SELECT id,prompt,a,b,c,d,question_type,correct_answers,correct FROM questions WHERE test_id=? ORDER BY id", (test_id,)).fetchall()
        if not questions:
            flash("В этом тесте пока нет вопросов.", "warning")
            return redirect(url_for("tests_list"))
        cur = conn.execute("INSERT INTO attempts(user_id,test_id,started_at) VALUES(?,?,?)",
                           (user["id"], test_id, now()))
        conn.commit()
        return render_template("take_test.html", test=test, questions=questions, attempt_id=cur.lastrowid)
    cadet_full_name = request.form.get("cadet_full_name", "").strip()
    cadet_static = request.form.get("cadet_static", "").strip()
    examiner_full_name = request.form.get("examiner_full_name", "").strip()
    if not cadet_full_name or not cadet_static or not examiner_full_name:
        flash("Заполните ФИО курсанта, статик и ФИО экзаменатора.", "danger")
        return redirect(url_for("take_test", test_id=test_id))
    if len(cadet_full_name) > 120 or len(cadet_static) > 40 or len(examiner_full_name) > 120:
        flash("Проверьте длину введённых данных.", "danger")
        return redirect(url_for("take_test", test_id=test_id))
    attempt = conn.execute("SELECT * FROM attempts WHERE id=? AND user_id=? AND test_id=? AND finished_at IS NULL",
                           (safe_int(request.form.get("attempt_id"),0,1,2**31-1), user["id"], test_id)).fetchone()
    if not attempt: abort(400, "Попытка уже завершена или не найдена.")
    started = datetime.fromisoformat(attempt["started_at"])
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    questions = conn.execute("SELECT * FROM questions WHERE test_id=? ORDER BY id", (test_id,)).fetchall()
    answers = {}
    score = 0
    for q in questions:
        if q["question_type"] == "multiple":
            selected = sorted(set(x for x in request.form.getlist(f"q_{q['id']}") if x in ("A","B","C","D")))
            if selected:
                answers[str(q["id"])] = selected
            try:
                expected = sorted(set(json.loads(q["correct_answers"] or "[]")))
            except (ValueError, TypeError):
                expected = [q["correct"]]
            if selected and selected == expected:
                score += 1
        else:
            ans = request.form.get(f"q_{q['id']}", "")
            if ans in ("A","B","C","D"):
                answers[str(q["id"])] = ans
                if ans == q["correct"]: score += 1
    total = len(questions)
    percent = round(score * 100 / total) if total else 0
    passed = int(percent >= test["pass_percent"] and elapsed <= test["duration_minutes"] * 60 + 10)
    if elapsed > test["duration_minutes"] * 60 + 10:
        flash("Время вышло. Результат сохранён, но экзамен не засчитан.", "warning")
    conn.execute("""UPDATE attempts
        SET finished_at=?, score=?, total=?, passed=?, answers_json=?,
            cadet_full_name=?, cadet_static=?, examiner_full_name=?
        WHERE id=?""",
        (now(), score, total, passed, json.dumps(answers), cadet_full_name,
         cadet_static, examiner_full_name, attempt["id"]))
    conn.commit()
    return redirect(url_for("attempt_result", attempt_id=attempt["id"]))

@app.route("/result/<int:attempt_id>")
@login_required
def attempt_result(attempt_id):
    u = current_user()
    attempt = db().execute("""SELECT a.*,t.title,t.pass_percent FROM attempts a JOIN tests t ON t.id=a.test_id
        WHERE a.id=?""", (attempt_id,)).fetchone()
    if not attempt: abort(404)
    if u["role"] != "admin" and attempt["user_id"] != u["id"]: abort(403)
    return render_template("result.html", attempt=attempt)

@app.route("/support", methods=["GET", "POST"])
@login_required
def support():
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_support"))
    conn = db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        if not title or not message:
            flash("Укажите тему обращения и сообщение.", "danger")
        elif len(title) > 100 or len(message) > 4000:
            flash("Тема — до 100 символов, сообщение — до 4000 символов.", "danger")
        else:
            cur = conn.execute("INSERT INTO support_chats(user_id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (user["id"], title, "open", now(), now()))
            conn.execute("INSERT INTO support_messages(chat_id,sender_id,message,created_at) VALUES(?,?,?,?)",
                          (cur.lastrowid, user["id"], message, now()))
            conn.commit()
            flash("Обращение отправлено начальству ЦППС.", "success")
            return redirect(url_for("support_chat", chat_id=cur.lastrowid))
    chats = conn.execute("""SELECT c.*,
        (SELECT message FROM support_messages m WHERE m.chat_id=c.id ORDER BY m.id DESC LIMIT 1) last_message,
        (SELECT COUNT(*) FROM support_messages m WHERE m.chat_id=c.id) message_count
        FROM support_chats c WHERE c.user_id=? ORDER BY c.updated_at DESC""", (user["id"],)).fetchall()
    return render_template("support.html", chats=chats, chat=None, messages=[])

@app.route("/support/<int:chat_id>", methods=["GET", "POST"])
@login_required
def support_chat(chat_id):
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_support_chat", chat_id=chat_id))
    conn = db()
    chat = conn.execute("SELECT * FROM support_chats WHERE id=? AND user_id=?", (chat_id, user["id"])).fetchone()
    if not chat:
        abort(404)
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            flash("Введите сообщение.", "danger")
        elif len(message) > 4000:
            flash("Сообщение не должно превышать 4000 символов.", "danger")
        elif chat["status"] == "closed":
            flash("Обращение закрыто. Создайте новое, если вопрос остался.", "warning")
        else:
            conn.execute("INSERT INTO support_messages(chat_id,sender_id,message,created_at) VALUES(?,?,?,?)",
                         (chat_id, user["id"], message, now()))
            conn.execute("UPDATE support_chats SET updated_at=? WHERE id=?", (now(), chat_id))
            conn.commit()
            return redirect(url_for("support_chat", chat_id=chat_id))
    messages = conn.execute("""SELECT m.*,u.username,u.role FROM support_messages m
        JOIN users u ON u.id=m.sender_id WHERE m.chat_id=? ORDER BY m.id""", (chat_id,)).fetchall()
    chats = conn.execute("SELECT * FROM support_chats WHERE user_id=? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return render_template("support.html", chats=chats, chat=chat, messages=messages)

@app.route("/support/api/<int:chat_id>/messages")
@login_required
def support_messages_api(chat_id):
    user = current_user()
    chat = db().execute("SELECT id,user_id FROM support_chats WHERE id=?", (chat_id,)).fetchone()
    if not chat:
        abort(404)
    if user["role"] != "admin" and chat["user_id"] != user["id"]:
        abort(403)
    try:
        after_id = max(0, int(request.args.get("after", "0")))
    except (TypeError, ValueError):
        after_id = 0
    rows = db().execute("""SELECT m.id,m.sender_id,m.message,m.created_at,u.username,u.role
        FROM support_messages m JOIN users u ON u.id=m.sender_id
        WHERE m.chat_id=? AND m.id>? ORDER BY m.id""", (chat_id, after_id)).fetchall()
    return jsonify([dict(row) for row in rows])

@app.route("/admin/support")
@admin_required
def admin_support():
    chats = db().execute("""SELECT c.*,u.username,
        (SELECT message FROM support_messages m WHERE m.chat_id=c.id ORDER BY m.id DESC LIMIT 1) last_message,
        (SELECT COUNT(*) FROM support_messages m WHERE m.chat_id=c.id) message_count
        FROM support_chats c JOIN users u ON u.id=c.user_id
        WHERE c.status='open' ORDER BY c.updated_at DESC""").fetchall()
    return render_template("support_admin.html", chats=chats, chat=None, messages=[])

@app.route("/admin/support/<int:chat_id>", methods=["GET", "POST"])
@admin_required
def admin_support_chat(chat_id):
    conn = db()
    chat = conn.execute("""SELECT c.*,u.username FROM support_chats c
        JOIN users u ON u.id=c.user_id WHERE c.id=?""", (chat_id,)).fetchone()
    if not chat:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action", "reply")
        if action == "status":
            status = request.form.get("status")
            if status in ("open", "closed"):
                conn.execute("UPDATE support_chats SET status=?,updated_at=? WHERE id=?", (status, now(), chat_id))
                conn.commit()
                flash("Обращение закрыто и убрано из списка поддержки." if status == "closed" else "Обращение открыто.", "success")
                if status == "closed":
                    return redirect(url_for("admin_support"))
                return redirect(url_for("admin_support_chat", chat_id=chat_id))
        else:
            message = request.form.get("message", "").strip()
            if not message:
                flash("Введите ответ курсанту.", "danger")
            elif len(message) > 4000:
                flash("Сообщение не должно превышать 4000 символов.", "danger")
            else:
                conn.execute("INSERT INTO support_messages(chat_id,sender_id,message,created_at) VALUES(?,?,?,?)",
                             (chat_id, current_user()["id"], message, now()))
                conn.execute("UPDATE support_chats SET status='open',updated_at=? WHERE id=?", (now(), chat_id))
                conn.commit()
                return redirect(url_for("admin_support_chat", chat_id=chat_id))
    messages = conn.execute("""SELECT m.*,u.username,u.role FROM support_messages m
        JOIN users u ON u.id=m.sender_id WHERE m.chat_id=? ORDER BY m.id""", (chat_id,)).fetchall()
    chats = conn.execute("""SELECT c.*,u.username,
        (SELECT message FROM support_messages m WHERE m.chat_id=c.id ORDER BY m.id DESC LIMIT 1) last_message,
        (SELECT COUNT(*) FROM support_messages m WHERE m.chat_id=c.id) message_count
        FROM support_chats c JOIN users u ON u.id=c.user_id
        WHERE c.status='open' ORDER BY c.updated_at DESC""").fetchall()
    return render_template("support_admin.html", chats=chats, chat=chat, messages=messages)

@app.route("/lectures")
@login_required
def lectures():
    conn = db()
    user = current_user()
    course_filter = request.args.get("course", "all")
    if course_filter not in ("all", "1", "2"):
        course_filter = "all"
    where = "WHERE l.published=1"
    params = []
    if course_filter in ("1", "2"):
        where += " AND l.course=?"
        params.append(int(course_filter))
    lessons = conn.execute(f"""SELECT l.*,
        (SELECT viewed_at FROM lesson_views lv WHERE lv.lesson_id=l.id AND lv.user_id=?) AS viewed_at,
        (SELECT COUNT(*) FROM lesson_views lv WHERE lv.lesson_id=l.id) AS viewed_count
        FROM lessons l {where} ORDER BY l.course,l.id DESC""", [user["id"], *params]).fetchall()
    viewers = []
    if user["role"] == "admin":
        viewers = conn.execute("""SELECT lv.lesson_id,l.title,l.course,u.username,lv.viewed_at
            FROM lesson_views lv JOIN lessons l ON l.id=lv.lesson_id
            JOIN users u ON u.id=lv.user_id ORDER BY l.course,l.id DESC,lv.viewed_at DESC""").fetchall()
        all_lessons = conn.execute("""SELECT l.*,
            (SELECT COUNT(*) FROM lesson_views lv WHERE lv.lesson_id=l.id) AS viewed_count
            FROM lessons l ORDER BY l.course,l.id DESC""").fetchall()
    else:
        all_lessons = []
    return render_template("lectures.html", lessons=lessons, course_filter=course_filter,
                           all_lessons=all_lessons, viewers=viewers)

@app.route("/lectures/<int:lesson_id>/viewed", methods=["POST"])
@login_required
def mark_lesson_viewed(lesson_id):
    conn = db()
    lesson = conn.execute("SELECT id FROM lessons WHERE id=? AND published=1", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    user = current_user()
    conn.execute("""INSERT INTO lesson_views(lesson_id,user_id,viewed_at) VALUES(?,?,?)
        ON CONFLICT(lesson_id,user_id) DO UPDATE SET viewed_at=excluded.viewed_at""",
        (lesson_id, user["id"], now()))
    conn.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"ok": True, "lesson_id": lesson_id, "message": "Просмотр сохранён"})
    flash("Урок отмечен как просмотренный. Руководство ЦППС увидит отметку.", "success")
    return redirect(url_for("lectures", course=request.form.get("course", "all")))

@app.route("/admin/lectures/new", methods=["POST"])
@admin_required
def create_lesson():
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    video_url = request.form.get("video_url", "").strip()
    uploaded = request.files.get("video_file")
    course = safe_int(request.form.get("course"), 1, 1, 2)

    has_file = bool(uploaded and uploaded.filename and uploaded.filename.strip())
    has_url = bool(video_url)
    if not title:
        flash("Укажите название урока.", "danger")
        return redirect(url_for("lectures"))
    if has_file == has_url:
        flash("Выберите видеофайл с компьютера ИЛИ укажите ссылку на видео — что-то одно.", "danger")
        return redirect(url_for("lectures"))

    if has_file:
        original_name = secure_filename(uploaded.filename)
        extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        if extension not in ALLOWED_VIDEO_EXTENSIONS:
            flash("Формат не поддерживается. Разрешены MP4, WebM, OGG и MOV.", "danger")
            return redirect(url_for("lectures"))
        os.makedirs(VIDEO_UPLOAD_DIR, exist_ok=True)
        saved_name = secrets.token_hex(16) + "." + extension
        saved_path = os.path.join(VIDEO_UPLOAD_DIR, saved_name)
        try:
            uploaded.save(saved_path)
        except Exception:
            app.logger.exception("Не удалось сохранить видеоурок")
            flash("Не удалось сохранить видеофайл. Проверь место на диске и лимит загрузки.", "danger")
            return redirect(url_for("lectures"))
        video_url = url_for("static", filename="uploads/lectures/" + saved_name)
    else:
        parsed = urlparse(video_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            flash("Укажите корректную ссылку на видео (https:// или http://).", "danger")
            return redirect(url_for("lectures"))

    conn = db()
    conn.execute("""INSERT INTO lessons(title,description,video_url,course,published,created_by,created_at)
        VALUES(?,?,?,?,?,?,?)""", (title, description, video_url, course,
        1 if request.form.get("published") == "on" else 0, current_user()["id"], now()))
    conn.commit()
    flash("Видео-урок добавлен.", "success")
    return redirect(url_for("lectures"))

@app.route("/admin/lectures/<int:lesson_id>/delete", methods=["POST"])
@admin_required
def delete_lesson(lesson_id):
    conn = db()
    lesson = conn.execute("SELECT id FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    lesson = conn.execute("SELECT video_url FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    video_url = lesson["video_url"] if lesson else ""
    conn.execute("DELETE FROM lesson_views WHERE lesson_id=?", (lesson_id,))
    conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    conn.commit()
    # Удаляем физический файл только для загруженных локальных видео; внешние ссылки не трогаем.
    prefix = "/static/uploads/lectures/"
    if video_url and video_url.startswith(prefix):
        filename = os.path.basename(video_url)
        file_path = os.path.join(VIDEO_UPLOAD_DIR, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except OSError:
            app.logger.exception("Не удалось удалить файл видеоурока")
    flash("Видео-урок удалён.", "success")
    return redirect(url_for("lectures"))

@app.route("/admin")
@admin_required
def admin():
    conn = db()
    stats = {
      "users": conn.execute("SELECT COUNT(*) n FROM users WHERE role!='admin'").fetchone()["n"],
      "tests": conn.execute("SELECT COUNT(*) n FROM tests").fetchone()["n"],
      "attempts": conn.execute("SELECT COUNT(*) n FROM attempts WHERE finished_at IS NOT NULL").fetchone()["n"],
      "passrate": conn.execute("SELECT ROUND(100.0*SUM(passed)/NULLIF(COUNT(*),0)) n FROM attempts WHERE finished_at IS NOT NULL").fetchone()["n"] or 0
    }
    tests = conn.execute("""SELECT t.*,(SELECT COUNT(*) FROM questions q WHERE q.test_id=t.id) qcount
        FROM tests t ORDER BY t.id DESC""").fetchall()
    users = conn.execute("SELECT id,username,role,created_at,full_name,cadet_static,academy_passed,access_blocked FROM users ORDER BY id DESC").fetchall()
    attempts = conn.execute("""SELECT a.*,u.username,t.title FROM attempts a
        JOIN users u ON u.id=a.user_id JOIN tests t ON t.id=a.test_id
        WHERE a.finished_at IS NOT NULL ORDER BY a.id DESC LIMIT 500""").fetchall()
    internships = conn.execute("SELECT * FROM internships ORDER BY id DESC").fetchall()
    teacher_statuses = get_teacher_statuses()
    return render_template("admin.html", stats=stats, tests=tests, users=users, attempts=attempts, internships=internships, teacher_statuses=teacher_statuses)

@app.route("/admin/test/new", methods=["POST"])
@admin_required
def create_test():
    title = request.form.get("title","").strip()
    event_type = request.form.get("event_type", "exam")
    if event_type not in ("exam", "recertification", "transfer"):
        event_type = "exam"
    if not title:
        flash("Укажите название мероприятия.", "danger")
        return redirect(url_for("admin"))
    cur = db().execute("""INSERT INTO tests(title,description,duration_minutes,pass_percent,max_attempts,published,event_type,created_by,created_at)
       VALUES(?,?,?,?,?,?,?,?,?)""",
       (title, request.form.get("description","").strip(),
        safe_int(request.form.get("duration"),20,1,180),
        safe_int(request.form.get("pass_percent"),70,1,100),
        safe_int(request.form.get("max_attempts"),2,1,20),
        1 if request.form.get("published")=="on" else 0, event_type, current_user()["id"], now()))
    db().commit()
    flash("Мероприятие создано. Теперь добавьте вопросы, если они нужны.", "success")
    return redirect(url_for("edit_test_alias", test_id=cur.lastrowid))

@app.route("/admin/test/<int:test_id>/edit", methods=["GET","POST"], endpoint="edit_test_alias")
@admin_required
def edit_test_alias(test_id):
    # Отдельный стабильный URL редактора; нужен для совместимости с размещённой версией сайта.
    return edit_test(test_id)

@app.route("/admin/test/<int:test_id>", methods=["GET","POST"])
@admin_required
def edit_test(test_id):
    conn = db()
    test = conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
    if not test: abort(404)
    if request.method == "POST":
        prompt = request.form.get("prompt","").strip()
        options = [request.form.get(k,"").strip() for k in ("a","b","c","d")]
        question_type = request.form.get("question_type", "single")
        correct = request.form.get("correct","A")
        correct_answers = sorted(set(x for x in request.form.getlist("correct_answers") if x in ("A","B","C","D")))
        valid_single = question_type == "single" and correct in ("A","B","C","D")
        valid_multiple = question_type == "multiple" and len(correct_answers) >= 2
        if not prompt or not all(options) or not (valid_single or valid_multiple):
            flash("Заполните вопрос, все четыре варианта и отметьте правильный ответ (для нескольких ответов — минимум два).", "danger")
        else:
            if question_type == "multiple":
                correct = correct_answers[0]  # Для совместимости со старыми записями.
            conn.execute("""INSERT INTO questions(test_id,prompt,a,b,c,d,correct,question_type,correct_answers)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (test_id,prompt,*options,correct,question_type,json.dumps(correct_answers if question_type == "multiple" else [correct])))
            conn.commit()
            flash("Вопрос добавлен.", "success")
        return redirect(url_for("edit_test", test_id=test_id))
    questions = conn.execute("SELECT * FROM questions WHERE test_id=? ORDER BY id", (test_id,)).fetchall()
    return render_template("edit_test.html", test=test, questions=questions)

@app.route("/admin/test/<int:test_id>/settings", methods=["POST"])
@admin_required
def test_settings(test_id):
    db().execute("""UPDATE tests SET title=?,description=?,duration_minutes=?,pass_percent=?,max_attempts=?,published=?
        WHERE id=?""",
        (request.form.get("title","").strip() or "Экзамен", request.form.get("description","").strip(),
         safe_int(request.form.get("duration"),20,1,180), safe_int(request.form.get("pass_percent"),70,1,100),
         safe_int(request.form.get("max_attempts"),2,1,20), 1 if request.form.get("published")=="on" else 0, test_id))
    db().commit()
    flash("Настройки сохранены.", "success")
    return redirect(url_for("edit_test_alias", test_id=test_id))

@app.route("/admin/question/<int:question_id>/delete", methods=["POST"])
@admin_required
def delete_question(question_id):
    q = db().execute("SELECT test_id FROM questions WHERE id=?", (question_id,)).fetchone()
    if q:
        db().execute("DELETE FROM questions WHERE id=?", (question_id,))
        db().commit()
        flash("Вопрос удалён.", "success")
        return redirect(url_for("edit_test_alias", test_id=q["test_id"]))
    abort(404)

@app.route("/admin/test/<int:test_id>/delete", methods=["POST"])
@admin_required
def delete_test(test_id):
    db().execute("DELETE FROM questions WHERE test_id=?", (test_id,))
    db().execute("DELETE FROM attempts WHERE test_id=?", (test_id,))
    db().execute("DELETE FROM tests WHERE id=?", (test_id,))
    db().commit()
    flash("Экзамен и связанные результаты удалены.", "success")
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:user_id>/role", methods=["POST"])
@admin_required
def change_role(user_id):
    role = request.form.get("role")
    if role in ("cadet","teacher","admin") and user_id != current_user()["id"]:
        db().execute("UPDATE users SET role=? WHERE id=?", (role,user_id))
        db().commit()
        flash("Роль пользователя обновлена.", "success")
    return redirect(url_for("admin"))

@app.route("/admin/user/<int:user_id>/academy-access", methods=["POST"])
@admin_required
def update_user_academy_access(user_id):
    conn = db()
    target = conn.execute("SELECT id,username,role FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        abort(404)
    if target["id"] == current_user()["id"] or target["role"] == "admin":
        flash("Нельзя менять статусы своего аккаунта или аккаунта администратора.", "warning")
        return redirect(url_for("admin"))
    academy_passed = 1 if request.form.get("academy_passed") == "on" else 0
    # После отметки «Академия пройдена» доступ блокируется автоматически.
    access_blocked = 1 if (request.form.get("access_blocked") == "on" or academy_passed) else 0
    conn.execute("UPDATE users SET academy_passed=?,access_blocked=? WHERE id=?",
                 (academy_passed, access_blocked, user_id))
    conn.commit()
    flash(f"Статусы аккаунта {target['username']} сохранены.", "success")
    return redirect(url_for("admin"))

@app.route("/admin/internship/new", methods=["POST"])
@admin_required
def create_internship():
    title = request.form.get("title","").strip()
    details = request.form.get("details","").strip()
    if title and details:
        db().execute("INSERT INTO internships(title,details,status,created_at) VALUES(?,?,?,?)",
                     (title,details,"active",now()))
        db().commit()
        flash("Стажировка создана.", "success")
    else:
        flash("Заполните название и описание стажировки.", "danger")
    return redirect(url_for("admin"))

@app.route("/admin/internship/<int:internship_id>/assign", methods=["POST"])
@admin_required
def assign_internship(internship_id):
    username = request.form.get("username","").strip()
    user = db().execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        flash("Курсант с таким никнеймом не найден.", "danger")
    else:
        try:
            db().execute("INSERT INTO internship_assignments(internship_id,user_id,status,assigned_at) VALUES(?,?,?,?)",
                         (internship_id,user["id"],"assigned",now()))
            db().commit()
            flash("Стажировка назначена.", "success")
        except sqlite3.IntegrityError:
            flash("Эта стажировка уже назначена курсанту.", "warning")
    return redirect(url_for("admin"))

@app.route("/admin/assignment/<int:assignment_id>/status", methods=["POST"])
@admin_required
def assignment_status(assignment_id):
    status = request.form.get("status")
    if status in ("assigned","in_progress","completed"):
        db().execute("UPDATE internship_assignments SET status=? WHERE id=?", (status,assignment_id))
        db().commit()
    return redirect(url_for("admin"))

@app.errorhandler(403)
def forbidden(_): return render_template("error.html", code=403, message="Недостаточно прав для просмотра этой страницы."), 403
@app.errorhandler(404)
def not_found(_): return render_template("error.html", code=404, message="Страница не найдена."), 404
@app.errorhandler(400)
def bad_request(e): return render_template("error.html", code=400, message=getattr(e,"description","Некорректный запрос.")), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)