from __future__ import annotations

import io
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


CSV_URL = "https://seekingalpha.com/account/undercovered_stocks.csv"
DATA_DIR = Path("data")
SNAPSHOT_PATH = DATA_DIR / "undercovered_stocks_top10.csv"


def _load_cookie() -> str:
    load_dotenv()
    cookie = os.getenv("SA_COOKIE")
    if not cookie:
        raise ValueError("Missing SA_COOKIE. Set it as an environment variable or secret.")
    return cookie


def _download_latest_top10(cookie: str) -> pd.DataFrame:
    headers = {
        "cookie": cookie,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
    }

    response = requests.get(CSV_URL, headers=headers, timeout=60)
    response.raise_for_status()

    latest = pd.read_csv(io.BytesIO(response.content), dtype=str).fillna("")
    return latest.head(10).reset_index(drop=True)


def _load_snapshot() -> pd.DataFrame | None:
    if not SNAPSHOT_PATH.exists():
        return None

    snapshot = pd.read_csv(SNAPSHOT_PATH, dtype=str).fillna("")
    return snapshot.head(10).reset_index(drop=True)


def _save_snapshot(top10: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    top10.to_csv(SNAPSHOT_PATH, index=False)


def _changed_row_numbers(previous: pd.DataFrame, current: pd.DataFrame) -> list[int]:
    changed_rows: list[int] = []
    max_rows = max(len(previous), len(current))

    for index in range(max_rows):
        if index >= len(previous) or index >= len(current):
            changed_rows.append(index + 1)
            continue

        if not previous.iloc[index].equals(current.iloc[index]):
            changed_rows.append(index + 1)

    return changed_rows


def _email_settings() -> dict[str, object]:
    recipients_raw = os.getenv("EMAIL_TO", "")
    recipients = [entry.strip() for entry in recipients_raw.replace(";", ",").split(",") if entry.strip()]
    if not recipients:
        raise ValueError("Missing EMAIL_TO. Set at least one recipient email address.")

    host = os.getenv("SMTP_HOST")
    if not host:
        raise ValueError("Missing SMTP_HOST. Set your SMTP server host.")

    port_raw = os.getenv("SMTP_PORT")
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    port = int(port_raw) if port_raw else (465 if use_ssl else 587)
    use_starttls = os.getenv("SMTP_USE_STARTTLS", "true").lower() == "true"

    return {
        "host": host,
        "port": port,
        "use_ssl": use_ssl,
        "use_starttls": use_starttls,
        "username": os.getenv("SMTP_USERNAME"),
        "password": os.getenv("SMTP_PASSWORD"),
        "sender": os.getenv("EMAIL_FROM") or os.getenv("SMTP_USERNAME") or recipients[0],
        "recipients": recipients,
    }


def _build_email(
    previous: pd.DataFrame,
    current: pd.DataFrame,
    changed_rows: list[int],
    settings: dict[str, object],
) -> EmailMessage:
    subject_prefix = os.getenv("EMAIL_SUBJECT_PREFIX", "Stock Picks Alert")
    subject = f"[{subject_prefix}] Top 10 rows changed"

    changed_rows_text = ", ".join(str(row_number) for row_number in changed_rows)
    text_body = (
        "The top 10 rows in undercovered_stocks.csv changed.\n\n"
        f"Changed row positions: {changed_rows_text}\n\n"
        "Previous top 10:\n"
        f"{previous.to_string(index=False)}\n\n"
        "Current top 10:\n"
        f"{current.to_string(index=False)}\n"
    )

    html_body = f"""
    <html>
      <body style=\"font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;\">
        <h2 style=\"margin-bottom: 0.25rem;\">Top 10 rows changed</h2>
        <p style=\"margin-top: 0;\">Changed row positions: {changed_rows_text}</p>
        <h3>Previous top 10</h3>
        {previous.to_html(index=False, border=0)}
        <h3>Current top 10</h3>
        {current.to_html(index=False, border=0)}
      </body>
    </html>
    """

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = str(settings["sender"])
    message["To"] = ", ".join(str(recipient) for recipient in settings["recipients"])
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def _send_email(message: EmailMessage, settings: dict[str, object]) -> None:
    smtp_class = smtplib.SMTP_SSL if settings["use_ssl"] else smtplib.SMTP

    with smtp_class(str(settings["host"]), int(settings["port"])) as smtp:
        if not settings["use_ssl"] and settings["use_starttls"]:
            smtp.starttls()

        username = settings["username"]
        password = settings["password"]
        if username and password:
            smtp.login(str(username), str(password))

        smtp.send_message(message)


def main() -> int:
    cookie = _load_cookie()
    previous_top10 = _load_snapshot()
    current_top10 = _download_latest_top10(cookie)

    if previous_top10 is None:
        _save_snapshot(current_top10)
        print(f"Created top 10 snapshot at {SNAPSHOT_PATH}.")
        return 0

    if current_top10.equals(previous_top10):
        _save_snapshot(current_top10)
        print("No changes detected in the top 10 rows.")
        return 0

    changed_rows = _changed_row_numbers(previous_top10, current_top10)
    email_settings = _email_settings()
    email_message = _build_email(previous_top10, current_top10, changed_rows, email_settings)
    _send_email(email_message, email_settings)
    _save_snapshot(current_top10)

    print(f"Top 10 rows changed at positions: {', '.join(str(row) for row in changed_rows)}")
    print(f"Snapshot updated at {SNAPSHOT_PATH} and email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())