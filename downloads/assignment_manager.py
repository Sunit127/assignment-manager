#!/usr/bin/env python3
"""
Assignment Manager v3
=====================
A beautiful, client-first desktop app for tracking assignments and deadlines.

Architecture
- Python standard-library backend (SQLite + tiny JSON HTTP API on 127.0.0.1)
- Modern animated HTML/CSS/JS interface (ui/index.html)
- Native desktop window via pywebview (WKWebView on macOS, WebView2 on Windows);
  falls back to a Chrome/Edge "app mode" window, then to the default browser
- Background reminder thread -> native OS notifications
- Shares ~/.assignment_manager/assignments.db with the Claude extension

Run:  python3 assignment_manager.py            (opens the app window)
      python3 assignment_manager.py --server   (API + UI only, prints the URL)
"""

import base64
import csv
import hashlib
import json
import os
import secrets
import ssl
import urllib.error
import urllib.request
import uuid
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_NAME = "Assignment Manager"
VERSION = "3.1.0"
DATE_FMT = "%Y-%m-%d %H:%M"
DAY_FMT = "%Y-%m-%d"
DATA_DIR = os.environ.get("ASSIGNMENT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".assignment_manager")
DB_PATH = os.path.join(DATA_DIR, "assignments.db")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
EMBEDDED_UI = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Assignment Manager</title>\n<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22%3E%3Cdefs%3E%3ClinearGradient id=%22g%22%3E%3Cstop offset=%220%22 stop-color=%22%238b5cf6%22/%3E%3Cstop offset=%221%22 stop-color=%22%2306b6d4%22/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width=%22100%22 height=%22100%22 rx=%2224%22 fill=%22url(%23g)%22/%3E%3Ctext x=%2250%22 y=%2268%22 font-size=%2256%22 text-anchor=%22middle%22 fill=%22white%22%3E%E2%9C%A6%3C/text%3E%3C/svg%3E">\n<style>\n/* ------------------------------------------------------------------ tokens */\n:root{\n  --r:18px; --r-sm:12px;\n  --ease: cubic-bezier(.2,.8,.2,1);\n  --spring: cubic-bezier(.34,1.56,.64,1);\n  --overdue:#ef4444; --today:#f97316; --soon:#f59e0b; --normal:#6366f1; --done:#10b981;\n  --font: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;\n}\nbody[data-mode="dark"]{\n  --bg:#060914; --text:#eef1f8; --muted:#8e98b0; --faint:#5b647a;\n  --glass:rgba(16,21,38,.58); --glass-2:rgba(255,255,255,.045); --glass-3:rgba(255,255,255,.08);\n  --border:rgba(255,255,255,.08); --shadow:0 20px 60px -20px rgba(0,0,0,.65);\n  --input:rgba(255,255,255,.06);\n}\nbody[data-mode="light"]{\n  --bg:#eef1f9; --text:#0d1326; --muted:#5d6782; --faint:#97a0b6;\n  --glass:rgba(255,255,255,.66); --glass-2:rgba(255,255,255,.72); --glass-3:rgba(255,255,255,.9);\n  --border:rgba(15,23,42,.08); --shadow:0 20px 50px -24px rgba(30,41,90,.35);\n  --input:rgba(255,255,255,.85);\n}\nbody[data-theme="aurora"]{--c1:#7c3aed;--c2:#06b6d4;--c3:#22c55e;--accent:#8b5cf6;--accent2:#06b6d4}\nbody[data-theme="sunset"]{--c1:#f97316;--c2:#ec4899;--c3:#8b5cf6;--accent:#f43f5e;--accent2:#f97316}\nbody[data-theme="ocean"] {--c1:#0ea5e9;--c2:#6366f1;--c3:#14b8a6;--accent:#3b82f6;--accent2:#14b8a6}\nbody[data-theme="rose"]  {--c1:#f43f5e;--c2:#a855f7;--c3:#f59e0b;--accent:#e11d48;--accent2:#a855f7}\nbody[data-theme="forest"]{--c1:#10b981;--c2:#84cc16;--c3:#0ea5e9;--accent:#059669;--accent2:#0ea5e9}\n\n*{box-sizing:border-box}\nhtml,body{margin:0;height:100%;overflow:hidden}\nbody{font-family:var(--font);background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;\n  transition:background .6s var(--ease),color .4s var(--ease)}\nbutton,input,select,textarea{font:inherit;color:inherit}\n::selection{background:color-mix(in srgb,var(--accent) 40%,transparent)}\n::-webkit-scrollbar{width:10px;height:10px}\n::-webkit-scrollbar-thumb{background:var(--glass-3);border-radius:10px;border:3px solid transparent;background-clip:padding-box}\n::-webkit-scrollbar-track{background:transparent}\n\n/* -------------------------------------------------------------- background */\n.bg{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none}\n.blob{position:absolute;width:62vmax;height:62vmax;border-radius:50%;filter:blur(90px);opacity:.55;mix-blend-mode:screen;\n  transition:background 1.2s var(--ease)}\nbody[data-mode="light"] .blob{opacity:.42;mix-blend-mode:multiply;filter:blur(110px)}\n.b1{background:radial-gradient(circle at 30% 30%,var(--c1),transparent 60%);top:-22vmax;left:-12vmax;animation:drift1 26s ease-in-out infinite alternate}\n.b2{background:radial-gradient(circle at 60% 40%,var(--c2),transparent 60%);bottom:-28vmax;right:-14vmax;animation:drift2 32s ease-in-out infinite alternate}\n.b3{background:radial-gradient(circle at 50% 50%,var(--c3),transparent 62%);top:30%;left:35%;width:44vmax;height:44vmax;opacity:.32;animation:drift3 38s ease-in-out infinite alternate}\n@keyframes drift1{to{transform:translate(18vmax,12vmax) scale(1.15) rotate(40deg)}}\n@keyframes drift2{to{transform:translate(-16vmax,-10vmax) scale(1.2) rotate(-30deg)}}\n@keyframes drift3{0%{transform:translate(-10vmax,-6vmax) scale(.9)}100%{transform:translate(12vmax,8vmax) scale(1.25)}}\n.grid-overlay{position:absolute;inset:0;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);\n  background-size:56px 56px;mask-image:radial-gradient(ellipse at 50% 30%,#000 10%,transparent 70%);opacity:.5}\n.grain{position:absolute;inset:-50%;opacity:.05;background-image:url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'160\' height=\'160\'%3E%3Cfilter id=\'n\'%3E%3CfeTurbulence type=\'fractalNoise\' baseFrequency=\'.9\' numOctaves=\'3\' stitchTiles=\'stitch\'/%3E%3C/filter%3E%3Crect width=\'100%25\' height=\'100%25\' filter=\'url(%23n)\'/%3E%3C/svg%3E");\n  animation:grain 8s steps(10) infinite}\n@keyframes grain{0%,100%{transform:translate(0,0)}20%{transform:translate(-5%,4%)}40%{transform:translate(4%,-6%)}60%{transform:translate(-3%,7%)}80%{transform:translate(6%,2%)}}\n\n/* ------------------------------------------------------------------ layout */\n.app{position:relative;z-index:1;display:grid;grid-template-columns:284px 1fr;height:100vh}\n.glass{background:var(--glass);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);border:1px solid var(--border)}\n.sidebar{margin:14px 0 14px 14px;border-radius:24px;display:flex;flex-direction:column;overflow:hidden;box-shadow:var(--shadow);\n  animation:slideIn .7s var(--ease) both}\n@keyframes slideIn{from{opacity:0;transform:translateX(-24px)}}\n.brand{display:flex;align-items:center;gap:12px;padding:22px 20px 16px}\n.logo{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;font-size:20px;color:#fff;\n  background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 10px 24px -8px var(--accent);position:relative;overflow:hidden}\n.logo::after{content:"";position:absolute;inset:0;background:linear-gradient(120deg,transparent 30%,rgba(255,255,255,.5) 50%,transparent 70%);\n  transform:translateX(-100%);animation:shine 4.5s ease-in-out infinite}\n@keyframes shine{60%,100%{transform:translateX(100%)}}\n.brand b{font-size:17px;letter-spacing:-.02em;display:block}\n.brand small{color:var(--muted);font-size:12px}\n.nav{padding:4px 12px;display:flex;flex-direction:column;gap:2px}\n.nav-item,.client-item{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:12px;cursor:pointer;color:var(--muted);\n  transition:background .25s var(--ease),color .25s,transform .25s var(--ease);position:relative;border:0;background:none;width:100%;text-align:left}\n.nav-item:hover,.client-item:hover{background:var(--glass-2);color:var(--text);transform:translateX(3px)}\n.nav-item.active,.client-item.active{background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 26%,transparent),color-mix(in srgb,var(--accent2) 14%,transparent));color:var(--text)}\n.nav-item.active::before,.client-item.active::before{content:"";position:absolute;left:-12px;top:22%;bottom:22%;width:4px;border-radius:4px;background:linear-gradient(var(--accent),var(--accent2))}\n.nav-item .ico{width:20px;text-align:center;font-size:16px}\n.kbd{margin-left:auto;font-size:11px;color:var(--faint);border:1px solid var(--border);border-radius:6px;padding:1px 6px}\n.side-label{display:flex;align-items:center;justify-content:space-between;padding:18px 24px 8px;font-size:11px;font-weight:700;letter-spacing:.12em;color:var(--faint);text-transform:uppercase}\n.search{margin:0 14px 8px;position:relative}\n.search input{width:100%;padding:10px 12px 10px 34px;border-radius:12px;border:1px solid var(--border);background:var(--input);outline:none;transition:border .2s,box-shadow .2s}\n.search input:focus{border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 18%,transparent)}\n.search::before{content:"⌕";position:absolute;left:12px;top:50%;transform:translateY(-52%);color:var(--muted);font-size:16px}\n.clients{flex:1;overflow:auto;padding:0 12px 10px}\n.avatar{flex:none;width:36px;height:36px;border-radius:11px;display:grid;place-items:center;font-weight:700;font-size:13px;color:#fff;letter-spacing:.02em}\n.client-item .meta{min-width:0;flex:1}\n.client-item .name{color:var(--text);font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.client-item .sub{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.badge{min-width:22px;height:22px;padding:0 7px;border-radius:11px;display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff}\n.badge.overdue{background:var(--overdue);box-shadow:0 0 0 0 rgba(239,68,68,.6);animation:pulse 2s infinite}\n.badge.soon{background:var(--soon)}\n@keyframes pulse{70%{box-shadow:0 0 0 10px rgba(239,68,68,0)}100%{box-shadow:0 0 0 0 rgba(239,68,68,0)}}\n.side-foot{padding:12px;border-top:1px solid var(--border);display:grid;gap:8px}\n.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 16px;border-radius:12px;border:1px solid var(--border);\n  background:var(--glass-2);cursor:pointer;font-weight:600;font-size:14px;transition:transform .2s var(--spring),background .2s,box-shadow .25s,border-color .2s;white-space:nowrap}\n.btn:hover{transform:translateY(-2px);background:var(--glass-3)}\n.btn:active{transform:translateY(0) scale(.97)}\n.btn.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border:0;box-shadow:0 12px 28px -12px var(--accent)}\n.btn.primary:hover{box-shadow:0 16px 36px -12px var(--accent)}\n.btn.danger{color:#fff;background:linear-gradient(135deg,#ef4444,#f43f5e);border:0}\n.btn.ghost{background:transparent}\n.btn.sm{padding:7px 12px;font-size:13px;border-radius:10px}\n.btn.icon{width:38px;height:38px;padding:0}\n.row{display:flex;gap:8px;align-items:center}\n\nmain{overflow:auto;padding:14px 14px 40px 22px;scroll-behavior:smooth}\n#view{max-width:1400px;margin:0 auto}\n.view-enter > *{animation:fadeUp .6s var(--ease) both;animation-delay:calc(var(--i,0) * 55ms)}\n@keyframes fadeUp{from{opacity:0;transform:translateY(16px) scale(.985)}}\n\n/* ------------------------------------------------------------------ header */\n.page-head{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding:22px 6px 18px}\n.page-head h1{font-size:34px;letter-spacing:-.035em;margin:0;line-height:1.1}\n.page-head p{margin:6px 0 0;color:var(--muted)}\n.grad-text{background:linear-gradient(120deg,var(--text) 20%,var(--accent) 60%,var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent;\n  background-size:200% auto;animation:gradmove 8s linear infinite}\n@keyframes gradmove{to{background-position:200% center}}\n\n.card{background:var(--glass);backdrop-filter:blur(22px) saturate(150%);-webkit-backdrop-filter:blur(22px) saturate(150%);\n  border:1px solid var(--border);border-radius:var(--r);box-shadow:var(--shadow);position:relative}\n.card.hover{transition:transform .35s var(--ease),box-shadow .35s var(--ease),border-color .3s}\n.card.hover:hover{transform:translateY(-4px);border-color:color-mix(in srgb,var(--accent) 40%,var(--border));box-shadow:0 30px 60px -24px color-mix(in srgb,var(--accent) 45%,rgba(0,0,0,.6))}\n.clickable{cursor:pointer}\n\n/* hero */\n.hero{display:grid;grid-template-columns:1.3fr 1fr;gap:20px;padding:26px 28px;overflow:hidden}\n.hero::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;background:linear-gradient(135deg,var(--accent),transparent 40%,transparent 60%,var(--accent2));\n  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none}\n.hero .glow{position:absolute;width:360px;height:360px;border-radius:50%;right:-80px;top:-140px;background:radial-gradient(circle,color-mix(in srgb,var(--accent) 55%,transparent),transparent 70%);\n  filter:blur(30px);opacity:.6;animation:breathe 6s ease-in-out infinite}\n@keyframes breathe{50%{transform:scale(1.15);opacity:.85}}\n.eyebrow{font-size:12px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--accent2)}\n.hero h2{font-size:26px;letter-spacing:-.02em;margin:8px 0 6px;line-height:1.2}\n.hero .who{color:var(--muted);display:flex;gap:14px;flex-wrap:wrap;font-size:14px}\n.countdown{display:flex;gap:10px;align-items:center;justify-content:flex-end;position:relative}\n.cd{min-width:78px;padding:14px 10px 10px;border-radius:16px;text-align:center;background:var(--glass-2);border:1px solid var(--border)}\n.cd b{display:block;font-size:38px;font-weight:800;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1}\n.cd span{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}\n.cd b.tick{animation:tick .45s var(--spring)}\n@keyframes tick{from{transform:translateY(-6px);opacity:.3}}\n.hero-foot{display:flex;align-items:center;gap:16px;margin-top:18px}\n.bar{flex:1;height:8px;border-radius:8px;background:var(--glass-3);overflow:hidden}\n.bar i{display:block;height:100%;border-radius:8px;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width 1.2s var(--ease);position:relative;overflow:hidden}\n.bar i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);animation:shimmer 2.2s infinite}\n@keyframes shimmer{from{transform:translateX(-100%)}to{transform:translateX(100%)}}\n\n.alert{display:flex;align-items:center;gap:14px;padding:14px 18px;border-radius:16px;margin-bottom:16px;cursor:pointer;\n  background:linear-gradient(135deg,rgba(239,68,68,.22),rgba(244,63,94,.12));border:1px solid rgba(239,68,68,.35)}\n.alert .dot{width:10px;height:10px;border-radius:50%;background:var(--overdue);animation:pulse 1.6s infinite}\n\n/* stats */\n.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:18px 0}\n.stat{padding:18px 18px 16px;overflow:hidden;cursor:pointer}\n.stat .ic{width:36px;height:36px;border-radius:11px;display:grid;place-items:center;font-size:17px;margin-bottom:12px}\n.stat b{font-size:32px;letter-spacing:-.03em;display:block;font-variant-numeric:tabular-nums}\n.stat span{color:var(--muted);font-size:13px}\n.stat::after{content:"";position:absolute;right:-30px;bottom:-30px;width:110px;height:110px;border-radius:50%;background:var(--c);opacity:.14;filter:blur(8px);transition:transform .5s var(--ease)}\n.stat:hover::after{transform:scale(1.4)}\n\n.grid2{display:grid;grid-template-columns:1.25fr 1fr;gap:18px}\n.section-title{display:flex;align-items:center;justify-content:space-between;margin:6px 4px 12px}\n.section-title h3{margin:0;font-size:17px;letter-spacing:-.01em}\n.section-title span{color:var(--muted);font-size:13px}\n.pad{padding:18px}\n\n/* timeline */\n.timeline{position:relative;padding:6px 6px 6px 26px}\n.timeline::before{content:"";position:absolute;left:11px;top:14px;bottom:14px;width:2px;background:linear-gradient(var(--accent),transparent)}\n.tl-item{position:relative;padding:12px 14px;border-radius:14px;cursor:pointer;transition:background .25s,transform .3s var(--ease);display:flex;gap:12px;align-items:center}\n.tl-item:hover{background:var(--glass-2);transform:translateX(4px)}\n.tl-item::before{content:"";position:absolute;left:-20px;top:50%;width:12px;height:12px;border-radius:50%;transform:translateY(-50%);background:var(--u);box-shadow:0 0 0 4px color-mix(in srgb,var(--u) 25%,transparent)}\n.tl-item .main{flex:1;min-width:0}\n.tl-item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\n.tl-item .s{font-size:12.5px;color:var(--muted)}\n.when{font-weight:800;font-variant-numeric:tabular-nums;color:var(--u);white-space:nowrap;text-align:right}\n.when small{display:block;font-weight:500;color:var(--muted);font-size:11.5px}\n\n/* workload chart */\n.chart{display:flex;align-items:flex-end;gap:6px;height:120px;padding:10px 4px 0}\n.chart .col{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px;height:100%;justify-content:flex-end}\n.chart .b{width:100%;border-radius:8px 8px 4px 4px;background:linear-gradient(var(--accent),var(--accent2));min-height:4px;transform-origin:bottom;animation:grow .9s var(--spring) both;animation-delay:calc(var(--i)*40ms);opacity:.9}\n.chart .b.zero{background:var(--glass-3)}\n.chart .lbl{font-size:10.5px;color:var(--muted)}\n.chart .col.today .lbl{color:var(--accent2);font-weight:700}\n@keyframes grow{from{transform:scaleY(0)}}\n\n/* client cards */\n.client-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}\n.cc{padding:18px}\n.cc .top{display:flex;gap:12px;align-items:center}\n.cc .top b{font-size:16px}\n.cc .top small{display:block;color:var(--muted);font-size:12.5px}\n.cc .next{margin-top:14px;padding-top:12px;border-top:1px solid var(--border);font-size:13px;color:var(--muted)}\n.cc .next b{display:block;color:var(--text);font-size:14px;margin:2px 0}\n\n/* chips */\n.chips{display:flex;flex-wrap:wrap;gap:6px}\n.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;background:var(--glass-3);border:1px solid var(--border);white-space:nowrap}\n.chip .d{width:7px;height:7px;border-radius:50%;background:currentColor}\n.c-overdue{color:#fca5a5;background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.3)}\n.c-today{color:#fdba74;background:rgba(249,115,22,.16);border-color:rgba(249,115,22,.3)}\n.c-soon{color:#fcd34d;background:rgba(245,158,11,.16);border-color:rgba(245,158,11,.3)}\n.c-normal{color:#a5b4fc;background:rgba(99,102,241,.16);border-color:rgba(99,102,241,.3)}\n.c-done{color:#6ee7b7;background:rgba(16,185,129,.16);border-color:rgba(16,185,129,.3)}\nbody[data-mode="light"] .c-overdue{color:#b91c1c} body[data-mode="light"] .c-today{color:#c2410c}\nbody[data-mode="light"] .c-soon{color:#a16207} body[data-mode="light"] .c-normal{color:#4338ca} body[data-mode="light"] .c-done{color:#047857}\n.p-Urgent{color:#fda4af} .p-High{color:#fdba74} .p-Medium{color:#93c5fd} .p-Low{color:var(--muted)}\nbody[data-mode="light"] .p-Urgent{color:#be123c} body[data-mode="light"] .p-High{color:#c2410c} body[data-mode="light"] .p-Medium{color:#1d4ed8}\n.pay-Unpaid{color:#fca5a5} .pay-Partially{color:#fcd34d} .pay-Paid{color:#6ee7b7}\nbody[data-mode="light"] .pay-Unpaid{color:#b91c1c} body[data-mode="light"] .pay-Partially{color:#a16207} body[data-mode="light"] .pay-Paid{color:#047857}\n\n/* client page */\n.client-head{display:flex;align-items:center;gap:20px;padding:26px 28px;overflow:hidden}\n.client-head .avatar{width:72px;height:72px;border-radius:22px;font-size:26px;box-shadow:0 16px 36px -14px var(--accent)}\n.client-head h1{margin:0;font-size:32px;letter-spacing:-.03em}\n.client-head p{margin:6px 0 0;color:var(--muted)}\n.back{color:var(--muted);cursor:pointer;font-size:13px;display:inline-flex;gap:6px;align-items:center;margin:16px 6px 10px;transition:color .2s,transform .2s}\n.back:hover{color:var(--text);transform:translateX(-3px)}\n.mini-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:16px 0}\n.mini{padding:16px 18px}\n.mini span{color:var(--muted);font-size:12.5px}\n.mini b{display:block;font-size:22px;letter-spacing:-.02em;margin-top:4px}\n.mini small{color:var(--muted);font-size:12px}\n.tabs{display:inline-flex;padding:4px;border-radius:14px;background:var(--glass-2);border:1px solid var(--border);position:relative}\n.tab{padding:7px 16px;border-radius:10px;cursor:pointer;font-weight:600;font-size:13.5px;color:var(--muted);position:relative;z-index:1;transition:color .25s;border:0;background:none}\n.tab.active{color:#fff}\n.tab-ink{position:absolute;top:4px;bottom:4px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));transition:left .4s var(--spring),width .4s var(--spring);box-shadow:0 8px 20px -10px var(--accent)}\n\n.modules{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;margin-top:14px}\n.mod{padding:20px 20px 16px;overflow:hidden;cursor:pointer}\n.mod::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--u);border-radius:var(--r) 0 0 var(--r)}\n.mod.sel{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 45%,transparent),var(--shadow)}\n.mod .head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}\n.mod .code{font-size:20px;font-weight:800;letter-spacing:-.02em;background:linear-gradient(120deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}\n.mod .title{font-weight:650;font-size:15.5px;margin:6px 0 2px;line-height:1.35}\n.mod .subj{color:var(--muted);font-size:13px}\n.mod .mid{display:flex;align-items:center;gap:16px;margin:16px 0 14px;padding:14px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border)}\n.ring{flex:none;position:relative;width:62px;height:62px}\n.ring svg{transform:rotate(-90deg)}\n.ring circle{fill:none;stroke-width:6}\n.ring .bgc{stroke:var(--glass-3)}\n.ring .fg{stroke:var(--u);stroke-linecap:round;transition:stroke-dashoffset 1.4s var(--ease)}\n.ring span{position:absolute;inset:0;display:grid;place-items:center;font-size:12.5px;font-weight:800}\n.mod .dl b{display:block;font-size:20px;font-weight:800;color:var(--u);font-variant-numeric:tabular-nums;letter-spacing:-.02em}\n.mod .dl small{color:var(--muted);font-size:13px}\n.mod .foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px;color:var(--faint);font-size:12px}\n\n/* board */\n.board{display:grid;grid-template-columns:repeat(5,minmax(230px,1fr));gap:14px;align-items:start;overflow-x:auto;padding-bottom:10px}\n.col-b{padding:14px;min-height:300px;transition:background .25s,border-color .25s}\n.col-b.over{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,var(--glass))}\n.col-b h4{margin:2px 4px 12px;display:flex;justify-content:space-between;align-items:center;font-size:14px}\n.col-b h4 span{font-size:12px;color:var(--muted);background:var(--glass-3);border-radius:999px;padding:2px 9px}\n.kc{padding:12px 14px;border-radius:14px;background:var(--glass-2);border:1px solid var(--border);margin-bottom:10px;cursor:grab;transition:transform .25s var(--ease),box-shadow .25s,opacity .2s;position:relative;overflow:hidden}\n.kc::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--u)}\n.kc:hover{transform:translateY(-2px);box-shadow:0 14px 30px -18px rgba(0,0,0,.6)}\n.kc.dragging{opacity:.4;transform:scale(.97)}\n.kc b{font-size:13px;color:var(--accent2)}\n.kc .t{font-weight:600;font-size:13.5px;margin:3px 0;line-height:1.3}\n.kc .s{font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:8px}\n\n/* calendar */\n.cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}\n.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}\n.cal .dow{text-align:center;font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;padding:6px 0}\n.day{min-height:112px;padding:8px;border-radius:14px;background:var(--glass-2);border:1px solid var(--border);transition:background .2s,transform .25s var(--ease);overflow:hidden}\n.day:hover{background:var(--glass-3)}\n.day.out{opacity:.38}\n.day.today{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}\n.day .n{font-size:13px;font-weight:700;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between}\n.day.today .n b{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#fff;border-radius:8px;padding:1px 7px}\n.pill{display:block;font-size:11.5px;font-weight:600;padding:4px 7px;border-radius:8px;margin-bottom:4px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;\n  background:color-mix(in srgb,var(--u) 22%,transparent);border-left:3px solid var(--u);transition:transform .2s var(--ease)}\n.pill:hover{transform:translateX(3px)}\n\n/* list view */\n.list-tools{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}\n.input{padding:10px 14px;border-radius:12px;border:1px solid var(--border);background:var(--input);outline:none;transition:border .2s,box-shadow .2s;min-width:0}\n.input:focus{border-color:var(--accent);box-shadow:0 0 0 4px color-mix(in srgb,var(--accent) 18%,transparent)}\nselect.input{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);\n  background-position:calc(100% - 16px) 52%,calc(100% - 11px) 52%;background-size:5px 5px;background-repeat:no-repeat;padding-right:32px}\nselect.input option{background:#111827;color:#eef1f8}\nbody[data-mode="light"] select.input option{background:#fff;color:#0d1326}\n.table{width:100%;border-collapse:separate;border-spacing:0 8px}\n.table th{text-align:left;font-size:12px;color:var(--muted);font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:0 14px}\n.table td{padding:14px;background:var(--glass-2);border-top:1px solid var(--border);border-bottom:1px solid var(--border);font-size:14px}\n.table tr td:first-child{border-left:1px solid var(--border);border-radius:14px 0 0 14px;box-shadow:inset 4px 0 0 var(--u)}\n.table tr td:last-child{border-right:1px solid var(--border);border-radius:0 14px 14px 0}\n.table tbody tr{cursor:pointer;transition:transform .25s var(--ease)}\n.table tbody tr:hover{transform:translateX(4px)}\n.table tbody tr:hover td{background:var(--glass-3)}\n\n/* drawer */\n#scrim{position:fixed;inset:0;background:rgba(3,6,15,.45);backdrop-filter:blur(3px);opacity:0;pointer-events:none;transition:opacity .35s var(--ease);z-index:20}\n#scrim.on{opacity:1;pointer-events:auto}\n.drawer{position:fixed;top:14px;right:14px;bottom:14px;width:min(460px,92vw);border-radius:24px;z-index:21;transform:translateX(calc(100% + 30px));\n  transition:transform .55s var(--ease);display:flex;flex-direction:column;overflow:hidden;box-shadow:-30px 0 80px -30px rgba(0,0,0,.7);background:var(--glass)}\n.drawer.on{transform:none}\n.dr-head{padding:24px 24px 20px;position:relative;overflow:hidden;color:#fff;background:linear-gradient(135deg,var(--u),color-mix(in srgb,var(--u) 55%,#1e1b4b))}\n.dr-head::after{content:"";position:absolute;width:260px;height:260px;border-radius:50%;right:-80px;top:-120px;background:rgba(255,255,255,.18);filter:blur(10px)}\n.dr-head .x{position:absolute;right:16px;top:16px;width:34px;height:34px;border-radius:10px;display:grid;place-items:center;cursor:pointer;background:rgba(255,255,255,.18);border:0;color:#fff;font-size:16px;z-index:2;transition:transform .3s var(--spring),background .2s}\n.dr-head .x:hover{transform:rotate(90deg);background:rgba(255,255,255,.3)}\n.dr-head .code{font-size:28px;font-weight:800;letter-spacing:-.03em}\n.dr-head .t{font-size:15px;opacity:.92;margin-top:4px;line-height:1.35;padding-right:30px}\n.dr-head .big{font-size:34px;font-weight:800;margin-top:14px;font-variant-numeric:tabular-nums;letter-spacing:-.03em}\n.dr-head .date{opacity:.9;font-size:14px}\n.dr-body{flex:1;overflow:auto;padding:18px 22px 26px}\n.dr-actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:16px}\n.field-row{display:grid;grid-template-columns:110px 1fr;align-items:center;gap:10px;margin:8px 0}\n.field-row label{color:var(--muted);font-size:13px}\n.kv{display:grid;grid-template-columns:110px 1fr;gap:8px 10px;margin:16px 0;font-size:14px}\n.kv dt{color:var(--muted);font-size:13px}\n.kv dd{margin:0;word-break:break-word}\n.block{margin-top:16px}\n.block h5{margin:0 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}\n.block p{margin:0;padding:14px 16px;border-radius:14px;background:var(--glass-2);border:1px solid var(--border);white-space:pre-wrap;line-height:1.55;font-size:14px}\n\n/* modal */\n.modal-wrap{position:fixed;inset:0;z-index:40;display:grid;place-items:center;background:rgba(3,6,15,.55);backdrop-filter:blur(6px);animation:fade .3s var(--ease)}\n@keyframes fade{from{opacity:0}}\n.modal{width:min(820px,94vw);max-height:90vh;overflow:auto;border-radius:24px;padding:26px 28px;animation:pop .5s var(--spring);background:var(--glass);box-shadow:0 40px 100px -30px rgba(0,0,0,.8)}\nbody[data-mode="dark"] .modal{background:rgba(14,19,35,.92)}\nbody[data-mode="light"] .modal{background:rgba(255,255,255,.95)}\n@keyframes pop{from{opacity:0;transform:translateY(20px) scale(.94)}}\n.modal h2{margin:0 0 4px;font-size:24px;letter-spacing:-.02em}\n.modal .sub{color:var(--muted);margin:0 0 20px}\n.form{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px}\n.form .full{grid-column:1/-1}\n.form label{display:block;font-size:12.5px;font-weight:600;color:var(--muted);margin-bottom:6px}\n.form .input{width:100%}\ntextarea.input{resize:vertical;min-height:90px;line-height:1.5}\n.err{color:#fca5a5;font-size:13px;min-height:18px;margin-top:10px}\n.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}\n.swatches{display:flex;gap:10px;flex-wrap:wrap}\n.sw{width:54px;height:54px;border-radius:16px;cursor:pointer;border:2px solid transparent;transition:transform .25s var(--spring),border-color .2s;position:relative}\n.sw:hover{transform:translateY(-3px) scale(1.05)}\n.sw.on{border-color:var(--text)}\n.sw.on::after{content:"✓";position:absolute;inset:0;display:grid;place-items:center;color:#fff;font-weight:800;text-shadow:0 1px 4px rgba(0,0,0,.4)}\n.switch{position:relative;width:46px;height:26px;border-radius:26px;background:var(--glass-3);cursor:pointer;border:1px solid var(--border);transition:background .3s}\n.switch::after{content:"";position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:transform .35s var(--spring);box-shadow:0 2px 6px rgba(0,0,0,.3)}\n.switch.on{background:linear-gradient(135deg,var(--accent),var(--accent2))}\n.switch.on::after{transform:translateX(20px)}\n\n/* palette */\n.palette{width:min(640px,92vw);border-radius:22px;overflow:hidden;animation:pop .45s var(--spring);background:var(--glass);box-shadow:0 40px 100px -30px rgba(0,0,0,.8)}\nbody[data-mode="dark"] .palette{background:rgba(14,19,35,.94)}\nbody[data-mode="light"] .palette{background:rgba(255,255,255,.96)}\n.palette input{width:100%;padding:20px 22px;border:0;outline:none;background:transparent;font-size:18px;border-bottom:1px solid var(--border)}\n.pal-list{max-height:420px;overflow:auto;padding:8px}\n.pal-item{display:flex;gap:12px;align-items:center;padding:11px 14px;border-radius:12px;cursor:pointer}\n.pal-item.on{background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 26%,transparent),color-mix(in srgb,var(--accent2) 12%,transparent))}\n.pal-item .k{margin-left:auto;font-size:12px;color:var(--muted)}\n.pal-group{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);padding:10px 14px 4px}\n\n/* toasts */\n#toasts{position:fixed;right:22px;bottom:22px;z-index:60;display:flex;flex-direction:column;gap:10px;align-items:flex-end}\n.toast{min-width:300px;max-width:400px;padding:14px 16px 14px 18px;border-radius:16px;display:flex;gap:12px;align-items:flex-start;cursor:pointer;\n  animation:toastIn .55s var(--spring) both;box-shadow:0 20px 50px -20px rgba(0,0,0,.7);position:relative;overflow:hidden}\nbody[data-mode="dark"] .toast{background:rgba(17,22,40,.92)}\nbody[data-mode="light"] .toast{background:rgba(255,255,255,.96)}\n.toast::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--u)}\n.toast .ti{font-size:20px}\n.toast b{display:block;font-size:14px;margin-bottom:2px}\n.toast span{font-size:13px;color:var(--muted);white-space:pre-line}\n.toast .life{position:absolute;left:0;bottom:0;height:3px;background:var(--u);animation:life linear forwards}\n.toast.out{animation:toastOut .4s var(--ease) forwards}\n@keyframes toastIn{from{opacity:0;transform:translateX(60px) scale(.9)}}\n@keyframes toastOut{to{opacity:0;transform:translateX(60px) scale(.9)}}\n@keyframes life{from{width:100%}to{width:0}}\n#confetti{position:fixed;inset:0;pointer-events:none;z-index:70}\n\n.sync-pill{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--muted);cursor:pointer;margin-top:2px}\n.sync-pill i{width:7px;height:7px;border-radius:50%;background:var(--faint)}\n.sync-pill.ok i{background:var(--done);box-shadow:0 0 0 3px color-mix(in srgb,var(--done) 25%,transparent)}\n.sync-pill.syncing i{background:var(--accent2);animation:pulse 1s infinite}\n.sync-pill.error i{background:var(--overdue)}\n.empty{text-align:center;padding:60px 20px;color:var(--muted)}\n.empty .big{font-size:56px;margin-bottom:10px;animation:float 4s ease-in-out infinite}\n@keyframes float{50%{transform:translateY(-10px)}}\n.empty h3{color:var(--text);margin:0 0 6px}\n.loader{position:fixed;inset:0;display:grid;place-items:center;z-index:80;background:var(--bg);transition:opacity .6s var(--ease)}\n.loader .ring2{width:56px;height:56px;border-radius:50%;border:4px solid var(--glass-3);border-top-color:var(--accent);animation:spin .9s linear infinite}\n@keyframes spin{to{transform:rotate(360deg)}}\n\n@media (max-width:1200px){.stats{grid-template-columns:repeat(3,1fr)}.grid2,.hero{grid-template-columns:1fr}.countdown{justify-content:flex-start}.mini-stats{grid-template-columns:repeat(2,1fr)}}\n@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}\n</style>\n</head>\n<body data-mode="dark" data-theme="aurora">\n<div class="loader" id="loader"><div class="ring2"></div></div>\n<div class="bg"><div class="blob b1"></div><div class="blob b2"></div><div class="blob b3"></div><div class="grid-overlay"></div><div class="grain"></div></div>\n\n<div class="app">\n  <aside class="sidebar glass">\n    <div class="brand"><div class="logo">✦</div><div><b>Assignment Manager</b><small id="ver">Client deadlines, beautifully</small><div class="sync-pill" id="sync-pill" title="iPhone sync"><i></i><span>Sync off</span></div></div></div>\n    <nav class="nav" id="nav"></nav>\n    <div class="side-label"><span>Clients</span><span id="client-count"></span></div>\n    <div class="search"><input id="client-search" placeholder="Search clients…" autocomplete="off"></div>\n    <div class="clients" id="clients"></div>\n    <div class="side-foot">\n      <button class="btn primary" id="btn-new">＋ New assignment <span class="kbd" style="color:#fff;border-color:rgba(255,255,255,.4)">N</span></button>\n      <div class="row">\n        <button class="btn sm" style="flex:1" id="btn-palette">⌘K Search</button>\n        <button class="btn sm icon" id="btn-mode" title="Light / dark (D)">🌓</button>\n        <button class="btn sm icon" id="btn-settings" title="Settings">⚙</button>\n      </div>\n    </div>\n  </aside>\n  <main id="main"><div id="view"></div></main>\n</div>\n\n<div id="scrim"></div>\n<aside class="drawer glass" id="drawer"></aside>\n<div id="modal-root"></div>\n<div id="toasts"></div>\n<canvas id="confetti"></canvas>\n\n<script>\n"use strict";\nconst TOKEN = "__API_TOKEN__";\nconst $ = (s, el = document) => el.querySelector(s);\nconst $$ = (s, el = document) => [...el.querySelectorAll(s)];\nconst esc = s => String(s ?? "").replace(/[&<>"\']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\'":"&#39;"}[c]));\nconst NOCLIENT = "No client";\nconst U = {overdue:"var(--overdue)", today:"var(--today)", soon:"var(--soon)", normal:"var(--normal)", done:"var(--done)"};\nconst ULABEL = {overdue:"Overdue", today:"Due today", soon:"Due soon", normal:"Upcoming", done:"Done"};\nconst AV_GRADS = [["#8b5cf6","#06b6d4"],["#f43f5e","#f97316"],["#10b981","#0ea5e9"],["#f59e0b","#ef4444"],["#6366f1","#ec4899"],["#14b8a6","#84cc16"],["#3b82f6","#a855f7"]];\n\nconst S = {rows:[], settings:{}, meta:{}, sig:null, view:"dashboard", client:null, selected:null,\n           filter:"Active", cal:new Date(), listQ:"", listStatus:"active", lastEvent:Date.now()/1000, first:true};\n\n/* ------------------------------------------------------------------ api */\nasync function api(path, method = "GET", body) {\n  const res = await fetch(path, {method, headers:{"X-Token":TOKEN, "Content-Type":"application/json"},\n                                 body: body ? JSON.stringify(body) : undefined});\n  const data = await res.json().catch(() => ({}));\n  if (!res.ok) throw new Error(data.error || res.statusText);\n  return data;\n}\nasync function load(force) {\n  const st = await api("/api/state");\n  const changed = force || st.signature !== S.sig;\n  S.rows = st.assignments; S.settings = st.settings; S.meta = st.meta; S.sig = st.signature; S.sync = st.sync || {}; renderSync();\n  $("#ver").textContent = "Version " + st.version;\n  for (const e of st.events || []) {\n    if (e.t > S.lastEvent) {\n      S.lastEvent = e.t;\n      toast(e.title, e.message, e.stage === "overdue" || e.stage === "1h" ? "overdue" : e.stage === "1d" ? "today" : "soon", "⏰", () => openAssignment(e.id));\n    }\n  }\n  applyTheme();\n  if (changed) render(!S.first);\n  if (S.first) { S.first = false; setTimeout(() => { $("#loader").style.opacity = 0; setTimeout(() => $("#loader").remove(), 600); }, 250); startupSummary(); }\n}\n\nfunction renderSync() {\n  const p = $("#sync-pill"); if (!p) return; const st = (S.sync || {}).state || "off";\n  p.className = "sync-pill " + st;\n  p.querySelector("span").textContent = st === "ok" ? "Synced with iPhone" : st === "syncing" ? "Syncing…" : st === "error" ? "Sync problem" : "iPhone sync off";\n  p.title = (S.sync && S.sync.error) || (S.sync && S.sync.last ? "Last sync " + S.sync.last : "Set up in Settings");\n}\n/* ------------------------------------------------------------------ dates */\nfunction pd(s) {\n  if (!s) return null;\n  const m = String(s).match(/(\\d{4})-(\\d{2})-(\\d{2})(?:[ T](\\d{2}):(\\d{2}))?/);\n  if (!m) return null;\n  return new Date(+m[1], +m[2] - 1, +m[3], m[4] ? +m[4] : 23, m[5] ? +m[5] : 59);\n}\nconst pad2 = n => String(n).padStart(2, "0");\nconst toInput = d => d ? `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}` : "";\nconst toDb = v => v ? v.replace("T", " ").slice(0, 16) : "";\nconst fmtDate = d => d ? d.toLocaleDateString(undefined, {weekday:"short", day:"2-digit", month:"short", year:"numeric"}) + ", " + d.toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit"}) : "—";\nconst fmtShort = d => d ? d.toLocaleDateString(undefined, {day:"2-digit", month:"short"}) : "—";\nfunction left(d, now = new Date()) {\n  if (!d) return "—";\n  let s = Math.round((d - now) / 1000); const over = s < 0; s = Math.abs(s);\n  const dd = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);\n  const t = dd ? `${dd}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;\n  return over ? `Overdue ${t}` : `${t} left`;\n}\nconst isDone = r => (S.meta.done || []).includes(r.status);\nfunction urg(r, now = new Date()) {\n  if (isDone(r)) return "done";\n  const d = pd(r.deadline); if (!d) return "normal";\n  if (d < now) return "overdue";\n  if (d.toDateString() === now.toDateString()) return "today";\n  if (d - now <= (+S.settings.soon_days || 3) * 864e5) return "soon";\n  return "normal";\n}\nfunction progress(r, now = new Date()) {\n  const end = pd(r.deadline); const start = pd(r.assigned_date) || pd(r.created_at);\n  if (!end || !start) return 0;\n  const s = new Date(start); if (r.assigned_date && r.assigned_date.length <= 10) s.setHours(0, 0, 0, 0);\n  if (isDone(r)) return 1;\n  return Math.max(0, Math.min(1, (now - s) / Math.max(1, end - s)));\n}\nconst clientOf = r => (r.client || "").trim() || NOCLIENT;\nconst initials = n => n === NOCLIENT ? "?" : n.split(/\\s+/).slice(0, 2).map(p => p[0]).join("").toUpperCase();\nfunction avGrad(n) { let h = 0; for (const c of n) h = (h * 31 + c.charCodeAt(0)) >>> 0; const g = AV_GRADS[h % AV_GRADS.length]; return `linear-gradient(135deg,${g[0]},${g[1]})`; }\n\nfunction clients() {\n  const now = new Date(), map = new Map();\n  for (const r of S.rows) {\n    const n = clientOf(r);\n    if (!map.has(n)) map.set(n, {name:n, rows:[], active:0, overdue:0, soon:0, next:null, contact:"", unpaid:0});\n    const c = map.get(n); c.rows.push(r);\n    if (r.client_contact && !c.contact) c.contact = r.client_contact;\n    if (r.payment_status !== "Paid") c.unpaid++;\n    const u = urg(r, now); if (u === "done") continue;\n    c.active++; if (u === "overdue") c.overdue++; else if (u === "today" || u === "soon") c.soon++;\n    const d = pd(r.deadline); if (d && d >= now && (!c.next || d < pd(c.next.deadline))) c.next = r;\n  }\n  return [...map.values()].sort((a, b) => (b.overdue - a.overdue) || ((a.next ? pd(a.next.deadline) : 9e15) - (b.next ? pd(b.next.deadline) : 9e15)) || a.name.localeCompare(b.name));\n}\n\n/* ------------------------------------------------------------------ theme */\nfunction applyTheme() {\n  document.body.dataset.mode = S.settings.mode || "dark";\n  document.body.dataset.theme = S.settings.theme || "aurora";\n}\nasync function saveSettings(patch) {\n  Object.assign(S.settings, patch); applyTheme();\n  S.settings = await api("/api/settings", "PUT", patch);\n}\n\n/* ------------------------------------------------------------------ render */\nfunction render(keepScroll) {\n  const main = $("#main"); const y = main.scrollTop;\n  renderNav(); renderClients();\n  const v = $("#view");\n  v.className = keepScroll ? "" : "view-enter";\n  if (S.view === "client") v.innerHTML = viewClient();\n  else if (S.view === "board") v.innerHTML = viewBoard();\n  else if (S.view === "calendar") v.innerHTML = viewCalendar();\n  else if (S.view === "all") v.innerHTML = viewAll();\n  else v.innerHTML = viewDashboard();\n  $$("#view > *").forEach((el, i) => el.style.setProperty("--i", i));\n  bindView();\n  if (keepScroll) main.scrollTop = y; else main.scrollTop = 0;\n  if (S.selected) renderDrawer(true);\n  requestAnimationFrame(() => { animateNumbers(); animateRings(); moveInk(); });\n  tick(true);\n}\nfunction go(view, client = null) {\n  S.view = view; S.client = client; if (view !== "client") S.filter = "Active";\n  render(false);\n}\n\nfunction renderNav() {\n  const counts = {overdue: S.rows.filter(r => urg(r) === "overdue").length};\n  const items = [["dashboard","◈","Dashboard","1"],["board","▦","Board","2"],["calendar","◷","Calendar","3"],["all","☰","All assignments","4"]];\n  $("#nav").innerHTML = items.map(([k, ic, label, key]) =>\n    `<button class="nav-item ${S.view === k ? "active" : ""}" data-go="${k}"><span class="ico">${ic}</span>${label}\n     ${k === "dashboard" && counts.overdue ? `<span class="badge overdue" style="margin-left:auto">${counts.overdue}</span>` : `<span class="kbd">${key}</span>`}</button>`).join("");\n  $$("#nav [data-go]").forEach(b => b.onclick = () => go(b.dataset.go));\n}\nfunction renderClients() {\n  const q = $("#client-search").value.trim().toLowerCase();\n  const list = clients(); $("#client-count").textContent = list.length || "";\n  const html = list.filter(c => !q || c.name.toLowerCase().includes(q)).map(c => {\n    const n = c.rows.length;\n    const sub = c.overdue ? `${n} module${n !== 1 ? "s" : ""} · ${c.overdue} overdue` : c.next ? `${n} module${n !== 1 ? "s" : ""} · next ${fmtShort(pd(c.next.deadline))}` : `${n} module${n !== 1 ? "s" : ""} · all done ✓`;\n    const badge = c.overdue ? `<span class="badge overdue">${c.overdue}</span>` : c.soon ? `<span class="badge soon">${c.soon}</span>` : "";\n    return `<button class="client-item ${S.view === "client" && S.client === c.name ? "active" : ""}" data-client="${esc(c.name)}">\n      <span class="avatar" style="background:${avGrad(c.name)}">${esc(initials(c.name))}</span>\n      <span class="meta"><div class="name">${esc(c.name)}</div><div class="sub">${esc(sub)}</div></span>${badge}</button>`;\n  }).join("");\n  $("#clients").innerHTML = html || `<div style="padding:14px;color:var(--muted);font-size:13px">${q ? "No match" : "No clients yet — add your first assignment."}</div>`;\n  $$("#clients [data-client]").forEach(b => b.onclick = () => go("client", b.dataset.client));\n}\n\n/* dashboard ---------------------------------------------------------------- */\nfunction greet() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"; }\nfunction viewDashboard() {\n  const now = new Date(); const rows = S.rows;\n  const active = rows.filter(r => !isDone(r));\n  const overdue = active.filter(r => urg(r, now) === "overdue");\n  const next = active.find(r => pd(r.deadline) >= now);\n  const c = {overdue:overdue.length, today:active.filter(r => urg(r, now) === "today").length,\n             soon:active.filter(r => ["today","soon"].includes(urg(r, now))).length, active:active.length, done:rows.length - active.length};\n  const owner = (S.settings.owner || "").trim();\n  let out = `<div class="page-head"><div><h1 class="grad-text">${greet()}${owner ? ", " + esc(owner) : ""} ✦</h1>\n    <p>${now.toLocaleDateString(undefined, {weekday:"long", day:"numeric", month:"long", year:"numeric"})} · ${c.active} active assignment${c.active !== 1 ? "s" : ""} across ${clients().filter(x => x.active).length} client${clients().filter(x => x.active).length !== 1 ? "s" : ""}</p></div>\n    <button class="btn primary" data-new>＋ New assignment</button></div>`;\n  if (overdue.length) out += `<div class="alert" data-open="${overdue[0].id}"><span class="dot"></span><div><b>${overdue.length} assignment${overdue.length > 1 ? "s are" : " is"} overdue</b><div style="font-size:13px;color:var(--muted)">${esc(clientOf(overdue[0]))} — ${esc(overdue[0].module_code || overdue[0].title)} · ${left(pd(overdue[0].deadline))}</div></div><span style="margin-left:auto">Open →</span></div>`;\n  if (next) {\n    const d = pd(next.deadline), u = urg(next, now), p = progress(next, now);\n    out += `<div class="card hero clickable" data-open="${next.id}" style="--u:${U[u]}"><div class="glow"></div>\n      <div style="position:relative"><div class="eyebrow">⏱ Current deadline</div>\n        <h2>${esc(next.title)}</h2>\n        <div class="who"><span>👤 ${esc(clientOf(next))}</span>${next.module_code ? `<span>📘 ${esc(next.module_code)}</span>` : ""}<span>📅 ${fmtDate(d)}</span></div>\n        <div class="hero-foot"><div class="bar"><i style="width:0" data-w="${(p * 100).toFixed(1)}%"></i></div><span style="font-size:13px;color:var(--muted);white-space:nowrap">${Math.round(p * 100)}% of time used</span></div>\n      </div>\n      <div class="countdown" data-hero="${esc(next.deadline)}">${["Days","Hours","Mins","Secs"].map(l => `<div class="cd"><b data-cd="${l}">00</b><span>${l}</span></div>`).join("")}</div></div>`;\n  } else {\n    out += `<div class="card hero"><div class="glow"></div><div><div class="eyebrow">All clear</div><h2>No upcoming deadlines 🎉</h2><div class="who">Add an assignment to start tracking.</div></div><div></div></div>`;\n  }\n  const stat = (k, label, ic, color, val) => `<div class="card stat hover" data-stat="${k}" style="--c:${color}"><div class="ic" style="background:color-mix(in srgb,${color} 22%,transparent);color:${color}">${ic}</div><b data-num="${val}">0</b><span>${label}</span></div>`;\n  out += `<div class="stats">${stat("overdue","Overdue","⚠","var(--overdue)",c.overdue)}${stat("today","Due today","☀","var(--today)",c.today)}${stat("soon",`Due in ${S.settings.soon_days || 3} days`,"⏳","var(--soon)",c.soon)}${stat("active","Active","⚡","var(--accent)",c.active)}${stat("done","Completed","✓","var(--done)",c.done)}</div>`;\n\n  // timeline + workload/clients\n  const up = active.slice(0, 7);\n  const tl = up.length ? up.map(r => { const d = pd(r.deadline), u = urg(r, now);\n      return `<div class="tl-item" data-open="${r.id}" style="--u:${U[u]}"><div class="main"><div class="t">${r.module_code ? `<span style="color:var(--accent2);font-weight:800">${esc(r.module_code)}</span> · ` : ""}${esc(r.title)}</div>\n      <div class="s">👤 ${esc(clientOf(r))} · ${fmtDate(d)}</div></div><div class="when" data-left="${esc(r.deadline)}">${left(d, now)}</div></div>`; }).join("")\n    : `<div class="empty" style="padding:30px"><div class="big">🌤</div><h3>Nothing due</h3>Enjoy the calm.</div>`;\n  // workload next 14 days\n  const days = [...Array(14)].map((_, i) => { const d = new Date(now); d.setHours(0,0,0,0); d.setDate(d.getDate() + i); return d; });\n  const per = days.map(d => active.filter(r => { const x = pd(r.deadline); return x && x.toDateString() === d.toDateString(); }).length);\n  const mx = Math.max(1, ...per);\n  const chart = days.map((d, i) => `<div class="col ${i === 0 ? "today" : ""}" title="${per[i]} due ${fmtShort(d)}"><div class="b ${per[i] ? "" : "zero"}" style="--i:${i};height:${per[i] ? 14 + per[i] / mx * 76 : 4}%"></div><div class="lbl">${i === 0 ? "Today" : d.toLocaleDateString(undefined, {weekday:"narrow"}) + d.getDate()}</div></div>`).join("");\n\n  const cl = clients();\n  const cards = cl.map(cc => clientCard(cc, now)).join("") || `<div class="empty"><div class="big">👥</div><h3>No clients yet</h3></div>`;\n  out += `<div class="grid2"><div><div class="section-title"><h3>Upcoming deadlines</h3><span>all clients</span></div><div class="card pad"><div class="timeline">${tl}</div></div></div>\n    <div><div class="section-title"><h3>Workload · next 14 days</h3><span>${per.reduce((a, b) => a + b, 0)} due</span></div><div class="card pad"><div class="chart">${chart}</div></div>\n    <div class="section-title" style="margin-top:18px"><h3>Clients</h3><span>${cl.length} total</span></div><div class="client-grid">${cards}</div></div></div>`;\n  return out;\n}\nfunction clientCard(cc, now) {\n  const n = cc.rows.length;\n  const next = cc.next ? `<div class="next">Next deadline<b>${esc(cc.next.module_code || cc.next.title)} · ${fmtShort(pd(cc.next.deadline))}</b><span style="color:${U[urg(cc.next, now)]};font-weight:700" data-left="${esc(cc.next.deadline)}">${left(pd(cc.next.deadline), now)}</span></div>`\n    : `<div class="next"><b style="color:var(--done)">✓ All done</b></div>`;\n  return `<div class="card cc hover clickable" data-client="${esc(cc.name)}"><div class="top"><span class="avatar" style="background:${avGrad(cc.name)}">${esc(initials(cc.name))}</span>\n    <div style="min-width:0"><b>${esc(cc.name)}</b><small>${n} module${n !== 1 ? "s" : ""} · ${cc.active} active${cc.overdue ? ` · <span style="color:var(--overdue)">${cc.overdue} overdue</span>` : ""}</small></div></div>${next}</div>`;\n}\n\n/* client page -------------------------------------------------------------- */\nfunction viewClient() {\n  const now = new Date(); const cc = clients().find(c => c.name === S.client);\n  if (!cc) { S.view = "dashboard"; return viewDashboard(); }\n  let rows = cc.rows;\n  if (S.filter === "Active") rows = rows.filter(r => !isDone(r));\n  if (S.filter === "Completed") rows = rows.filter(isDone);\n  const n = cc.rows.length;\n  const nx = cc.next;\n  const mini = (label, big, small, color) => `<div class="card mini"><span>${label}</span><b style="color:${color}">${big}</b><small>${small || "&nbsp;"}</small></div>`;\n  return `<div class="back" data-go="dashboard">← Back to dashboard</div>\n  <div class="card client-head"><div class="glow" style="position:absolute;width:300px;height:300px;right:-60px;top:-160px;border-radius:50%;background:radial-gradient(circle,color-mix(in srgb,var(--accent2) 50%,transparent),transparent 70%);filter:blur(20px)"></div>\n    <span class="avatar" style="background:${avGrad(cc.name)}">${esc(initials(cc.name))}</span>\n    <div style="flex:1;min-width:0;position:relative"><h1>${esc(cc.name)}</h1><p>${n} module${n !== 1 ? "s" : ""} · ${cc.active} active${cc.overdue ? ` · <span style="color:var(--overdue);font-weight:700">${cc.overdue} overdue</span>` : ""}${cc.contact ? ` · ✉ ${esc(cc.contact)}` : ""}</p></div>\n    <button class="btn primary" data-new-client="${esc(cc.name)}" style="position:relative">＋ Add module</button></div>\n  <div class="mini-stats">\n    ${mini("Next deadline", nx ? esc(nx.module_code || nx.title) : "—", nx ? `<span data-left="${esc(nx.deadline)}">${left(pd(nx.deadline), now)}</span>` : "", "var(--accent2)")}\n    ${mini("Active modules", cc.active, "in progress", "var(--accent)")}\n    ${mini("Overdue", cc.overdue, cc.overdue ? "needs attention" : "all on track", cc.overdue ? "var(--overdue)" : "var(--done)")}\n    ${mini("Payments", cc.unpaid ? `${cc.unpaid} unpaid` : "All paid", "", cc.unpaid ? "var(--overdue)" : "var(--done)")}\n  </div>\n  <div class="row" style="justify-content:space-between;margin:22px 4px 0"><h3 style="margin:0;font-size:18px">Modules</h3>\n    <div class="tabs" id="tabs"><div class="tab-ink"></div>${["Active","Completed","All"].map(t => `<button class="tab ${S.filter === t ? "active" : ""}" data-filter="${t}">${t}</button>`).join("")}</div></div>\n  ${rows.length ? `<div class="modules">${rows.map((r, i) => moduleCard(r, now, i)).join("")}</div>` : `<div class="empty"><div class="big">📭</div><h3>Nothing here</h3>No ${S.filter.toLowerCase()} modules for ${esc(cc.name)}.</div>`}`;\n}\nfunction ring(p, u, label) {\n  const C = 2 * Math.PI * 26;\n  return `<div class="ring" style="--u:${U[u]}"><svg width="62" height="62"><circle class="bgc" cx="31" cy="31" r="26"/><circle class="fg" cx="31" cy="31" r="26" stroke-dasharray="${C}" stroke-dashoffset="${C}" data-off="${C * (1 - p)}"/></svg><span>${label}</span></div>`;\n}\nfunction chipU(u) { return `<span class="chip c-${u}"><span class="d"></span>${ULABEL[u]}</span>`; }\nfunction chipsFor(r) {\n  return `<div class="chips"><span class="chip">${esc(r.status)}</span>${r.priority ? `<span class="chip p-${esc(r.priority)}">${esc(r.priority)}</span>` : ""}${r.payment_status ? `<span class="chip pay-${esc((r.payment_status || "").split(" ")[0])}">💳 ${esc(r.payment_status)}${r.amount ? " · " + esc(r.amount) : ""}</span>` : ""}</div>`;\n}\nfunction moduleCard(r, now, i) {\n  const u = urg(r, now), d = pd(r.deadline), p = progress(r, now);\n  return `<div class="card mod hover ${S.selected === r.id ? "sel" : ""}" data-open="${r.id}" style="--u:${U[u]};animation:fadeUp .6s var(--ease) both;animation-delay:${120 + i * 70}ms">\n    <div class="head"><div style="min-width:0"><div class="code">${esc(r.module_code || r.code)}</div><div class="title">${esc(r.title)}</div>${r.subject ? `<div class="subj">${esc(r.subject)}</div>` : ""}</div>${chipU(u)}</div>\n    <div class="mid">${ring(p, u, u === "done" ? "✓" : Math.round(p * 100) + "%")}<div class="dl"><b data-left="${esc(r.deadline)}" data-done="${u === "done" ? 1 : 0}">${u === "done" ? "Completed" : left(d, now)}</b><small>📅 ${fmtDate(d)}</small></div></div>\n    ${chipsFor(r)}\n    <div class="foot"><span>${esc(r.code)}</span><span>${r.word_count ? "📝 " + esc(r.word_count) : ""}</span></div></div>`;\n}\n\n/* board -------------------------------------------------------------------- */\nfunction viewBoard() {\n  const now = new Date(); const cols = ["Not Started","In Progress","Review","Submitted","Completed"];\n  return `<div class="page-head"><div><h1 class="grad-text">Board</h1><p>Drag cards between columns to update their status.</p></div><button class="btn primary" data-new>＋ New assignment</button></div>\n  <div class="board">${cols.map(st => { const rows = S.rows.filter(r => r.status === st);\n    return `<div class="card col-b" data-col="${st}"><h4>${st}<span>${rows.length}</span></h4>${rows.map(r => { const u = urg(r, now);\n      return `<div class="kc" draggable="true" data-id="${r.id}" data-open="${r.id}" style="--u:${U[u]}"><b>${esc(r.module_code || r.code)}</b><div class="t">${esc(r.title)}</div>\n        <div class="s"><span>👤 ${esc(clientOf(r))}</span><span style="color:${U[u]};font-weight:700" data-left="${esc(r.deadline)}" data-done="${u === "done" ? 1 : 0}">${u === "done" ? "✓" : left(pd(r.deadline), now)}</span></div></div>`; }).join("")}</div>`; }).join("")}</div>`;\n}\n\n/* calendar ----------------------------------------------------------------- */\nfunction viewCalendar() {\n  const now = new Date(); const m = new Date(S.cal.getFullYear(), S.cal.getMonth(), 1);\n  const start = new Date(m); start.setDate(1 - ((m.getDay() + 6) % 7));\n  const cells = [...Array(42)].map((_, i) => { const d = new Date(start); d.setDate(start.getDate() + i); return d; });\n  return `<div class="page-head"><div><h1 class="grad-text">${m.toLocaleDateString(undefined, {month:"long", year:"numeric"})}</h1><p>Every deadline at a glance.</p></div>\n    <div class="row"><button class="btn sm" data-cal="-1">‹</button><button class="btn sm" data-cal="0">Today</button><button class="btn sm" data-cal="1">›</button><button class="btn primary sm" data-new>＋ New</button></div></div>\n  <div class="card pad"><div class="cal">${["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(d => `<div class="dow">${d}</div>`).join("")}\n  ${cells.map(d => { const items = S.rows.filter(r => { const x = pd(r.deadline); return x && x.toDateString() === d.toDateString(); });\n    return `<div class="day ${d.getMonth() !== m.getMonth() ? "out" : ""} ${d.toDateString() === now.toDateString() ? "today" : ""}"><div class="n"><b>${d.getDate()}</b>${items.length > 2 ? `<span>${items.length}</span>` : ""}</div>\n      ${items.map(r => `<span class="pill" data-open="${r.id}" style="--u:${U[urg(r, now)]}" title="${esc(clientOf(r) + " — " + r.title)}">${esc(pd(r.deadline).toLocaleTimeString(undefined, {hour:"2-digit", minute:"2-digit"}))} ${esc(r.module_code || r.title)}</span>`).join("")}</div>`; }).join("")}</div></div>`;\n}\n\n/* all list ----------------------------------------------------------------- */\nfunction viewAll() {\n  const now = new Date(); const q = S.listQ.toLowerCase();\n  let rows = S.rows.filter(r => S.listStatus === "all" ? true : S.listStatus === "active" ? !isDone(r) : S.listStatus === "overdue" ? urg(r, now) === "overdue" : r.status === S.listStatus);\n  if (q) rows = rows.filter(r => [r.code, r.title, r.client, r.subject, r.module_code, r.description, r.notes].join(" ").toLowerCase().includes(q));\n  return `<div class="page-head"><div><h1 class="grad-text">All assignments</h1><p>${rows.length} shown</p></div><button class="btn primary" data-new>＋ New assignment</button></div>\n  <div class="list-tools"><input class="input" id="list-q" placeholder="Search title, client, module, notes…" style="flex:1" value="${esc(S.listQ)}">\n    <select class="input" id="list-status">${[["active","Active"],["all","All"],["overdue","Overdue"],...(S.meta.statuses || []).map(s => [s, s])].map(([v, l]) => `<option value="${esc(v)}" ${S.listStatus === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>\n    <button class="btn" id="btn-export">⤓ Export CSV</button></div>\n  ${rows.length ? `<table class="table"><thead><tr><th>Module</th><th>Client</th><th>Deadline</th><th>Time left</th><th>Status</th><th>Payment</th></tr></thead><tbody>\n  ${rows.map(r => { const u = urg(r, now); return `<tr data-open="${r.id}" style="--u:${U[u]}"><td><b style="color:var(--accent2)">${esc(r.module_code || r.code)}</b><div style="font-size:13px;color:var(--muted);max-width:380px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.title)}</div></td>\n    <td>${esc(clientOf(r))}</td><td>${fmtDate(pd(r.deadline))}</td><td style="color:${U[u]};font-weight:700" data-left="${esc(r.deadline)}" data-done="${u === "done" ? 1 : 0}">${u === "done" ? "✓ Done" : left(pd(r.deadline), now)}</td>\n    <td><span class="chip">${esc(r.status)}</span></td><td><span class="chip pay-${esc((r.payment_status || "").split(" ")[0])}">${esc(r.payment_status || "")}</span></td></tr>`; }).join("")}</tbody></table>`\n  : `<div class="empty"><div class="big">🔍</div><h3>No matches</h3>Try a different search or filter.</div>`}`;\n}\n\n/* bindings ----------------------------------------------------------------- */\nfunction bindView() {\n  const v = $("#view");\n  $$("[data-open]", v).forEach(el => el.addEventListener("click", e => { e.stopPropagation(); openAssignment(+el.dataset.open); }));\n  $$("[data-client]", v).forEach(el => el.onclick = () => go("client", el.dataset.client));\n  $$("[data-go]", v).forEach(el => el.onclick = () => go(el.dataset.go));\n  $$("[data-new]", v).forEach(el => el.onclick = () => openForm());\n  $$("[data-new-client]", v).forEach(el => el.onclick = () => openForm(null, el.dataset.newClient));\n  $$("[data-filter]", v).forEach(el => el.onclick = () => { S.filter = el.dataset.filter; render(true); });\n  $$("[data-stat]", v).forEach(el => el.onclick = () => { const k = el.dataset.stat; S.listStatus = k === "done" ? "Completed" : k === "overdue" ? "overdue" : "active"; S.listQ = ""; go("all"); });\n  $$("[data-cal]", v).forEach(el => el.onclick = () => { const k = +el.dataset.cal; S.cal = k ? new Date(S.cal.getFullYear(), S.cal.getMonth() + k, 1) : new Date(); render(false); });\n  const lq = $("#list-q"); if (lq) { lq.oninput = () => { S.listQ = lq.value; const pos = lq.selectionStart; render(true); const n = $("#list-q"); n.focus(); n.setSelectionRange(pos, pos); }; }\n  const ls = $("#list-status"); if (ls) ls.onchange = () => { S.listStatus = ls.value; render(true); };\n  const ex = $("#btn-export"); if (ex) ex.onclick = exportCsv;\n  // board drag & drop\n  $$(".kc", v).forEach(k => {\n    k.addEventListener("dragstart", e => { k.classList.add("dragging"); e.dataTransfer.setData("text/plain", k.dataset.id); });\n    k.addEventListener("dragend", () => k.classList.remove("dragging"));\n  });\n  $$(".col-b", v).forEach(col => {\n    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("over"); });\n    col.addEventListener("dragleave", () => col.classList.remove("over"));\n    col.addEventListener("drop", async e => { e.preventDefault(); col.classList.remove("over");\n      const id = +e.dataTransfer.getData("text/plain"); const r = S.rows.find(x => x.id === id);\n      if (r && r.status !== col.dataset.col) await setField(id, "status", col.dataset.col); });\n  });\n}\n\n/* animations --------------------------------------------------------------- */\nfunction animateNumbers() {\n  $$("[data-num]").forEach(el => { const to = +el.dataset.num, t0 = performance.now(), dur = 900;\n    const step = t => { const k = Math.min(1, (t - t0) / dur); el.textContent = Math.round(to * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };\n    requestAnimationFrame(step); });\n  $$("[data-w]").forEach(el => setTimeout(() => el.style.width = el.dataset.w, 80));\n}\nfunction animateRings() { $$("circle[data-off]").forEach(c => setTimeout(() => c.style.strokeDashoffset = c.dataset.off, 120)); }\nfunction moveInk() {\n  const tabs = $("#tabs"); if (!tabs) return; const a = $(".tab.active", tabs), ink = $(".tab-ink", tabs);\n  if (a && ink) { ink.style.left = a.offsetLeft + "px"; ink.style.width = a.offsetWidth + "px"; }\n}\nfunction tick(force) {\n  const now = new Date();\n  $$("[data-left]").forEach(el => { if (el.dataset.done === "1") return; el.textContent = left(pd(el.dataset.left), now); });\n  const hero = $("[data-hero]");\n  if (hero) {\n    let s = Math.max(0, Math.round((pd(hero.dataset.hero) - now) / 1000));\n    const vals = {Days:Math.floor(s / 86400), Hours:Math.floor(s % 86400 / 3600), Mins:Math.floor(s % 3600 / 60), Secs:s % 60};\n    for (const [k, v] of Object.entries(vals)) { const el = $(`[data-cd="${k}"]`, hero); const t = pad2(v);\n      if (el && el.textContent !== t) { el.textContent = t; if (!force) { el.classList.remove("tick"); void el.offsetWidth; el.classList.add("tick"); } } }\n  }\n  const dr = $("#dr-big"); if (dr && dr.dataset.left && dr.dataset.done !== "1") dr.textContent = left(pd(dr.dataset.left), now);\n}\n\n/* drawer ------------------------------------------------------------------- */\nfunction openAssignment(id) {\n  const r = S.rows.find(x => x.id === id); if (!r) return;\n  S.selected = id; renderDrawer(); $$(".mod").forEach(m => m.classList.toggle("sel", +m.dataset.open === id));\n}\nfunction closeDrawer() { S.selected = null; $("#drawer").classList.remove("on"); $("#scrim").classList.remove("on"); $$(".mod.sel").forEach(m => m.classList.remove("sel")); }\nfunction renderDrawer(silent) {\n  const r = S.rows.find(x => x.id === S.selected); const dr = $("#drawer");\n  if (!r) return closeDrawer();\n  const u = urg(r), d = pd(r.deadline);\n  const sel = (field, opts) => `<select class="input" data-field="${field}">${opts.map(o => `<option ${r[field] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;\n  const kv = [["Client", clientOf(r)], ["Contact", r.client_contact], ["Subject", r.subject], ["Module", r.module_code], ["Word count", r.word_count], ["Amount", r.amount], ["Assigned", r.assigned_date], ["ID", r.code], ["Updated", r.updated_at]].filter(x => x[1]);\n  dr.style.setProperty("--u", U[u]);\n  dr.innerHTML = `<div class="dr-head"><button class="x" id="dr-x">✕</button><div class="code">${esc(r.module_code || r.code)}</div><div class="t">${esc(r.title)}</div>\n      <div class="big" id="dr-big" data-left="${esc(r.deadline)}" data-done="${u === "done" ? 1 : 0}">${u === "done" ? "✓ Completed" : left(d)}</div><div class="date">📅 ${fmtDate(d)} · ${ULABEL[u]}</div></div>\n    <div class="dr-body">\n      <div class="dr-actions"><button class="btn sm" id="dr-edit">✎ Edit</button>\n        ${isDone(r) ? `<button class="btn sm" id="dr-reopen">↺ Reopen</button>` : `<button class="btn sm primary" id="dr-done">✓ Complete</button>`}\n        <button class="btn sm" id="dr-del" style="color:#fca5a5">🗑 Delete</button></div>\n      <div class="field-row"><label>Status</label>${sel("status", S.meta.statuses || [])}</div>\n      <div class="field-row"><label>Payment</label>${sel("payment_status", S.meta.payments || [])}</div>\n      <div class="field-row"><label>Priority</label>${sel("priority", S.meta.priorities || [])}</div>\n      <dl class="kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>\n      ${r.description ? `<div class="block"><h5>Requirements</h5><p>${esc(r.description)}</p></div>` : ""}\n      ${r.notes ? `<div class="block"><h5>Notes</h5><p>${esc(r.notes)}</p></div>` : ""}\n      <div class="row" style="margin-top:18px"><button class="btn sm" id="dr-copy">⧉ Copy details</button><button class="btn sm" id="dr-client">👤 Open ${esc(clientOf(r))}</button></div>\n    </div>`;\n  if (!silent || !dr.classList.contains("on")) { dr.classList.add("on"); $("#scrim").classList.add("on"); }\n  $("#dr-x").onclick = closeDrawer;\n  $("#dr-edit").onclick = () => openForm(r);\n  $("#dr-del").onclick = () => confirmDelete(r);\n  const done = $("#dr-done"); if (done) done.onclick = () => setField(r.id, "status", "Completed");\n  const re = $("#dr-reopen"); if (re) re.onclick = () => setField(r.id, "status", "In Progress");\n  $("#dr-client").onclick = () => { closeDrawer(); go("client", clientOf(r)); };\n  $("#dr-copy").onclick = () => { const txt = `${r.code} — ${r.module_code || ""} ${r.title}\\nClient: ${clientOf(r)}\\nDeadline: ${fmtDate(d)}\\nStatus: ${r.status} · ${r.priority} · ${r.payment_status}\\n\\n${r.description || ""}\\n\\n${r.notes || ""}`;\n    (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(() => toast("Copied", "Assignment details copied to clipboard", "done", "⧉")).catch(() => toast("Copy failed", "Clipboard not available", "overdue", "!")); };\n  $$("[data-field]", dr).forEach(s => s.onchange = () => setField(r.id, s.dataset.field, s.value));\n}\nasync function setField(id, field, value) {\n  try {\n    const before = S.rows.find(x => x.id === id);\n    await api(`/api/assignments/${id}`, "PUT", {[field]: value});\n    await load(true);\n    if (field === "status" && (S.meta.done || []).includes(value) && !(S.meta.done || []).includes(before?.status)) { confetti(); toast("Nice work! 🎉", `${before.module_code || before.title} marked ${value.toLowerCase()}`, "done", "✓"); }\n    else toast("Updated", `${field.replace("_", " ")} → ${value}`, "normal", "✓");\n  } catch (e) { toast("Couldn\'t update", e.message, "overdue", "!"); }\n}\n\n/* modal: form -------------------------------------------------------------- */\nfunction modal(html, onMount) {\n  const wrap = document.createElement("div"); wrap.className = "modal-wrap"; wrap.innerHTML = html;\n  $("#modal-root").appendChild(wrap);\n  const close = () => { wrap.style.animation = "fade .25s reverse forwards"; setTimeout(() => wrap.remove(), 220); };\n  wrap.addEventListener("mousedown", e => { if (e.target === wrap) close(); });\n  wrap._close = close; onMount && onMount(wrap, close); return wrap;\n}\nfunction openForm(r = null, presetClient = null) {\n  const now = new Date(); const def = new Date(now); def.setDate(def.getDate() + 7); def.setHours(17, 0, 0, 0);\n  const v = k => esc(r ? (r[k] || "") : "");\n  const opts = (list, cur) => list.map(o => `<option ${cur === o ? "selected" : ""}>${esc(o)}</option>`).join("");\n  const clientVal = r ? (r.client || "") : (presetClient && presetClient !== NOCLIENT ? presetClient : (S.view === "client" && S.client !== NOCLIENT ? S.client || "" : ""));\n  modal(`<div class="modal"><h2>${r ? "Edit " + esc(r.module_code || r.code) : "New assignment"}</h2><p class="sub">${r ? esc(r.code) : "Fill in the details — only title and deadline are required."}</p>\n    <form class="form" id="f">\n      <div class="full"><label>Title *</label><input class="input" name="title" value="${v("title")}" placeholder="e.g. Individual Business Report" required></div>\n      <div><label>Client</label><input class="input" name="client" list="clientlist" value="${esc(clientVal)}" placeholder="Client name"><datalist id="clientlist">${clients().filter(c => c.name !== NOCLIENT).map(c => `<option value="${esc(c.name)}">`).join("")}</datalist></div>\n      <div><label>Client contact</label><input class="input" name="client_contact" value="${v("client_contact")}" placeholder="Email or phone"></div>\n      <div><label>Module code</label><input class="input" name="module_code" value="${v("module_code")}" placeholder="e.g. BM7701"></div>\n      <div><label>Subject / module name</label><input class="input" name="subject" value="${v("subject")}"></div>\n      <div><label>Deadline *</label><input class="input" type="datetime-local" name="deadline" value="${toInput(r ? pd(r.deadline) : def)}" required></div>\n      <div><label>Assigned date</label><input class="input" type="date" name="assigned_date" value="${r ? v("assigned_date") : toInput(now).slice(0, 10)}"></div>\n      <div><label>Word count / pages</label><input class="input" name="word_count" value="${v("word_count")}" placeholder="e.g. 3000 words"></div>\n      <div><label>Amount / fee</label><input class="input" name="amount" value="${v("amount")}" placeholder="e.g. £150"></div>\n      <div><label>Priority</label><select class="input" name="priority">${opts(S.meta.priorities || [], r ? r.priority : "Medium")}</select></div>\n      <div><label>Status</label><select class="input" name="status">${opts(S.meta.statuses || [], r ? r.status : "Not Started")}</select></div>\n      <div><label>Payment</label><select class="input" name="payment_status">${opts(S.meta.payments || [], r ? r.payment_status : "Unpaid")}</select></div>\n      <div></div>\n      <div class="full"><label>Requirements / brief</label><textarea class="input" name="description" rows="4">${v("description")}</textarea></div>\n      <div class="full"><label>Notes</label><textarea class="input" name="notes" rows="3">${v("notes")}</textarea></div>\n    </form><div class="err" id="ferr"></div>\n    <div class="modal-actions"><button class="btn ghost" data-x>Cancel</button><button class="btn primary" id="fsave">${r ? "Save changes" : "Create assignment"} <span class="kbd" style="color:#fff;border-color:rgba(255,255,255,.4)">⌘↵</span></button></div></div>`,\n  (wrap, close) => {\n    const f = $("#f", wrap); setTimeout(() => f.elements.title.focus(), 60);\n    $("[data-x]", wrap).onclick = close;\n    const submit = async () => {\n      const data = Object.fromEntries(new FormData(f).entries());\n      data.deadline = toDb(data.deadline);\n      try {\n        const res = r ? await api(`/api/assignments/${r.id}`, "PUT", data) : await api("/api/assignments", "POST", data);\n        close(); await load(true);\n        toast(r ? "Saved" : "Assignment created ✨", `${res.code} — ${res.title}`, "done", r ? "✓" : "✦");\n        if (!r) { go("client", clientOf(res)); setTimeout(() => openAssignment(res.id), 350); }\n      } catch (e) { $("#ferr", wrap).textContent = e.message; $(".modal", wrap).animate([{transform:"translateX(0)"},{transform:"translateX(-8px)"},{transform:"translateX(8px)"},{transform:"translateX(0)"}], {duration:300}); }\n    };\n    $("#fsave", wrap).onclick = submit;\n    f.onsubmit = e => { e.preventDefault(); submit(); };\n    wrap.addEventListener("keydown", e => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(); if (e.key === "Escape") close(); });\n  });\n}\nfunction confirmDelete(r) {\n  modal(`<div class="modal" style="width:min(460px,92vw)"><h2>Delete ${esc(r.module_code || r.code)}?</h2><p class="sub">“${esc(r.title)}” will be permanently removed. This can\'t be undone.</p>\n    <div class="modal-actions"><button class="btn ghost" data-x>Cancel</button><button class="btn danger" data-del>Delete</button></div></div>`,\n  (wrap, close) => { $("[data-x]", wrap).onclick = close; $("[data-del]", wrap).onclick = async () => {\n    await api(`/api/assignments/${r.id}`, "DELETE"); close(); closeDrawer(); await load(true); toast("Deleted", `${r.code} removed`, "overdue", "🗑"); }; });\n}\n\n/* settings ----------------------------------------------------------------- */\nfunction openSettings() {\n  const themes = [["aurora","#7c3aed","#06b6d4"],["sunset","#f97316","#ec4899"],["ocean","#0ea5e9","#6366f1"],["rose","#f43f5e","#a855f7"],["forest","#10b981","#0ea5e9"]];\n  modal(`<div class="modal" style="width:min(620px,94vw)"><h2>Settings</h2><p class="sub">Make it yours.</p>\n    <div class="form">\n      <div class="full"><label>Your name (for the greeting)</label><input class="input" id="s-owner" value="${esc(S.settings.owner || "")}" placeholder="e.g. Sunit"></div>\n      <div class="full"><label>Background theme</label><div class="swatches">${themes.map(([k, a, b]) => `<div class="sw ${S.settings.theme === k ? "on" : ""}" data-theme="${k}" title="${k}" style="background:linear-gradient(135deg,${a},${b})"></div>`).join("")}</div></div>\n      <div><label>Appearance</label><div class="tabs"><button class="tab ${S.settings.mode !== "light" ? "active" : ""}" data-mode="dark" style="${S.settings.mode !== "light" ? "background:linear-gradient(135deg,var(--accent),var(--accent2))" : ""}">🌙 Dark</button><button class="tab ${S.settings.mode === "light" ? "active" : ""}" data-mode="light" style="${S.settings.mode === "light" ? "background:linear-gradient(135deg,var(--accent),var(--accent2))" : ""}">☀ Light</button></div></div>\n      <div><label>Remind me before deadline (days)</label><input class="input" id="s-days" type="number" min="1" max="30" value="${esc(S.settings.soon_days || 3)}"></div>\n      <div class="full row" style="justify-content:space-between"><div><b>Desktop notifications</b><div style="color:var(--muted);font-size:13px">3 days, 24 hours and 1 hour before, and when overdue</div></div>\n        <div class="row"><button class="btn sm" id="s-test">Test</button><div class="switch ${S.settings.notifications !== "0" ? "on" : ""}" id="s-notif"></div></div></div>\n      <div class="full" style="border-top:1px solid var(--border);padding-top:16px"><b>📱 Sync with iPhone (via GitHub)</b>\n        <div style="color:var(--muted);font-size:13px;margin:4px 0 12px">Your assignments are stored in a private GitHub repo and synced with the iPhone app. ${S.sync && S.sync.state === "ok" ? `<span style="color:var(--done)">● Synced ${esc(S.sync.last || "")}</span>` : S.sync && S.sync.state === "error" ? `<span style="color:var(--overdue)">● ${esc(S.sync.error || "")}</span>` : ""}</div></div>\n      <div><label>Data repository (owner/name)</label><input class="input" id="s-repo" value="${esc(S.settings.gh_repo || "")}" placeholder="Sunit127/assignment-data"></div>\n      <div><label>GitHub token ${S.settings.gh_token_set ? "(saved ✓)" : ""}</label><input class="input" id="s-token" type="password" placeholder="${S.settings.gh_token_set ? "•••••••• (leave blank to keep)" : "github_pat_…"}" autocomplete="off"></div>\n      <div class="full row" style="justify-content:flex-end;gap:8px"><button class="btn sm" id="s-disconnect">Disconnect</button><button class="btn sm primary" id="s-sync">⟳ Save & sync now</button></div>\n      <div class="full row" style="justify-content:space-between"><div><b>Export</b><div style="color:var(--muted);font-size:13px">Save all assignments as a CSV in Downloads</div></div><button class="btn sm" id="s-export">⤓ Export CSV</button></div>\n    </div>\n    <div class="modal-actions"><button class="btn primary" data-x>Done</button></div></div>`,\n  (wrap, close) => {\n    $("[data-x]", wrap).onclick = async () => { await saveSettings({owner:$("#s-owner", wrap).value.trim(), soon_days:Math.max(1, +$("#s-days", wrap).value || 3)}); close(); render(true); };\n    $$("[data-theme]", wrap).forEach(s => s.onclick = async () => { $$(".sw", wrap).forEach(x => x.classList.remove("on")); s.classList.add("on"); await saveSettings({theme:s.dataset.theme}); });\n    $$("[data-mode]", wrap).forEach(b => b.onclick = async () => { $$("[data-mode]", wrap).forEach(x => { x.classList.remove("active"); x.style.background = ""; }); b.classList.add("active"); b.style.background = "linear-gradient(135deg,var(--accent),var(--accent2))"; await saveSettings({mode:b.dataset.mode}); });\n    $("#s-notif", wrap).onclick = async e => { const on = !e.currentTarget.classList.contains("on"); e.currentTarget.classList.toggle("on", on); await saveSettings({notifications:on ? "1" : "0"}); };\n    $("#s-test", wrap).onclick = async () => { await api("/api/test-notification", "POST"); toast("Test sent", "Check the top-right corner of your screen.", "normal", "🔔"); };\n    $("#s-export", wrap).onclick = exportCsv;\n    $("#s-sync", wrap).onclick = async () => { const patch = {gh_repo:$("#s-repo", wrap).value.trim()}; const t = $("#s-token", wrap).value.trim(); if (t) patch.gh_token = t;\n      await saveSettings(patch); await api("/api/sync", "POST"); toast("Syncing…", "Connecting to " + patch.gh_repo, "normal", "⟳"); setTimeout(() => load(true), 2500); setTimeout(() => { load(true); }, 6000); };\n    $("#s-disconnect", wrap).onclick = async () => { await saveSettings({gh_token:"", gh_repo:""}); toast("Disconnected", "iPhone sync is off on this Mac", "soon", "⏻"); close(); };\n  });\n}\nasync function exportCsv() { try { const r = await api("/api/export", "POST"); toast("Exported", r.path, "done", "⤓"); } catch (e) { toast("Export failed", e.message, "overdue", "!"); } }\n\n/* command palette ---------------------------------------------------------- */\nfunction openPalette() {\n  if ($(".palette")) return;\n  const actions = [["＋","New assignment",() => openForm(),"N"],["◈","Go to Dashboard",() => go("dashboard"),"1"],["▦","Go to Board",() => go("board"),"2"],["◷","Go to Calendar",() => go("calendar"),"3"],["☰","All assignments",() => go("all"),"4"],\n    ["◐","Toggle light / dark",toggleMode,"D"],["⚙","Settings",openSettings,""],["⤓","Export CSV",exportCsv,""]];\n  modal(`<div class="palette"><input id="pq" placeholder="Search clients, modules, actions…" autocomplete="off"><div class="pal-list" id="pl"></div></div>`, (wrap, close) => {\n    const q = $("#pq", wrap); let items = [], idx = 0;\n    const draw = () => {\n      const s = q.value.trim().toLowerCase(); const out = []; items = [];\n      const add = (group, list) => { if (!list.length) return; out.push(`<div class="pal-group">${group}</div>`); for (const it of list) { out.push(`<div class="pal-item" data-i="${items.length}">${it.html}</div>`); items.push(it); } };\n      add("Actions", actions.filter(a => !s || a[1].toLowerCase().includes(s)).slice(0, s ? 8 : 5).map(a => ({html:`<span>${a[0]}</span>${esc(a[1])}<span class="k">${a[3]}</span>`, run:a[2]})));\n      add("Clients", clients().filter(c => !s || c.name.toLowerCase().includes(s)).slice(0, 6).map(c => ({html:`<span class="avatar" style="width:26px;height:26px;border-radius:8px;font-size:10px;background:${avGrad(c.name)}">${esc(initials(c.name))}</span>${esc(c.name)}<span class="k">${c.rows.length} modules</span>`, run:() => go("client", c.name)})));\n      add("Assignments", S.rows.filter(r => !s || [r.code, r.title, r.client, r.module_code, r.subject].join(" ").toLowerCase().includes(s)).slice(0, 8).map(r => ({html:`<span style="color:${U[urg(r)]}">●</span><b style="color:var(--accent2)">${esc(r.module_code || r.code)}</b> ${esc(r.title)}<span class="k">${esc(clientOf(r))}</span>`, run:() => openAssignment(r.id)})));\n      $("#pl", wrap).innerHTML = out.join("") || `<div class="empty" style="padding:30px">No results</div>`;\n      idx = Math.min(idx, Math.max(0, items.length - 1)); hl();\n      $$(".pal-item", wrap).forEach(el => { el.onmouseenter = () => { idx = +el.dataset.i; hl(); }; el.onclick = () => run(); });\n    };\n    const hl = () => $$(".pal-item", wrap).forEach(el => el.classList.toggle("on", +el.dataset.i === idx));\n    const run = () => { const it = items[idx]; if (it) { close(); setTimeout(it.run, 120); } };\n    q.oninput = () => { idx = 0; draw(); };\n    q.onkeydown = e => { if (e.key === "ArrowDown") { idx = Math.min(items.length - 1, idx + 1); hl(); e.preventDefault(); $(".pal-item.on", wrap)?.scrollIntoView({block:"nearest"}); }\n      else if (e.key === "ArrowUp") { idx = Math.max(0, idx - 1); hl(); e.preventDefault(); $(".pal-item.on", wrap)?.scrollIntoView({block:"nearest"}); }\n      else if (e.key === "Enter") run(); else if (e.key === "Escape") close(); };\n    draw(); setTimeout(() => q.focus(), 50);\n  });\n}\nasync function toggleMode() { await saveSettings({mode:S.settings.mode === "light" ? "dark" : "light"}); }\n\n/* toasts & confetti -------------------------------------------------------- */\nfunction toast(title, msg, u = "normal", icon = "✦", onClick, ms = 5200) {\n  const t = document.createElement("div"); t.className = "toast glass"; t.style.setProperty("--u", U[u] || U.normal);\n  t.innerHTML = `<div class="ti">${icon}</div><div><b>${esc(title)}</b><span>${esc(msg)}</span></div><div class="life" style="animation-duration:${ms}ms"></div>`;\n  const kill = () => { t.classList.add("out"); setTimeout(() => t.remove(), 380); };\n  t.onclick = () => { onClick && onClick(); kill(); };\n  $("#toasts").appendChild(t); setTimeout(kill, ms);\n  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();\n}\nfunction confetti() {\n  const c = $("#confetti"), ctx = c.getContext("2d"); c.width = innerWidth * devicePixelRatio; c.height = innerHeight * devicePixelRatio; ctx.scale(devicePixelRatio, devicePixelRatio);\n  const cs = getComputedStyle(document.body); const colors = [cs.getPropertyValue("--accent"), cs.getPropertyValue("--accent2"), "#f59e0b", "#10b981", "#f43f5e", "#fff"];\n  const P = [...Array(160)].map(() => ({x:innerWidth / 2, y:innerHeight * .45, vx:(Math.random() - .5) * 18, vy:-Math.random() * 16 - 4, r:Math.random() * 6 + 3, c:colors[Math.random() * colors.length | 0], a:Math.random() * 6, va:(Math.random() - .5) * .4}));\n  let f = 0; const t0 = performance.now(); const step = () => { ctx.clearRect(0, 0, innerWidth, innerHeight); f = Math.max(f + 1, (performance.now() - t0) / 16.7);\n    for (const p of P) { p.vy += .45; p.vx *= .99; p.x += p.vx; p.y += p.vy; p.a += p.va; ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.a); ctx.fillStyle = p.c; ctx.globalAlpha = Math.max(0, 1 - f / 120); ctx.fillRect(-p.r / 2, -p.r / 2, p.r, p.r * .6); ctx.restore(); }\n    if (f < 120) requestAnimationFrame(step); else ctx.clearRect(0, 0, innerWidth, innerHeight); };\n  step();\n}\nfunction startupSummary() {\n  const now = new Date(); const o = S.rows.filter(r => urg(r, now) === "overdue").length, t = S.rows.filter(r => urg(r, now) === "today").length;\n  const next = S.rows.find(r => !isDone(r) && pd(r.deadline) >= now);\n  if (o || t) toast("Today\'s summary", [o ? `${o} overdue` : "", t ? `${t} due today` : ""].filter(Boolean).join(" · "), o ? "overdue" : "today", "📋");\n  else if (next) toast("Welcome back", `Next: ${clientOf(next)} — ${next.module_code || next.title} · ${left(pd(next.deadline), now)}`, "normal", "👋");\n}\n\n/* global ------------------------------------------------------------------- */\n$("#btn-new").onclick = () => openForm();\n$("#btn-settings").onclick = openSettings;\n$("#btn-palette").onclick = openPalette;\n$("#btn-mode").onclick = toggleMode;\n$("#sync-pill").onclick = openSettings;\n$("#scrim").onclick = closeDrawer;\n$("#client-search").oninput = renderClients;\ndocument.addEventListener("keydown", e => {\n  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);\n  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }\n  if (e.key === "Escape") { const m = $(".modal-wrap"); if (m) m._close(); else closeDrawer(); return; }\n  if (typing || $(".modal-wrap") || e.metaKey || e.ctrlKey || e.altKey) return;\n  const k = e.key.toLowerCase();\n  if (k === "n") { e.preventDefault(); openForm(); }\n  else if (k === "d") toggleMode();\n  else if (k === "1") go("dashboard"); else if (k === "2") go("board"); else if (k === "3") go("calendar"); else if (k === "4") go("all");\n  else if (k === "/") { e.preventDefault(); $("#client-search").focus(); }\n});\nwindow.addEventListener("resize", moveInk);\nsetInterval(() => tick(false), 1000);\nsetInterval(async () => { try { const s = await api("/api/signature"); if (s.signature !== S.sig) await load(); } catch (e) {} }, 4000);\nsetInterval(() => load().catch(() => {}), 30000);\nload(true).catch(e => { $("#loader").innerHTML = `<div class="empty"><h3>Couldn\'t connect</h3>${esc(e.message)}</div>`; });\n</script>\n</body>\n</html>\n'

STATUSES = ["Not Started", "In Progress", "Review", "Submitted", "Completed", "Cancelled"]
PRIORITIES = ["Low", "Medium", "High", "Urgent"]
PAYMENTS = ["Unpaid", "Partially Paid", "Paid"]
DONE_STATUSES = {"Submitted", "Completed", "Cancelled"}
FIELDS = ["title", "client", "client_contact", "subject", "module_code", "word_count",
          "assigned_date", "deadline", "priority", "status", "amount", "payment_status",
          "description", "notes"]
DEFAULT_SETTINGS = {"soon_days": "3", "theme": "aurora", "mode": "dark", "owner": "",
                    "notifications": "1", "gh_token": "", "gh_repo": ""}
SYNC_FIELDS = ["code"] + FIELDS + ["created_at", "updated_at"]
SYNC_FILE = "assignments.json"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
class Database:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, title TEXT NOT NULL,
                client TEXT, client_contact TEXT, subject TEXT, module_code TEXT,
                description TEXT, word_count TEXT, assigned_date TEXT, deadline TEXT NOT NULL,
                priority TEXT, status TEXT, amount TEXT, payment_status TEXT, notes TEXT,
                notified TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS tombstones (uid TEXT PRIMARY KEY, deleted_ms INTEGER);
            CREATE TABLE IF NOT EXISTS sync_state (uid TEXT PRIMARY KEY, hash TEXT);
            """)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(assignments)")}
        if "uid" not in cols:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN uid TEXT")
        if "mtime" not in cols:
            self.conn.execute("ALTER TABLE assignments ADD COLUMN mtime INTEGER DEFAULT 0")
        self.conn.commit()

    # settings
    def settings(self):
        with self.lock:
            s = dict(DEFAULT_SETTINGS)
            s.update({r["key"]: r["value"] for r in
                      self.conn.execute("SELECT key, value FROM settings")})
            return s

    def save_settings(self, data):
        with self.lock:
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                      (k, str(v)))
            self.conn.commit()

    # assignments
    def all(self):
        with self.lock:
            return [dict(r) for r in
                    self.conn.execute("SELECT * FROM assignments ORDER BY deadline, id")]

    def get(self, row_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM assignments WHERE id=?", (row_id,)).fetchone()
            return dict(r) if r else None

    def next_code(self, year=None):
        year = year or datetime.now().year
        best = 0
        for (code,) in self.conn.execute("SELECT code FROM assignments WHERE code LIKE ?",
                                         (f"ASG-{year}-%",)):
            try:
                best = max(best, int(str(code).rsplit("-", 1)[1]))
            except (ValueError, IndexError):
                pass
        return f"ASG-{year}-{best + 1:04d}"

    def add(self, data):
        data = validate(data)
        data.setdefault("priority", "Medium")
        data.setdefault("status", "Not Started")
        data.setdefault("payment_status", "Unpaid")
        data.setdefault("assigned_date", datetime.now().strftime(DAY_FMT))
        now = datetime.now().strftime(DATE_FMT)
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO assignments ({','.join(FIELDS)}, created_at, updated_at) "
                f"VALUES ({','.join('?' * len(FIELDS))}, ?, ?)",
                [data.get(f, "") for f in FIELDS] + [now, now])
            code = self.next_code()
            self.conn.execute("UPDATE assignments SET code=?, uid=? WHERE id=?",
                              (code, uuid.uuid4().hex, cur.lastrowid))
            self.conn.commit()
            return self.get(cur.lastrowid)

    def update(self, row_id, data):
        data = validate(data, partial=True)
        with self.lock:
            old = self.get(row_id)
            if not old:
                raise KeyError("Assignment not found")
            if not data:
                return old
            sets = ", ".join(f"{k}=?" for k in data)
            if "deadline" in data and data["deadline"] != old["deadline"]:
                sets += ", notified=''"
            self.conn.execute(f"UPDATE assignments SET {sets}, updated_at=? WHERE id=?",
                              list(data.values()) + [datetime.now().strftime(DATE_FMT), row_id])
            self.conn.commit()
            return self.get(row_id)

    def delete(self, row_id):
        with self.lock:
            self.conn.execute("DELETE FROM assignments WHERE id=?", (row_id,))
            self.conn.commit()

    def mark_notified(self, row_id, stages):
        with self.lock:
            row = self.get(row_id)
            cur = set(filter(None, (row["notified"] or "").split(",")))
            cur.update(stages)
            self.conn.execute("UPDATE assignments SET notified=? WHERE id=?",
                              (",".join(sorted(cur)), row_id))
            self.conn.commit()

    # ------------------------------------------------------------ sync
    @staticmethod
    def rec_hash(r):
        return hashlib.sha1(json.dumps([str(r.get(k) or "") for k in SYNC_FIELDS
                                        if k != "updated_at"]).encode()).hexdigest()

    def prepare_sync(self):
        """Give every row a uid, stamp locally-changed rows with mtime=now and turn
        rows deleted since the last sync into tombstones. Returns (records, tombs)."""
        now = int(time.time() * 1000)
        with self.lock:
            rows = self.all()
            for r in rows:
                if not r.get("uid"):
                    r["uid"] = uuid.uuid4().hex
                    self.conn.execute("UPDATE assignments SET uid=? WHERE id=?", (r["uid"], r["id"]))
            state = dict(self.conn.execute("SELECT uid, hash FROM sync_state").fetchall())
            for r in rows:
                if state.get(r["uid"]) != self.rec_hash(r):
                    r["mtime"] = max(now, int(r.get("mtime") or 0))
                    self.conn.execute("UPDATE assignments SET mtime=? WHERE id=?", (r["mtime"], r["id"]))
            live = {r["uid"] for r in rows}
            tombs = dict(self.conn.execute("SELECT uid, deleted_ms FROM tombstones").fetchall())
            for uid in state:
                if uid not in live and uid not in tombs:
                    tombs[uid] = now
                    self.conn.execute("INSERT OR REPLACE INTO tombstones VALUES (?, ?)", (uid, now))
            self.conn.commit()
            recs = {}
            for r in rows:
                rec = {k: r.get(k) or "" for k in SYNC_FIELDS}
                rec["uid"], rec["mtime"] = r["uid"], int(r.get("mtime") or 0)
                recs[r["uid"]] = rec
            return recs, tombs

    def apply_sync(self, recs, tombs):
        """Make the local database match the merged result."""
        changed = False
        with self.lock:
            local = {r["uid"]: r for r in self.all() if r.get("uid")}
            for uid, r in local.items():
                if uid not in recs:
                    self.conn.execute("DELETE FROM assignments WHERE id=?", (r["id"],))
                    changed = True
            for uid, rec in recs.items():
                cur = local.get(uid)
                vals = {k: str(rec.get(k) or "") for k in SYNC_FIELDS}
                if cur is None:
                    if self.conn.execute("SELECT 1 FROM assignments WHERE code=?", (vals["code"],)).fetchone() \
                            or not vals["code"]:
                        vals["code"] = self.next_code()
                    cols = list(vals) + ["uid", "mtime", "notified"]
                    self.conn.execute(
                        f"INSERT INTO assignments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        list(vals.values()) + [uid, int(rec.get("mtime") or 0), ""])
                    changed = True
                elif int(rec.get("mtime") or 0) > int(cur.get("mtime") or 0) and \
                        self.rec_hash(cur) != self.rec_hash(rec):
                    if vals["code"] != cur["code"] and self.conn.execute(
                            "SELECT 1 FROM assignments WHERE code=? AND id<>?",
                            (vals["code"], cur["id"])).fetchone():
                        vals["code"] = cur["code"]
                    notified = cur["notified"] if vals["deadline"] == cur["deadline"] else ""
                    self.conn.execute(
                        f"UPDATE assignments SET {', '.join(k + '=?' for k in vals)}, mtime=?, notified=? "
                        f"WHERE id=?", list(vals.values()) + [int(rec["mtime"]), notified, cur["id"]])
                    changed = True
            cutoff = int(time.time() * 1000) - 120 * 86400 * 1000
            self.conn.execute("DELETE FROM tombstones")
            for uid, ms in tombs.items():
                if ms >= cutoff:
                    self.conn.execute("INSERT INTO tombstones VALUES (?, ?)", (uid, int(ms)))
            self.conn.execute("DELETE FROM sync_state")
            for r in self.all():
                self.conn.execute("INSERT INTO sync_state VALUES (?, ?)", (r["uid"], self.rec_hash(r)))
            self.conn.commit()
        return changed

    def signature(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, updated_at, status, deadline, notified FROM assignments "
                "ORDER BY id").fetchall()
            sets = self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return hashlib.md5(repr([tuple(r) for r in rows] + [tuple(s) for s in sets])
                           .encode()).hexdigest()


def parse_dt(text):
    text = (text or "").strip().replace("T", " ")
    for fmt in (DATE_FMT, "%Y-%m-%d %H:%M:%S", DAY_FMT):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(hour=23, minute=59) if fmt == DAY_FMT else dt
        except ValueError:
            pass
    return None


def validate(data, partial=False):
    clean = {}
    for k, v in (data or {}).items():
        if k not in FIELDS or v is None:
            continue
        v = str(v).strip()
        if k == "deadline":
            dt = parse_dt(v)
            if not dt:
                raise ValueError("Deadline must look like 2026-12-05 17:00")
            v = dt.strftime(DATE_FMT)
        elif k == "assigned_date" and v:
            dt = parse_dt(v)
            if not dt:
                raise ValueError("Assigned date must look like 2026-12-01")
            v = dt.strftime(DAY_FMT)
        elif k == "status" and v not in STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
        elif k == "priority" and v not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        elif k == "payment_status" and v not in PAYMENTS:
            raise ValueError(f"Payment must be one of: {', '.join(PAYMENTS)}")
        clean[k] = v
    if not partial:
        if not clean.get("title"):
            raise ValueError("Title is required")
        if not clean.get("deadline"):
            raise ValueError("Deadline is required")
    return clean


# --------------------------------------------------------------------------- #
# Notifications & reminders
# --------------------------------------------------------------------------- #
def send_notification(title, message):
    text = message.replace("\n", " — ")[:250]
    try:
        if sys.platform == "darwin":
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{esc(text)}" with title "{esc(APP_NAME)}" '
                 f'subtitle "{esc(title)}" sound name "Glass"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                  "ContentType = WindowsRuntime] > $null; "
                  "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                  "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                  f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) "
                  "> $null; "
                  f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode("
                  f"'{text.replace(chr(39), '')}')) > $null; "
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                  f"'{APP_NAME}').Show([Windows.UI.Notifications.ToastNotification]::new($t))")
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", f"{APP_NAME}: {title}", text])
    except Exception:
        pass


def time_left(dl, now):
    secs = int((dl - now).total_seconds())
    over = secs < 0
    secs = abs(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    txt = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    return f"overdue by {txt}" if over else f"{txt} left"


class Reminders(threading.Thread):
    ORDER = ["soon", "1d", "1h", "overdue"]

    def __init__(self, db, events):
        super().__init__(daemon=True)
        self.db = db
        self.events = events  # list shared with the UI (in-app toasts)

    def check(self):
        s = self.db.settings()
        soon_days = int(s.get("soon_days") or 3)
        now = datetime.now()
        for r in self.db.all():
            if r["status"] in DONE_STATUSES:
                continue
            dl = parse_dt(r["deadline"])
            if not dl:
                continue
            sent = set(filter(None, (r["notified"] or "").split(",")))
            rem = dl - now
            stage = None
            if rem.total_seconds() < 0:
                stage, title = "overdue", "Deadline passed"
            elif rem <= timedelta(hours=1):
                stage, title = "1h", "Due within 1 hour"
            elif rem <= timedelta(days=1):
                stage, title = "1d", "Due within 24 hours"
            elif rem <= timedelta(days=soon_days):
                stage, title = "soon", f"Due in {soon_days} days or less"
            if stage and stage not in sent:
                client = r["client"] or "No client"
                mod = f"{r['module_code']} – " if r["module_code"] else ""
                msg = f"{client}: {mod}{r['title']}\n{dl:%a %d %b, %H:%M} • {time_left(dl, now)}"
                if s.get("notifications", "1") == "1":
                    send_notification(title, msg)
                self.events.append({"t": time.time(), "title": title, "message": msg,
                                    "id": r["id"], "stage": stage})
                del self.events[:-30]
                self.db.mark_notified(r["id"], self.ORDER[: self.ORDER.index(stage) + 1])

    def run(self):
        time.sleep(2)
        while True:
            try:
                self.check()
            except Exception as e:  # never die
                print("reminder error:", e, file=sys.stderr)
            time.sleep(30)


# --------------------------------------------------------------------------- #
# GitHub sync (shared with the iPhone app)
# --------------------------------------------------------------------------- #
GH_API = os.environ.get("ASSIGNMENT_GH_API", "https://api.github.com")


def public_settings(s):
    out = dict(s)
    out["gh_token_set"] = bool(out.get("gh_token"))
    out["gh_token"] = ""
    return out


def merge_docs(L, LT, R, RT):
    """Last-writer-wins per record (by mtime); tombstones beat older edits.
    Identical logic lives in the iPhone app."""
    tombs = {u: max(int(LT.get(u, 0) or 0), int(RT.get(u, 0) or 0)) for u in set(LT) | set(RT)}
    recs = {}
    for u in set(L) | set(R):
        a, b = L.get(u), R.get(u)
        w = a if (b is None or (a is not None and int(a.get("mtime") or 0) >= int(b.get("mtime") or 0))) else b
        t = tombs.get(u)
        if t and t >= int(w.get("mtime") or 0):
            continue
        recs[u] = w
    return recs, tombs


def canonical(recs, tombs):
    return json.dumps({"a": [recs[u] for u in sorted(recs)],
                       "t": {u: tombs[u] for u in sorted(tombs)}}, sort_keys=True)


_SSL = None


def ssl_context():
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        if sys.platform == "darwin":  # python.org builds may lack a CA bundle
            try:
                pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                      "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                     capture_output=True, text=True, timeout=20).stdout
                if pem:
                    ctx.load_verify_locations(cadata=pem)
            except Exception:
                pass
        _SSL = ctx
    return _SSL


class GitHubSync(threading.Thread):
    INTERVAL = 30

    def __init__(self, db):
        super().__init__(daemon=True)
        self.db = db
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status = {"state": "off", "last": None, "error": None, "repo": ""}

    def _req(self, method, url, token, body=None):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "User-Agent": "AssignmentManager",
                                              "Content-Type": "application/json"})
        ctx = ssl_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            return json.loads(r.read() or b"{}")

    def sync_once(self):
        s = self.db.settings()
        token, repo = s.get("gh_token", "").strip(), s.get("gh_repo", "").strip().strip("/")
        if not token or not repo:
            self.status = {"state": "off", "last": None, "error": None, "repo": repo}
            return
        url = f"{GH_API}/repos/{repo}/contents/{SYNC_FILE}"
        with self.lock:
            self.status.update(state="syncing", repo=repo)
            for attempt in range(3):
                try:
                    local, ltombs = self.db.prepare_sync()
                    sha, remote, rtombs = None, {}, {}
                    try:
                        meta = self._req("GET", url, token)
                        sha = meta.get("sha")
                        doc = json.loads(base64.b64decode(meta.get("content", "")).decode("utf-8") or "{}")
                        remote = {r["uid"]: r for r in doc.get("assignments", []) if r.get("uid")}
                        rtombs = {t["uid"]: t["deleted_ms"] for t in doc.get("tombstones", [])}
                    except urllib.error.HTTPError as e:
                        if e.code != 404:
                            raise
                    recs, tombs = merge_docs(local, ltombs, remote, rtombs)
                    self.db.apply_sync(recs, tombs)
                    if canonical(recs, tombs) != canonical(remote, rtombs):
                        doc = {"schema": 1, "app": "assignment-manager",
                               "updated_ms": int(time.time() * 1000),
                               "assignments": [recs[u] for u in sorted(recs, key=lambda u: (recs[u].get("deadline", ""), u))],
                               "tombstones": [{"uid": u, "deleted_ms": tombs[u]} for u in sorted(tombs)]}
                        body = {"message": f"Sync from desktop ({len(recs)} assignments)",
                                "content": base64.b64encode(json.dumps(doc, indent=1, ensure_ascii=False)
                                                            .encode("utf-8")).decode()}
                        if sha:
                            body["sha"] = sha
                        try:
                            self._req("PUT", url, token, body)
                        except urllib.error.HTTPError as e:
                            if e.code in (409, 422) and attempt < 2:
                                continue  # someone else wrote first: merge again
                            raise
                    self.status = {"state": "ok", "error": None, "repo": repo,
                                   "last": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    return
                except urllib.error.HTTPError as e:
                    msg = {401: "GitHub token is invalid or expired",
                           403: "Token has no permission for this repository",
                           404: "Repository not found (check owner/name and token access)"}.get(
                        e.code, f"GitHub error {e.code}")
                    self.status = {"state": "error", "error": msg, "repo": repo,
                                   "last": self.status.get("last")}
                    return
                except Exception as e:
                    self.status = {"state": "error", "error": f"Offline or network error: {e}",
                                   "repo": repo, "last": self.status.get("last")}
                    return

    def run(self):
        time.sleep(1)
        while True:
            try:
                self.sync_once()
            except Exception as e:
                self.status = {"state": "error", "error": str(e), "last": self.status.get("last")}
            self.wake.wait(self.INTERVAL)
            self.wake.clear()
            time.sleep(0.8)  # debounce bursts of edits


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class Server:
    def __init__(self, db):
        self.db = db
        self.token = secrets.token_urlsafe(24)
        self.sync = None
        self.events = []
        self.last_ping = time.time()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def export_csv(self):
        folder = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(folder):
            folder = DATA_DIR
        path = os.path.join(folder, f"assignments_{datetime.now():%Y%m%d_%H%M}.csv")
        cols = ["code"] + FIELDS + ["created_at", "updated_at"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in self.db.all():
                w.writerow([r.get(c, "") for c in cols])
        return path

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                return secrets.compare_digest(self.headers.get("X-Token", ""), server.token)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    ui_file = os.path.join(UI_DIR, "index.html")
                    if os.path.exists(ui_file):
                        with open(ui_file, "rb") as fh:
                            html = fh.read()
                    else:
                        html = (EMBEDDED_UI or "<h1>UI missing</h1>").encode("utf-8")
                    html = html.replace(b"__API_TOKEN__", server.token.encode())
                    return self._send(200, html, "text/html; charset=utf-8")
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if not self._authed():
                    return self._send(403, {"error": "forbidden"})
                server.last_ping = time.time()
                if path == "/api/state":
                    return self._send(200, {
                        "assignments": server.db.all(),
                        "settings": public_settings(server.db.settings()),
                        "sync": server.sync.status if server.sync else {"state": "off"},
                        "signature": server.db.signature(),
                        "events": server.events[-10:],
                        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "version": VERSION,
                        "meta": {"statuses": STATUSES, "priorities": PRIORITIES,
                                 "payments": PAYMENTS, "done": sorted(DONE_STATUSES)}})
                if path == "/api/signature":
                    return self._send(200, {"signature": server.db.signature()})
                return self._send(404, {"error": "not found"})

            def _mutate(self, method):
                if not self._authed():
                    return self._send(403, {"error": "forbidden"})
                path = urlparse(self.path).path
                parts = [p for p in path.split("/") if p]
                try:
                    body = self._body()
                    if parts == ["api", "sync"] and method == "POST":
                        if server.sync:
                            server.sync.wake.set()
                        return self._send(200, {"ok": True})
                    if server.sync and method in ("POST", "PUT", "DELETE"):
                        threading.Timer(0.3, server.sync.wake.set).start()
                    if parts == ["api", "assignments"] and method == "POST":
                        return self._send(201, server.db.add(body))
                    if len(parts) == 3 and parts[:2] == ["api", "assignments"]:
                        rid = int(parts[2])
                        if method == "PUT":
                            return self._send(200, server.db.update(rid, body))
                        if method == "DELETE":
                            server.db.delete(rid)
                            return self._send(200, {"ok": True})
                    if parts == ["api", "settings"] and method == "PUT":
                        server.db.save_settings(body)
                        return self._send(200, public_settings(server.db.settings()))
                    if parts == ["api", "export"] and method == "POST":
                        return self._send(200, {"path": server.export_csv()})
                    if parts == ["api", "test-notification"] and method == "POST":
                        send_notification("Notifications are working",
                                          "You'll be reminded before every deadline.")
                        return self._send(200, {"ok": True})
                    return self._send(404, {"error": "not found"})
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e).strip("'")})

            def do_POST(self):
                self._mutate("POST")

            def do_PUT(self):
                self._mutate("PUT")

            def do_DELETE(self):
                self._mutate("DELETE")

        return Handler

    def serve(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #
def ensure_webview():
    """Make pywebview available: install it once into ~/.assignment_manager/venv and
    re-launch with that interpreter. Returns True if webview can be imported here."""
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        pass
    if "--no-webview" in sys.argv or os.environ.get("AM_NO_BOOTSTRAP"):
        return False
    win = sys.platform.startswith("win")
    venv = os.path.join(DATA_DIR, "venv")
    vpy = os.path.join(venv, "Scripts", "python.exe") if win else os.path.join(venv, "bin", "python")
    if os.path.realpath(sys.executable) == os.path.realpath(vpy) or os.environ.get("AM_IN_VENV"):
        return False  # already inside the venv and it still failed
    marker = os.path.join(DATA_DIR, "webview_failed")
    if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 86400:
        return False  # failed recently; don't slow every launch down
    os.makedirs(DATA_DIR, exist_ok=True)
    log = open(os.path.join(DATA_DIR, "setup.log"), "a")
    try:
        ok = os.path.exists(vpy) and subprocess.run(
            [vpy, "-c", "import webview"], stdout=log, stderr=log).returncode == 0
        if not ok:
            send_notification("Setting up", "Preparing the app window (first launch only, about a minute)…")
            if not os.path.exists(vpy):
                subprocess.run([sys.executable, "-m", "venv", venv], check=True, stdout=log,
                               stderr=log, timeout=300)
            subprocess.run([vpy, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "pywebview", "certifi"], check=True, stdout=log, stderr=log, timeout=900)
        log.close()
        env = dict(os.environ, AM_IN_VENV="1")
        args = [vpy, os.path.abspath(__file__)] + sys.argv[1:]
        if win:
            sys.exit(subprocess.call(args, env=env))
        os.execve(vpy, args, env)
    except Exception as e:
        print(f"webview setup failed: {e}", file=log)
        log.close()
        open(marker, "w").close()
        return False


def open_app_window(url):
    """Native window via pywebview; fall back to a browser 'app' window."""
    try:
        import webview  # noqa: F401
        return "webview"
    except Exception:
        pass
    candidates = []
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium"):
            if os.path.isdir(f"/Applications/{app}.app"):
                candidates.append(["open", "-na", app, "--args", f"--app={url}",
                                   "--window-size=1400,880"])
    elif sys.platform.startswith("win"):
        for exe in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(exe):
                candidates.append([exe, f"--app={url}", "--window-size=1400,880"])
    else:
        for exe in ("google-chrome", "chromium", "chromium-browser"):
            if shutil.which(exe):
                candidates.append([exe, f"--app={url}"])
    for cmd in candidates:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "browser-app"
        except Exception:
            continue
    webbrowser.open(url)
    return "browser"


def main():
    if "--server" not in sys.argv:
        ensure_webview()  # may re-launch this script inside the private venv
    db = Database()
    server = Server(db)
    server.sync = GitHubSync(db)
    server.sync.start()
    server.serve()
    Reminders(db, server.events).start()

    if "--server" in sys.argv:
        print(server.url, flush=True)
        while True:
            time.sleep(3600)

    mode = open_app_window(server.url)
    if mode == "webview":
        import webview
        webview.create_window(APP_NAME, server.url, width=1440, height=900,
                              min_size=(1100, 700), background_color="#0b1020",
                              text_select=True)
        webview.start()
        return
    # Browser fallback: keep running while the UI is open (it pings every 10 s).
    server.last_ping = time.time() + 60  # grace period for the window to open
    while time.time() - server.last_ping < 90:
        time.sleep(5)


if __name__ == "__main__":
    main()
