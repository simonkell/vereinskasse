from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import sqlite3
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from .camt import parse_camt
from .db import close_db, get_db, init_db, log_action


ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
TAX_AREAS = (
    "Ideeller Bereich",
    "Vermögensverwaltung",
    "Zweckbetrieb",
    "Wirtschaftlicher Geschäftsbetrieb",
)


def create_app(test_config=None):
    app = Flask(__name__)
    data_dir = Path(os.environ.get("DATA_DIR", "data")).resolve()
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin"),
        DATA_DIR=data_dir,
        DATABASE=str(data_dir / "vereinskasse.sqlite3"),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)

    for folder in ("imports", "attachments"):
        (Path(app.config["DATA_DIR"]) / folder).mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    @app.template_filter("money")
    def money(cents, currency="EUR"):
        amount = (cents or 0) / 100
        formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{formatted} {currency}"

    @app.template_filter("date_de")
    def date_de(value):
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except (TypeError, ValueError):
            return value or ""

    def login_required(view):
        @wraps(view)
        def wrapped(**kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login", next=request.path))
            return view(**kwargs)

        return wrapped

    @app.before_request
    def csrf_protect():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "login":
            token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not token or not hmac.compare_digest(token, session.get("csrf_token", "")):
                abort(400, "Ungültiges CSRF-Token")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.context_processor
    def globals_for_templates():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf_token"], "tax_areas": TAX_AREAS}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if hmac.compare_digest(password, app.config["ADMIN_PASSWORD"]):
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                target = request.args.get("next", "")
                if not target.startswith("/") or target.startswith("//"):
                    target = url_for("dashboard")
                return redirect(target)
            flash("Das Passwort ist nicht korrekt.", "error")
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        db = get_db()
        selected_year = request.args.get("year", str(datetime.now().year))
        years = [
            row["year"]
            for row in db.execute(
                "SELECT DISTINCT substr(booking_date, 1, 4) AS year FROM transactions ORDER BY year DESC"
            )
        ]
        if selected_year not in years:
            years.insert(0, selected_year)
        totals = db.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) income,
              COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) expenses,
              COALESCE(SUM(amount_cents), 0) balance,
              COUNT(*) count,
              SUM(CASE WHEN receipt_status = 'missing' THEN 1 ELSE 0 END) missing,
              SUM(CASE WHEN category_id IS NULL THEN 1 ELSE 0 END) uncategorized
            FROM transactions WHERE substr(booking_date, 1, 4) = ?
            """,
            (selected_year,),
        ).fetchone()
        by_category = db.execute(
            """
            SELECT COALESCE(c.name, 'Ohne Kategorie') name,
                   COALESCE(c.tax_area, 'Nicht zugeordnet') tax_area,
                   SUM(t.amount_cents) total, COUNT(*) count
            FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
            WHERE substr(t.booking_date, 1, 4) = ?
            GROUP BY c.id, c.name, c.tax_area ORDER BY ABS(SUM(t.amount_cents)) DESC
            """,
            (selected_year,),
        ).fetchall()
        recent = db.execute(
            """
            SELECT t.*, c.name category_name,
                   (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
            FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
            ORDER BY booking_date DESC, t.id DESC LIMIT 8
            """
        ).fetchall()
        return render_template(
            "dashboard.html",
            totals=totals,
            by_category=by_category,
            recent=recent,
            years=years,
            selected_year=selected_year,
        )

    @app.get("/transactions")
    @login_required
    def transactions():
        db = get_db()
        year = request.args.get("year", "")
        status = request.args.get("status", "")
        query = """
            SELECT t.*, c.name category_name,
                   (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
            FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE 1=1
        """
        params = []
        if year:
            query += " AND substr(t.booking_date,1,4)=?"
            params.append(year)
        if status == "missing":
            query += " AND t.receipt_status='missing'"
        elif status == "uncategorized":
            query += " AND t.category_id IS NULL"
        query += " ORDER BY t.booking_date DESC, t.id DESC"
        rows = db.execute(query, params).fetchall()
        years = db.execute(
            "SELECT DISTINCT substr(booking_date,1,4) year FROM transactions ORDER BY year DESC"
        ).fetchall()
        return render_template("transactions.html", transactions=rows, years=years, year=year, status=status)

    @app.get("/transactions/<int:transaction_id>")
    @login_required
    def transaction_detail(transaction_id):
        db = get_db()
        transaction = db.execute(
            """SELECT t.*, c.name category_name, c.tax_area
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?""",
            (transaction_id,),
        ).fetchone()
        if transaction is None:
            abort(404)
        attachments = db.execute(
            "SELECT * FROM attachments WHERE transaction_id=? ORDER BY created_at", (transaction_id,)
        ).fetchall()
        categories = db.execute("SELECT * FROM categories WHERE active=1 ORDER BY name").fetchall()
        return render_template(
            "transaction_detail.html",
            transaction=transaction,
            attachments=attachments,
            categories=categories,
        )

    @app.post("/transactions/<int:transaction_id>/update")
    @login_required
    def transaction_update(transaction_id):
        db = get_db()
        category_id = request.form.get("category_id") or None
        status = request.form.get("receipt_status", "missing")
        if status not in {"missing", "complete", "not_required"}:
            abort(400)
        db.execute(
            """UPDATE transactions SET category_id=?, receipt_status=?, note=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (category_id, status, request.form.get("note", "").strip(), transaction_id),
        )
        log_action(
            db,
            "updated",
            "transaction",
            transaction_id,
            json.dumps({"category_id": category_id, "receipt_status": status}, ensure_ascii=False),
        )
        db.commit()
        flash("Buchung gespeichert.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.post("/transactions/<int:transaction_id>/attachments")
    @login_required
    def attachment_upload(transaction_id):
        upload = request.files.get("attachment")
        if not upload or not upload.filename:
            flash("Bitte eine Datei auswählen.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))
        extension = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
        if extension not in ALLOWED_EXTENSIONS:
            flash("Erlaubt sind PDF-, PNG- und JPEG-Dateien.", "error")
            return redirect(url_for("transaction_detail", transaction_id=transaction_id))

        db = get_db()
        if db.execute("SELECT 1 FROM transactions WHERE id=?", (transaction_id,)).fetchone() is None:
            abort(404)
        storage_name = f"{uuid.uuid4().hex}.{extension}"
        target = Path(app.config["DATA_DIR"]) / "attachments" / storage_name
        upload.save(target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        db.execute(
            """INSERT INTO attachments(transaction_id, original_name, stored_path, mime_type, file_hash, size_bytes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                transaction_id,
                secure_filename(upload.filename) or f"beleg.{extension}",
                str(target.relative_to(app.config["DATA_DIR"])),
                upload.mimetype,
                digest,
                target.stat().st_size,
            ),
        )
        db.execute(
            "UPDATE transactions SET receipt_status='complete', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (transaction_id,),
        )
        log_action(db, "uploaded", "attachment", transaction_id, upload.filename)
        db.commit()
        flash("Beleg hochgeladen.", "success")
        return redirect(url_for("transaction_detail", transaction_id=transaction_id))

    @app.get("/attachments/<int:attachment_id>")
    @login_required
    def attachment_download(attachment_id):
        row = get_db().execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if row is None:
            abort(404)
        path = Path(app.config["DATA_DIR"]) / row["stored_path"]
        return send_file(path, download_name=row["original_name"], as_attachment=False)

    @app.route("/import", methods=["GET", "POST"])
    @login_required
    def import_file():
        if request.method == "GET":
            batches = get_db().execute(
                "SELECT * FROM import_batches ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            return render_template("import.html", batches=batches)

        upload = request.files.get("statement")
        if not upload or not upload.filename:
            flash("Bitte eine CAMT-Datei auswählen.", "error")
            return redirect(url_for("import_file"))
        temporary = Path(app.config["DATA_DIR"]) / "imports" / f"tmp-{uuid.uuid4().hex}.xml"
        upload.save(temporary)
        file_hash = hashlib.sha256(temporary.read_bytes()).hexdigest()
        db = get_db()
        if db.execute("SELECT 1 FROM import_batches WHERE file_hash=?", (file_hash,)).fetchone():
            temporary.unlink(missing_ok=True)
            flash("Diese CAMT-Datei wurde bereits importiert.", "error")
            return redirect(url_for("import_file"))
        try:
            report = parse_camt(temporary)
        except ValueError as exc:
            temporary.unlink(missing_ok=True)
            flash(str(exc), "error")
            return redirect(url_for("import_file"))

        final_path = Path(app.config["DATA_DIR"]) / "imports" / f"{file_hash}.xml"
        shutil.move(temporary, final_path)
        cursor = db.execute(
            """INSERT INTO import_batches(filename,file_hash,stored_path,account_iban)
               VALUES (?,?,?,?)""",
            (
                secure_filename(upload.filename) or "kontoauszug.xml",
                file_hash,
                str(final_path.relative_to(app.config["DATA_DIR"])),
                report.account_iban,
            ),
        )
        batch_id = cursor.lastrowid
        imported = 0
        duplicates = 0
        for tx in report.transactions:
            try:
                db.execute(
                    """INSERT INTO transactions(
                       import_batch_id,fingerprint,account_iban,booking_date,value_date,amount_cents,
                       currency,counterparty,counterparty_iban,purpose,bank_reference,bank_transaction_code
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id,
                        tx.fingerprint,
                        tx.account_iban,
                        tx.booking_date,
                        tx.value_date,
                        tx.amount_cents,
                        tx.currency,
                        tx.counterparty,
                        tx.counterparty_iban,
                        tx.purpose,
                        tx.bank_reference,
                        tx.bank_transaction_code,
                    ),
                )
                imported += 1
            except sqlite3.IntegrityError as exc:
                if "transactions.fingerprint" not in str(exc):
                    raise
                duplicates += 1
        db.execute(
            "UPDATE import_batches SET imported_count=?, duplicate_count=? WHERE id=?",
            (imported, duplicates, batch_id),
        )
        log_action(
            db,
            "imported",
            "import_batch",
            batch_id,
            json.dumps({"filename": upload.filename, "imported": imported, "duplicates": duplicates}),
        )
        db.commit()
        flash(f"Import abgeschlossen: {imported} neue Buchungen, {duplicates} Duplikate.", "success")
        return redirect(url_for("transactions", status="uncategorized"))

    @app.get("/imports/<int:batch_id>/original")
    @login_required
    def import_download(batch_id):
        row = get_db().execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            abort(404)
        return send_file(
            Path(app.config["DATA_DIR"]) / row["stored_path"],
            download_name=row["filename"],
            as_attachment=True,
        )

    @app.route("/categories", methods=["GET", "POST"])
    @login_required
    def categories():
        db = get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            tax_area = request.form.get("tax_area", "")
            if not name or tax_area not in TAX_AREAS:
                flash("Bitte Name und Steuerbereich vollständig angeben.", "error")
            else:
                try:
                    cursor = db.execute(
                        "INSERT INTO categories(name,tax_area) VALUES (?,?)", (name, tax_area)
                    )
                    log_action(db, "created", "category", cursor.lastrowid, name)
                    db.commit()
                    flash("Kategorie angelegt.", "success")
                except Exception as exc:
                    if "UNIQUE constraint failed" in str(exc):
                        flash("Diese Kategorie existiert bereits.", "error")
                    else:
                        raise
            return redirect(url_for("categories"))
        rows = db.execute(
            """SELECT c.*, COUNT(t.id) transaction_count FROM categories c
               LEFT JOIN transactions t ON t.category_id=c.id GROUP BY c.id ORDER BY c.tax_area,c.name"""
        ).fetchall()
        return render_template("categories.html", categories=rows)

    @app.get("/audit")
    @login_required
    def audit():
        rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 250").fetchall()
        return render_template("audit.html", entries=rows)

    @app.get("/export.csv")
    @login_required
    def export_csv():
        year = request.args.get("year", str(datetime.now().year))
        rows = get_db().execute(
            """SELECT t.*, c.name category_name, c.tax_area,
                      (SELECT COUNT(*) FROM attachments a WHERE a.transaction_id=t.id) attachment_count
               FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
               WHERE substr(t.booking_date,1,4)=? ORDER BY t.booking_date,t.id""",
            (year,),
        ).fetchall()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(
            ["Buchungsdatum", "Wertstellung", "Betrag", "Währung", "Gegenpartei", "IBAN", "Zweck", "Kategorie", "Steuerbereich", "Belegstatus", "Belege", "Bankreferenz"]
        )
        labels = {"missing": "Fehlt", "complete": "Vollständig", "not_required": "Nicht erforderlich"}
        for row in rows:
            writer.writerow(
                [
                    row["booking_date"], row["value_date"], f"{row['amount_cents']/100:.2f}".replace(".", ","),
                    row["currency"], row["counterparty"], row["counterparty_iban"], row["purpose"],
                    row["category_name"], row["tax_area"], labels[row["receipt_status"]],
                    row["attachment_count"], row["bank_reference"],
                ]
            )
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="vereinskasse-{year}.csv"'},
        )

    return app
