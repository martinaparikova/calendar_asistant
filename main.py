#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calendar Summary via ICS feeds (Google/Outlook or any iCal)
- Daily summary (tomorrow)
- Weekly summary (Mon–Sun), intended to run on Sundays
Outputs an HTML email and sends it via SMTP + Slack.

Usage:
  python main.py --mode daily
  python main.py --mode weekly

Config: config.yaml (see README)
"""
import argparse
import os
import sys
import smtplib
import ssl
import pytz
import yaml
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, time
from dateutil.tz import gettz
from dateutil import parser as dtparser
from collections import defaultdict
from jinja2 import Template

from icalendar import Calendar
import recurring_ical_events

HTML_TEMPLATE = Template(u"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{ title }}</title>
    <style>
      body { font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; line-height: 1.45; }
      h1 { font-size: 20px; margin: 0 0 12px 0; }
      h2 { font-size: 16px; margin: 18px 0 8px 0; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
      .event { margin: 6px 0 10px 0; }
      .time { font-weight: 600; }
      .title { font-weight: 600; }
      .loc { font-style: italic; color: #333; }
      .cal { color: #555; font-size: 12px; }
      .allday { background: #f4f4f4; border-radius: 4px; padding: 2px 6px; font-size: 12px; margin-left: 6px; }
      .footer { margin-top: 18px; font-size: 12px; color: #666; }
    </style>
  </head>
  <body>
    <h1>{{ title }}</h1>
    {% if intro %}<p>{{ intro }}</p>{% endif %}

    {% if grouped_events %}
      {% for day, items in grouped_events.items() %}
        <h2>{{ day }}</h2>
        {% for ev in items %}
          <div class="event">
            <div class="title">{{ ev.title | e }}
              {% if ev.is_all_day %}<span class="allday">celý den</span>{% endif %}
            </div>
            <div class="time">
              {% if not ev.is_all_day %}{{ ev.start_str }}–{{ ev.end_str }}{% else %}—{% endif %}
            </div>
            {% if ev.location %}<div class="loc">{{ ev.location | e }}</div>{% endif %}
            <div class="cal">{{ ev.calendar_name }}</div>
          </div>
        {% endfor %}
      {% endfor %}
    {% else %}
      <p>Žádné události v daném období.</p>
    {% endif %}

    <div class="footer">
      Vygenerováno automaticky. Časové pásmo: {{ tzname }}.
    </div>
  </body>
</html>
""")

# ===== Messaging integrations: Slack only =====
import json as _json

def html_to_text(html):
    import re
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def send_slack_webhook(cfg, subject, html_body):
    slack = cfg.get("slack", {})
    if not slack.get("enabled"):
        return
    url = slack.get("webhook_url")
    if not url:
        print("[WARN] Slack webhook enabled but webhook_url missing.", file=sys.stderr)
        return
    # Slack neumí HTML – převedeme na prostý text
    text = html_to_text(html_body)
    payload = {"text": f"*{subject}*\n{text[:38000]}"}
    r = requests.post(url, data=_json.dumps(payload),
                      headers={"Content-Type":"application/json"}, timeout=15)
    r.raise_for_status()

def send_slack_bot(cfg, subject, html_body):
    bot = cfg.get("slack_bot", {})
    if not bot.get("enabled"):
        return
    token = bot.get("token")
    channel = bot.get("channel_id")
    if not token or not channel:
        print("[WARN] Slack bot enabled but token/channel_id missing.", file=sys.stderr)
        return
    text = f"*{subject}*\n" + html_to_text(html_body)
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {token}"},
                      data={"channel": channel, "text": text}, timeout=20)
    r.raise_for_status()

def load_config(path="config.yaml"):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}. Create it from config.example.yaml.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def daterange_window(mode, tz):
    now = datetime.now(tz)
    if mode == "daily":
        target = now + timedelta(days=1)
        start = datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=tz)
        end   = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=tz)
        title = f"Zítřejší plán – {start.strftime('%A %d.%m.%Y')}"
    elif mode == "weekly":
        weekday = now.weekday()  # Mon=0..Sun=6
        days_to_next_monday = (7 - weekday) % 7
        monday = (now + timedelta(days=days_to_next_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = monday
        end = (monday + timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)
        title = f"Týdenní plán – {start.strftime('%d.%m.')}–{end.strftime('%d.%m.%Y')}"
    else:
        raise ValueError("mode must be 'daily' or 'weekly'")
    return start, end, title

def safe_str(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except Exception:
            return v.decode("latin-1", errors="ignore")
    return str(v)

from datetime import datetime, timedelta, time, date  # ← doplň "date", pokud chybí

def parse_ics(url, tz, calendar_name):
    """Fetch an ICS and expand occurrences into normalized events in target TZ."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)

    events = []

    # --- kompatibilní expand (starší i novější verze recurring-ical-events) ---
    start_utc = datetime(1970, 1, 1, tzinfo=pytz.UTC)
    end_utc   = datetime(2100, 1, 1, tzinfo=pytz.UTC)
    try:
        comps = recurring_ical_events.of(cal).between(start_utc, end_utc, include=True)
    except TypeError:
        comps = recurring_ical_events.of(cal).between(start_utc, end_utc)

    for component in comps:
        if component.name != "VEVENT":
            continue

        summary  = safe_str(component.get("SUMMARY"))
        location = safe_str(component.get("LOCATION"))
        dtstart  = component.get("DTSTART").dt
        dtend    = component.get("DTEND").dt if component.get("DTEND") else None

        # --- all-day (VALUE=DATE) vs. datetimes, "floating time" → cílová TZ ---
        if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
            # celodenní: ber půlnoc v cílovém pásmu; Google typicky dává DTEND = další den
            start_dt = tz.localize(datetime.combine(dtstart, time.min))
            if dtend and isinstance(dtend, date) and not isinstance(dtend, datetime):
                end_dt = tz.localize(datetime.combine(dtend, time.min))
            else:
                end_dt = start_dt + timedelta(days=1)
            is_all_day = True
        else:
            # má čas; pokud chybí tzinfo, ber jako „floating“ v cílové TZ
            if isinstance(dtstart, datetime):
                start_dt = dtstart if dtstart.tzinfo else tz.localize(dtstart)
            else:
                start_dt = tz.localize(datetime.combine(dtstart, time.min))

            if dtend:
                if isinstance(dtend, datetime):
                    end_dt = dtend if dtend.tzinfo else tz.localize(dtend)
                else:
                    end_dt = tz.localize(datetime.combine(dtend, time.min))
            else:
                end_dt = start_dt + timedelta(hours=1)

            # sjednoť do cílové TZ
            start_dt = start_dt.astimezone(tz)
            end_dt   = end_dt.astimezone(tz)
            is_all_day = False

        events.append({
            "title": summary or "(Bez názvu)",
            "start": start_dt,
            "end":   end_dt,
            "is_all_day": is_all_day,
            "location": location or "",
            "calendar_name": calendar_name
        })

    return events


def within_range(ev, start, end):
    return not (ev["end"] <= start or ev["start"] >= end)

def dedupe_and_sort(events):
    keyset = set()
    out = []
    for ev in events:
        k = (ev["title"], ev["start"], ev["end"], ev["calendar_name"])
        if k in keyset:
            continue
        keyset.add(k)
        out.append(ev)
    out.sort(key=lambda e: (e["start"], e["title"]))
    return out

def group_by_day(events, tz):
    grouped = defaultdict(list)
    for ev in events:
        day_label = ev["start"].strftime("%A %d.%m.%Y")
        item = {
            "title": ev["title"],
            "start_str": ev["start"].strftime("%H:%M"),
            "end_str": ev["end"].strftime("%H:%M"),
            "is_all_day": ev["is_all_day"],
            "location": ev["location"],
            "calendar_name": ev["calendar_name"]
        }
        grouped[day_label].append(item)
    return grouped

def send_email(cfg, subject, html_body):
    smtp = cfg["smtp"]
    sender = smtp["from"]
    to_list = smtp["to"] if isinstance(smtp["to"], list) else [smtp["to"]]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    server = smtp.get("server", "smtp.gmail.com")
    port = int(smtp.get("port", 587))
    username = smtp.get("username")
    password = smtp.get("password")
    use_tls = smtp.get("use_tls", True)

    with smtplib.SMTP(server, port) as s:
        if use_tls:
            s.starttls(context=context)
        if username and password:
            s.login(username, password)
        s.sendmail(sender, to_list, msg.as_string())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], required=True, help="daily (tomorrow) or weekly (Mon-Sun)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    tzname = cfg.get("time_zone", "Europe/Prague")
    tz = pytz.timezone(tzname)

    start, end, title = daterange_window(args.mode, tz)

    events = []
    for cal in cfg.get("calendars", []):
        name = cal["name"]
        if "ics_url" not in cal:
            print(f"Skipping {name}: missing ics_url", file=sys.stderr)
            continue
        try:
            evs = parse_ics(cal["ics_url"], tz, name)
            evs = [e for e in evs if within_range(e, start, end)]
            events.extend(evs)
        except Exception as e:
            print(f"[WARN] Calendar '{name}' failed: {e}", file=sys.stderr)

    events = dedupe_and_sort(events)
    grouped = group_by_day(events, tz)

    intro = cfg.get("intro_text_daily" if args.mode=="daily" else "intro_text_weekly", "")
    html = HTML_TEMPLATE.render(
        title=title,
        intro=intro,
        grouped_events=grouped,
        tzname=tzname
    )

    if cfg.get("dry_run", False):
        out = f"output_{args.mode}.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[DRY RUN] Wrote {out}")
        return

    # Send email + Slack
    send_email(cfg, subject=title, html_body=html)
    send_slack_webhook(cfg, subject=title, html_body=html)
    send_slack_bot(cfg, subject=title, html_body=html)
    print("Notifications sent.")

if __name__ == "__main__":
    main()
