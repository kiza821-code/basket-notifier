from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, abort
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import firebase_admin
from firebase_admin import credentials, messaging
import os
from dotenv import load_dotenv
import math

app = Flask(__name__)
load_dotenv()

# --- Проверка обязательных переменных окружения ---
_SECRET_KEY = os.environ.get("SECRET_KEY")
_TASK_SECRET = os.environ.get("TASK_SECRET")
_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

_missing = [
    name for name, val in [
        ("SECRET_KEY", _SECRET_KEY),
        ("TASK_SECRET", _TASK_SECRET),
        ("ADMIN_EMAIL", _ADMIN_EMAIL),
        ("ADMIN_PASSWORD", _ADMIN_PASSWORD),
    ]
    if not val
]
if _missing:
    raise RuntimeError(
        f"Отсутствуют обязательные переменные окружения: {', '.join(_missing)}. "
        f"Задайте их в файле .env или в системном окружении."
    )

app.secret_key = _SECRET_KEY
TASK_SECRET = _TASK_SECRET

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

APP_TZ = ZoneInfo("Asia/Novosibirsk")

# Первый админ создаётся автоматически при старте, если его ещё нет
ADMIN_EMAIL = _ADMIN_EMAIL
ADMIN_PASSWORD = _ADMIN_PASSWORD

# Mail SMTP
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.mail.ru")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")

# Firebase web config
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_AUTH_DOMAIN = os.environ.get("FIREBASE_AUTH_DOMAIN", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_MESSAGING_SENDER_ID = os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "")
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "")
FIREBASE_VAPID_KEY = os.environ.get("FIREBASE_VAPID_KEY", "")

# Firebase admin
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT_FILE",
    "/var/www/basket_app/firebase-service-account.json"
)

PAYMENT_LINK_TUESDAY = os.environ.get("PAYMENT_LINK_TUESDAY", "")
PAYMENT_LINK_THURSDAY = os.environ.get("PAYMENT_LINK_THURSDAY", "")
PAYMENT_LINK_FRIDAY = os.environ.get("PAYMENT_LINK_FRIDAY", "")

PAYMENT_PHONE_TUESDAY = os.environ.get("PAYMENT_PHONE_TUESDAY", "89138462207")
PAYMENT_PHONE_THURSDAY = os.environ.get("PAYMENT_PHONE_THURSDAY", "89627830203")


PAYMENT_QR_TUESDAY = "qr_tuesday.png"
PAYMENT_QR_THURSDAY = "qr_thursday.png"


BASE_URL = os.environ.get("BASE_URL", "https://basketapp.ru")

def now_local():
    return datetime.now(APP_TZ)


def parse_local_datetime(date_str, time_str):
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=APP_TZ)


def parse_local_datetime_input(datetime_str):
    dt = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M")
    return dt.replace(tzinfo=APP_TZ)


def validate_training_form(title, training_date, training_time, max_players_str,
                           registration_start, registration_end):
    """
    Проверяет данные формы тренировки.
    Возвращает (None, max_players_int) при успехе
    или (сообщение_об_ошибке, None) при ошибке.
    """
    if not title or not training_date or not training_time or not registration_start or not registration_end:
        return "Заполните все обязательные поля.", None

    # Проверка формата даты тренировки
    try:
        datetime.strptime(training_date, "%Y-%m-%d")
    except ValueError:
        return "Некорректный формат даты тренировки (ожидается ГГГГ-ММ-ДД).", None

    # Проверка формата времени тренировки
    try:
        datetime.strptime(training_time, "%H:%M")
    except ValueError:
        return "Некорректный формат времени тренировки (ожидается ЧЧ:ММ).", None

    # Проверка формата datetime-local полей
    try:
        datetime.strptime(registration_start, "%Y-%m-%dT%H:%M")
    except ValueError:
        return "Некорректный формат даты начала записи.", None

    try:
        datetime.strptime(registration_end, "%Y-%m-%dT%H:%M")
    except ValueError:
        return "Некорректный формат даты окончания записи.", None

    # Проверка числа игроков
    try:
        max_players = int(max_players_str)
        if max_players < 1 or max_players > 100:
            return "Количество игроков должно быть от 1 до 100.", None
    except ValueError:
        return "Количество игроков должно быть числом.", None

    # Проверка порядка дат записи
    if registration_start >= registration_end:
        return "Время начала записи должно быть раньше времени окончания.", None

    return None, max_players


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

    user_columns = [
        row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()
    ]

    if "is_superadmin" not in user_columns:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN is_superadmin INTEGER NOT NULL DEFAULT 0
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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL DEFAULT 'member',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        UNIQUE(group_id, user_id),
        FOREIGN KEY (group_id) REFERENCES groups(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        token TEXT NOT NULL UNIQUE,
        created_by INTEGER NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY (group_id) REFERENCES groups(id),
        FOREIGN KEY (created_by) REFERENCES users(id)
    )
    """)

    training_columns = [
        row[1] for row in cursor.execute("PRAGMA table_info(trainings)").fetchall()
    ]

    if "group_id" not in training_columns:
        cursor.execute("""
            ALTER TABLE trainings
            ADD COLUMN group_id INTEGER
        """)

    default_group = cursor.execute("""
        SELECT * FROM groups
        WHERE name = ?
    """, ("Основная группа",)).fetchone()

    if not default_group:
        cursor.execute("""
            INSERT INTO groups (name, description, created_at)
            VALUES (?, ?, ?)
        """, (
            "Вторник, Четверг. ТГАСУ",
            "Группа для существующих пользователей и тренировок",
            now_local().isoformat()
        ))

        default_group_id = cursor.lastrowid
    else:
        default_group_id = default_group["id"]

    cursor.execute("""
        UPDATE trainings
        SET group_id = ?
        WHERE group_id IS NULL
    """, (default_group_id,))

    approved_users = cursor.execute("""
        SELECT * FROM users
        WHERE status = 'approved'
    """).fetchall()

    for old_user in approved_users:
        exists = cursor.execute("""
            SELECT * FROM group_members
            WHERE group_id = ? AND user_id = ?
        """, (default_group_id, old_user["id"])).fetchone()

        if not exists:
            role = "admin" if old_user["is_admin"] == 1 else "member"

            cursor.execute("""
                INSERT INTO group_members (
                    group_id, user_id, role, status, created_at
                )
                VALUES (?, ?, ?, 'approved', ?)
            """, (
                default_group_id,
                old_user["id"],
                role,
                now_local().isoformat()
            ))


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

    if "completed_notification_sent" not in training_columns:
        cursor.execute("""
                ALTER TABLE trainings
                ADD COLUMN completed_notification_sent INTEGER NOT NULL DEFAULT 0
            """)

    registration_columns = [row[1] for row in cursor.execute("PRAGMA table_info(registrations)").fetchall()]

    if "is_paid" not in registration_columns:
        cursor.execute("""
            ALTER TABLE registrations
            ADD COLUMN is_paid INTEGER NOT NULL DEFAULT 0
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

    OLD_ADMIN_EMAIL = "admin@example.com"

    cursor.execute("""
        UPDATE users
        SET is_admin = 0,
            is_superadmin = 0
        WHERE email = ?
    """, (OLD_ADMIN_EMAIL,))

    NEW_SUPERADMIN_EMAIL = "basketapp@mail.ru"

    cursor.execute("""
        UPDATE users
        SET is_admin = 1,
            is_superadmin = 1,
            status = 'approved'
        WHERE email = ?
    """, (NEW_SUPERADMIN_EMAIL,))

    db.commit()
    db.close()


def init_firebase_admin():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        firebase_admin.initialize_app(cred)


def render_message_page(title, message):
    return render_template("message.html", title=title, message=message)

def send_email(to_email, subject, body):
    print(f"EMAIL: попытка отправки на {to_email}")

    if not to_email:
        print("EMAIL: пустой адрес")
        return

    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("EMAIL: не заданы EMAIL_ADDRESS или EMAIL_PASSWORD")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = f"Basket App <{EMAIL_ADDRESS}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT)
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, to_email, msg.as_string())
        server.quit()

        print(f"EMAIL: отправлено на {to_email}")

    except Exception as e:
        print("EMAIL ERROR:", repr(e))


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

    results = []

    if not url.startswith("http://") and not url.startswith("https://"):
        if not url.startswith("/"):
            url = "/" + url
        full_url = BASE_URL + url
    else:
        full_url = url

    for row in tokens:
        token = row["fcm_token"]

        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body
                ),
                webpush=messaging.WebpushConfig(
                    headers={
                        "Urgency": "high"
                    },
                    notification=messaging.WebpushNotification(
                        title=title,
                        body=body,
                        icon=f"{BASE_URL}/static/icon-192.png"
                    ),
                    fcm_options=messaging.WebpushFCMOptions(
                        link=full_url
                    )
                ),
                data={
                    "title": title,
                    "body": body,
                    "url": full_url
                },
                token=token
            )

            response = messaging.send(message)

            results.append({
                "status": "success",
                "user_id": user_id,
                "token_prefix": token[:25],
                "response": response
            })

        except Exception as e:
            results.append({
                "status": "error",
                "user_id": user_id,
                "token_prefix": token[:25],
                "error": repr(e)
            })

    return results


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

def is_superadmin(user):
    return user and user["is_superadmin"] == 1


def is_group_admin(cursor, user_id, group_id):
    row = cursor.execute("""
        SELECT *
        FROM group_members
        WHERE user_id = ?
          AND group_id = ?
          AND role = 'admin'
          AND status = 'approved'
    """, (user_id, group_id)).fetchone()

    return row is not None


def get_admin_groups(cursor, user):
    if is_superadmin(user):
        return cursor.execute("""
            SELECT *
            FROM groups
            ORDER BY name ASC
        """).fetchall()

    return cursor.execute("""
        SELECT g.*
        FROM group_members gm
        JOIN groups g ON g.id = gm.group_id
        WHERE gm.user_id = ?
          AND gm.role = 'admin'
          AND gm.status = 'approved'
        ORDER BY g.name ASC
    """, (user["id"],)).fetchall()

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

def group_admin_or_superadmin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = get_current_user()

        if not user:
            return redirect(url_for("login"))

        db = get_db()
        cursor = db.cursor()

        groups = get_admin_groups(cursor, user)

        db.close()

        if not groups:
            return render_message_page(
                "Нет доступа",
                "У вас нет прав администратора группы."
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

def get_user_active_main_registration(cursor, training_id, user_id):
    return cursor.execute("""
        SELECT *
        FROM registrations
        WHERE training_id = ?
          AND user_id = ?
          AND status = 'active'
          AND is_plus_one = 0
        LIMIT 1
    """, (training_id, user_id)).fetchone()

def round_up_to_10(amount):
    if amount <= 0:
        return 0
    return int(math.ceil(amount / 10.0) * 10)


def get_main_roster_count(cursor, training_id):
    row = cursor.execute("""
        SELECT COUNT(*) as count
        FROM registrations
        WHERE training_id = ?
          AND status = 'active'
    """, (training_id,)).fetchone()

    return row["count"] if row else 0


def calculate_training_payment_amount(cursor, training_id):
    main_count = get_main_roster_count(cursor, training_id)

    if main_count <= 0:
        return 0

    raw_amount = 2600 / main_count
    return round_up_to_10(raw_amount)

def can_user_pay_for_training(cursor, training, user):
    if not user or user["status"] != "approved":
        return False

    now = now_local()
    training_dt = get_training_datetime(training)
    payment_deadline = training_dt + timedelta(hours=24)

    if now < training_dt:
        return False

    if now > payment_deadline:
        return False

    registration = get_user_active_main_registration(cursor, training["id"], user["id"])

    if not registration:
        return False

    if registration["is_paid"] == 1:
        return False

    return True

def is_training_visible_for_user(cursor, training, user):
    now = now_local()
    training_dt = get_training_datetime(training)

    # До начала тренировка видна всем авторизованным пользователям
    if now < training_dt:
        return True

    # После начала — ещё 24 часа показываем только тем,
    # кто был в основном составе
    visible_until = training_dt + timedelta(hours=24)

    if now <= visible_until and user and user["status"] == "approved":
        return get_user_active_main_registration(cursor, training["id"], user["id"]) is not None
    return False

def is_training_hidden_for_users(training):
    """
    Скрываем тренировку с главной для обычных пользователей,
    когда время тренировки уже наступило.
    """
    return now_local() >= get_training_datetime(training)


def is_training_expired_for_admin(training):
    """
    Через 14 дней после начала тренировки она считается устаревшей
    и может быть удалена из базы.
    """
    return now_local() >= get_training_datetime(training) + timedelta(days=14)

def get_payment_page_url(training):
    training_dt = get_training_datetime(training)
    weekday = training_dt.weekday()  # Monday=0, Tuesday=1, Thursday=3

    if weekday == 1:  # Вторник
        return "/payment/tuesday"

    if weekday == 3:  # Четверг
        return "/payment/thursday"

    if weekday == 4:  # Пятница
        return "/payment/friday"

    return "/"

def get_payment_phone(training):
    training_dt = get_training_datetime(training)
    weekday = training_dt.weekday()  # Monday=0, Tuesday=1, Thursday=3, Friday=4

    if weekday == 1:
        return PAYMENT_PHONE_TUESDAY

    if weekday == 3:
        return PAYMENT_PHONE_THURSDAY

    return ""

def get_user_active_unpaid_training_for_weekday(user_id, weekday):
    db = get_db()
    cursor = db.cursor()

    registrations = cursor.execute("""
        SELECT r.*, t.title, t.training_date, t.training_time
        FROM registrations r
        JOIN trainings t ON t.id = r.training_id
        WHERE r.user_id = ?
          AND r.status = 'active'
          AND r.is_plus_one = 0
          AND r.is_paid = 0
        ORDER BY t.training_date DESC, t.training_time DESC
    """, (user_id,)).fetchall()

    db.close()

    now = now_local()

    for row in registrations:
        training_dt = parse_local_datetime(row["training_date"], row["training_time"])

        if training_dt.weekday() != weekday:
            continue

        if now < training_dt:
            continue

        if now > training_dt + timedelta(hours=24):
            continue

        return row

    return None

def get_payment_reminder_text(cursor, training):
    training_dt = get_training_datetime(training)
    weekday = training_dt.weekday()

    amount = calculate_training_payment_amount(cursor, training["id"])
    phone = get_payment_phone(training)

    if amount > 0 and phone:
        return f"Не забудь оплатить тренировку. {amount} ₽ на номер {phone}, Сбер"

    if amount > 0:
        return f"Не забудь оплатить тренировку. Сумма: {amount} ₽"

    return "Не забудь оплатить тренировку."

def is_training_finished(training):
    return now_local() > get_training_datetime(training) + timedelta(hours=3)

def cleanup_old_trainings():
    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    deleted_count = 0

    for training in trainings:
        if is_training_expired_for_admin(training):
            cursor.execute("""
                DELETE FROM registrations WHERE training_id = ?
            """, (training["id"],))

            cursor.execute("""
                DELETE FROM trainings WHERE id = ?
            """, (training["id"],))

            deleted_count += 1

    db.commit()
    db.close()

    print(f"CLEANUP: deleted old trainings = {deleted_count}")

def can_plus_one_be_added(training, active_count):
    training_dt = get_training_datetime(training)
    now = now_local()

    plus_one_open_dt = training_dt.replace(hour=8, minute=0, second=0, microsecond=0)
    before_training = now < training_dt

    return now >= plus_one_open_dt and before_training and active_count < training["max_players"]

def notify_completed_trainings():
    db = get_db()
    cursor = db.cursor()

    trainings = cursor.execute("""
        SELECT * FROM trainings
        WHERE completed_notification_sent = 0
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    admins = cursor.execute("""
        SELECT * FROM users
        WHERE is_admin = 1 AND status = 'approved'
    """).fetchall()

    for training in trainings:
        training_dt = get_training_datetime(training)
        finish_dt = training_dt + timedelta(hours=1, minutes=30)

        if now_local() >= finish_dt:
            active_players = cursor.execute("""
                SELECT r.*, u.email
                FROM registrations r
                JOIN users u ON u.id = r.user_id
                WHERE r.training_id = ?
                  AND r.status = 'active'
                ORDER BY r.created_at ASC
            """, (training["id"],)).fetchall()

            # Собираем письмо админу с полным составом
            if active_players:
                players_lines = []
                for idx, player in enumerate(active_players, start=1):
                    players_lines.append(f"{idx}. {player['display_name']} ({player['email']})")
                players_text = "\n".join(players_lines)
            else:
                players_text = "Никто не был записан в основной состав."

            subject = f"Состав участников: {training['title']} {training['training_date']} {training['training_time']}"
            body = (
                f"Тренировка завершена.\n\n"
                f"Название: {training['title']}\n"
                f"Дата: {training['training_date']}\n"
                f"Время: {training['training_time']}\n\n"
                f"Основной состав:\n{players_text}"
            )

            for admin in admins:
                try:
                    send_email(admin["email"], subject, body)
                except Exception as e:
                    print("COMPLETED ADMIN EMAIL ERROR:", repr(e))

            # Пуш всем из основного состава
            payment_page_url = get_payment_page_url(training)

            for player in active_players:
                try:
                    send_push_to_user_tokens(
                        player["user_id"],
                        "Напоминание об оплате",
                        "Не забудь оплатить тренировку",
                        payment_page_url
                    )
                except Exception as e:
                    print("COMPLETED PLAYER PUSH ERROR:", repr(e))

            # Помечаем тренировку как обработанную
            cursor.execute("""
                UPDATE trainings
                SET completed_notification_sent = 1
                WHERE id = ?
            """, (training["id"],))

    db.commit()
    db.close()

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
            title = "Открыта запись на тренировку"
            body = f"{training['title']} — {training['training_date']} {training['training_time']}"

            for user in approved_users:
                try:
                    send_push_to_user_tokens(
                        user["id"],
                        title,
                        body,
                        "/"
                    )
                except Exception as e:
                    print("OPEN REG PUSH ERROR:", repr(e))

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
                try:
                    send_push_to_user_tokens(
                        player["user_id"],
                        "Можно взять гостя",
                        "Еще есть места, можешь позвать с собой кого-нибудь!",
                        "/"
                    )
                except Exception as e:
                    print("PLUS ONE PUSH ERROR:", repr(e))

            cursor.execute("""
                UPDATE trainings
                SET plus_one_notification_sent = 1
                WHERE id = ?
            """, (training["id"],))

    db.commit()
    db.close()

def notify_free_spot_on_training_day(training_id):
    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return

    training_dt = get_training_datetime(training)
    now = now_local()

    # Только в день тренировки
    if now.date() != training_dt.date():
        db.close()
        return

    active_count = cursor.execute("""
        SELECT COUNT(*) as count
        FROM registrations
        WHERE training_id = ? AND status = 'active'
    """, (training_id,)).fetchone()["count"]

    # Если свободного места нет — выходим
    if active_count >= training["max_players"]:
        db.close()
        return

    waitlist_count = cursor.execute("""
        SELECT COUNT(*) as count
        FROM registrations
        WHERE training_id = ? AND status = 'waitlist'
    """, (training_id,)).fetchone()["count"]

    # Если есть очередь — общий пуш не отправляем
    if waitlist_count > 0:
        db.close()
        return

    free_users = cursor.execute("""
        SELECT u.*
        FROM users u
        WHERE u.status = 'approved'
          AND NOT EXISTS (
              SELECT 1
              FROM registrations r
              WHERE r.training_id = ?
                AND r.user_id = u.id
          )
    """, (training_id,)).fetchall()

    active_main_players = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ?
          AND r.status = 'active'
          AND r.is_plus_one = 0
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    plus_one_available = can_plus_one_be_added(training, active_count)

    db.close()

    # 1) Пуш свободным пользователям
    title = "Освободилось место"
    body = f"{training['title']} — сегодня в {training['training_time']}. Успей записаться."

    for user in free_users:
        try:
            send_push_to_user_tokens(
                user["id"],
                title,
                body,
                "/"
            )
        except Exception as e:
            print("FREE SPOT PUSH ERROR:", repr(e))

    # 2) Пуш основному составу, что можно взять +1
    if plus_one_available:
        plus_one_title = "Можно взять +1"
        plus_one_body = "Освободилось место. Если хочешь, можешь добавить гостя +1."

        for player in active_main_players:
            try:
                send_push_to_user_tokens(
                    player["user_id"],
                    plus_one_title,
                    plus_one_body,
                    "/"
                )
            except Exception as e:
                print("PLUS ONE FREE SPOT PUSH ERROR:", repr(e))

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

    all_trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date ASC, training_time ASC
    """).fetchall()

    trainings = [
        t for t in all_trainings
        if is_training_visible_for_user(cursor, t, user)
    ]

    trainings_data = []

    for training in trainings:

        registration = None

        if user:
            registration = cursor.execute("""
                SELECT *
                FROM registrations
                WHERE training_id = ?
                  AND user_id = ?
                  AND status = 'active'
                  AND is_plus_one = 0
                LIMIT 1
            """, (training["id"], user["id"])).fetchone()

        # ЕСЛИ ОПЛАЧЕНО — ПРОПУСКАЕМ ТРЕНИРОВКУ
        if registration and registration["is_paid"] == 1:
            continue

        payment_available = can_user_pay_for_training(cursor, training, user)
        payment_url = get_payment_page_url(training)

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

        date_obj = datetime.strptime(training["training_date"], "%Y-%m-%d")
        weekday_map = [
            "ПОНЕДЕЛЬНИК",
            "ВТОРНИК",
            "СРЕДА",
            "ЧЕТВЕРГ",
            "ПЯТНИЦА",
            "СУББОТА",
            "ВОСКРЕСЕНЬЕ"
        ]
        weekday_name = weekday_map[date_obj.weekday()]

        training_started = now_local() >= get_training_datetime(training)

        trainings_data.append({
            "training": training,
            "players": active_players,
            "waitlist": waitlist,
            "registration_status": get_registration_status(training),
            "plus_one_available": can_plus_one_be_added(training, len(active_players)),
            "weekday": weekday_name,
            "current_user_registration": current_user_registration,
            "payment_available": payment_available,
            "payment_url": payment_url,
            "training_started": training_started
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

    all_trainings = cursor.execute("""
        SELECT * FROM trainings
        ORDER BY training_date DESC, training_time DESC
    """).fetchall()

    trainings = [t for t in all_trainings if not is_training_expired_for_admin(t)]

    db.close()

    return render_template(
        "admin_panel.html",
        user=user,
        pending_users=pending_users,
        all_users=all_users,
        trainings=trainings
    )

@app.route("/payment/confirm/<weekday_key>", methods=["POST"])
@login_required
def confirm_payment(weekday_key):
    user = get_current_user()

    weekday_map = {
        "tuesday": 1,
        "thursday": 3,
        "friday": 4,
    }

    if weekday_key not in weekday_map:
        return render_message_page("Ошибка", "Некорректный тип оплаты.")

    unpaid_training = get_user_active_unpaid_training_for_weekday(
        user["id"],
        weekday_map[weekday_key]
    )

    if not unpaid_training:
        return render_message_page(
            "Оплата не найдена",
            "Для этого дня нет подходящей неоплаченной тренировки."
        )

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE registrations
        SET is_paid = 1
        WHERE id = ?
    """, (unpaid_training["id"],))

    db.commit()
    db.close()

    return render_message_page(
        "Спасибо",
        "Оплата отмечена. Тренировка больше не будет показываться в списке."
    )

@app.route("/payment/tuesday")
@login_required
def payment_tuesday():
    user = get_current_user()
    unpaid_training = get_user_active_unpaid_training_for_weekday(user["id"], 1)

    amount = 0
    if unpaid_training:
        db = get_db()
        cursor = db.cursor()
        amount = calculate_training_payment_amount(cursor, unpaid_training["training_id"])
        db.close()

    payment_text = f"{amount} ₽ на Сбер" if amount > 0 else "Оплата тренировки на Сбер"

    return render_template(
        "payment_page.html",
        payment_title="Оплата тренировки во вторник",
        payment_text=payment_text,
        payment_link=PAYMENT_LINK_TUESDAY,
        payment_phone=PAYMENT_PHONE_TUESDAY,
        payment_qr=PAYMENT_QR_TUESDAY,
        payment_day_key="tuesday",
        payment_amount=amount
    )


@app.route("/payment/thursday")
@login_required
def payment_thursday():
    user = get_current_user()
    unpaid_training = get_user_active_unpaid_training_for_weekday(user["id"], 3)

    amount = 0
    if unpaid_training:
        db = get_db()
        cursor = db.cursor()
        amount = calculate_training_payment_amount(cursor, unpaid_training["training_id"])
        db.close()

    payment_text = f"{amount} ₽ на Сбер" if amount > 0 else "Оплата тренировки на Сбер"

    return render_template(
        "payment_page.html",
        payment_title="Оплата тренировки в четверг",
        payment_text=payment_text,
        payment_link=PAYMENT_LINK_THURSDAY,
        payment_phone=PAYMENT_PHONE_THURSDAY,
        payment_qr=PAYMENT_QR_THURSDAY,
        payment_day_key="thursday",
        payment_amount=amount
    )

@app.route("/payment/friday")
@login_required
def payment_friday():
    user = get_current_user()
    unpaid_training = get_user_active_unpaid_training_for_weekday(user["id"], 4)

    amount = 0
    if unpaid_training:
        db = get_db()
        cursor = db.cursor()
        amount = calculate_training_payment_amount(cursor, unpaid_training["training_id"])
        db.close()

    payment_text = f"{amount} ₽ на Сбер" if amount > 0 else "Оплата тренировки на Сбер"

    return render_template(
        "payment_page.html",
        payment_title="Оплата тренировки в пятницу",
        payment_text=payment_text,
        payment_link=PAYMENT_LINK_FRIDAY,
        payment_phone="",
        payment_qr="",
        payment_day_key="friday",
        payment_amount=amount
    )

@app.route("/tasks/check-open-notifications")
def check_open_notifications():
    key = request.args.get("key", "")
    if key != TASK_SECRET:
        abort(403)
    notify_open_trainings()
    return "ok", 200


@app.route("/tasks/check-plus-one-notifications")
def check_plus_one_notifications():
    key = request.args.get("key", "")
    if key != TASK_SECRET:
        abort(403)
    notify_plus_one_available()
    return "ok", 200


@app.route("/tasks/run-all")
def run_all_tasks():
    key = request.args.get("key", "")

    if key != TASK_SECRET:
        abort(403)

    notify_open_trainings()
    notify_plus_one_available()
    notify_completed_trainings()
    cleanup_old_trainings()

    return "ok", 200


@app.route("/register", methods=["GET", "POST"])
def register_account():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        group_id = request.form.get("group_id", "").strip()

        if not display_name or not email or not password or not group_id:
            return render_message_page(
                "Ошибка",
                "Заполните все поля."
            )

        # Минимальная политика паролей
        if len(password) < 8:
            return render_message_page(
                "Слабый пароль",
                "Пароль должен содержать не менее 8 символов."
            )

        # Базовая проверка формата email
        if "@" not in email or "." not in email.split("@")[-1]:
            return render_message_page(
                "Некорректный email",
                "Введите корректный адрес электронной почты."
            )

        db = get_db()
        cursor = db.cursor()

        group = cursor.execute("""
            SELECT *
            FROM groups
            WHERE id = ?
        """, (group_id,)).fetchone()

        if not group:
            db.close()
            return render_message_page(
                "Ошибка",
                "Выбранная группа не найдена."
            )

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

        new_user_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO group_members (
                group_id, user_id, role, status, created_at
            )
            VALUES (?, ?, 'member', 'pending', ?)
        """, (
            group_id,
            new_user_id,
            now_local().isoformat()
        ))

        db.commit()

        admins = cursor.execute("""
            SELECT u.*
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
              AND gm.role = 'admin'
              AND gm.status = 'approved'
              AND u.status = 'approved'
        """, (group_id,)).fetchall()

        db.close()

        for admin in admins:
            try:
                send_push_to_user_tokens(
                    admin["id"],
                    "Новая заявка",
                    f"Пользователь {display_name} ждёт одобрения",
                    "/admin-panel"
                )
            except Exception as e:
                print("REGISTER ADMIN PUSH ERROR:", repr(e))

        return render_message_page(
            "Заявка отправлена",
            "Ваш аккаунт создан и ожидает одобрения администратором."
        )

    db = get_db()
    cursor = db.cursor()

    groups = cursor.execute("""
        SELECT *
        FROM groups
        ORDER BY name ASC
    """).fetchall()

    db.close()

    return render_template("register.html", groups=groups)


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

        session.permanent = True
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
            training_id, user_id, display_name, status, created_at, is_plus_one, parent_registration_id, is_paid
        )
        VALUES (?, ?, ?, ?, ?, 0, NULL, 0)
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

            try:
                send_push_to_user_tokens(
                    next_user["user_id"],
                    "Вы в основном составе",
                    f"{training['title']} — {training['training_date']} {training['training_time']}",
                    "/"
                )
            except Exception as e:
                print("QUEUE PROMOTE PUSH ERROR:", repr(e))

            active_count += 1

    if removed_status == "active":
        notify_free_spot_on_training_day(training_id)

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
            training_id, user_id, display_name, status, created_at, is_plus_one, parent_registration_id, is_paid
        )
        VALUES (?, ?, ?, 'active', ?, 1, ?, 0)
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


@app.route("/admin/approve_user/<int:user_id>", methods=["POST"])
@admin_required
def approve_user(user_id):
    db = get_db()
    cursor = db.cursor()

    user_to_approve = cursor.execute("""
        SELECT * FROM users WHERE id = ?
    """, (user_id,)).fetchone()

    if not user_to_approve:
        db.close()
        return redirect(url_for("admin_panel"))

    cursor.execute("""
        UPDATE users
        SET status = 'approved'
        WHERE id = ?
    """, (user_id,))

    db.commit()
    db.close()

    try:
        send_push_to_user_tokens(
            user_to_approve["id"],
            "Доступ одобрен",
            "Теперь вы можете записываться на тренировки.",
            "/"
        )
    except Exception as e:
        print("APPROVE PUSH ERROR:", repr(e))

    try:
        send_email(
            user_to_approve["email"],
            "Заявка одобрена",
            (
                f"Здравствуйте, {user_to_approve['display_name']}!\n\n"
                f"Ваша заявка на доступ к Basket App одобрена.\n"
                f"Теперь вы можете войти на сайт и записываться на тренировки.\n\n"
                f"Сайт: {BASE_URL}"
            )
        )
    except Exception as e:
        print("APPROVE EMAIL ERROR:", repr(e))

    return redirect(url_for("admin_panel"))


@app.route("/admin/block_user/<int:user_id>", methods=["POST"])
@admin_required
def block_user(user_id):
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        UPDATE users
        SET status = 'blocked'
        WHERE id = ?
    """, (user_id,))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))

@app.route("/admin/delete_user/<int:user_id>", methods=["POST"])
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

    deleted_email = user_to_delete["email"]
    deleted_name = user_to_delete["display_name"]

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

    if removed_status == "active":
        notify_free_spot_on_training_day(training_id)

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

    try:
        send_email(
            deleted_email,
            "Ваш аккаунт удалён",
            (
                f"Здравствуйте, {deleted_name}!\n\n"
                f"Ваш аккаунт в Basket App был удалён администратором.\n"
                f"Если это произошло по ошибке, свяжитесь с администратором."
            )
        )
    except Exception as e:
        print("DELETE USER EMAIL ERROR:", repr(e))

    return redirect(url_for("admin_panel"))

@app.route("/admin/create_training", methods=["POST"])
@admin_required
def create_training():
    title = request.form.get("title", "").strip()
    training_date = request.form.get("training_date", "").strip()
    training_time = request.form.get("training_time", "").strip()
    max_players_str = request.form.get("max_players", "15").strip()
    registration_start = request.form.get("registration_start", "").strip()
    registration_end = request.form.get("registration_end", "").strip()

    error, max_players = validate_training_form(
        title, training_date, training_time, max_players_str,
        registration_start, registration_end
    )
    if error:
        return render_message_page("Ошибка данных", error)

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO trainings (
            title, training_date, training_time, max_players,
            registration_start, registration_end,
            open_notification_sent, plus_one_notification_sent, completed_notification_sent
        )
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
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
    max_players_str = request.form.get("max_players", "15").strip()
    registration_start = request.form.get("registration_start", "").strip()
    registration_end = request.form.get("registration_end", "").strip()

    error, max_players = validate_training_form(
        title, training_date, training_time, max_players_str,
        registration_start, registration_end
    )
    if error:
        return render_message_page("Ошибка данных", error)

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


@app.route("/admin/delete_training/<int:training_id>", methods=["POST"])
@admin_required
def delete_training(training_id):
    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return redirect(url_for("admin_panel"))

    registrations = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ?
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    for registration in registrations:
        try:
            send_push_to_user_tokens(
                registration["user_id"],
                "Тренировка отменена",
                f"{training['title']} — {training['training_date']} {training['training_time']}",
                "/"
            )
        except Exception as e:
            print("DELETE TRAINING PUSH ERROR:", repr(e))

    cursor.execute("DELETE FROM registrations WHERE training_id = ?", (training_id,))
    cursor.execute("DELETE FROM trainings WHERE id = ?", (training_id,))

    db.commit()
    db.close()

    return redirect(url_for("admin_panel"))


@app.route("/admin/generate_schedule", methods=["POST"])
@admin_required
def generate_schedule():
    db = get_db()
    cursor = db.cursor()

    today = now_local().date()
    days_ahead = 7
    created = 0

    for i in range(days_ahead):
        day = today + timedelta(days=i)

        if day.weekday() in [1, 3]:
            training_date = day.strftime("%Y-%m-%d")
            training_time = "20:30"

            reg_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=APP_TZ) - timedelta(days=1)
            reg_start = reg_start.replace(hour=12, minute=0)

            reg_end = datetime.combine(day, datetime.min.time()).replace(tzinfo=APP_TZ)
            reg_end = reg_end.replace(hour=19, minute=30)

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
                        open_notification_sent, plus_one_notification_sent, completed_notification_sent
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
                """, (
                    "ТГАСУ/Партизанская 16",
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

@app.route("/admin/toggle-payment/<int:registration_id>", methods=["POST"])
@admin_required
def toggle_payment(registration_id):
    db = get_db()
    cursor = db.cursor()

    registration = cursor.execute("""
        SELECT * FROM registrations WHERE id = ?
    """, (registration_id,)).fetchone()

    if not registration:
        db.close()
        return redirect(url_for("admin_panel"))

    training_id = registration["training_id"]
    new_value = 0 if registration["is_paid"] == 1 else 1

    cursor.execute("""
        UPDATE registrations
        SET is_paid = ?
        WHERE id = ?
    """, (new_value, registration_id))

    db.commit()
    db.close()

    # Безопасный редирект: только на внутреннюю страницу тренировки
    return redirect(url_for("admin_training_detail", training_id=training_id))

@app.route("/debug/all-users")
@admin_required
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
@admin_required
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
    results = send_push_to_user_tokens(
        user_id=user_id,
        title="Тестовое уведомление",
        body="Push-уведомления работают!",
        url="/"
    )

    if not results:
        return "no active tokens found"

    lines = []
    for item in results:
        if item["status"] == "success":
            lines.append(
                f"SUCCESS | user_id={item['user_id']} | token={item['token_prefix']}... | response={item['response']}"
            )
        else:
            lines.append(
                f"ERROR | user_id={item['user_id']} | token={item['token_prefix']}... | error={item['error']}"
            )

    return "<br>".join(lines)

@app.route("/debug/send-test-email")
@admin_required
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


@app.route("/debug/time")
@admin_required
def debug_time():
    now = now_local()
    return (
        f"app_now={now.isoformat()}<br>"
        f"app_tz={APP_TZ}<br>"
        f"server_now_naive={datetime.now().isoformat()}"
    )

@app.route("/debug/test-payment-push/tuesday/<int:user_id>")
@admin_required
def debug_test_payment_push_tuesday(user_id):
    try:
        send_push_to_user_tokens(
            user_id,
            "Не забудь оплатить тренировку 💳",
            "Перейди, чтобы оплатить",
            "/payment/tuesday"
        )
        return "tuesday push sent", 200
    except Exception as e:
        return f"error: {repr(e)}", 500

@app.route("/debug/payment-links")
@login_required
def debug_payment_links():
    return render_template(
        "debug_payment_links.html",
        tuesday_url="/payment/tuesday",
        thursday_url="/payment/thursday"
    )

@app.route("/admin/training/<int:training_id>")
@admin_required
def admin_training_detail(training_id):
    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return render_message_page("Не найдено", "Тренировка не найдена.")

    if is_training_expired_for_admin(training):
        db.close()
        return render_message_page(
            "Архив удалён",
            "Эта тренировка старше 14 дней и больше недоступна."
        )

    active_players = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ? AND r.status = 'active'
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    waitlist_players = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ? AND r.status = 'waitlist'
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    main_roster_count = get_main_roster_count(cursor, training_id)
    raw_payment_amount = 2600 / main_roster_count if main_roster_count > 0 else 0
    payment_amount = calculate_training_payment_amount(cursor, training_id)

    db.close()

    return render_template(
        "admin_training_detail.html",
        training=training,
        active_players=active_players,
        waitlist_players=waitlist_players,
        payment_amount=payment_amount,
        main_roster_count=main_roster_count,
        raw_payment_amount=raw_payment_amount
    )

@app.route("/admin/training/<int:training_id>/send-payment-reminder", methods=["POST"])
@admin_required
def admin_send_payment_reminder(training_id):
    db = get_db()
    cursor = db.cursor()

    training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not training:
        db.close()
        return render_message_page("Не найдено", "Тренировка не найдена.")

    unpaid_players = cursor.execute("""
        SELECT r.*, u.email
        FROM registrations r
        JOIN users u ON u.id = r.user_id
        WHERE r.training_id = ?
          AND r.status = 'active'
          AND r.is_paid = 0
          AND r.is_plus_one = 0
        ORDER BY r.created_at ASC
    """, (training_id,)).fetchall()

    if not unpaid_players:
        db.close()
        return render_message_page(
            "Готово",
            "У этой тренировки нет неоплаченных участников в основном составе."
        )

    payment_page_url = get_payment_page_url(training)
    reminder_text = get_payment_reminder_text(cursor, training)

    sent_count = 0
    error_count = 0

    for player in unpaid_players:
        try:
            results = send_push_to_user_tokens(
                player["user_id"],
                "Напоминание об оплате",
                reminder_text,
                payment_page_url
            )

            if results:
                has_success = any(item["status"] == "success" for item in results)
                if has_success:
                    sent_count += 1
                else:
                    error_count += 1
            else:
                error_count += 1

        except Exception as e:
            print("ADMIN PAYMENT REMINDER PUSH ERROR:", repr(e))
            error_count += 1

    db.close()

    return render_message_page(
        "Напоминание отправлено",
        f"Успешно отправлено: {sent_count}. Ошибок: {error_count}."
    )

@app.route("/admin/training/<int:training_id>/update", methods=["POST"])
@admin_required
def admin_training_update(training_id):
    title = request.form.get("title", "").strip()
    training_date = request.form.get("training_date", "").strip()
    training_time = request.form.get("training_time", "").strip()
    max_players_str = request.form.get("max_players", "15").strip()
    registration_start = request.form.get("registration_start", "").strip()
    registration_end = request.form.get("registration_end", "").strip()

    error, max_players = validate_training_form(
        title, training_date, training_time, max_players_str,
        registration_start, registration_end
    )
    if error:
        return render_message_page("Ошибка данных", error)

    db = get_db()
    cursor = db.cursor()

    old_training = cursor.execute("""
        SELECT * FROM trainings WHERE id = ?
    """, (training_id,)).fetchone()

    if not old_training:
        db.close()
        return render_message_page("Ошибка", "Тренировка не найдена.")

    new_open_notification_sent = old_training["open_notification_sent"]
    new_plus_one_notification_sent = old_training["plus_one_notification_sent"]
    new_completed_notification_sent = old_training["completed_notification_sent"]

    if old_training["registration_start"] != registration_start:
        new_open_notification_sent = 0

    if (
        old_training["training_date"] != training_date
        or old_training["training_time"] != training_time
    ):
        new_plus_one_notification_sent = 0
        new_completed_notification_sent = 0

    cursor.execute("""
        UPDATE trainings
        SET title = ?, training_date = ?, training_time = ?, max_players = ?,
            registration_start = ?, registration_end = ?,
            open_notification_sent = ?, plus_one_notification_sent = ?, completed_notification_sent = ?
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
        new_completed_notification_sent,
        training_id
    ))

    db.commit()
    db.close()

    return redirect(url_for("admin_training_detail", training_id=training_id))

@app.route("/admin/training/<int:training_id>/remove-registration/<int:registration_id>", methods=["POST"])
@admin_required
def admin_remove_registration(training_id, registration_id):
    db = get_db()
    cursor = db.cursor()

    registration = cursor.execute("""
        SELECT * FROM registrations WHERE id = ?
    """, (registration_id,)).fetchone()

    if not registration:
        db.close()
        return redirect(url_for("admin_training_detail", training_id=training_id))

    # Защита от IDOR: убедиться что регистрация принадлежит именно этой тренировке
    if registration["training_id"] != training_id:
        db.close()
        return render_message_page(
            "Ошибка",
            "Регистрация не принадлежит указанной тренировке."
        )

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

            try:
                send_push_to_user_tokens(
                    next_user["user_id"],
                    "Вы в основном составе",
                    f"{training['title']} — {training['training_date']} {training['training_time']}",
                    "/"
                )
            except Exception as e:
                print("ADMIN REMOVE REG PUSH ERROR:", repr(e))

            active_count += 1

    if removed_status == "active":
        notify_free_spot_on_training_day(training_id)

    db.close()
    return redirect(url_for("admin_training_detail", training_id=training_id))

@app.route("/debug/groups")
@admin_required
def debug_groups():
    db = get_db()
    cursor = db.cursor()

    rows = cursor.execute("""
        SELECT 
            g.id as group_id,
            g.name as group_name,
            u.display_name,
            u.email,
            gm.role,
            gm.status
        FROM group_members gm
        JOIN groups g ON g.id = gm.group_id
        JOIN users u ON u.id = gm.user_id
        ORDER BY g.id, u.display_name
    """).fetchall()

    db.close()

    return "<br>".join([
        f"group={r['group_id']} {r['group_name']} | {r['display_name']} | {r['email']} | role={r['role']} | status={r['status']}"
        for r in rows
    ])

@app.route("/group-admin")
@group_admin_or_superadmin_required
def group_admin_panel():
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    admin_groups = get_admin_groups(cursor, user)

    groups_data = []

    for group in admin_groups:
        pending_members = cursor.execute("""
            SELECT gm.*, u.display_name, u.email
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
              AND gm.status = 'pending'
            ORDER BY gm.created_at ASC
        """, (group["id"],)).fetchall()

        approved_members = cursor.execute("""
            SELECT gm.*, u.display_name, u.email
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
              AND gm.status = 'approved'
            ORDER BY u.display_name ASC
        """, (group["id"],)).fetchall()

        trainings = cursor.execute("""
            SELECT *
            FROM trainings
            WHERE group_id = ?
            ORDER BY training_date DESC, training_time DESC
        """, (group["id"],)).fetchall()

        groups_data.append({
            "group": group,
            "pending_members": pending_members,
            "approved_members": approved_members,
            "trainings": trainings
        })

    db.close()

    return render_template(
        "group_admin_panel.html",
        user=user,
        groups_data=groups_data
    )

@app.route("/group-admin/approve-member/<int:group_member_id>", methods=["POST"])
@group_admin_or_superadmin_required
def approve_group_member(group_member_id):
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    member = cursor.execute("""
        SELECT gm.*, u.email, u.display_name
        FROM group_members gm
        JOIN users u ON u.id = gm.user_id
        WHERE gm.id = ?
    """, (group_member_id,)).fetchone()

    if not member:
        db.close()
        return redirect(url_for("group_admin_panel"))

    if not is_superadmin(user) and not is_group_admin(cursor, user["id"], member["group_id"]):
        db.close()
        return render_message_page(
            "Нет доступа",
            "Вы не можете одобрять заявки в этой группе."
        )

    cursor.execute("""
        UPDATE group_members
        SET status = 'approved'
        WHERE id = ?
    """, (group_member_id,))

    cursor.execute("""
        UPDATE users
        SET status = 'approved'
        WHERE id = ?
    """, (member["user_id"],))

    db.commit()
    db.close()

    try:
        send_push_to_user_tokens(
            member["user_id"],
            "Доступ в группу одобрен",
            "Теперь вы можете записываться на тренировки своей группы.",
            "/"
        )
    except Exception as e:
        print("APPROVE GROUP MEMBER PUSH ERROR:", repr(e))

    try:
        send_email(
            member["email"],
            "Заявка в группу одобрена",
            (
                f"Здравствуйте, {member['display_name']}!\n\n"
                f"Ваша заявка в группу одобрена.\n"
                f"Теперь вы можете войти на сайт и записываться на тренировки."
            )
        )
    except Exception as e:
        print("APPROVE GROUP MEMBER EMAIL ERROR:", repr(e))

    return redirect(url_for("group_admin_panel"))

@app.route("/group-admin/block-member/<int:group_member_id>", methods=["POST"])
@group_admin_or_superadmin_required
def block_group_member(group_member_id):
    user = get_current_user()

    db = get_db()
    cursor = db.cursor()

    member = cursor.execute("""
        SELECT *
        FROM group_members
        WHERE id = ?
    """, (group_member_id,)).fetchone()

    if not member:
        db.close()
        return redirect(url_for("group_admin_panel"))

    if not is_superadmin(user) and not is_group_admin(cursor, user["id"], member["group_id"]):
        db.close()
        return render_message_page(
            "Нет доступа",
            "Вы не можете отклонять заявки в этой группе."
        )

    cursor.execute("""
        UPDATE group_members
        SET status = 'blocked'
        WHERE id = ?
    """, (group_member_id,))

    db.commit()
    db.close()

    return redirect(url_for("group_admin_panel"))

if __name__ == "__main__":
    app.run(debug=True)