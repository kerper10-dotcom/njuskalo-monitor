#!/usr/bin/env python3
"""
Njuskalo Monitor v2.0
=====================
Periodicki provjerava nove oglase na njuskalo.hr i salje Telegram obavijesti.

Podrzava:
  - GitHub Actions (public repo → unlimited minutes)
  - Cron / VPS / PythonAnywhere

Pri prvom pokretanju samo puni bazu (bez slanja obavijesti).

Konfiguracija (samo preko GitHub Secrets):
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID

Tokeni se nikad ne stavljaju u kod.
"""

import json
import os
import re
import sqlite3
import time
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# =============================================================================
#  KONFIGURACIJA
# =============================================================================

# URL-ovi za pracenje
URLS = {
    "YARIS HIBRID": (
        "https://www.njuskalo.hr/rabljeni-auti/toyota-yaris"
        "?yearManufactured%5Bmin%5D=2020&fuelTypeId=604"
    ),
    "MAZDA CX-30": (
        "https://www.njuskalo.hr/rabljeni-auti/mazda-cx-30"
        "?transmissionTypeId%5B611%5D=611&transmissionTypeId%5B612%5D=612&transmissionTypeId%5B613%5D=613"
    ),
    "MAZDA CX-5": (
        "https://www.njuskalo.hr/rabljeni-auti/mazda-cx-5"
        "?fuelTypeId=600&transmissionTypeId%5B611%5D=611&transmissionTypeId%5B612%5D=612&transmissionTypeId%5B613%5D=613"
    ),
    "ZEMLJISTA ZADAR": (
        "https://www.njuskalo.hr/prodaja-zemljista/zadar"
        "?landTypeId=235"
    ),
    "ZEMLJISTA OKOLICA": (
        "https://www.njuskalo.hr/prodaja-zemljista"
        "?price%5Bmax%5D=100000&landTypeId=235"
        "&geo%5BlocationIds%5D=8692%2C8696%2C8809%2C8797"
    ),
    "STANOVI ZAGREB": (
        "https://www.njuskalo.hr/prodaja-stanova/zagreb"
        "?price%5Bmin%5D=9001&price%5Bmax%5D=165000"
        "&buildingFloorPosition%5Bmin%5D=ground-floor"
        "&buildingFloorPosition%5Bmax%5D=25"
    ),
    "STANOVI ZG ZUPANIJA": (
        "https://www.njuskalo.hr/prodaja-stanova/zagrebacka"
        "?price%5Bmin%5D=9001&price%5Bmax%5D=165000"
        "&buildingFloorPosition%5Bmin%5D=ground-floor"
        "&buildingFloorPosition%5Bmax%5D=25"
    ),
}

# Telegram podesavanja (env varijable imaju prednost nad ovim vrijednostima)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Koliko stranica provjeriti po URL-u
PAGES_PER_URL = 2

# Pauza izmedu URL-ova (sekunde)
DELAY_BETWEEN_URLS = 2.0

# Playwright
HEADLESS = True
BROWSER_TIMEOUT = 30000  # ms

# SQLite baza
DB_FILE = str(Path(__file__).parent / "njuskalo.db")

# Json fajl sa spremljenim oglasima (za price tracking)
SAVED_ADS_FILE = str(Path(__file__).parent / "saved_ads.json")

# Telegram limit: 4096 znakova po poruci
TELEGRAM_MAX_CHARS = 4000

# Ignoriraj oglase starije od ovog datuma (format: DD.MM.YYYY)
# Postavi na None ako zelis SVE oglase bez obzira na datum
SKIP_BEFORE_DATE = "28.05.2026"  # npr. "28.05.2026" ili None


# =============================================================================
#  SQLite BAZA
# =============================================================================


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_ads ("
        "  id INTEGER PRIMARY KEY,"
        "  category TEXT NOT NULL,"
        "  first_seen TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        "  title TEXT,"
        "  url TEXT,"
        "  pub_date TEXT DEFAULT ''"
        ")"
    )
    try:
        conn.execute("ALTER TABLE seen_ads ADD COLUMN pub_date TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_ads_id ON seen_ads(id)")

    # Tablica za spremljene oglase (price tracking)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS saved_ads ("
        "  id INTEGER PRIMARY KEY,"
        "  title TEXT,"
        "  url TEXT,"
        "  saved_price REAL,"
        "  saved_date TEXT,"
        "  last_price TEXT,"
        "  last_checked TEXT"
        ")"
    )

    conn.commit()
    conn.close()


def db_is_empty() -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT COUNT(*) FROM seen_ads")
    count = cur.fetchone()[0]
    conn.close()
    return count == 0


def parse_date_croatian(date_str: str) -> str:
    """Parsira datum iz razlicitih formata u DD.MM.YYYY."""
    if not date_str:
        return ""
    # Vec je u DD.MM.YYYY. formatu
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", date_str)
    if m:
        return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}"
    return ""

def is_too_old(pub_date: str) -> bool:
    """Provjerava je li oglas stariji od SKIP_BEFORE_DATE."""
    if SKIP_BEFORE_DATE is None or not pub_date:
        return False
    try:
        d = datetime.strptime(pub_date, "%d.%m.%Y")
        cutoff = datetime.strptime(SKIP_BEFORE_DATE, "%d.%m.%Y")
        return d < cutoff
    except ValueError:
        return False


def is_new_ad(ad_id: int) -> bool:
    """Provjerava je li oglas vec viden u bazi."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT 1 FROM seen_ads WHERE id = ?", (ad_id,))
    result = cur.fetchone() is None
    conn.close()
    return result


def save_ad(ad_id: int, category: str, title: str, url: str, pub_date: str = ""):
    """Sprema novi oglas u bazu."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO seen_ads (id, category, title, url, pub_date) VALUES (?, ?, ?, ?, ?)",
        (ad_id, category, title, url, pub_date),
    )
    conn.commit()
    conn.close()


def get_db_stats() -> dict:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT COUNT(*) FROM seen_ads")
    total = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT category, COUNT(*) FROM seen_ads GROUP BY category ORDER BY 2 DESC"
    )
    by_cat = dict(cur.fetchall())
    conn.close()
    return {"total": total, "by_category": by_cat}


def sync_saved_ads_from_json():
    """Sinkronizira saved_ads.json u bazu."""
    if not Path(SAVED_ADS_FILE).exists():
        print("  [i] saved_ads.json ne postoji, preskacem")
        return

    with open(SAVED_ADS_FILE, encoding="utf-8") as f:
        ads = json.load(f)

    json_ids = {ad["id"] for ad in ads}
    conn = sqlite3.connect(DB_FILE)
    added = updated = removed = 0

    for ad in ads:
        if conn.execute("SELECT 1 FROM saved_ads WHERE id = ?", (ad["id"],)).fetchone():
            conn.execute(
                "UPDATE saved_ads SET title = ?, url = ?, saved_date = ? WHERE id = ?",
                (ad["title"], ad["url"], ad.get("saved_date"), ad["id"]),
            )
            updated += 1
        else:
            conn.execute(
                "INSERT INTO saved_ads (id, title, url, saved_price, saved_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (ad["id"], ad["title"], ad["url"], ad.get("saved_price"), ad.get("saved_date")),
            )
            added += 1

    for (db_id,) in conn.execute("SELECT id FROM saved_ads"):
        if db_id not in json_ids:
            conn.execute("DELETE FROM saved_ads WHERE id = ?", (db_id,))
            removed += 1

    conn.commit()
    conn.close()
    msg = f"  [✓] saved_ads sync: {added} novih, {updated} azurirano"
    if removed:
        msg += f", {removed} uklonjeno"
    print(f"{msg} (ukupno {len(ads)})")


def export_saved_ads_to_json():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT id, title, url, saved_price, saved_date FROM saved_ads ORDER BY id"
    ).fetchall()
    conn.close()
    ads = [
        {"id": r[0], "title": r[1], "url": r[2], "saved_price": r[3], "saved_date": r[4]}
        for r in rows
    ]
    with open(SAVED_ADS_FILE, "w", encoding="utf-8") as f:
        json.dump(ads, f, ensure_ascii=False, indent=2)


def _is_sold_or_removed(body_snippet: str, page_html: str = "") -> bool:
    """Prodani oglas na Njuskalu cesto i dalje prikazuje cijenu.

    PAZNJA: naslov SVIH nekretnina na prodaju ima sufiks '(prodaja)' — to NIJE prodano!
    Jedini pouzdan signal je banner 'Ovaj oglas je prodan.'
    """
    body_top = (body_snippet or "")[:2500].lower()
    if "ovaj oglas je prodan" in body_top:
        return True
    html_chunk = (page_html or "")[:20000].lower()
    return "ovaj oglas je prodan" in html_chunk


def check_saved_ads(page) -> tuple[list[str], int]:
    """Provjerava cijene spremljenih oglasa. Vraca (poruke, broj preskocenih CAPTCHA)."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT id, title, url, saved_price, last_price FROM saved_ads")
    saved = cur.fetchall()
    conn.close()

    if not saved:
        return [], 0

    messages = []
    skipped_captcha = 0
    checked = 0
    now = time.strftime("%d.%m.%Y. %H:%M")

    for ad_id, title, url, saved_price, last_price in saved:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
        except Exception:
            continue

        page_title = page.title()

        # ShieldSquare CAPTCHA
        if "shieldsquare" in page_title.lower() or "captcha" in page_title.lower():
            skipped_captcha += 1
            continue

        try:
            body_probe = page.locator("body").inner_text()[:500].lower()
        except Exception:
            body_probe = ""
        if "shieldsquare" in body_probe or "tamnu stranu" in body_probe:
            skipped_captcha += 1
            continue

        # Provjeri redirect - ako URL vodi drugdje, oglas je uklonjen
        current_url = page.url
        if current_url != url and "njuskalo.hr" not in current_url:
            skipped_captcha += 1
            continue

        # Redirect na pretragu/home — cesto bot-blok, NE brisati
        if "/pretraga" in current_url or current_url.rstrip("/") == "https://www.njuskalo.hr":
            print(f"    [!] Redirect za saved ad {ad_id}, preskacem")
            skipped_captcha += 1
            continue

        try:
            body_snippet = page.locator("body").inner_text()
        except Exception:
            body_snippet = ""
        try:
            page_html = page.content()
        except Exception:
            page_html = ""

        if _is_sold_or_removed(body_snippet, page_html):
            conn = sqlite3.connect(DB_FILE)
            conn.execute("DELETE FROM saved_ads WHERE id = ?", (ad_id,))
            conn.commit()
            conn.close()
            messages.append(f"🚫 <b>PRODANO / UKLONJENO</b>\n{title or page_title}\n🔗 {url}")
            continue

        # Dohvati trenutnu cijenu (robustnije - DOM + fallback na sadržaj i JSON)
        current_price_text = ""
        # 1. Pokušaj DOM selectore
        for sel in [
            ".price--hrk",
            "strong.price",
            "dl.ClassifiedDetailSummary-priceRow dd",
            "[class*='priceDomestic']",
            "[class*='priceValue']",
            "[class*='Price']",
        ]:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    txt = el.inner_text().strip()
                    if txt and "€" in txt:
                        current_price_text = txt
                        break
            except Exception:
                pass

        # 2. Fallback: regex na cijelom sadržaju stranice
        if not current_price_text:
            try:
                body_text = page.content()
                matches = re.findall(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*€', body_text)
                if matches:
                    current_price_text = matches[0] + " €"
            except Exception:
                pass

        # 3. Fallback: iz embedded JSON (često u __NEXT_DATA__ ili LD+JSON)
        if not current_price_text:
            try:
                price_json = page.evaluate('''() => {
                    const scripts = document.querySelectorAll('script[type="application/ld+json"], script#__NEXT_DATA__');
                    for (let s of scripts) {
                        try {
                            let txt = s.textContent || '';
                            if (txt.includes('price') || txt.includes('€')) {
                                const data = JSON.parse(txt);
                                if (data) {
                                    if (data.offers && data.offers.price) return data.offers.price + ' €';
                                    if (data.price) return data.price;
                                    const str = JSON.stringify(data);
                                    const m = str.match(/"price"\\s*:\\s*"([0-9. ]+ ?€?)"/i);
                                    if (m) return m[1];
                                }
                            }
                        } catch(e) {}
                    }
                    return "";
                }''')
                if price_json and "€" in str(price_json):
                    current_price_text = str(price_json)
            except Exception:
                pass

        if not current_price_text:
            # NEMA brisanja kad cijena nije pronadjena — opisi oglasa sadrze "prodano" itd.
            print(f"    [!] Nema cijene za saved ad {ad_id}, preskacem (nije prodano)")
            continue

        checked += 1

        # Azuriraj naslov
        if not title:
            title = page_title.split(" - ")[0].strip()

        # Azuriraj saved_price ako je NULL
        if saved_price is None:
            saved_price = _extract_price_num(current_price_text)

        current_num = _extract_price_num(current_price_text)
        saved_num = float(saved_price) if saved_price else None

        # Detektiraj promjenu
        if saved_num is not None and current_num is not None and current_num != saved_num:
            direction = "📈 PORASLA" if current_num > saved_num else "📉 PALA"
            old_price_str = f"{saved_price:.0f} €"
            messages.append(
                f"{direction}\n<b>{title}</b>\n"
                f"💾 Bilo: {old_price_str}\n"
                f"💰 Sada: {current_price_text}\n"
                f"🔗 {url}"
            )
            saved_price = current_num

        # Spremi u bazu
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "UPDATE saved_ads SET last_price = ?, last_checked = ?, title = ?, saved_price = ? WHERE id = ?",
            (current_price_text, now, title, saved_price, ad_id),
        )
        conn.commit()
        conn.close()

    if skipped_captcha:
        print(f"  [i] Preskoceno {skipped_captcha} spremljenih oglasa (CAPTCHA)")
    if checked:
        print(f"  [i] Provjereno {checked} spremljenih oglasa")
    if messages:
        print(f"  [!] {len(messages)} promjena detektirano")

    return messages, skipped_captcha


def _extract_price_num(price_text: str) -> float | None:
    """Izvlači brojčanu vrijednost iz tekstualne cijene.
    Podržava EU format: '17.600 €' (17600), '699,00 €' (699), '1.234,56 €' (1234.56).
    """
    if not price_text:
        return None
    # Ukloni € i whitespace
    text = re.sub(r'[€\s]', '', price_text.strip())
    if not text:
        return None
    # Ako ima zarez, to je decimalni separator (EU format): prvo ukloni točke (tisuće), zamijeni zarez točkom
    if ',' in text:
        text = text.replace('.', '').replace(',', '.')
    else:
        # Samo točke kao tisuće separator
        text = text.replace('.', '')
    try:
        return float(text)
    except ValueError:
        return None


# =============================================================================
#  SCRAPING
# =============================================================================


def scrape_listings(page, url: str, pages: int) -> list[dict]:
    """Vraca listu oglasa: [{id, title, price, url, date}, ...]"""
    all_ads = []
    seen_ids = set()

    for p in range(1, pages + 1):
        page_url = url if p == 1 else f"{url}&page={p}"

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        except Exception as e:
            print(f"    [!] Greska ucitavanja str.{p}: {e}")
            continue

        time.sleep(1.5)

        if "shield" in page.content()[:5000].lower():
            print(f"    [!] CAPTCHA na str.{p}, preskacem URL")
            break

        ads = page.evaluate("""
            () => {
                const results = [];

                document.querySelectorAll('.EntityList-item--Regular article.entity-body').forEach(article => {
                    const link = article.querySelector('h3.entity-title a.link');
                    if (!link) return;
                    const titleEl = link.querySelector('span');
                    const title = titleEl ? titleEl.textContent.trim() : link.textContent.trim();
                    const href = link.getAttribute('href');
                    const nameId = link.getAttribute('name');
                    const priceEl = article.querySelector('.price--hrk, strong.price');
                    const price = priceEl ? priceEl.textContent.trim() : '';
                    const dateEl = article.querySelector('.date--full, .entity-pub-date time');
                    const pubDate = dateEl ? dateEl.textContent.trim() : '';
                    const datetime = dateEl ? dateEl.getAttribute('datetime') : '';

                    let adId = nameId ? parseInt(nameId) : null;
                    if (!adId && href) {
                        const m = href.match(/-oglas-(\\d+)/);
                        if (m) adId = parseInt(m[1]);
                    }
                    if (adId) results.push({ id: adId, title, price, url: href, date: pubDate, datetime: datetime });
                });

                return results;
            }
        """)

        # Deduplikacija i sredivanje URL-ova
        for ad in ads:
            ad_id = ad["id"]
            if ad_id and ad_id not in seen_ids:
                seen_ids.add(ad_id)
                if ad["url"].startswith("/"):
                    ad["url"] = "https://www.njuskalo.hr" + ad["url"]
                all_ads.append(ad)

    return all_ads


# =============================================================================
#  TELEGRAM
# =============================================================================


def telegram_configured() -> bool:
    return (
        TELEGRAM_BOT_TOKEN not in ("YOUR_BOT_TOKEN_HERE", "")
        and TELEGRAM_CHAT_ID not in ("YOUR_CHAT_ID_HERE", "")
    )


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(text: str):
    """Salje poruku. Ako je preduga, dijeli je na vise poruka."""
    if not telegram_configured():
        print("  [!] Telegram nije konfiguriran")
        return

    import requests

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_message(text, TELEGRAM_MAX_CHARS)

    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(
                base_url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"    [!] Telegram error {resp.status_code}: {resp.text[:200]}")
            else:
                label = f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""
                print(f"    [✓] Telegram poslan{label}")
        except Exception as e:
            print(f"    [!] Telegram greska: {e}")


def _split_message(text: str, max_len: int) -> list[str]:
    """Dijeli dugu poruku na chunkove, pazeci na granice redaka."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


# =============================================================================
#  MAIN
# =============================================================================


def run():
    init_db()
    sync_saved_ads_from_json()
    first_run = db_is_empty()

    print("=" * 60)
    print("  NJuskalo Monitor v2.0")
    print(f"  {time.strftime('%d.%m.%Y. %H:%M:%S')}")
    if first_run:
        print("  [i] PRVO POKRETANJE - punim bazu bez slanja obavijesti")
    print("=" * 60)

    total_new = 0
    telegram_body = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="hr-HR",
        )

        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Inicijalno ucitavanje + kolacici
        first_url = list(URLS.values())[0]
        try:
            page.goto(first_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            try:
                btn = page.locator("#didomi-notice-agree-button")
                btn.wait_for(state="visible", timeout=4000)
                btn.click()
                time.sleep(0.5)
            except Exception:
                pass
        except Exception:
            pass

        # Obradi svaki URL
        categories = list(URLS.keys())
        for idx, (category, url) in enumerate(URLS.items()):
            print(f"\n[{category}]")
            print(f"  URL: {url}")

            ads = scrape_listings(page, url, PAGES_PER_URL)
            print(f"  [i] Ukupno na stranicama: {len(ads)} oglasa")

            new_ads = []
            skipped_old = 0
            for ad in ads:
                # Parsiraj i filtriraj po datumu
                pub_date = parse_date_croatian(ad.get("date", ""))
                ad["date"] = pub_date

                if is_too_old(pub_date):
                    skipped_old += 1
                    continue

                if is_new_ad(ad["id"]):
                    new_ads.append(ad)
                    save_ad(ad["id"], category, ad["title"], ad["url"], pub_date)

            if skipped_old:
                print(f"  [i] Preskoceno {skipped_old} prestarih oglasa (prije {SKIP_BEFORE_DATE})")

            if new_ads:
                print(f"  [✓] {len(new_ads)} NOVIH!")
                total_new += len(new_ads)

                telegram_body += f"\n<b>━━━ {category} ━━━</b>\n"
                telegram_body += f"<i>{len(new_ads)} novih oglasa</i>\n\n"

                for ad in new_ads:
                    title_safe = escape_html(ad["title"])
                    price_safe = ad["price"].strip() if ad["price"] else "?"
                    url_safe = escape_html(ad["url"])
                    date_safe = ad.get("date", "")
                    telegram_body += f"<b>{title_safe}</b>\n"
                    telegram_body += f"💰 {price_safe}\n"
                    if date_safe:
                        telegram_body += f"📅 {date_safe}\n"
                    telegram_body += f"🔗 {url_safe}\n\n"
            else:
                print(f"  [~] Nema novih")

            if idx < len(categories) - 1:
                time.sleep(DELAY_BETWEEN_URLS)

        # ------------------------------------------------------------------
        # Provjera spremljenih oglasa (price tracking)
        # ------------------------------------------------------------------
        saved_messages, _ = check_saved_ads(page)
        if saved_messages:
            print(f"\n[S] Promjene na spremljenim oglasima: {len(saved_messages)}")
            telegram_body += "\n<b>━━━ SPREMLJENI OGLASI ━━━</b>\n"
            for msg in saved_messages:
                telegram_body += msg + "\n\n"
                print(f"  [!] {msg.split(chr(10))[0]}")

        browser.close()

    export_saved_ads_to_json()

    # Salji Telegram samo kad ima stvarnih događaja (novi oglasi ili promjene cijena / prodano)
    if not first_run and telegram_configured():
        if total_new > 0 or saved_messages:
            header = (
                f"🆕 <b>NJUSKALO - NOVI OGLASI</b>\n"
                f"📅 {time.strftime('%d.%m.%Y. %H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            send_telegram(header + telegram_body)
    elif first_run and total_new > 0:
        print(f"\n[i] Inicijalno spremljeno {total_new} oglasa u bazu (bez obavijesti)")

    # Ispis statistike
    stats = get_db_stats()
    print(f"\n{'=' * 60}")
    print(f"  GOTOVO! Novih: {total_new} | Baza ukupno: {stats['total']}")
    for cat, cnt in sorted(stats.get("by_category", {}).items()):
        print(f"    {cat}: {cnt}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    run()
