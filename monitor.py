"""
Monitor mese cash Texas Hold'em.
Notificari pe Telegram, cu memorie zilnica per nivel de blinds.
URL-ul monitorizat vine din secretul TARGET_URL (nu apare in cod).

Reguli:
- Ziua de poker se reseteaza la 07:00 (ora Spaniei) - casino inchis.
- Pentru fiecare nivel (5/10, 10/20, 20/50...) se tine un "holder" cu
  ultima stare vazuta AZI. Trigger cand:
    * numarul de mese difera de holder (inclusiv prima deschidere azi
      si inchiderea) -> emoji 🎰 / ❌
    * masa inchisa (0 mese) si lista >= 5, noua sau schimbata -> ⏳
    * 2/5: numar de mese >= 5 (anormal), nou sau schimbat -> 🔥
- Afisarea e mereu snapshot complet, in ordinea de pe site (mic -> mare).
- Protectie la glitch: daca site-ul arata brusc 0 mese peste tot desi azi
  erau mese deschise, prima citire de acest fel e ignorata; doar daca se
  repeta si la urmatoarea rulare e considerata reala.
"""

import datetime
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

URL = os.environ.get("TARGET_URL", "").strip()
MIN_BIG_BLIND = 10.0          # 5/10 sau mai mare (dupa big blind)
WAITLIST_THRESHOLD = 5        # masa inchisa: trigger daca lista >= 5
LOW_STAKES_TABLE_TRIGGER = 5  # 2/5: trigger doar la >= 5 mese (anormal)
DAY_RESET_HOUR = 7            # casino inchide la 07:00 -> zi noua
STATE_FILE = "state.json"
TZ = ZoneInfo("Europe/Madrid")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
IS_MANUAL_RUN = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


if not URL:
    print("[!] Lipseste secretul TARGET_URL.")
    sys.exit(1)


def poker_day(now: datetime.datetime) -> str:
    """Ziua de poker: 07:00 -> 07:00. Ex: 18.07 ora 01:30 apartine zilei de 17.07."""
    return (now - datetime.timedelta(hours=DAY_RESET_HOUR)).date().isoformat()


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
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"
            )
        )
        page.goto(URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(8_000)
        text = page.inner_text("body")
        browser.close()
    return text


def parse_texas_tables(text: str):
    """Extrage randurile din sectiunile Texas: {section, blinds, sb, bb, mesas, lista}"""
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
                        "sb": sb,
                        "bb": bb,
                        "mesas": int(m.group(3)),
                        "lista": int(m.group(4)),
                    }
                )
            continue
        if re.search(r"(texas|omaha|machine|punto|sala)", line, re.IGNORECASE):
            if "ciegas" not in line.lower() and "hold'em nl" not in line.lower():
                current_section = line
    return rows


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    return {
        "day": s.get("day", ""),
        "levels": s.get("levels", {}),
        "zero_streak": s.get("zero_streak", 0),
    }


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fmt_num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else str(x).replace(".", ",")


def level_key(r) -> str:
    return f"{r['section']}|{fmt_num(r['sb'])}/{fmt_num(r['bb'])}"


def main() -> None:
    now = datetime.datetime.now(TZ)
    stamp = now.strftime("%d.%m %H:%M")
    today = poker_day(now)
    print(f"[i] Rulare {stamp} (Madrid), zi poker={today}, manual={IS_MANUAL_RUN}")

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

    rows = parse_texas_tables(text)
    if not rows:
        if "no es posible mostrar" in text.lower():
            print("[i] Date live indisponibile (mesaj festival).")
            if IS_MANUAL_RUN:
                send_telegram("ℹ️ Test: date live indisponibile (festival).")
        else:
            print("[!] Niciun rand Texas gasit.")
            if IS_MANUAL_RUN:
                send_telegram("⚠️ Test: nu am gasit tabelul Texas in pagina.")
        return

    # Ordinea de pe site: de la mic la mare
    rows.sort(key=lambda r: (r["sb"], r["bb"], r["section"]))

    state = load_state()
    if state["day"] != today:
        print(f"[i] Zi noua de poker ({state['day']!r} -> {today!r}): holderii se reseteaza.")
        state = {"day": today, "levels": {}, "zero_streak": 0}
    holders = state["levels"]

    for r in rows:
        print(f"    {r['section']} | {fmt_num(r['sb'])}/{fmt_num(r['bb'])} | "
              f"mese={r['mesas']} lista={r['lista']}")

    # --- Protectie la glitch: totul 0 mese desi azi existau mese deschise ---
    all_zero = all(r["mesas"] == 0 for r in rows)
    had_open_today = any(h.get("mesas", 0) > 0 for h in holders.values())
    if all_zero and had_open_today and not IS_MANUAL_RUN:
        state["zero_streak"] += 1
        if state["zero_streak"] == 1:
            print("[!] Posibil glitch pe site (totul 0). Ignor aceasta citire; "
                  "confirm la urmatoarea rulare.")
            save_state(state)   # doar contorul; holderii raman neatinsi
            return
        print("[i] A doua citire consecutiva cu totul 0 -> o consider reala.")
    else:
        state["zero_streak"] = 0

    # --- Evaluare triggere per nivel, fata de holderul de azi ---
    triggered = {}   # level_key -> emoji
    for r in rows:
        key = level_key(r)
        h = holders.get(key)
        if r["bb"] >= MIN_BIG_BLIND:
            if r["mesas"] >= 1:
                # prima deschidere azi sau schimbare in numarul de mese
                if h is None or h.get("mesas", 0) != r["mesas"]:
                    triggered[key] = "🎰 "
                # masa deschisa: lista trece intre 0 si diferit de 0
                # (in orice directie); NU conteaza cati sunt in lista
                elif (h.get("lista", 0) == 0) != (r["lista"] == 0):
                    triggered[key] = "📋 "
            else:
                if h is not None and h.get("mesas", 0) >= 1:
                    # era deschisa azi si acum e 0 -> inchidere
                    triggered[key] = "❌ "
                elif r["lista"] >= WAITLIST_THRESHOLD and (
                    h is None
                    or h.get("mesas") != 0
                    or h.get("lista") != r["lista"]
                ):
                    # inchisa, lista la prag: noua azi sau lista schimbata
                    triggered[key] = "⏳ "
        else:
            # 2/5: doar situatia anormala (multe mese), noua sau schimbata
            if r["mesas"] >= LOW_STAKES_TABLE_TRIGGER and (
                h is None or h.get("mesas") != r["mesas"]
            ):
                triggered[key] = "🔥 "

    # --- Afisare: mereu acelasi stil, snapshot complet in ordinea de pe
    # site, FARA emoji. Triggerele decid doar DACA se trimite mesajul. ---
    def snapshot_lines() -> list:
        lines = []
        for r in rows:
            mese = f"{r['mesas']} " + ("masa" if r["mesas"] == 1 else "mese")
            lines.append(
                f"{fmt_num(r['sb'])}/{fmt_num(r['bb'])} · {mese} · "
                f"{r['lista']} in lista"
            )
        return lines

    if IS_MANUAL_RUN:
        send_telegram(f"✅ Test OK · {stamp}\n" + "\n".join(snapshot_lines()))
        print("[i] Rulare manuala: starea NU se salveaza.")
        return

    if triggered:
        print(f"[i] Triggere: {triggered}")
        send_telegram("\n".join(snapshot_lines()) + f"\n{stamp}")
    else:
        print("[i] Nicio schimbare relevanta fata de holderii de azi.")

    # --- Actualizare holderi (mereu, per nivel) si salvare ---
    for r in rows:
        holders[level_key(r)] = {"mesas": r["mesas"], "lista": r["lista"]}
    state["levels"] = holders
    state["day"] = today
    save_state(state)


if __name__ == "__main__":
    main()
