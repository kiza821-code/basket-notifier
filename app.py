from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import firebase_admin
from firebase_admin import credentials, messaging

app = Flask(__name__)
app.secret_key = "super_secret_key_12345"

APP_TZ = timezone(timedelta(hours=7))

# Первый админ создаётся автоматически при старте, если его ещё нет
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "12345"

# Gmail SMTP
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_ADDRESS = "kiza821@gmail.com"
EMAIL_PASSWORD = "rgipuxjxyxxllsrs"

# Firebase web config
FIREBASE_API_KEY = "AIzaSyD89nzI1hfe5KpqoQ2SBofIk7gju2EK78M"
FIREBASE_AUTH_DOMAIN = "basket-training-app.firebaseapp.com"
FIREBASE_PROJECT_ID = "basket-training-app"
FIREBASE_MESSAGING_SENDER_ID = "44535672611"
FIREBASE_APP_ID = "1:44535672611:web:b3990a50f2aefdec9ae696"
FIREBASE_VAPID_KEY = "BDKbWkhUbgcmXmOL3flbwWNeIdZ92B-lBPjgcNreUCx_aXRajSOa0wno6EAPipWlgwj5wv1NhJtlQ3MvHO5xrws"

# Firebase admin
FIREBASE_SERVICE_ACCOUNT_FILE = "firebase-service-account.json"


def now_local():
    return datetime.now(APP_TZ)


def parse_local_datetime(date_str, time_str):
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=APP_TZ)


def parse_local_datetime_input(datetime_str):
    dt = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=APP_TZ)


def get_db():
    conn = sqlite3.connect("db.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trainings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        training_date TEXT NOT NULL,
        training_time TEXT NOT NULL,
        max_players INTEGER NOT NULL DEFAULT 15,
        registration_start TEXT NOT NULL,
        registration_end TEXT NOT NULL,
        open_notification_sent INTEGER NOT NULL DEFAULT 0,
        plus_one_notification_sent INTEGER NOT NULL DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        training_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_plus_one INTEGER NOT NULL DEFAULT 0,
        parent_registration_id INTEGER,
        FOREIGN KEY (training_id) REFERENCES trainings(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        fcm_token TEXT NOT NULL UNIQUE,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    training_columns = [row[1] for row in cursor.execute("PRAGMA table_info(trainings)").fetchall()]

    if "open_notification_sent" not in training_columns:
        cursor.execute("""
            ALTER TABLE trainings
            ADD COLUMN open_notification_sent INTEGER NOT NULL DEFAULT 0
        """)

    if "plus_one_notification_sent" not in training_columns:
        cursor.execute("""
            ALTER TABLE trainings
            ADD COLUMN plus_one_notification_sent INTEGER NOT NULL DEFAULT 0
        """)

    admin = cursor.execute("""
        SELECT * FROM users WHERE email = ?
    """, (ADMIN_EMAIL,)).fetchone()

    if not admin:
        cursor.execute("""
            INSERT INTO users (
                email, password_hash, display_name, status, is_admin, created_at
            )
            VALUES (?, ?, ?, 'approved', 1, ?)
        """, (
            ADMIN_EMAIL,
            generate_password_hash(ADMIN_PASSWORD),
            "Администратор",
            now_local().isoformat()
        ))

    db.commit()
    db.close()


def init_firebase_admin():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)


def render_message_page(title, message):
    return render_template("message.html", title=title, message=message)


def send_email(to_email, subject, body):
    print(f"EMAIL DISABLED: письмо не отправляется. Получатель: {to_email}, тема: {subject}")
    return


def save_push_subscription(user_id, fcm_token):
    if not user_id or not fcm_token:
        return

    db = get_db()
    cursor = db.cursor()

    existing = cursor.execute("""
        SELECT * FROM push_subscriptions
        WHERE fcm_token = ?
    """, (fcm_token,)).fetchone()

    if existing:
        cursor.execute("""
            UPDATE push_subscriptions
            SET user_id = ?, is_active = 1, updated_at = ?
            WHERE fcm_token = ?
        """, (
            user_id,
            now_local().isoformat(),
            fcm_token
        ))
    else:
        cursor.execute("""
            INSERT INTO push_subscriptions (
                user_id, fcm_token, is_active, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?)
        """, (
            user_id,
            fcm_token,
            now_local().isoformat(),
            now_local().isoformat()
        ))

    db.commit()
    db.close()


def send_push_to_user_tokens(user_id, title, body, url="/"):
    db = get_db()
    cursor = db.cursor()

    tokens = cursor.execute("""
        SELECT * FROM push_subscriptions
        WHERE user_id = ? AND is_active = 1
    """, (user_id,)).fetchall()

    db.close()

    print(f"PUSH: user_id={user_id}, tokens_found={len(tokens)}")

    for row in tokens:
        token = row["fcm_token"]

        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body
                ),
                webpush=messaging.WebpushConfig(
                    fcm_options=messaging.WebpushFCMOptions(
                        link=url
                    )
                ),
                data={
                    "url": url
                },
                token=token
            )

            response = messaging.send(message)
            print(f"PUSH: отправлено успешно, response={response}, user_id={user_id}")

        except Exception as e:
            print("PUSH ERROR:", repr(e))


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None

    db = get_db()
    cursor = db.cursor()
    user = cursor.execute("""
        SELECT * FROM users WHERE id = ?
    """, (user_id,)).fetchone()
    db.close()
    return user


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapper


def approved_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))

        if user["status"] != "approved":
            return render_message_page(
                "Доступ ограничен",
                "У вас пока нет доступа к записи на тренировки."
            )
        return func(*args, **kwargs)
    return wrapper


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if user["is_admin"] != 1:
            return render_message_page(
                "Нет доступа",
                "У вас нет доступа к админ-панели."
            )
        return func(*args, **kwargs)
    return wrapper


def get_registration_status(training):
    now = now_local()
    reg_start = parse_local_datetime_input(training["registration_start"])
    reg_end = parse_local_datetime_input(training["registration_end"])

    if now < reg_start:
        return "not_started"
    if reg_start <= now <= reg_end:
        return "open"
    return "closed"


def get_training_datetime(training):
    return parse_local_datetime(training["training_date"], training["training_time"])


def is_training_finished(training):
    return now_local() > get_training_datetime(training)


def can_plus_one_be_added(training, active_count):
    training_dt = get_training_datetime(training)
    now = now_local()

    within_12_hours = now >= training_dt - timedelta(hours=12)
    before_training = now < training_dt

    return within_12_hours and before_training and active_count < training["max_players"]


def notify_open_trainings():
    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        WHERE open_notification_sent = 0
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    approved_users = cursor.execute("""
        SELECT * FROM users
        WHERE status = 'approved'
    """).fetchall()

    for training in trainings:
        if get_registration_status(training) == "open":
            subject = f"Открыта запись: {training['title']}"
            body = (
                f"Открыта запись на тренировку '{training['title']}'.\n\n"
                f"Дата: {training['training_date']}\n"
                f"Время: {training['training_time']}\n\n"
                f"Зайдите на сайт и запишитесь."
            )

            for user in approved_users:
                # email сейчас отключён, но можно оставить вызов
                send_email(user["email"], subject, body)

                # push-уведомление
                send_push_to_user_tokens(
                    user["id"],
                    "Открыта запись на тренировку",
                    f"{training['title']} — {training['training_date']} {training['training_time']}",
                    "/"
                )

            cursor.execute("""
                UPDATE trainings
                SET open_notification_sent = 1
                WHERE id = ?
            """, (training["id"],))

    db.commit()
    db.close()


def notify_plus_one_available():
    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        WHERE plus_one_notification_sent = 0
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    for training in trainings:
        active_players = cursor.execute("""
            SELECT r.*, u.email
            FROM registrations r
            JOIN users u ON u.id = r.user_id
            WHERE r.training_id = ?
              AND r.status = 'active'
              AND r.is_plus_one = 0
            ORDER BY r.created_at ASC
        """, (training["id"],)).fetchall()

        active_count = cursor.execute("""
            SELECT COUNT(*) as count FROM registrations
            WHERE training_id = ? AND status = 'active'
        """, (training["id"],)).fetchone()["count"]

        if can_plus_one_be_added(training, active_count):
            for player in active_players:
                send_email(
                    player["email"],
                    "Появилась возможность добавить гостя",
                    "Еще есть места, можешь позвать с собой кого-нибудь!"
                )
                send_push_to_user_tokens(
                    player["user_id"],
                    "Можно взять гостя",
                    "Еще есть места, можешь позвать с собой кого-нибудь!",
                    "/"
                )

            cursor.execute("""
                UPDATE trainings
                SET plus_one_notification_sent = 1
                WHERE id = ?
            """, (training["id"],))

    db.commit()
    db.close()


init_db()
init_firebase_admin()


@app.route("/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/")
def index():
    user = get_current_user()

    if not user:
        return redirect(url_for("login"))

    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    trainings_data = []

    for training in trainings:
        if is_training_finished(training):
            continue

        active_players = cursor.execute("""
            SELECT * FROM registrations
            WHERE training_id = ? AND status = 'active'
            ORDER BY created_at ASC
        """, (training["id"],)).fetchall()

        waitlist = cursor.execute("""
            SELECT * FROM registrations
            WHERE training_id = ? AND status = 'waitlist'
            ORDER BY created_at ASC
        """, (training["id"],)).fetchall()

        current_user_registration = None
        if user["status"] == "approved":
            current_user_registration = cursor.execute("""
                SELECT * FROM registrations
                WHERE training_id = ? AND user_id = ? AND is_plus_one = 0
            """, (training["id"], user["id"])).fetchone()

        trainings_data.append({
            "training": training,
            "players": active_players,
            "waitlist": waitlist,
            "registration_status": get_registration_status(training),
            "plus_one_available": can_plus_one_be_added(training, len(active_players)),
            "current_user_registration": current_user_registration
        })

    db.close()

    return render_template(
        "index.html",
        user=user,
        trainings_data=trainings_data,
        firebase_api_key=FIREBASE_API_KEY,
        firebase_auth_domain=FIREBASE_AUTH_DOMAIN,
        firebase_project_id=FIREBASE_PROJECT_ID,
        firebase_messaging_sender_id=FIREBASE_MESSAGING_SENDER_ID,
        firebase_app_id=FIREBASE_APP_ID,
        firebase_vapid_key=FIREBASE_VAPID_KEY
    )


@app.route("/admin-panel")
@admin_required
def admin_panel():
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    pending_users = cursor.execute("""
        SELECT * FROM users
        WHERE status = 'pending'
        ORDER BY created_at ASC
    """).fetchall()

    all_users = cursor.execute("""
        SELECT * FROM users
        ORDER BY created_at ASC
    """).fetchall()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    db.close()

    return render_template(
        "admin_panel.html",
        user=user,
        pending_users=pending_users,
        all_users=all_users,
        trainings=trainings
    )


@app.route("/tasks/check-open-notifications")
def check_open_notifications():
    notify_open_trainings()
    return "ok", 200


@app.route("/tasks/check-plus-one-notifications")
def check_plus_one_notifications():
    notify_plus_one_available()
    return "ok", 200


@app.route("/register", methods=["GET", "POST"])
def register_account():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not display_name or not email or not password:
            return render_message_page(
                "Ошибка",
                "Заполните все поля."
            )

        db = get_db()
        cursor = db.cursor()

        existing = cursor.execute("""
            SELECT * FROM users WHERE email = ?
        """, (email,)).fetchone()

        if existing:
            db.close()
            return render_message_page(
                "Аккаунт уже существует",
                "Пользователь с таким email уже зарегистрирован."
            )

        cursor.execute("""
            INSERT INTO users (
                email, password_hash, display_name, status, is_admin, created_at
            )
            VALUES (?, ?, ?, 'pending', 0, ?)
        """, (
            email,
            generate_password_hash(password),
            display_name,
            now_local().isoformat()
        ))

        db.commit()
        db.close()

        return render_message_page(
            "Заявка отправлена",
            "Ваш аккаунт создан и ожидает одобрения администратором."
        )

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        db = get_db()
        cursor = db.cursor()

        user = cursor.execute("""
            SELECT * FROM users WHERE email = ?
        """, (email,)).fetchone()

        db.close()

        if not user or not check_password_hash(user["password_hash"], password):
            return render_template("login.html", error="Неверный email или пароль")

        session["user_id"] = user["id"]
        return redirect(url_for("index"))

    return render_template("login.html", error="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/push/save-token", methods=["POST"])
@login_required
def save_push_token():
    user = get_current_user()
    fcm_token = request.form.get("fcm_token", "").strip()

    if not fcm_token:
        return "missing token", 400

    save_push_subscription(user["id"], fcm_token)
    return "ok", 200


@app.route("/register_training/<int:training_id>", methods=["POST"])
@login_required
@approved_required
def register_training(training_id):
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return render_message_page(
            "Тренировка не найдена",
            "Похоже, эта тренировка была удалена."
        )

    registration_status = get_registration_status(training)

    if registration_status == "not_started":
        db.close()
        return render_message_page(
            "Запись ещё не началась",
            "Регистрация на эту тренировку пока закрыта."
        )

    if registration_status == "closed":
        db.close()
        return render_message_page(
            "Запись закрыта",
            "Время регистрации на эту тренировку уже закончилось."
        )

    existing = cursor.execute("""
        SELECT * FROM registrations
        WHERE training_id = ? AND user_id = ? AND is_plus_one = 0
    """, (training_id, user["id"])).fetchone()

    if existing:
        db.close()
        return render_message_page(
            "Вы уже записаны",
            "Вы уже есть в списке участников или в очереди."
        )

    active_count = cursor.execute("""
        SELECT COUNT(*) as count FROM registrations
        WHERE training_id = ? AND status = 'active'
    """, (training_id,)).fetchone()["count"]

    status = "active" if active_count < training["max_players"] else "waitlist"

    cursor.execute("""
        INSERT INTO registrations (
            training_id, user_id, display_name, status, created_at, is_plus_one, parent_registration_id
        )
        VALUES (?, ?, ?, ?, ?, 0, NULL)
    """, (
        training_id,
        user["id"],
        user["display_name"],
        status,
        now_local().isoformat()
    ))

    db.commit()
    db.close()

    return redirect(url_for("index"))


@app.route("/cancel/<int:registration_id>", methods=["POST"])
@login_required
def cancel(registration_id):
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    registration = cursor.execute("""
        SELECT * FROM registrations WHERE id = ?
    """, (registration_id,)).fetchone()

    if not registration:
        db.close()
        return redirect(url_for("index"))

    if registration["user_id"] != user["id"]:
        db.close()
        return render_message_page(
            "Нет доступа",
            "Вы можете отменить только свою собственную запись."
        )

    training_id = registration["training_id"]
    removed_status = registration["status"]

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if registration["is_plus_one"] == 0:
        cursor.execute("""
            DELETE FROM registrations
            WHERE parent_registration_id = ? AND is_plus_one = 1
        """, (registration["id"],))

    cursor.execute("""
        DELETE FROM registrations WHERE id = ?
    """, (registration_id,))
    db.commit()

    if removed_status == "active":
        active_count = cursor.execute("""
            SELECT COUNT(*) as count FROM registrations
            WHERE training_id = ? AND status = 'active'
        """, (training_id,)).fetchone()["count"]

        while active_count < training["max_players"]:
            next_user = cursor.execute("""
                SELECT r.*, u.email
                FROM registrations r
                JOIN users u ON u.id = r.user_id
                WHERE r.training_id = ? AND r.status = 'waitlist'
                ORDER BY r.created_at ASC
                LIMIT 1
            """, (training_id,)).fetchone()

            if not next_user:
                break

            cursor.execute("""
                UPDATE registrations
                SET status = 'active'
                WHERE id = ?
            """, (next_user["id"],))
            db.commit()

            send_email(
                next_user["email"],
                "Вы переведены в основной состав",
                (
                    f"Освободилось место на тренировку '{training['title']}'.\n\n"
                    f"Дата: {training['training_date']}\n"
                    f"Время: {training['training_time']}\n\n"
                    f"Теперь вы в основном составе."
                )
            )

            send_push_to_user_tokens(
                next_user["user_id"],
                "Вы в основном составе",
                f"{training['title']} — {training['training_date']} {training['training_time']}",
                "/"
            )

            active_count += 1

    db.close()
    return redirect(url_for("index"))


@app.route("/add_plus_one/<int:training_id>", methods=["POST"])
@login_required
@approved_required
def add_plus_one(training_id):
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return render_message_page(
            "Тренировка не найдена",
            "Похоже, эта тренировка была удалена."
        )

    owner_registration = cursor.execute("""
        SELECT * FROM registrations
        WHERE training_id = ?
          AND user_id = ?
          AND status = 'active'
          AND is_plus_one = 0
    """, (training_id, user["id"])).fetchone()

    if not owner_registration:
        db.close()
        return render_message_page(
            "Нельзя добавить +1",
            "Функция +1 доступна только игрокам из основного состава."
        )

    existing_plus_one = cursor.execute("""
        SELECT * FROM registrations
        WHERE training_id = ?
          AND parent_registration_id = ?
          AND is_plus_one = 1
    """, (training_id, owner_registration["id"])).fetchone()

    if existing_plus_one:
        db.close()
        return render_message_page(
            "Нельзя добавить +1",
            "Вы уже добавили одного игрока."
        )

    active_count = cursor.execute("""
        SELECT COUNT(*) as count FROM registrations
        WHERE training_id = ? AND status = 'active'
    """, (training_id,)).fetchone()["count"]

    if not can_plus_one_be_added(training, active_count):
        db.close()
        return render_message_page(
            "Нельзя добавить +1",
            "Кнопка +1 доступна только за 12 часов до тренировки, если в основном составе меньше максимума."
        )

    plus_one_name = f"Гость +1 от {owner_registration['display_name']}"

    cursor.execute("""
        INSERT INTO registrations (
            training_id, user_id, display_name, status, created_at, is_plus_one, parent_registration_id
        )
        VALUES (?, ?, ?, 'active', ?, 1, ?)
    """, (
        training_id,
        user["id"],
        plus_one_name,
        now_local().isoformat(),
        owner_registration["id"]
    ))

    db.commit()
    db.close()

    return redirect(url_for("index"))


@app.route("/admin/approve_user/<int:user_id>")
@admin_required
def approve_user(user_id):
    print(f"APPROVE: старт approve_user для user_id={user_id}")

    db = get_db()
    cursor = db.cursor()

    user_to_approve = cursor.execute("""
        SELECT * FROM users WHERE id = ?
    """, (user_id,)).fetchone()

    if not user_to_approve:
        print("APPROVE: пользователь не найден")
        db.close()
        return redirect(url_for("admin_panel"))

    print(f"APPROVE: найден пользователь {user_to_approve['email']}")

    cursor.execute("""
        UPDATE users
        SET status = 'approved'
        WHERE id = ?
    """, (user_id,))

    db.commit()
    db.close()

    print("APPROVE: статус обновлён")

    try:
        send_email(
            user_to_approve["email"],
            "Доступ к тренировкам одобрен",
            (
                f"Здравствуйте, {user_to_approve['display_name']}!\n\n"
                f"Ваша заявка одобрена. Теперь вы можете войти на сайт и записываться на тренировки."
            )
        )
        print("APPROVE: email отправлен")
    except Exception as e:
        print("APPROVE EMAIL ERROR:", repr(e))

    try:
        send_push_to_user_tokens(
            user_to_approve["id"],
            "Доступ одобрен",
            "Теперь вы можете записываться на тренировки.",
            "/"
        )
        print("APPROVE: push отправлен")
    except Exception as e:
        print("APPROVE PUSH ERROR:", repr(e))

    return redirect(url_for("admin_panel"))


@app.route("/admin/block_user/<int:user_id>")
@admin_required
def block_user(user_id):
    db = get_db()`
    cursor = db.cursor()

    cursor.execute("""
        UPDATE users
        SET status = 'blocked'
        WHERE id = ?
    """, (user_id,))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))

@app.route("/admin/delete_user/<int:user_id>")
@admin_required
def delete_user(user_id):
    current_admin = get_current_user()

    db = get_db()
    cursor = db.cursor()

    user_to_delete = cursor.execute("""
        SELECT * FROM users WHERE id = ?
    """, (user_id,)).fetchone()

    if not user_to_delete:
        db.close()
        return redirect(url_for("admin_panel"))

    # Нельзя удалить самого себя
    if user_to_delete["id"] == current_admin["id"]:
        db.close()
        return render_message_page(
            "Удаление запрещено",
            "Нельзя удалить самого себя."
        )

    # Нельзя удалить другого администратора
    if user_to_delete["is_admin"] == 1:
        db.close()
        return render_message_page(
            "Удаление запрещено",
            "Нельзя удалить администратора."
        )

    # Находим все записи пользователя
    registrations = cursor.execute("""
        SELECT * FROM registrations
        WHERE user_id = ?
        ORDER BY created_at ASC
    """, (user_id,)).fetchall()

    for registration in registrations:
        training_id = registration["training_id"]
        removed_status = registration["status"]

        training = cursor.execute("""
            SELECT * FROM trainings WHERE id = ?
        """, (training_id,)).fetchone()

        if not training:
            continue

        # Если это основная запись, удаляем и его +1
        if registration["is_plus_one"] == 0:
            cursor.execute("""
                DELETE FROM registrations
                WHERE parent_registration_id = ? AND is_plus_one = 1
            """, (registration["id"],))

        cursor.execute("""
            DELETE FROM registrations WHERE id = ?
        """, (registration["id"],))

        # Если пользователь был в основном составе — поднимаем из очереди
        if removed_status == "active":
            active_count = cursor.execute("""
                SELECT COUNT(*) as count FROM registrations
                WHERE training_id = ? AND status = 'active'
            """, (training_id,)).fetchone()["count"]

            while active_count < training["max_players"]:
                next_user = cursor.execute("""
                    SELECT r.*, u.email
                    FROM registrations r
                    JOIN users u ON u.id = r.user_id
                    WHERE r.training_id = ? AND r.status = 'waitlist'
                    ORDER BY r.created_at ASC
                    LIMIT 1
                """, (training_id,)).fetchone()

                if not next_user:
                    break

                cursor.execute("""
                    UPDATE registrations
                    SET status = 'active'
                    WHERE id = ?
                """, (next_user["id"],))

                try:
                    send_push_to_user_tokens(
                        next_user["user_id"],
                        "Вы в основном составе",
                        f"{training['title']} — {training['training_date']} {training['training_time']}",
                        "/"
                    )
                except Exception as e:
                    print("DELETE USER PUSH ERROR:", repr(e))

                active_count += 1

    # Удаляем push-токены пользователя
    cursor.execute("""
        DELETE FROM push_subscriptions WHERE user_id = ?
    """, (user_id,))

    # На всякий случай удаляем остаточные регистрации
    cursor.execute("""
        DELETE FROM registrations WHERE user_id = ?
    """, (user_id,))

    # Удаляем самого пользователя
    cursor.execute("""
        DELETE FROM users WHERE id = ?
    """, (user_id,))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))

@app.route("/admin/create_training", methods=["POST"])
@admin_required
def create_training():
    title = request.form.get("title", "").strip()
    training_date = request.form.get("training_date", "").strip()
    training_time = request.form.get("training_time", "").strip()
    max_players = request.form.get("max_players", "15").strip()
    registration_start = request.form.get("registration_start", "").strip()
    registration_end = request.form.get("registration_end", "").strip()

    if not title or not training_date or not training_time or not registration_start or not registration_end:
        return redirect(url_for("admin_panel"))

    try:
        max_players = int(max_players)
    except ValueError:
        return render_message_page("Ошибка данных", "Количество игроков должно быть числом.")

    if registration_start >= registration_end:
        return render_message_page(
            "Ошибка времени записи",
            "Время начала записи должно быть раньше времени окончания записи."
        )

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO trainings (
            title, training_date, training_time, max_players,
            registration_start, registration_end,
            open_notification_sent, plus_one_notification_sent
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, 0)
    """, (
        title,
        training_date,
        training_time,
        max_players,
        registration_start,
        registration_end
    ))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))


@app.route("/admin/update_training/<int:training_id>", methods=["POST"])
@admin_required
def update_training(training_id):
    title = request.form.get("title", "").strip()
    training_date = request.form.get("training_date", "").strip()
    training_time = request.form.get("training_time", "").strip()
    max_players = request.form.get("max_players", "15").strip()
    registration_start = request.form.get("registration_start", "").strip()
    registration_end = request.form.get("registration_end", "").strip()

    try:
        max_players = int(max_players)
    except ValueError:
        return render_message_page("Ошибка данных", "Количество игроков должно быть числом.")

    if registration_start >= registration_end:
        return render_message_page(
            "Ошибка времени записи",
            "Время начала записи должно быть раньше времени окончания записи."
        )

    db = get_db()
    cursor = db.cursor()

    old_training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    new_open_notification_sent = 0
    new_plus_one_notification_sent = 0

    if old_training:
        new_open_notification_sent = old_training["open_notification_sent"]
        new_plus_one_notification_sent = old_training["plus_one_notification_sent"]

        if old_training["registration_start"] != registration_start:
            new_open_notification_sent = 0

        if (
            old_training["training_date"] != training_date
            or old_training["training_time"] != training_time
        ):
            new_plus_one_notification_sent = 0

    cursor.execute("""
        UPDATE trainings
        SET title = ?, training_date = ?, training_time = ?, max_players = ?,
            registration_start = ?, registration_end = ?,
            open_notification_sent = ?, plus_one_notification_sent = ?
        WHERE id = ?
    """, (
        title,
        training_date,
        training_time,
        max_players,
        registration_start,
        registration_end,
        new_open_notification_sent,
        new_plus_one_notification_sent,
        training_id
    ))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))


@app.route("/admin/delete_training/<int:training_id>")
@admin_required
def delete_training(training_id):
    print(f"DELETE TRAINING: старт training_id={training_id}")

    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        print("DELETE TRAINING: тренировка не найдена")
        db.close()
        return redirect(url_for("admin_panel"))

    registrations = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ?
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    print(f"DELETE TRAINING: найдено записей {len(registrations)}")

    for registration in registrations:
        try:
            send_email(
                registration["email"],
                "Тренировка отменена",
                (
                    f"К сожалению, тренировка '{training['title']}' "
                    f"на {training['training_date']} в {training['training_time']} была отменена.\n\n"
                    f"Ваша запись аннулирована."
                )
            )
            print(f"DELETE TRAINING: email отправлен {registration['email']}")
        except Exception as e:
            print("DELETE TRAINING EMAIL ERROR:", repr(e))

        try:
            send_push_to_user_tokens(
                registration["user_id"],
                "Тренировка отменена",
                f"{training['title']} — {training['training_date']} {training['training_time']}",
                "/"
            )
            print(f"DELETE TRAINING: push отправлен user_id={registration['user_id']}")
        except Exception as e:
            print("DELETE TRAINING PUSH ERROR:", repr(e))

    try:
        cursor.execute("DELETE FROM registrations WHERE training_id = ?", (training_id,))
        cursor.execute("DELETE FROM trainings WHERE id = ?", (training_id,))
        db.commit()
        print("DELETE TRAINING: удаление из БД выполнено")
    except Exception as e:
        db.close()
        print("DELETE TRAINING DB ERROR:", repr(e))
        return "Ошибка удаления тренировки, смотри консоль", 500

    db.close()
    return redirect(url_for("admin_panel"))


@app.route("/admin/generate_schedule")
@admin_required
def generate_schedule():
    db = get_db()
    cursor = db.cursor()

    today = now_local().date()
    days_ahead = 30
    created = 0

    for i in range(days_ahead):
        day = today + timedelta(days=i)

        if day.weekday() in [1, 3]:
            training_date = day.strftime("%Y-%m-%d")
            training_time = "20:30"

            reg_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=APP_TZ) - timedelta(days=1)
            reg_start = reg_start.replace(hour=12, minute=0)

            reg_end = datetime.combine(day, datetime.min.time()).replace(tzinfo=APP_TZ)
            reg_end = reg_end.replace(hour=18, minute=30)

            reg_start_str = reg_start.strftime("%Y-%m-%dT%H:%M")
            reg_end_str = reg_end.strftime("%Y-%m-%dT%H:%M")

            exists = cursor.execute("""
                SELECT * FROM trainings
                WHERE training_date = ? AND training_time = ?
            """, (training_date, training_time)).fetchone()

            if not exists:
                cursor.execute("""
                    INSERT INTO trainings (
                        title, training_date, training_time, max_players,
                        registration_start, registration_end,
                        open_notification_sent, plus_one_notification_sent
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                """, (
                    "Баскетбольная тренировка",
                    training_date,
                    training_time,
                    15,
                    reg_start_str,
                    reg_end_str
                ))
                created += 1

    db.commit()
    db.close()

    return render_message_page(
        "Расписание создано",
        f"Добавлено тренировок: {created}"
    )


@app.route("/debug/all-users")
def debug_all_users():
    db = get_db()
    cursor = db.cursor()

    users = cursor.execute("""
        SELECT id, email, display_name, status, is_admin
        FROM users
        ORDER BY id
    """).fetchall()

    db.close()

    return "<br>".join([
        f"id={u['id']} | email={u['email']} | name={u['display_name']} | status={u['status']} | admin={u['is_admin']}"
        for u in users
    ])


@app.route("/debug/me")
@login_required
def debug_me():
    user = get_current_user()
    return (
        f"id={user['id']} | "
        f"email={user['email']} | "
        f"name={user['display_name']} | "
        f"status={user['status']} | "
        f"is_admin={user['is_admin']}"
    )


@app.route("/debug/users")
@admin_required
def debug_users():
    db = get_db()
    cursor = db.cursor()

    users = cursor.execute("""
        SELECT * FROM users
        ORDER BY id ASC
    """).fetchall()

    db.close()

    return "<br>".join([
        f"id={u['id']} | {u['display_name']} | {u['email']} | status={u['status']} | admin={u['is_admin']}"
        for u in users
    ])


@app.route("/debug/push-tokens")
@admin_required
def debug_push_tokens():
    db = get_db()
    cursor = db.cursor()

    rows = cursor.execute("""
        SELECT ps.*, u.email
        FROM push_subscriptions ps
        JOIN users u ON u.id = ps.user_id
        ORDER BY ps.updated_at DESC
    """).fetchall()

    db.close()

    return "<br>".join([
        f"{row['email']} | active={row['is_active']} | token={row['fcm_token'][:40]}..."
        for row in rows
    ])


@app.route("/debug/trainings")
def debug_trainings():
    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    rows = []
    for t in trainings:
        rows.append(
            f"id={t['id']} | {t['title']} | {t['training_date']} {t['training_time']} | "
            f"start={t['registration_start']} | end={t['registration_end']}"
        )

    db.close()
    return "<br>".join(rows)


@app.route("/debug/send-test-push/<int:user_id>")
@admin_required
def debug_send_test_push(user_id):
    send_push_to_user_tokens(
        user_id=user_id,
        title="Тестовое уведомление",
        body="Push-уведомления работают!",
        url="/"
    )
    return "push sent"

@app.route("/debug/send-test-email")
def debug_send_test_email():
    send_email(
        "ki-za@mail.ru",
        "Проверка доставки",
        (
            "Здравствуйте!\n\n"
            "Это тестовое письмо от сайта записи на тренировки.\n"
            "Если вы его получили, доставка работает корректно."
        )
    )
    return "email sent"

@app.route("/tasks/run-all")
def run_all_tasks():
    print("CRON: запуск всех задач")

    notify_open_trainings()
    notify_plus_one_available()

    return "ok", 200

if __name__ == "__main__":
    app.run(debug=True)