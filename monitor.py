"""
Monitor Casino Barcelona - Poker Cash
Verifica daca exista mese Texas Hold'em cu blinds 5/10 sau mai mari
si trimite notificare pe Telegram.

Ruleaza in GitHub Actions la ~15 minute, intre 19:45 si 05:00 (ora Spaniei).
"""

import datetime
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.casinobarcelona.com/poker-cash"
MIN_BIG_BLIND = 10.0          # 5/10 sau mai mare (dupa big blind)
WAITLIST_THRESHOLD = 5        # notifica si daca masa e inchisa dar lista >= 5
LOW_STAKES_TABLE_TRIGGER = 5  # 2/5: notifica doar daca sunt cel putin 5 mese (anormal)
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Madrid")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# "changes" = notifica doar cand se deschide/inchide o masa (recomandat)
# "always"  = notifica la fiecare rulare cat timp masa e deschisa
NOTIFY_MODE = os.environ.get("NOTIFY_MODE", "changes").strip().lower()
IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


def in_time_window(now: datetime.datetime) -> bool:
    """19:45 - 05:00 ora Spaniei."""
    t = now.time()
    return t >= datetime.time(19, 45) or t <= datetime.time(5, 0)


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("[!] Lipsesc TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text},
        timeout=30,
    )
    print(f"[i] Telegram: {r.status_code} {r.text[:200]}")


def fetch_page_text() -> str:
    """Deschide pagina cu browser real (executa JavaScript) si intoarce textul."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        # Timp suplimentar pentru widgetul live incarcat prin JS
        page.wait_for_timeout(8_000)
        text = page.inner_text("body")
        browser.close()
    return text


def parse_texas_tables(text: str):
    """
    Extrage randurile din sectiunile Texas.
    Randurile de tabel apar ca: '5,00/10,00 <tab> 1 <tab> 10'
    Returneaza lista de dict: {section, blinds, bb, mesas, lista}
    """
    rows = []
    current_section = ""
    row_re = re.compile(
        r"^\s*(\d+[.,]\d+)\s*/\s*(\d+[.,]\d+)[\s\t]+(\d+)[\s\t]+(\d+)\s*$"
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = row_re.match(line)
        if m:
            if "texas" in current_section.lower():
                sb = float(m.group(1).replace(",", "."))
                bb = float(m.group(2).replace(",", "."))
                rows.append(
                    {
                        "section": current_section,
                        "blinds": f"{m.group(1)}/{m.group(2)}",
                        "sb": sb,
                        "bb": bb,
                        "mesas": int(m.group(3)),
                        "lista": int(m.group(4)),
                    }
                )
            continue
        # Linie care nu e rand de tabel -> poate fi titlu de sectiune
        if re.search(r"(texas|omaha|machine|punto|sala)", line, re.IGNORECASE):
            # Ignoram headerul de coloane si textele generice
            if "ciegas" not in line.lower() and "hold'em nl" not in line.lower():
                current_section = line
    return rows


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"open": [], "festival": False}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main() -> None:
    now = datetime.datetime.now(TZ)
    stamp = now.strftime("%d.%m %H:%M")
    print(f"[i] Rulare la {stamp} (Madrid), mod={NOTIFY_MODE}, manual={IS_MANUAL_RUN}")

    if not IS_MANUAL_RUN and not in_time_window(now):
        print("[i] In afara intervalului 19:45-05:00. Iesire.")
        return

    try:
        text = fetch_page_text()
    except Exception as e:
        print(f"[!] Eroare la incarcarea paginii: {e}")
        if IS_MANUAL_RUN:
            send_telegram(f"⚠️ Test: pagina nu s-a putut incarca ({e})")
        sys.exit(0)

    state = load_state()
    rows = parse_texas_tables(text)
    festival_notice = "no es posible mostrar" in text.lower()

    # Mesajul de festival exista uneori in HTML-ul static chiar si cand
    # tabelele live se incarca prin JS. Il luam in serios DOAR daca
    # nu am gasit niciun rand de tabel Texas.
    if not rows and festival_notice:
        print("[i] Site-ul afiseaza mesajul de festival - date live indisponibile.")
        if IS_MANUAL_RUN:
            send_telegram(
                "ℹ️ Test OK, dar site-ul afiseaza: date live indisponibile "
                "din cauza festivalului de poker."
            )
        if not state.get("festival"):
            state["festival"] = True
            save_state(state)
        return

    big_rows = [r for r in rows if r["bb"] >= MIN_BIG_BLIND]
    low_rows = [r for r in rows if r["bb"] < MIN_BIG_BLIND]   # ex. 2/5
    open_rows = [r for r in big_rows if r["mesas"] >= 1]
    # Mese inchise, dar cu lista de asteptare la prag -> semnal ca se deschide curand
    waiting_rows = [
        r for r in big_rows if r["mesas"] == 0 and r["lista"] >= WAITLIST_THRESHOLD
    ]
    # 2/5: trigger doar in situatie anormala (numar mare de mese)
    abnormal_low_rows = [
        r for r in low_rows if r["mesas"] >= LOW_STAKES_TABLE_TRIGGER
    ]
    alert_rows = open_rows + waiting_rows + abnormal_low_rows
    print(
        f"[i] Randuri Texas: {len(rows)}; 5/10+: {len(big_rows)}; "
        f"deschise: {len(open_rows)}; in prag de deschidere: {len(waiting_rows)}; "
        f"2/5 anormal: {len(abnormal_low_rows)}"
    )
    for r in rows:
        print(f"    {r['section']} | {r['blinds']} | mese={r['mesas']} lista={r['lista']}")

    # Semnatura include mesele si lista de asteptare pentru toate randurile
    # relevante (deschise SAU inchise cu lista >= prag). Orice modificare
    # (ex. lista trece de la 5 la 6) declanseaza notificare; daca nimic
    # nu s-a schimbat, nu se trimite nimic.
    signature = sorted(
        f"{r['section']}|{r['blinds']}|{r['mesas']}|{r['lista']}" for r in alert_rows
    )
    prev_signature = state.get("open", [])

    def format_rows(rs):
        lines = []
        for r in rs:
            if r["bb"] < MIN_BIG_BLIND:
                lines.append(
                    f"🔥 {r['section']} — {r['blinds']}: {r['mesas']} mese deschise "
                    f"(neobisnuit de multe), lista de asteptare: {r['lista']}"
                )
            elif r["mesas"] >= 1:
                lines.append(
                    f"• {r['section']} — {r['blinds']}: {r['mesas']} masa/mese "
                    f"DESCHISE, lista de asteptare: {r['lista']}"
                )
            else:
                lines.append(
                    f"⏳ {r['section']} — {r['blinds']}: inca inchisa, dar "
                    f"{r['lista']} oameni pe lista — probabil se deschide curand"
                )
        return "\n".join(lines)

    def low_stakes_context():
        """Starea 2/5, atasata oricarei notificari, indiferent de numarul de mese."""
        if not low_rows:
            return ""
        info = "\n".join(
            f"  {r['blinds']}: {r['mesas']} mese, lista: {r['lista']}"
            for r in low_rows
        )
        return f"\nℹ️ Context 2/5:\n{info}"

    if IS_MANUAL_RUN:
        if alert_rows:
            send_telegram(
                f"✅ Test OK ({stamp})\nStare mese:\n{format_rows(alert_rows)}"
                f"{low_stakes_context()}"
            )
        elif big_rows or low_rows:
            send_telegram(
                f"✅ Test OK ({stamp})\nNimic de semnalat (mese 5/10+ inchise, "
                f"liste sub prag).\n{format_rows(big_rows)}{low_stakes_context()}"
            )
        else:
            send_telegram(f"✅ Test OK ({stamp})\nNu am gasit randuri in tabelul Texas (posibil pagina goala).")
    else:
        if alert_rows and (NOTIFY_MODE == "always" or signature != prev_signature):
            if open_rows and not prev_signature:
                header = "🎰 S-a deschis masa Texas 5/10+ la Casino Barcelona!"
            elif open_rows:
                header = "🔄 Update mese Texas 5/10+:"
            elif waiting_rows:
                header = "⏳ Miscare pe lista de asteptare Texas 5/10+:"
            else:
                header = "🔥 Actiune neobisnuita la 2/5:"
            send_telegram(
                f"{header} ({stamp})\n{format_rows(alert_rows)}{low_stakes_context()}"
            )
        elif not alert_rows and prev_signature:
            send_telegram(
                f"❌ Nicio masa Texas 5/10+ deschisa si listele au scazut sub "
                f"{WAITLIST_THRESHOLD}. ({stamp}){low_stakes_context()}"
            )

    state["open"] = signature
    state["festival"] = False
    save_state(state)


if __name__ == "__main__":
    main()
