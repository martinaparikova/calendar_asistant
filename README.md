# Calendar Summary Bot (ICS + Email + Slack)

Tento mini‑skript denně/ týdně sečte události z více kalendářů (Google, Outlook, cokoliv s **ICS**)
a pošle **HTML e‑mail** i **Slack zprávu** s přehledem.

## 1) Instalace

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# vyplň config.yaml (ICS URL + SMTP + Slack webhook / bot)
```

Ověř lokálně (bez odeslání e‑mailu/Slacku):
```bash
# v config.yaml nastav dry_run: true
python main.py --mode daily
python main.py --mode weekly
```

Odeslání (email+Slack):
```bash
# nastav dry_run: false a konfiguraci SMTP a Slacku
python main.py --mode daily
```

## 2) ICS odkazy
Viz „Integrace kalendáře“ u konkrétního kalendáře v Google (Tajná adresa ve formátu iCal) nebo Publish v Outlooku.

## 3) Cron / GitHub Actions
Viz níže a `schedule.yml`.

## Slack výstup
### Varianta A – Incoming Webhook
1. Vytvoř *Incoming Webhook* (Slack App → Incoming Webhooks → Add New Webhook to Workspace).
2. Do `config.yaml` vlož:
```yaml
slack:
  enabled: true
  webhook_url: "https://hooks.slack.com/services/XXX/YYY/ZZZ"
```
### Varianta B – Slack Bot
```yaml
slack_bot:
  enabled: true
  token: "xoxb-..."
  channel_id: "C0123ABCDE"
```

## Poznámky
- **Time zone:** default `Europe/Prague` – přepni v `config.yaml`.
- **All‑day** události označené „celý den“.
- **Duplicity:** jednoduchá deduplikace (název+čas+kalendář).
- **Soukromí:** ICS URL je tajná, lze kdykoli zneplatnit.
