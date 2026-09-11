#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════════════════════
#  🔍 FORIDUL DOMAIN FINDER & BUG HUNTER BOT v1.0 (RENDER + FLASK WEBHOOK)
# ══════════════════════════════════════════════════════════════════════════════

import os
import re
import socket
import ssl
import logging
from io import BytesIO
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import telebot
from telebot import types
import requests
import dns.resolver
from flask import Flask, request, jsonify

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("DOMAIN_BOT_TOKEN", "8877299023:AAGe7VgeDeiF8r_H1AutS5_7fA9Lkc_k1ao")
ADMIN_IDS = [int(x) for x in os.environ.get("DOMAIN_BOT_ADMINS", "5802122865").split(",")]
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://bot-tg-8ms7.onrender.com")

MAX_WORKERS = 20
REQUEST_TIMEOUT = 8
VERSION = "1.0.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("DomainBot")

# ──────────────────────────────────────────────────────────────────────────────
# BOT INIT
# ──────────────────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
user_states = {}

# Databases
SOCIAL_DOMAINS = {"facebook.com", "fbcdn.net", "fbsbx.com", "fb.com", "fb.me", "instagram.com", "whatsapp.com", "messenger.com", "snapchat.com", "tiktok.com", "twitter.com", "linkedin.com", "reddit.com"}
CHAT_DOMAINS = {"telegram.org", "t.me", "viber.com", "signal.org", "wechat.com", "discord.com", "slack.com", "skype.com", "zoom.us"}
RIDE_DOMAINS = {"uber.com", "pathao.com", "obhai.com", "shohoz.com", "foodpanda.com"}
VIDEO_DOMAINS = {"youtube.com", "netflix.com", "vimeo.com", "twitch.tv", "toffee.com.bd"}
GAMING_DOMAINS = {"freefire.com", "garena.com", "pubgmobile.com", "epicgames.com", "steampowered.com", "ea.com", "supercell.com"}

CDN_PROVIDERS = {
    "cloudflare": ["cloudflare", "cf-ray"],
    "akamai": ["akamai", "akamaized"],
    "cloudfront": ["cloudfront", "amzn"],
    "fastly": ["fastly"],
    "google": ["gstatic", "google"],
    "facebook": ["fbcdn", "facebook"],
}

# ══════════════════════════════════════════════════════════════════════════════
#  CORE SCANNING ENGINES (same as before)
# ══════════════════════════════════════════════════════════════════════════════

def find_subdomains_crtsh(domain):
    subs = set()
    try:
        r = requests.get(f"https://crt.sh/?q=%.{domain}&output=json", timeout=15)
        if r.status_code == 200:
            for entry in r.json():
                name = entry.get("name_value", "")
                for line in name.split("\n"):
                    line = line.strip().lower()
                    if line.endswith(domain) and "*" not in line:
                        subs.add(line)
    except: pass
    return subs

def find_subdomains_hackertarget(domain):
    subs = set()
    try:
        r = requests.get(f"https://api.hackertarget.com/hostsearch/?q={domain}", timeout=10)
        if r.status_code == 200 and "error" not in r.text.lower():
            for line in r.text.strip().split("\n"):
                parts = line.split(",")
                if parts and parts[0].strip().endswith(domain):
                    subs.add(parts[0].strip().lower())
    except: pass
    return subs

def find_subdomains_rapiddns(domain):
    subs = set()
    try:
        r = requests.get(f"https://rapiddns.io/subdomain/{domain}?full=1", timeout=10)
        if r.status_code == 200:
            for m in re.findall(r'<td>([a-zA-Z0-9._-]+\.' + re.escape(domain) + r')</td>', r.text):
                subs.add(m.strip().lower())
    except: pass
    return subs

def check_domain_live(domain):
    try:
        answers = dns.resolver.resolve(domain, "A", lifetime=5)
        return {"domain": domain, "live": True, "ips": [r.to_text() for r in answers]}
    except:
        return {"domain": domain, "live": False, "ips": []}

def check_http_status(domain):
    res = {"status_code": None, "server": "Unknown", "cdn": None, "redirect": None, "error": None}
    try:
        r = requests.get(f"https://{domain}", timeout=REQUEST_TIMEOUT, allow_redirects=False, verify=False)
        res["status_code"] = r.status_code
        res["server"] = r.headers.get("Server", "Unknown")
        res["redirect"] = r.headers.get("Location", "")
        all_h = str(r.headers).lower()
        for cdn, kws in CDN_PROVIDERS.items():
            if any(kw in all_h for kw in kws):
                res["cdn"] = cdn.title()
                break
        if "cf-ray" in r.headers: res["cdn"] = "Cloudflare"
    except Exception as e:
        res["error"] = str(e)[:50]
    return res

def check_websocket_support(domain):
    res = {"ws_support": False, "status": None, "error": None}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(REQUEST_TIMEOUT)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(sock, server_hostname=domain)
        sock.connect((domain, 443))
        req = (f"GET / HTTP/1.1\r\nHost: {domain}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.send(req.encode())
        resp = sock.recv(4096).decode(errors="ignore")
        sock.close()
        if "101" in resp:
            res["ws_support"] = True
            res["status"] = "101 Switching Protocols ✅"
        else:
            res["status"] = resp.split("\r\n")[0][:60] if resp else "Empty"
    except Exception as e:
        res["error"] = str(e)[:50]
    return res

def check_tls_info(domain):
    res = {"tls_version": None, "cipher": None, "cert_issuer": None, "cert_subject": None, "error": None}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((domain, 443), timeout=REQUEST_TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                res["tls_version"] = ssock.version()
                res["cipher"] = ssock.cipher()[0] if ssock.cipher() else None
    except Exception as e:
        res["error"] = str(e)[:50]
    return res

def reverse_ip_lookup(ip):
    domains = set()
    try:
        r = requests.get(f"https://api.hackertarget.com/reverseiplookup/?q={ip}", timeout=10)
        if r.status_code == 200 and "error" not in r.text.lower():
            for line in r.text.strip().split("\n"):
                if line and "." in line: domains.add(line.strip().lower())
    except: pass
    return {"ip": ip, "domains": list(domains)}

def classify_domain(domain):
    domain = domain.lower()
    for sd in SOCIAL_DOMAINS:
        if domain.endswith(sd): return "📱 Social"
    for sd in CHAT_DOMAINS:
        if domain.endswith(sd): return "💬 Chat/IM"
    for sd in VIDEO_DOMAINS:
        if domain.endswith(sd): return "📺 Video"
    for sd in GAMING_DOMAINS:
        if domain.endswith(sd): return "🎮 Gaming"
    return "🌐 General"

def full_domain_scan(domain):
    return {
        "domain": domain,
        "http": check_http_status(domain),
        "ws": check_websocket_support(domain),
        "tls": check_tls_info(domain),
        "category": classify_domain(domain)
    }

# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
def is_admin(m):
    if m.from_user.id in ADMIN_IDS: return True
    bot.reply_to(m, "⛔ Access Denied.")
    return False

def main_keyboard():
    mk = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add("🔍 Subdomain Finder", "🌐 Reverse IP Lookup", "⚡ SNI / Bug Checker")
    return mk

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    if not is_admin(message): return
    bot.send_message(message.chat.id, f"🔍 <b>Domain Finder & Bug Hunter Bot v{VERSION}</b>\n\nChoose an option below:", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "🔍 Subdomain Finder")
def handle_find_prompt(message):
    if not is_admin(message): return
    bot.send_message(message.chat.id, "Enter the root domain (e.g. robi.com.bd):", reply_markup=types.ForceReply())

@bot.message_handler(func=lambda m: getattr(m.reply_to_message, 'text', '').startswith('Enter the root domain'))
def handle_find_reply(message):
    if not is_admin(message): return
    domain = message.text.strip().lower()
    msg = bot.send_message(message.chat.id, f"🔍 Scanning <code>{domain}</code>...")
    
    subs = set()
    subs.update(find_subdomains_crtsh(domain))
    subs.update(find_subdomains_hackertarget(domain))
    subs.update(find_subdomains_rapiddns(domain))
    
    if not subs:
        bot.edit_message_text(f"❌ No subdomains found for {domain}", message.chat.id, msg.message_id)
        return
        
    bot.edit_message_text(f"🔍 Found {len(subs)} subdomains! Checking DNS...", message.chat.id, msg.message_id)
    
    live = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for res in as_completed([pool.submit(check_domain_live, s) for s in subs]):
            if res.result()["live"]: live.append(res.result())
            
    text = f"📊 <b>Total: {len(subs)} | Live: {len(live)}</b>\n\n"
    for idx, s in enumerate(live[:30], 1):
        text += f"{idx}. <code>{s['domain']}</code> ({classify_domain(s['domain'])})\n"
        
    if len(live) > 30: text += f"\n<i>...and {len(live)-30} more (see file)</i>"
    bot.edit_message_text(text, message.chat.id, msg.message_id)
    
    if live:
        file_content = f"# Live Subdomains for {domain}\n" + "\n".join(s['domain'] for s in live)
        doc = BytesIO(file_content.encode())
        doc.name = f"{domain}_live.txt"
        bot.send_document(message.chat.id, doc)

@bot.message_handler(func=lambda m: m.text == "⚡ SNI / Bug Checker")
def handle_check_prompt(message):
    if not is_admin(message): return
    bot.send_message(message.chat.id, "Enter domain for full SNI/Bug check:", reply_markup=types.ForceReply())

@bot.message_handler(func=lambda m: getattr(m.reply_to_message, 'text', '').startswith('Enter domain for full SNI'))
def handle_check_reply(message):
    if not is_admin(message): return
    domain = message.text.strip().lower()
    msg = bot.send_message(message.chat.id, f"⚡ Checking <code>{domain}</code>...")
    
    scan = full_domain_scan(domain)
    
    text = f"⚡ <b>SNI SCAN: {domain}</b>\n"
    text += f"🏷 Category: {scan['category']}\n\n"
    
    h = scan["http"]
    text += f"🌐 HTTP: {h['status_code']} | Server: {h['server']}\n"
    if h.get("cdn"): text += f"   CDN: {h['cdn']}\n"
    
    w = scan["ws"]
    text += f"🔗 WS: {'✅ 101 OK' if w['ws_support'] else ('❌ ' + str(w['status']))}\n"
    
    bot.edit_message_text(text, message.chat.id, msg.message_id)

@bot.message_handler(func=lambda m: m.text == "🌐 Reverse IP Lookup")
def handle_reverse_prompt(message):
    if not is_admin(message): return
    bot.send_message(message.chat.id, "Enter IP Address:", reply_markup=types.ForceReply())

@bot.message_handler(func=lambda m: getattr(m.reply_to_message, 'text', '').startswith('Enter IP Address'))
def handle_reverse_reply(message):
    if not is_admin(message): return
    ip = message.text.strip()
    msg = bot.send_message(message.chat.id, f"🌐 Reverse IP scanning <code>{ip}</code>...")
    
    res = reverse_ip_lookup(ip)
    doms = res["domains"]
    
    if not doms:
        bot.edit_message_text(f"❌ No domains found on {ip}", message.chat.id, msg.message_id)
        return
        
    text = f"🌐 <b>REVERSE IP: {ip}</b>\n📋 Found: {len(doms)}\n\n"
    for i, d in enumerate(doms[:30], 1): text += f"{i}. <code>{d}</code>\n"
    
    bot.edit_message_text(text, message.chat.id, msg.message_id)

# ══════════════════════════════════════════════════════════════════════════════
#  FLASK SERVER & WEBHOOK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET", "HEAD"])
def index():
    return "Bot is running!", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "error", 403

def setup_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    log.info(f"Webhook set to {WEBHOOK_URL}/webhook")

if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    setup_webhook()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
