# -*- coding: utf-8 -*-
# KOSMOS TV — alt bant soldan sağa + hareketli video logo
# / ana sayfa | /canliyayin oynatıcı | /admin panel | /proxy | kanal senkronu

from flask import Flask, request, redirect, session, jsonify, Response
import json, os, time, threading, re, secrets, hmac, html, subprocess, tempfile, socket
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import HTTPError
from urllib.parse import urljoin, quote

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('KOSMOS_SECRET', 'BYVMoobM2J6UrI9h')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
try:
    app.json.ensure_ascii = False
except Exception:
    try:
        app.config['JSON_AS_ASCII'] = False
    except Exception:
        pass

ADMIN_SIFRE = os.environ.get('KOSMOS_ADMIN_SIFRE', 'BYVMoobM2J6UrI9h')

# ================= ALT BANT YAZISI =================
TICKER_METIN = 'Salam Hörmətli Izləyicilər kanalımızda sizində reklamınız olması üçün : +994 55 829 92 86'
# ===================================================

# ================= HAREKETLİ KANAL LOGOSU (mp4) =================
KANAL_LOGO = 'https://videotourl.com/videos/1785564806236-0be517a3-d645-4ffd-bd84-15d7339788cd.mp4'
# ================================================================

PROXY_REFERER = os.environ.get('KOSMOS_REFERER', '')
PROXY_UA = os.environ.get('KOSMOS_UA',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36')

DB_FILE = 'playlist.json'
ZAMAN_FILE = 'zaman.json'
kilit = threading.Lock()
online_izleyiciler = {}

# ============================================================
# VERİTABANI
# ============================================================
def save_json(dosya, veri):
    tmp = dosya + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(veri, f, ensure_ascii=False, indent=2)
    os.replace(tmp, dosya)

def temizle_playlist(data):
    if not isinstance(data, list):
        return []
    temiz = []
    for oge in data:
        if isinstance(oge, dict) and oge.get('name') and oge.get('url'):
            try:
                sure = max(0, int(oge.get('sure', 0)))
            except Exception:
                sure = 0
            temiz.append({'name': str(oge['name']), 'url': str(oge['url']), 'sure': sure})
    return temiz

def load_playlist():
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return []
    return temizle_playlist(data)

def save_playlist(data):
    save_json(DB_FILE, data)

def load_zaman():
    try:
        with open(ZAMAN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        index = max(0, int(data.get('index', 0)))
    except Exception:
        index = 0
    try:
        baslangic = float(data.get('baslangic', time.time()))
    except Exception:
        baslangic = time.time()
    return {'index': index, 'baslangic': baslangic}

def save_zaman(data):
    save_json(ZAMAN_FILE, {'index': data['index'], 'baslangic': data['baslangic']})

def ilerlet(zaman, playlist):
    kalan = time.time() - zaman['baslangic']
    degisti = False
    while kalan > 0 and playlist:
        sure = playlist[zaman['index']].get('sure', 0)
        if sure <= 0 or kalan < sure:
            break
        kalan -= sure
        zaman['index'] = (zaman['index'] + 1) % len(playlist)
        zaman['baslangic'] = time.time() - kalan
        degisti = True
    if degisti:
        save_zaman(zaman)

if not os.path.exists(DB_FILE):
    save_playlist([])
if not os.path.exists(ZAMAN_FILE) or os.path.getsize(ZAMAN_FILE) == 0:
    save_json(ZAMAN_FILE, {'baslangic': time.time(), 'index': 0})

# ============================================================
# SÜRE
# ============================================================
def sure_format(sn):
    sn = max(0, int(sn))
    s = sn % 60
    m = (sn // 60) % 60
    h = sn // 3600
    if h > 0:
        return '%d:%02d:%02d' % (h, m, s)
    return '%02d:%02d' % (m, s)

def sure_al(url, timeout=20):
    try:
        cmd = ['ffprobe', '-v', 'error', '-user_agent', PROXY_UA,
               '-show_entries', 'format=duration', '-of', 'csv=p=0', url]
        if PROXY_REFERER:
            cmd.insert(2, '-headers')
            cmd.insert(3, 'Referer: ' + PROXY_REFERER)
        sonuc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        sure = float(sonuc.stdout.strip().split(',')[0])
        if sure > 0:
            return max(1, int(sure))
    except Exception:
        pass
    return 0

def sureyi_arka_planda_ogren(index, url):
    sure = sure_al(url)
    if sure:
        with kilit:
            playlist = load_playlist()
            if 0 <= index < len(playlist) and playlist[index].get('url') == url and not playlist[index].get('sure'):
                playlist[index]['sure'] = sure
                save_playlist(playlist)

def eksik_sureleri_doldur():
    time.sleep(3)
    with kilit:
        playlist = load_playlist()
        eksikler = [(i, o['url']) for i, o in enumerate(playlist) if not o.get('sure')]
    for i, url in eksikler:
        sureyi_arka_planda_ogren(i, url)

threading.Thread(target=eksik_sureleri_doldur, daemon=True).start()

# ============================================================
# PROXY
# ============================================================
def curl_cek(url, headers, timeout=30):
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
        tmp.close()
    except Exception:
        return None, 0, '', 'geçici dosya oluşturulamadı'
    cmd = ['curl', '-sS', '-L', '--compressed', '--insecure', '--noproxy', '*',
           '--connect-timeout', '10', '--max-time', str(timeout),
           '-w', '\n%{http_code}\n%{content_type}', '-o', tmp.name]
    for k, v in headers.items():
        cmd += ['-H', k + ': ' + v]
    cmd.append(url)
    try:
        sonuc = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
        hata = sonuc.stderr.decode('latin-1', errors='replace').strip()
        cikti = sonuc.stdout.decode('latin-1', errors='replace').strip().split('\n')
        cikti = [x.strip() for x in cikti if x.strip()]
        code = 0
        ctype = ''
        if len(cikti) >= 2:
            try:
                code = int(cikti[-2])
            except Exception:
                code = 0
            ctype = cikti[-1]
        elif len(cikti) == 1:
            try:
                code = int(cikti[0])
            except Exception:
                code = 0
        with open(tmp.name, 'rb') as f:
            icerik = f.read()
        if sonuc.returncode != 0 and not hata:
            hata = 'curl çıkış kodu %d' % sonuc.returncode
        return icerik, code, ctype, hata
    except FileNotFoundError:
        return None, 0, '', 'curl kurulu değil'
    except Exception as e:
        return None, 0, '', str(e)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

def urllib_cek(url, headers, timeout=30):
    try:
        opener = build_opener(ProxyHandler({}))
        req = Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as yanit:
            icerik = yanit.read()
            code = yanit.status if hasattr(yanit, 'status') else yanit.getcode()
            ctype = yanit.headers.get('Content-Type', 'application/octet-stream')
        return icerik, code, ctype, ''
    except HTTPError as e:
        return None, e.code, 'text/plain', ''
    except Exception as e:
        return None, 0, 'text/plain', str(e)

def proxy_cek(url, referer='', ua='', range_h=None, timeout=30):
    headers = {'User-Agent': ua or PROXY_UA}
    if referer or PROXY_REFERER:
        headers['Referer'] = referer or PROXY_REFERER
    if range_h:
        headers['Range'] = range_h
    icerik, code, ctype, hata = curl_cek(url, headers, timeout)
    if icerik is None or len(icerik) == 0 or code == 0:
        icerik2, code2, ctype2, hata2 = urllib_cek(url, headers, timeout)
        if icerik2 is not None and len(icerik2) > 0 and code2 != 0:
            return icerik2, code2, ctype2, hata2 or hata
    return icerik, code, ctype, hata

def proxy_link(url):
    return '/proxy?url=' + quote(url, safe='')

def m3u8_rewrite(icerik, url, host_url):
    try:
        metin = icerik.decode('utf-8')
    except Exception:
        try:
            metin = icerik.decode('latin-1', errors='replace')
        except Exception:
            return icerik
    if not metin.lstrip().startswith('#EXTM3U'):
        return icerik

    proxy_taban = host_url.rstrip('/') + '/proxy?url='

    def uri_degistir(m):
        u = m.group(1).strip('"')
        tam = urljoin(url, u)
        return 'URI="' + proxy_taban + quote(tam, safe='') + '"'

    satirlar = []
    for satir in metin.splitlines():
        satir = satir.strip()
        if not satir:
            continue
        if satir.startswith('#'):
            if 'URI="' in satir:
                satir = re.sub(r'URI="([^"]+)"', uri_degistir, satir)
            elif 'URI=' in satir:
                satir = re.sub(r'URI=([^\s,]+)', uri_degistir, satir)
            satirlar.append(satir)
        else:
            tam = urljoin(url, satir)
            satirlar.append(proxy_taban + quote(tam, safe=''))
    return '\n'.join(satirlar).encode('utf-8')

@app.route('/proxy')
def proxy():
    url = request.args.get('url', '')
    if not url.startswith(('http://', 'https://')):
        return 'Geçersiz URL', 400

    icerik, code, ctype, hata = proxy_cek(url, request.headers.get('Referer', ''),
                                          request.headers.get('User-Agent', ''),
                                          request.headers.get('Range'))
    if icerik is None or code == 0 or len(icerik) == 0:
        return 'Kaynak alınamadı (HTTP %s) %s' % (code, hata), 502

    m3u8_mi = ('mpegurl' in ctype.lower()) or url.lower().endswith('.m3u8')
    if not m3u8_mi and icerik:
        try:
            if icerik[:20].lstrip().startswith(b'#EXTM3U'):
                m3u8_mi = True
        except Exception:
            pass

    if m3u8_mi:
        icerik = m3u8_rewrite(icerik, url, request.host_url)
        ctype = 'application/vnd.apple.mpegurl'

    resp = Response(icerik, status=code if code else 200)
    resp.headers['Content-Type'] = ctype or 'application/octet-stream'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    resp.headers['Cache-Control'] = 'no-store'
    return resp

# ============================================================
# LINK TESTİ
# ============================================================
@app.route('/test_url')
def test_url():
    if not session.get('admin'):
        return jsonify({'ok': False, 'hata': 'Yetkisiz'})
    url = request.args.get('url', '').strip().replace(' ', '%20')
    if not url.startswith(('http://', 'https://')):
        return jsonify({'ok': False, 'hata': 'Geçersiz URL — http(s) ile başlamalı'})

    icerik, code, ctype, hata = proxy_cek(url)

    if icerik is None or len(icerik) == 0 or code == 0:
        mesaj = '❌ Bağlantı kurulamadı (HTTP %s)' % code
        if hata:
            mesaj += ' — ' + hata
        mesaj += ('\n\n000 demek: sunucudan bu adrese hiç ulaşılamıyor.\n'
                  '→ Link ölmüş/expired olabilir.\n'
                  '→ Veya CDN sunucunun IP adresini engelliyor olabilir.\n'
                  '→ Veya DNS çözümlenemiyor.')
        return jsonify({'ok': False, 'hata': mesaj})

    if code >= 400:
        if code == 403:
            mesaj = 'HTTP 403 — CDN erişimi reddediyor.\nÇözüm: KOSMOS_REFERER=https://siteningerçekadresi.com ile başlat.'
        elif code == 404:
            mesaj = 'HTTP 404 — Link ölmüş veya yanlış kopyalanmış.'
        elif code == 410:
            mesaj = 'HTTP 410 — Link kullanımdan kaldırılmış, yenisi gerek.'
        else:
            mesaj = 'HTTP %d — Sunucu hata verdi.' % code
        return jsonify({'ok': False, 'hata': mesaj})

    ilk = icerik[:300].decode('utf-8', errors='replace')
    return jsonify({'ok': True, 'status': code, 'ctype': ctype, 'ilk': ilk})

# ============================================================
# İZLEYİCİ SAYACI
# ============================================================
@app.route('/izleyici_bildir', methods=['POST'])
def izleyici_bildir():
    veri = request.get_json(silent=True) or {}
    kid = str(veri.get('id', ''))
    with kilit:
        if kid:
            online_izleyiciler[kid] = time.time()
        simdi = time.time()
        for k in [k for k, t in online_izleyiciler.items() if simdi - t > 30]:
            online_izleyiciler.pop(k, None)
    return 'ok'

@app.route('/izleyici_sayisi')
def izleyici_sayisi():
    with kilit:
        simdi = time.time()
        for k in [k for k, t in online_izleyiciler.items() if simdi - t > 30]:
            online_izleyiciler.pop(k, None)
        sayi = len(online_izleyiciler)
    return jsonify({'izleyici_sayisi': sayi})

# ============================================================
# KANAL DURUMU / SENKRON
# ============================================================
@app.route('/video_durum')
def video_durum():
    with kilit:
        playlist = load_playlist()
        zaman = load_zaman()
        ilerlet(zaman, playlist)
        if not playlist:
            return jsonify({'durum': 'bos'})
        if zaman['index'] >= len(playlist):
            zaman['index'] = 0
            zaman['baslangic'] = time.time()
            save_zaman(zaman)
        oge = playlist[zaman['index']]
        sure = oge.get('sure', 0)
        gecen = time.time() - zaman['baslangic']
        if sure > 0:
            gecen = max(0.0, min(gecen, sure - 1))
        else:
            gecen = 0
        return jsonify({
            'durum': 'var',
            'index': zaman['index'],
            'video_adi': oge['name'],
            'video_url': proxy_link(oge['url']),
            'url': oge['url'],
            'zaman': gecen,
            'sure': sure
        })

@app.route('/video_suresi', methods=['POST'])
def video_suresi():
    veri = request.get_json(silent=True) or {}
    idx = veri.get('index')
    sure = veri.get('sure')
    url = veri.get('url', '')
    if not isinstance(idx, int) or not isinstance(sure, (int, float)) or sure <= 0:
        return 'ok'
    with kilit:
        playlist = load_playlist()
        if 0 <= idx < len(playlist) and playlist[idx].get('url') == url:
            playlist[idx]['sure'] = int(sure)
            save_playlist(playlist)
    return 'ok'

@app.route('/video_bitti', methods=['POST'])
def video_bitti():
    veri = request.get_json(silent=True) or {}
    idx = veri.get('index')
    if not isinstance(idx, int):
        return 'ok'
    with kilit:
        playlist = load_playlist()
        zaman = load_zaman()
        if playlist and zaman['index'] == idx:
            zaman['index'] = (zaman['index'] + 1) % len(playlist)
            zaman['baslangic'] = time.time()
            save_zaman(zaman)
    return 'ok'

# ============================================================
# ANA SAYFA
# ============================================================
@app.route('/')
def ana_sayfa():
    with kilit:
        var_mi = bool(load_playlist())
    yayin_durum = 'CANLI YAYINA GİR' if var_mi else 'CANLI YAYIN'
    bos_not = '' if var_mi else '<div style="font-family:Arial;font-size:13px;color:rgba(255,255,255,0.35);margin-top:8px;">Yayın listesi şu an boş — yönetici panelinden video ekleyin</div>'
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>🌌 Kosmos TV</title>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&display=swap');
            * { margin:0; padding:0; box-sizing:border-box; }
            body { background:#0a0a1a; color:#fff; font-family:'Orbitron', sans-serif; min-height:100vh; display:flex; justify-content:center; align-items:center; overflow:hidden; position:relative; }
            #stars { position:fixed; top:0; left:0; width:100%; height:100%; z-index:0; pointer-events:none; }
            .star { position:absolute; background:#fff; border-radius:50%; animation:twinkle var(--d) ease-in-out infinite alternate; }
            @keyframes twinkle { 0% { opacity:0.2; transform:scale(0.8); } 100% { opacity:1; transform:scale(1.2); } }
            .nebula { position:fixed; border-radius:50%; filter:blur(80px); opacity:0.3; z-index:0; pointer-events:none; }
            .nebula1 { width:600px; height:600px; background:radial-gradient(circle,#e94560,transparent 70%); top:-200px; right:-200px; animation:nebulaFloat 20s ease-in-out infinite alternate; }
            .nebula2 { width:500px; height:500px; background:radial-gradient(circle,#6c5ce7,transparent 70%); bottom:-200px; left:-200px; animation:nebulaFloat 25s ease-in-out infinite alternate-reverse; }
            .nebula3 { width:400px; height:400px; background:radial-gradient(circle,#00b894,transparent 70%); top:50%; left:50%; transform:translate(-50%,-50%); animation:nebulaFloat 30s ease-in-out infinite alternate; }
            @keyframes nebulaFloat { 0% { transform:translate(0,0) scale(1); } 100% { transform:translate(40px,-30px) scale(1.2); } }
            .container { position:relative; z-index:1; text-align:center; padding:20px; max-width:700px; animation:fadeInUp 1.5s ease-out; }
            @keyframes fadeInUp { 0% { opacity:0; transform:translateY(40px) scale(0.95); } 100% { opacity:1; transform:translateY(0) scale(1); } }
            .logo-wrapper { position:relative; display:inline-block; margin-bottom:20px; }
            .logo-wrapper::before { content:''; position:absolute; top:-20px; left:-20px; right:-20px; bottom:-20px; background:radial-gradient(circle,rgba(233,69,96,0.15),transparent 70%); border-radius:50%; animation:pulse 3s ease-in-out infinite; }
            @keyframes pulse { 0%,100% { transform:scale(1); opacity:0.5; } 50% { transform:scale(1.1); opacity:1; } }
            .logo-wrapper video { width:200px; max-width:50vw; height:auto; display:block; position:relative; z-index:1; border-radius:16px; filter:drop-shadow(0 0 30px rgba(233,69,96,0.3)); animation:float 4s ease-in-out infinite; background:transparent; }
            @keyframes float { 0%,100% { transform:translateY(0px); } 50% { transform:translateY(-15px); } }
            h1 { font-size:clamp(36px,10vw,72px); font-weight:900; background:linear-gradient(135deg,#e94560,#ff6b81,#fdcb6e); background-size:300% 300%; -webkit-background-clip:text; -webkit-text-fill-color:transparent; animation:gradientShift 4s ease-in-out infinite; letter-spacing:4px; margin-bottom:5px; }
            @keyframes gradientShift { 0%,100% { background-position:0% 50%; } 50% { background-position:100% 50%; } }
            .slogan { font-size:clamp(12px,2vw,18px); color:rgba(255,255,255,0.4); letter-spacing:8px; font-weight:400; margin-bottom:10px; }
            .slogan span { color:#e94560; -webkit-text-fill-color:#e94560; }
            .description { font-family:'Arial', sans-serif; font-size:clamp(13px,1.5vw,18px); color:rgba(255,255,255,0.5); line-height:1.8; margin:20px 0 35px 0; letter-spacing:1px; max-width:500px; margin-left:auto; margin-right:auto; }
            .yayin-btn { display:inline-block; padding:18px 55px; background:linear-gradient(135deg,#e94560,#c73e54); color:#fff; font-size:clamp(16px,2.5vw,22px); font-weight:700; font-family:'Orbitron', sans-serif; text-decoration:none; border-radius:60px; border:none; cursor:pointer; position:relative; overflow:hidden; transition:all 0.4s cubic-bezier(0.25,0.46,0.45,0.94); box-shadow:0 0 40px rgba(233,69,96,0.25); letter-spacing:3px; z-index:2; }
            .yayin-btn::before { content:''; position:absolute; top:-50%; left:-50%; width:200%; height:200%; background:radial-gradient(circle,rgba(255,255,255,0.15),transparent 60%); opacity:0; transition:opacity 0.6s; }
            .yayin-btn:hover { transform:scale(1.05) translateY(-3px); box-shadow:0 0 70px rgba(233,69,96,0.5); }
            .yayin-btn:hover::before { opacity:1; }
            .yayin-btn:active { transform:scale(0.95); }
            .canli-dot { display:inline-block; width:12px; height:12px; background:#fff; border-radius:50%; margin-right:12px; animation:blink 1.2s infinite; vertical-align:middle; }
            @keyframes blink { 0%,100% { opacity:1; box-shadow:0 0 10px #fff; } 50% { opacity:0.2; box-shadow:0 0 0px #fff; } }
            .footer { position:fixed; bottom:25px; left:50%; transform:translateX(-50%); font-size:11px; color:rgba(255,255,255,0.12); letter-spacing:4px; font-weight:400; z-index:1; }
            .admin-link { position:fixed; top:20px; right:25px; color:rgba(255,255,255,0.1); text-decoration:none; font-size:12px; font-family:'Arial', sans-serif; transition:0.3s; z-index:10; letter-spacing:2px; }
            .admin-link:hover { color:rgba(255,255,255,0.4); }
            .features { display:flex; justify-content:center; gap:15px; margin-top:30px; flex-wrap:wrap; }
            .feature { font-family:'Arial', sans-serif; font-size:11px; color:rgba(255,255,255,0.2); background:rgba(255,255,255,0.03); padding:6px 18px; border-radius:30px; border:1px solid rgba(255,255,255,0.04); letter-spacing:1px; backdrop-filter:blur(5px); -webkit-backdrop-filter:blur(5px); }
            .feature span { margin-right:6px; }
            @media (max-width:600px) {
                .logo-wrapper video { width:120px; }
                .yayin-btn { padding:14px 30px; font-size:14px; }
                .features { gap:8px; }
                .feature { font-size:9px; padding:4px 12px; }
            }
        </style>
    </head>
    <body>
        <div class="nebula nebula1"></div>
        <div class="nebula nebula2"></div>
        <div class="nebula nebula3"></div>
        <div id="stars"></div>
        <a href="/admin" class="admin-link">⚙ ADMIN</a>
        <div class="container">
            <div class="logo-wrapper">
                <video src="__KANAL_LOGO__" autoplay muted loop playsinline></video>
            </div>
            <h1>KOSMOS TV</h1>
            <div class="slogan">✦ <span>UZAYDAN</span> YAYIN ✦</div>
            <div class="description">
                Kesintisiz film, dizi ve özel içerikler.
                <br>Her an, her yerde, <span style="color:rgba(255,255,255,0.7);">sınırsız eğlence</span>.
            </div>
            <a href="/canliyayin" class="yayin-btn">
                <span class="canli-dot"></span> __YAYIN_BUTON__
            </a>
            __BOS_NOT__
            <div class="features">
                <span class="feature"><span>📡</span> 7/24 Yayın</span>
                <span class="feature"><span>🎬</span> Film & Dizi</span>
                <span class="feature"><span>📱</span> Her Cihaz</span>
                <span class="feature"><span>🌍</span> Dünya'ya Özel</span>
            </div>
        </div>
        <div class="footer">✦ KOSMOS TV 2026 ✦</div>
        <script>
            const starsContainer = document.getElementById('stars');
            for (let i = 0; i < 250; i++) {
                const star = document.createElement('div');
                star.className = 'star';
                const size = Math.random() * 3 + 1;
                star.style.width = size + 'px';
                star.style.height = size + 'px';
                star.style.left = Math.random() * 100 + '%';
                star.style.top = Math.random() * 100 + '%';
                star.style.setProperty('--d', (Math.random() * 3 + 1) + 's');
                star.style.animationDelay = (Math.random() * 5) + 's';
                starsContainer.appendChild(star);
            }
        </script>
    </body>
    </html>
    '''.replace('__YAYIN_BUTON__', yayin_durum).replace('__BOS_NOT__', bos_not).replace('__KANAL_LOGO__', KANAL_LOGO)

# ============================================================
# CANLI YAYIN — hareketli logo + soldan sağa yavaş bant
# ============================================================
IZLEYICI_HTML = '''<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Kosmos TV - Canlı Yayın</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html, body { background:#000; height:100%; overflow:hidden; font-family:Arial, sans-serif; }
video#video { position:fixed; top:0; left:0; width:100vw; height:100vh; background:#000; object-fit:contain; z-index:1; }

/* ===== HAREKETLİ KANAL LOGOSU (sol kenar, büyük) ===== */
.logo-kutu {
    position:fixed; top:14px; left:14px; z-index:40;
    display:flex; flex-direction:column; align-items:flex-start; gap:8px;
    pointer-events:none;
}
.logo-kutu video.logo-video {
    width:180px; max-width:34vw; height:auto; display:block;
    background:rgba(0,0,0,0.35); padding:4px; border-radius:14px;
    filter:drop-shadow(0 2px 10px rgba(0,0,0,0.85));
    object-fit:contain;
}
.canli-badge {
    display:flex; align-items:center; gap:6px;
    background:rgba(233,69,96,0.95); color:#fff;
    font-size:12px; font-weight:bold; padding:5px 12px;
    border-radius:20px; letter-spacing:1.5px;
    box-shadow:0 2px 8px rgba(0,0,0,0.6);
}
.canli-badge .nokta {
    width:8px; height:8px; background:#fff; border-radius:50%;
    animation:yan 1s infinite;
}
@keyframes yan { 50% { opacity:0.2; } }

/* ===== ÜST SAĞ BUTONLAR ===== */
.fullscreen-btn {
    position:fixed; top:12px; right:12px; z-index:20;
    background:rgba(0,0,0,0.5); color:#fff;
    border:1px solid rgba(255,255,255,0.3); border-radius:8px;
    width:40px; height:40px; font-size:18px; cursor:pointer;
}
.fullscreen-btn:hover { background:rgba(0,0,0,0.8); }
.ses-btn {
    position:fixed; top:12px; right:62px; z-index:20;
    background:rgba(0,0,0,0.5); color:#fff;
    border:1px solid rgba(255,255,255,0.3); border-radius:8px;
    width:40px; height:40px; font-size:15px; cursor:pointer;
}
.ses-btn:hover { background:rgba(0,0,0,0.8); }
.izleyici-sayisi {
    position:fixed; top:18px; right:112px; z-index:20;
    color:#fff; font-size:11px; background:rgba(0,0,0,0.5);
    padding:4px 10px; border-radius:15px; letter-spacing:0.5px;
}
.geri-btn {
    position:fixed; top:16px; left:50%; transform:translateX(-50%); z-index:20;
    color:rgba(255,255,255,0.5); text-decoration:none; font-size:11px;
    background:rgba(0,0,0,0.4); padding:5px 14px; border-radius:20px;
}
.geri-btn:hover { color:#fff; }

/* ===== YÜKLENİYOR ===== */
.yukleniyor {
    position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
    color:#fff; font-size:16px; z-index:10; text-align:center;
    background:rgba(0,0,0,0.75); padding:16px 28px; border-radius:12px;
    display:none; max-width:85%;
}
.yukleniyor .loader {
    display:inline-block; width:16px; height:16px;
    border:3px solid rgba(255,255,255,0.2); border-top-color:#e94560;
    border-radius:50%; animation:don 0.8s linear infinite;
    margin-right:8px; vertical-align:middle;
}
@keyframes don { to { transform:rotate(360deg); } }

/* ===== ALT BANT — SOLDAN SAĞA, YAVAŞ ===== */
.ticker {
    position:fixed; bottom:0; left:0; width:100%; height:48px; z-index:30;
    background:rgba(0,0,0,0.88); border-top:3px solid #e94560;
    display:flex; align-items:stretch; overflow:hidden;
}
.ticker-badge {
    flex:0 0 auto; display:flex; align-items:center;
    background:#e94560; color:#fff; font-weight:bold; font-size:14px;
    letter-spacing:1px; padding:0 16px; text-transform:uppercase;
    text-shadow:0 1px 2px rgba(0,0,0,0.5); z-index:2;
}
.ticker-kaydir {
    flex:1; overflow:hidden; display:flex; align-items:center;
    position:relative;
}
.ticker-icerik {
    display:flex; white-space:nowrap; will-change:transform;
    /* soldan sağa, yavaş ve rahat okunur (70 saniye) */
    animation:akis 70s linear infinite;
}
.ticker-metin {
    display:inline-block; color:#fff; font-size:18px; font-weight:bold;
    letter-spacing:0.6px; text-shadow:0 1px 3px rgba(0,0,0,0.8);
    padding-right:80px;
}
/* SOLDAN SAĞA: yazı soldan girer, sağa doğru akar */
@keyframes akis {
    from { transform: translateX(-50%); }
    to   { transform: translateX(0); }
}

/* ===== ALTYAZILAR ===== */
.film-adi {
    position:fixed; bottom:60px; right:15px; z-index:20;
    color:#fff; font-size:15px; background:rgba(0,0,0,0.6);
    padding:8px 16px; border-radius:8px; max-width:70%; text-align:right;
}
.durum-bar {
    position:fixed; bottom:60px; left:15px; z-index:15;
    background:rgba(0,0,0,0.6); color:rgba(255,255,255,0.85);
    font-size:11px; padding:7px 12px; border-radius:8px;
    max-width:55%; letter-spacing:0.3px;
}

@media (max-width:600px) {
    .logo-kutu video.logo-video { width:110px; padding:3px; border-radius:10px; }
    .canli-badge { font-size:10px; padding:4px 9px; }
    .ticker { height:40px; }
    .ticker-badge { font-size:11px; padding:0 10px; }
    .ticker-metin { font-size:14px; padding-right:50px; }
    .ticker-icerik { animation-duration: 55s; }
    .film-adi { font-size:12px; bottom:50px; }
    .durum-bar { font-size:9px; bottom:50px; max-width:40%; }
}
</style>
</head>
<body>
<div class="logo-kutu">
    <video class="logo-video" src="__KANAL_LOGO__" autoplay muted loop playsinline></video>
    <div class="canli-badge"><span class="nokta"></span> CANLI</div>
</div>
<div class="izleyici-sayisi" id="izleyiciSayisi">0</div>
<button class="fullscreen-btn" id="fullscreenBtn">⛶</button>
<button class="ses-btn" id="sesBtn">🔇</button>
<a href="/" class="geri-btn">‹ Ana Sayfa</a>

<video id="video" autoplay playsinline></video>
<div class="yukleniyor" id="yukleniyor"><span class="loader"></span> Yayın başlıyor...</div>
<div class="durum-bar" id="durumBar">Bağlanıyor...</div>
<div class="film-adi" id="filmAdi">Yükleniyor...</div>

<div class="ticker">
    <div class="ticker-badge">📢 Reklam</div>
    <div class="ticker-kaydir">
        <div class="ticker-icerik">
            <span class="ticker-metin">__TICKER_METIN__</span>
            <span class="ticker-metin">__TICKER_METIN__</span>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<script>
(function() {
    const video = document.getElementById('video');
    const filmAdi = document.getElementById('filmAdi');
    const yukleniyor = document.getElementById('yukleniyor');
    const durumBar = document.getElementById('durumBar');
    let currentIndex = 0;
    let currentUrl = '';
    let originalUrl = '';
    let hls = null;
    let hataSayaci = 0;
    const izleyiciId = Math.random().toString(36).substring(2,10) + Date.now().toString(36);

    // Logo videosunu sürekli çalıştır (sessiz, döngü)
    const logoVideo = document.querySelector('.logo-video');
    if (logoVideo) {
        logoVideo.muted = true;
        logoVideo.loop = true;
        logoVideo.playsInline = true;
        logoVideo.play().catch(function(){});
    }

    function durumGoster(msg, hata) {
        durumBar.textContent = msg;
        durumBar.style.background = hata ? 'rgba(200,30,50,0.85)' : 'rgba(0,0,0,0.6)';
    }
    function sureFormat(sn) {
        sn = Math.floor(sn || 0);
        const s = sn % 60, m = Math.floor(sn/60) % 60, h = Math.floor(sn/3600);
        return (h > 0 ? h + ':' : '') + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
    }

    document.getElementById('fullscreenBtn').addEventListener('click', function() {
        if (!document.fullscreenElement && !document.webkitFullscreenElement) {
            const el = document.documentElement;
            if (el.requestFullscreen) el.requestFullscreen();
            else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
        } else {
            if (document.exitFullscreen) document.exitFullscreen();
            else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
        }
    });

    const sesBtn = document.getElementById('sesBtn');
    function sesiAc() {
        video.muted = false;
        video.play().catch(function(){});
        if (logoVideo) logoVideo.play().catch(function(){});
        sesBtn.style.display = 'none';
    }
    sesBtn.addEventListener('click', function(e){ e.stopPropagation(); sesiAc(); });
    document.addEventListener('click', sesiAc, { once: true });

    function zamanSabitle(z) {
        z = Number(z) || 0;
        if (z < 0) z = 0;
        const d = video.duration;
        if (isFinite(d) && d > 0 && z > d - 2) z = Math.max(0, d - 2);
        return z;
    }

    function videoyuYukle(url, zaman) {
        hataSayaci = 0;
        if (window.Hls && Hls.isSupported()) {
            if (hls) { hls.destroy(); hls = null; }
            hls = new Hls({ maxBufferLength: 30 });
            hls.loadSource(url);
            hls.attachMedia(video);

            hls.on(Hls.Events.MANIFEST_PARSED, function() {
                try { video.currentTime = zamanSabitle(zaman); } catch(e) {}
                video.muted = true;
                video.play().catch(function(){});
                durumGoster('Oynatılıyor: ' + filmAdi.textContent);
            });

            hls.on(Hls.Events.ERROR, function(e, data) {
                if (data && data.fatal) {
                    hataSayaci++;
                    const http = (data.response && data.response.code) ? 'HTTP ' + data.response.code : '';
                    const detay = data.details || '';
                    durumGoster('Hata (' + hataSayaci + '/3): ' + http + ' ' + detay, true);
                    if (hataSayaci < 3) {
                        setTimeout(function(){ videoyuYukle(url, 0); }, 3000);
                    } else {
                        yukleniyor.innerHTML = 'Yayın açılamadı<br><span style="font-size:12px;color:#999;">' +
                            (http || 'bağlantı hatası') + ' ' + detay +
                            '<br>Link ölü olabilir veya CDN engelliyor. Admin panelde 🔍 Link Testi ile kontrol et.</span>';
                        yukleniyor.style.display = 'block';
                    }
                }
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = url;
            video.addEventListener('loadedmetadata', function() {
                try { video.currentTime = zamanSabitle(zaman); } catch(e) {}
                video.muted = true;
                video.play().catch(function(){});
            }, { once: true });
        } else {
            durumGoster('Tarayıcı HLS desteklemiyor', true);
        }
    }

    video.addEventListener('loadedmetadata', function() {
        const d = video.duration;
        if (d && isFinite(d) && currentIndex !== null && originalUrl) {
            fetch('/video_suresi', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ index: currentIndex, sure: Math.round(d), url: originalUrl })
            }).catch(function(){});
        }
    });

    function videoDurumunuAl() {
        fetch('/video_durum', { cache: 'no-store' })
        .then(function(r){ return r.json(); })
        .then(function(data) {
            if (data.durum === 'bos') {
                yukleniyor.innerHTML = 'Kanalda yayın yok — admin panelden video ekleyin';
                yukleniyor.style.display = 'block';
                durumGoster('Kanal boş');
                if (hls) { hls.destroy(); hls = null; }
                return;
            }
            if (currentUrl !== data.video_url) {
                currentUrl = data.video_url;
                originalUrl = data.url || '';
                currentIndex = data.index;
                filmAdi.textContent = data.video_adi;
                yukleniyor.innerHTML = '<span class="loader"></span> Yayın başlıyor...';
                yukleniyor.style.display = 'block';
                videoyuYukle(currentUrl, data.zaman);
            } else {
                const hedef = zamanSabitle(data.zaman);
                const fark = Math.abs(video.currentTime - hedef);
                if (fark > 3) { try { video.currentTime = hedef; } catch(e) {} }
            }
            durumGoster(data.video_adi + ' - ' + sureFormat(video.currentTime));
        })
        .catch(function(){ durumGoster('Sunucuya ulaşılamıyor...', true); });
    }

    function izleyiciBildir() {
        fetch('/izleyici_bildir', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: izleyiciId })
        }).catch(function(){});
    }
    setInterval(izleyiciBildir, 10000);
    setTimeout(izleyiciBildir, 500);

    function izleyiciSayisiGuncelle() {
        fetch('/izleyici_sayisi', { cache: 'no-store' })
        .then(function(r){ return r.json(); })
        .then(function(d){ document.getElementById('izleyiciSayisi').textContent = d.izleyici_sayisi || 0; })
        .catch(function(){});
    }
    setInterval(izleyiciSayisiGuncelle, 5000);
    setTimeout(izleyiciSayisiGuncelle, 1000);

    window.addEventListener('beforeunload', function() {
        navigator.sendBeacon('/izleyici_bildir',
            new Blob([JSON.stringify({ id: izleyiciId })], { type: 'application/json' }));
    });

    video.addEventListener('ended', function() {
        fetch('/video_bitti', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: currentIndex })
        }).then(function(){ currentUrl = ''; setTimeout(videoDurumunuAl, 500); })
          .catch(function(){ setTimeout(videoDurumunuAl, 1000); });
    });
    video.addEventListener('waiting', function() {
        yukleniyor.innerHTML = '<span class="loader"></span> Yükleniyor...';
        yukleniyor.style.display = 'block';
    });
    video.addEventListener('playing', function() { yukleniyor.style.display = 'none'; });
    video.addEventListener('error', function() { setTimeout(videoDurumunuAl, 3000); });

    videoDurumunuAl();
    setInterval(videoDurumunuAl, 5000);
    document.addEventListener('visibilitychange', function() {
        if (!document.hidden) {
            videoDurumunuAl();
            if (logoVideo) logoVideo.play().catch(function(){});
        }
    });

    document.addEventListener('contextmenu', function(e) {
        if (e.target.tagName === 'VIDEO') { e.preventDefault(); return false; }
    });
    document.addEventListener('keydown', function(e) {
        if (e.ctrlKey && ['u','s','c','i'].indexOf(e.key) !== -1) { e.preventDefault(); return false; }
        if (e.key === 'F12') { e.preventDefault(); return false; }
    });
})();
</script>
</body>
</html>'''

@app.route('/canliyayin')
def canliyayin():
    return IZLEYICI_HTML.replace('__TICKER_METIN__', html.escape(TICKER_METIN)) \
                        .replace('__KANAL_LOGO__', KANAL_LOGO)

@app.route('/izle')
def izle():
    return IZLEYICI_HTML.replace('__TICKER_METIN__', html.escape(TICKER_METIN)) \
                        .replace('__KANAL_LOGO__', KANAL_LOGO)

@app.route('/favicon.ico')
def favicon():
    return '', 204

# ============================================================
# ADMIN
# ============================================================
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST':
        if hmac.compare_digest(request.form.get('sifre', ''), ADMIN_SIFRE):
            session['admin'] = True
            session['csrf'] = secrets.token_hex(16)
            return redirect('/panel')
        return '<div style="font-family:Arial;background:#1a1a2e;color:#fff;height:100vh;display:flex;align-items:center;justify-content:center;"><div style="text-align:center;"><h1>❌ Şifre yanlış!</h1><a href="/admin" style="color:#e94560;">Tekrar dene</a></div></div>'
    return '''
    <style>
        body { background:#1a1a2e; color:#fff; font-family:Arial; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }
        .box { background:#16213e; padding:40px; border-radius:15px; text-align:center; }
        input { padding:12px; width:250px; border:none; border-radius:8px; margin:10px; font-size:16px; }
        button { padding:12px 40px; background:#e94560; color:#fff; border:none; border-radius:8px; font-size:18px; cursor:pointer; }
        button:hover { background:#c73e54; }
    </style>
    <div class="box"><h1>🔐 Admin Girişi</h1><form method="POST"><input type="password" name="sifre" placeholder="Şifrenizi girin"><br><button type="submit">Giriş</button></form></div>
    '''

PANEL_CSS = '''
body { background:#0f0f1a; color:#fff; font-family:Arial, sans-serif; padding:20px; }
.header { display:flex; justify-content:space-between; align-items:center; background:#1a1a2e; padding:15px 30px; border-radius:10px; }
.header h1 { margin:0; color:#e94560; font-size:24px; }
.logout { color:#fff; text-decoration:none; background:#e94560; padding:8px 20px; border-radius:8px; }
.logout:hover { background:#c73e54; }
.ekle { background:#1a1a2e; padding:25px; border-radius:10px; margin:20px 0; }
.ekle h3 { margin-top:0; color:#e94560; }
.ekle input { padding:12px; width:300px; max-width:90%; border:none; border-radius:8px; margin:5px; font-size:14px; }
.ekle button { padding:12px 30px; background:#e94560; color:#fff; border:none; border-radius:8px; font-size:16px; cursor:pointer; }
.ekle button:hover { background:#c73e54; }
.liste { background:#1a1a2e; padding:20px; border-radius:10px; }
.liste h3 { margin-top:0; color:#e94560; }
.item { display:flex; justify-content:space-between; align-items:center; padding:12px; border-bottom:1px solid #333; gap:10px; flex-wrap:wrap; }
.item .name { font-size:16px; }
.item .url { color:#888; font-size:12px; max-width:380px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.sil { color:#e94560; background:none; border:none; font-size:15px; font-weight:bold; cursor:pointer; font-family:Arial; }
.kanal-link { display:inline-block; margin-top:20px; padding:12px 30px; background:#e94560; color:#fff; text-decoration:none; border-radius:8px; }
.kanal-link:hover { background:#c73e54; }
.bos { color:#888; text-align:center; padding:30px; }
.bilgi { color:#ccc; font-size:14px; margin-top:15px; background:#1a1a2e; padding:15px; border-radius:8px; line-height:1.9; }
.bilgi b { color:#e94560; }
.mesaj { background:#2d8f4e; color:#fff; padding:10px 20px; border-radius:8px; margin-bottom:15px; }
#testSonuc { margin-top:10px; font-size:13px; color:#ddd; white-space:pre-wrap; word-break:break-all; }
#testSonuc pre { background:#0f0f1a; padding:10px; border-radius:8px; color:#7fd69a; font-size:12px; max-height:120px; overflow:auto; margin:8px 0 0 0; }
.ok { color:#7fd69a; font-weight:bold; }
.hata { color:#ff6b6b; font-weight:bold; }
'''

def panel_html(playlist, csrf, mesaj=''):
    p = []
    p.append('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Kosmos TV - Yönetim</title>'
             '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
             '<style>' + PANEL_CSS + '</style></head><body>')
    p.append('<div class="header"><h1>🌌 Kosmos TV Yönetimi</h1><a href="/logout" class="logout">🚪 Çıkış</a></div>')
    if mesaj:
        p.append('<div class="mesaj">' + html.escape(mesaj) + '</div>')

    p.append('''<div class="ekle"><h3>🔍 Link Testi</h3>
        <input type="text" id="testUrl" placeholder="M3U8 linkini yapıştır" style="width:500px;max-width:90%;">
        <button onclick="testEt()">Test Et</button>
        <div id="testSonuc"></div></div>
        <script>
        function testEt() {
            const u = document.getElementById('testUrl').value.trim();
            const s = document.getElementById('testSonuc');
            if (!u) { s.innerHTML = '<span class="hata">Link boş</span>'; return; }
            s.innerHTML = '⏳ Deneniyor...';
            fetch('/test_url?url=' + encodeURIComponent(u))
            .then(function(r){ return r.json(); })
            .then(function(d) {
                if (d.ok) {
                    s.innerHTML = '<span class="ok">✅ HTTP ' + d.status + ' · ' + d.ctype + '</span><pre>' + d.ilk + '</pre>';
                } else {
                    s.innerHTML = '<span class="hata">❌ ' + d.hata.replace(/\\n/g, '<br>') + '</span>';
                }
            })
            .catch(function(e){ s.innerHTML = '<span class="hata">❌ Ağ hatası: ' + e + '</span>'; });
        }
        </script>''')

    p.append('<div class="ekle"><h3>➕ Yeni Ekle</h3>'
             '<form method="POST">'
             '<input type="hidden" name="csrf" value="' + csrf + '">'
             '<input type="text" name="name" placeholder="Film/Dizi Adı" required>'
             '<input type="text" name="url" placeholder="M3U8 Linki" required>'
             '<button type="submit">Ekle</button>'
             '</form></div>')

    p.append('<div class="liste"><h3>📋 Sıradaki Videolar (' + str(len(playlist)) + ' video)</h3>')
    if playlist:
        for i, item in enumerate(playlist):
            ad = html.escape(item['name'])
            url_kisa = html.escape(item['url'][:50]) + ('...' if len(item['url']) > 50 else '')
            sure_txt = sure_format(item.get('sure', 0)) if item.get('sure') else '⏳ süre öğreniliyor'
            p.append('''
            <div class="item">
                <span class="name">%d. %s</span>
                <span class="url">%s · ⏱ %s</span>
                <form method="POST" action="/sil/%d" style="display:inline"
                      onsubmit="return confirm('Bu videoyu silmek istediğine emin misin?');">
                    <input type="hidden" name="csrf" value="%s">
                    <button type="submit" class="sil">🗑️ Sil</button>
                </form>
            </div>''' % (i + 1, ad, url_kisa, sure_txt, i, csrf))
    else:
        p.append('<div class="bos">📭 Liste boş, yeni video ekleyin</div>')
    p.append('</div>')

    p.append('<a href="/canliyayin" class="kanal-link" target="_blank">📡 CANLI YAYINI AÇ</a>')
    p.append('<a href="/" class="kanal-link" style="background:#333;margin-left:10px;" target="_blank">🏠 ANA SAYFA</a>')

    p.append('''<div class="bilgi">
        <b>⚡ Senkron:</b> Herkes aynı anda aynı yayını izler.<br>
        <b>📢 Alt bant:</b> Kodun başında <b>TICKER_METIN</b> — soldan sağa, yavaş akar.<br>
        <b>🎬 Logo:</b> Kodun başında <b>KANAL_LOGO</b> — hareketli mp4 video logo.<br>
        <b>🔗 Proxy:</b> M3U8 linkleri kendi sunucumuzdan geçer.<br>
        <b>🔒 Güvenlik:</b> Panel şifreli, silme CSRF korumalı.
    </div>''')
    p.append('</body></html>')
    return ''.join(p)

@app.route('/panel', methods=['GET', 'POST'])
def panel():
    if not session.get('admin'):
        return redirect('/admin')
    if not session.get('csrf'):
        session['csrf'] = secrets.token_hex(16)
    csrf = session['csrf']
    mesaj = ''

    if request.method == 'POST':
        if request.form.get('csrf') != csrf:
            mesaj = '⚠️ Güvenlik doğrulaması başarısız. Lütfen tekrar dene.'
        else:
            name = request.form.get('name', '').strip()
            url = request.form.get('url', '').strip()
            if name and url:
                with kilit:
                    playlist = load_playlist()
                    playlist.append({'name': name, 'url': url, 'sure': 0})
                    save_playlist(playlist)
                    yeni_index = len(playlist) - 1
                threading.Thread(target=sureyi_arka_planda_ogren, args=(yeni_index, url), daemon=True).start()
                mesaj = '✅ Video eklendi! Süre öğreniliyor, kanal devam ediyor.'
            else:
                mesaj = '⚠️ Ad ve link boş olamaz.'

    with kilit:
        playlist = load_playlist()
    return panel_html(playlist, csrf, mesaj)

@app.route('/sil/<int:index>', methods=['POST'])
def sil(index):
    if not session.get('admin'):
        return redirect('/admin')
    if request.form.get('csrf') != session.get('csrf'):
        return redirect('/panel')
    with kilit:
        playlist = load_playlist()
        if 0 <= index < len(playlist):
            del playlist[index]
            save_playlist(playlist)
    return redirect('/panel')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/admin')

@app.after_request
def onbellek_engelle(resp):
    if resp.mimetype == 'application/json' or request.path.startswith('/proxy'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

def yerel_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    print('=' * 50)
    print('🌌 KOSMOS TV başladı')
    print('   Ana sayfa: http://127.0.0.1:%d/' % port)
    print('   Canlı:     http://127.0.0.1:%d/canliyayin' % port)
    print('   Panel:     http://127.0.0.1:%d/admin' % port)
    print('   Ağ (tel):  http://%s:%d/' % (yerel_ip(), port))
    print('=' * 50)
    app.run(host='0.0.0.0', port=port, debug=False)