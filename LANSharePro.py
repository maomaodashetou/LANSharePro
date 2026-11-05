# -*- coding: utf-8 -*-
"""LANShare Pro 1.5.1

把一个任意类型的文件，或一个文件夹（含全部子目录），以 HTTP 直链形式共享到局域网，
同网段的人用浏览器打开链接即可浏览、直接下载，支持断点续传（Range）。

Copyright (c) 2026 陈科雄 <kexiongchen@gmail.com>
SPDX-License-Identifier: MIT
Project: https://github.com/maomaodashetou/LANSharePro
"""

import atexit
import base64
import collections
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.parse import parse_qs, quote, unquote, urlparse

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox

APP_NAME = "LANShare Pro"
APP_VERSION = "1.5.1"
APP_AUTHOR = "陈科雄"
APP_EMAIL = "kexiongchen@gmail.com"
APP_REPO = "https://github.com/maomaodashetou/LANSharePro"
DEFAULT_PORT = 8899


# --------------------------------------------------------------------------- 通用工具
def resource_dir():
    """资源目录：开发时在脚本同级 assets，打包后位于 exe 解包目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets")


def _ipconfig_adapters():
    """解析 ipconfig，返回 [(网卡名, [IPv4...])]，含已连接的虚拟网卡。"""
    flags = 0x08000000 if hasattr(subprocess, "DEVNULL") else 0  # CREATE_NO_WINDOW
    try:
        out = subprocess.check_output(
            ["ipconfig"], stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, timeout=8,
        )
    except Exception:
        return []
    try:
        text = out.decode("mbcs", "replace")
    except Exception:
        text = out.decode("utf-8", "replace")

    adapters = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not raw.startswith((" ", "\t")) and line.endswith(":"):
            name = line[:-1]
            for kw in ("适配器", "adapter", "Adapter", "adapter "):
                if kw in name:
                    name = name.split(kw, 1)[1].strip() or name
                    break
            adapters.append({"name": name, "ips": [], "up": True, "gw": False})
            continue
        if not adapters:
            continue
        if "媒体已断开" in line or "Media disconnected" in line:
            adapters[-1]["up"] = False
        if "默认网关" in line or "Default Gateway" in line:
            if re.search(r"\d{1,3}(\.\d{1,3}){3}", line):
                adapters[-1]["gw"] = True
        found = None
        for kw in ("IPv4", "IPv4 地址", "IPv4 Address", "IPv4-Adresse"):
            idx = line.find(kw)
            if idx >= 0:
                found = line[idx:]
                break
        if found:
            parts = found.rsplit(":", 1)[-1].strip().split("(")[0].strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", parts):
                if not parts.startswith(("127.", "169.254.")):
                    adapters[-1]["ips"].append(parts)
    return [(a["name"], a["ips"], a["gw"]) for a in adapters if a["up"] and a["ips"]]


_VIRTUAL_HINTS = ("vmware", "virtualbox", "vethernet", "hyper-v", "loopback", "tap-",
                  "bluetooth", "wsl", "npcap", "virtual")
_PHYSICAL_HINTS = ("以太网", "wlan", "wi-fi", "wifi", "ethernet", "wireless", "无线")


def get_lan_ads():
    """返回 [(网卡名, IPv4)]，首个为主用地址（优先物理网卡、有默认网关的）。"""
    raw = []
    for name, ips, has_gw in _ipconfig_adapters():
        for ip in ips:
            raw.append((name, ip, has_gw))

    if raw:
        def rank(item):
            name = item[0].lower()
            virtual = any(h in name for h in _VIRTUAL_HINTS)
            physical = any(h in name for h in _PHYSICAL_HINTS)
            return (0 if physical else 1, 1 if virtual else 0, 0 if item[2] else 1)

        raw.sort(key=rank)
        ads = []
        for name, ip, _ in raw:
            if ip not in [x[1] for x in ads]:
                ads.append((name, ip))
        return ads
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [("本机", ip) for ip in ips if not ip.startswith("127.")]


def get_lan_ips():
    """返回本机所有非回环 IPv4 地址，首个为主用地址。"""
    return [ip for _, ip in get_lan_ads()]


# 目录体积缓存：{路径: (目录 mtime, (总字节, 文件数), 计算时间)}
_SIZE_CACHE = {}
_SIZE_LOCK = threading.Lock()


def dir_summary(path, ttl=30):
    """递归统计目录总大小与文件数，结果缓存 ttl 秒。"""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return 0, 0
    now = time.time()
    with _SIZE_LOCK:
        hit = _SIZE_CACHE.get(path)
    if hit and hit[0] == mtime and now - hit[2] < ttl:
        return hit[1]

    total = 0
    count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    with _SIZE_LOCK:
        _SIZE_CACHE[path] = (mtime, (total, count), now)
    return total, count


def human_size(n):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f TB" % value


def escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_LOGO_URI = None


def logo_data_uri():
    """网页里的 logo 直接内联，避免额外请求。"""
    global _LOGO_URI
    if _LOGO_URI is None:
        try:
            with open(os.path.join(resource_dir(), "logo_64.png"), "rb") as fp:
                _LOGO_URI = "data:image/png;base64," + base64.b64encode(fp.read()).decode("ascii")
        except OSError:
            _LOGO_URI = ""
    return _LOGO_URI


def brand_html(subtitle):
    uri = logo_data_uri()
    img = '<img src="%s" alt="">' % uri if uri else ""
    return '%s<span class="tag">%s &nbsp;·&nbsp; %s</span>' % (img, APP_NAME, subtitle)


def safe_join(root, rel):
    """把相对路径安全地拼到共享根目录下，越界返回 None。"""
    root_real = os.path.realpath(root)
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return root_real
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        return None
    return target


def enable_dpi_awareness():
    """高分屏下保持文字清晰，避免系统缩放导致的模糊。"""
    if os.name != "nt":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            from ctypes import windll

            windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------- 页面模板
PAGE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f5f5f7;color:#1d1d1f;
     font-family:-apple-system,"SF Pro Text","Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
     -webkit-font-smoothing:antialiased}
.wrap{max-width:780px;margin:56px auto 64px;background:#fff;border:1px solid #e5e5ea;
      border-radius:16px;padding:38px 34px 34px;
      box-shadow:0 1px 2px rgba(0,0,0,.03),0 12px 32px rgba(0,0,0,.05)}
.brand{display:flex;align-items:center;gap:10px}
.brand img{width:30px;height:30px;display:block}
.tag{font-size:11px;letter-spacing:.14em;color:#86868b;text-transform:uppercase}
h1{font-size:22px;font-weight:600;margin:16px 0 6px;word-break:break-all;line-height:1.4;
   letter-spacing:-.01em}
.meta{font-size:13px;color:#86868b}
.crumb{margin:24px 0 8px;font-size:13px;color:#86868b;word-break:break-all;line-height:1.9}
.crumb a{color:#1d1d1f;text-decoration:none;border-bottom:1px solid #d2d2d7}
.crumb span.sep{padding:0 6px;color:#c7c7cc}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:6px}
th{text-align:left;font-weight:500;font-size:12px;color:#86868b;padding:8px 10px;
   border-bottom:1px solid #e5e5ea;white-space:nowrap}
td{padding:12px 10px;border-bottom:1px solid #f0f0f2;vertical-align:middle}
td.n{word-break:break-all}
td.n a{color:#1d1d1f;text-decoration:none}
td.n a:hover{text-decoration:underline}
td.n span.dir{color:#86868b;margin-right:6px}
td.s,td.t{color:#86868b;font-size:12px;white-space:nowrap}
td.a{text-align:right;width:1%}
a.dl{display:inline-block;background:#f2f2f7;color:#1d1d1f;text-decoration:none;font-size:12px;
     padding:6px 14px;border-radius:8px;border:1px solid #e5e5ea;white-space:nowrap}
a.dl:hover{background:#eaeaef}
a.btn{display:block;text-align:center;background:#1d1d1f;color:#fff;text-decoration:none;
      padding:13px 18px;font-size:15px;border-radius:10px;margin-top:26px;font-weight:500}
a.btn:hover{background:#333336}
a.zipbtn{display:inline-block;background:#1d1d1f;color:#fff;text-decoration:none;font-size:13px;
         padding:9px 18px;border-radius:9px;margin:14px 0 4px}
a.zipbtn:hover{background:#333336}
.hint{margin-top:22px;font-size:12px;color:#86868b;line-height:1.8}
.hint code{background:#f2f2f7;padding:2px 6px;border-radius:5px}
.empty{padding:30px 10px;color:#86868b;font-size:13px}
.foot{margin-top:30px;padding-top:16px;border-top:1px solid #f0f0f2;
      font-size:11px;color:#a1a1a6;line-height:1.8}
.foot a{color:#6e6e73;text-decoration:none}
#dlbox{position:fixed;right:22px;bottom:22px;width:330px;display:none;z-index:9;
       background:#fff;border:1px solid #e5e5ea;border-radius:14px;padding:16px 18px 14px;
       box-shadow:0 2px 6px rgba(0,0,0,.04),0 16px 40px rgba(0,0,0,.10)}
.dlhead{display:flex;align-items:center;gap:8px;margin-bottom:12px}
#dlname{flex:1;font-size:13px;font-weight:500;color:#1d1d1f;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap}
#dlcancel{border:1px solid #e5e5ea;background:#fff;color:#1d1d1f;font-size:12px;
          padding:3px 10px;border-radius:7px;cursor:pointer;font-family:inherit}
#dlcancel:hover{background:#f5f5f7}
.dlbarwrap{height:6px;background:#f0f0f2;border-radius:3px;overflow:hidden}
#dlbar{height:100%;width:0;background:#1d1d1f;border-radius:3px;transition:width .2s linear}
.dlbarwrap.indet #dlbar{width:32%;animation:indet 1.2s ease-in-out infinite}
@keyframes indet{0%{margin-left:-32%}100%{margin-left:100%}}
#dlinfo{margin-top:10px;font-size:12px;color:#86868b;font-variant-numeric:tabular-nums}
"""

DOWNLOAD_BOX = """
<div id="dlbox">
  <div class="dlhead"><span id="dlname">文件</span>
  <button id="dlcancel" onclick="dlCancel()">取消</button></div>
  <div class="dlbarwrap" id="dlwrap"><div id="dlbar"></div></div>
  <div id="dlinfo">正在连接…</div>
</div>
"""

DOWNLOAD_JS = """<script>
(function(){
  var box=document.getElementById('dlbox');
  if(!box) return;
  var bar=document.getElementById('dlbar'), wrap=document.getElementById('dlwrap');
  var nameEl=document.getElementById('dlname'), infoEl=document.getElementById('dlinfo');
  var ctrl=null;

  function fmt(n){
    var u=['B','KB','MB','GB','TB'], i=0;
    while(n>=1024 && i<4){ n=n/1024; i++; }
    return (i ? n.toFixed(1) : Math.round(n)) + ' ' + u[i];
  }
  function dur(s){
    if(!isFinite(s) || s<1) return '不到 1 秒';
    if(s<60) return Math.round(s)+' 秒';
    var m=Math.floor(s/60), r=Math.round(s%60);
    if(m<60) return m+' 分 '+r+' 秒';
    return Math.floor(m/60)+' 小时 '+(m%60)+' 分';
  }
  function hide(){ box.style.display='none'; }

  window.dlCancel=function(){
    if(ctrl){ try{ ctrl.abort(); }catch(e){} }
    hide();
  };

  window.dl=function(ev){
    var a=ev.currentTarget || ev.srcElement;
    if(ev.preventDefault){ ev.preventDefault(); } else { ev.returnValue=false; }
    var url=a.getAttribute('href'), fname=a.getAttribute('data-name') || 'download';
    if(!window.fetch || !window.AbortController || !window.ReadableStream){
      window.location.href=url; return false;
    }
    if(ctrl){ try{ ctrl.abort(); }catch(e){} }
    ctrl=new AbortController();
    nameEl.textContent=fname;
    bar.style.width='0%'; wrap.className='dlbarwrap';
    infoEl.textContent='正在连接…'; box.style.display='block';
    var spd=0, lastT=0, lastB=0, got=0, total=0;

    fetch(url,{cache:'no-store', signal:ctrl.signal}).then(function(resp){
      if(!resp.ok) throw new Error('HTTP '+resp.status);
      total=parseInt(resp.headers.get('Content-Length')||'0',10);
      if(total>800*1024*1024){ hide(); window.location.href=url; return null; }
      if(!total) wrap.className='dlbarwrap indet';
      var reader=resp.body.getReader(), chunks=[];
      lastT=Date.now();
      function step(){
        return reader.read().then(function(r){
          if(r.done) return chunks;
          chunks.push(r.value); got+=r.value.length;
          var now=Date.now(), dt=(now-lastT)/1000;
          if(dt>0.25){ spd=(got-lastB)/dt; lastT=now; lastB=got; }
          if(total) bar.style.width=Math.min(100, got/total*100).toFixed(1)+'%';
          var txt=fmt(got);
          if(total) txt+=' / '+fmt(total);
          if(spd>0){
            txt+=' · '+fmt(spd)+'/s';
            if(total) txt+=' · 剩余 '+dur((total-got)/spd);
          }
          infoEl.textContent=txt;
          return step();
        });
      }
      return step();
    }).then(function(chunks){
      if(!chunks) return;
      var blob=new Blob(chunks), a2=document.createElement('a');
      a2.href=URL.createObjectURL(blob);
      a2.download=fname;
      document.body.appendChild(a2); a2.click(); a2.remove();
      setTimeout(function(){ URL.revokeObjectURL(a2.href); }, 20000);
      hide();
    }).catch(function(err){
      if(err && err.name==='AbortError'){ hide(); return; }
      hide(); window.location.href=url;
    });
    return false;
  };
})();
</script>"""


FILE_PAGE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title - $app</title><style>$css</style></head>
<body><div class="wrap">
  <div class="brand">$brand_inner</div>
  <h1>$title</h1>
  <div class="meta">文件大小 $size &nbsp;·&nbsp; 支持断点续传</div>
  <a class="btn" href="/download" data-name="$filename" onclick="return dl(event)">下载文件</a>
  <div class="hint">直链地址：<code>http://$host/download</code><br>
  可复制到迅雷、IDM 等下载工具使用。</div>
  <div class="foot">$app $version &nbsp;·&nbsp; 作者 $author &nbsp;·&nbsp;
  <a href="mailto:$email">$email</a></div>
</div>$dlbox$js</body></html>"""
)

DIR_PAGE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title - $app</title><style>$css</style></head>
<body><div class="wrap">
  <div class="brand">$brand_inner</div>
  <h1>$title</h1>
  <div class="meta">$meta</div>
  <div class="crumb">$crumb</div>
  $zipbar
  $table
  <div class="hint">点击文件夹可继续浏览子目录，点击「下载」会显示进度、速度和剩余时间；<br>
  需要整份拿走时点上方「打包下载当前目录」。</div>
  <div class="foot">$app $version &nbsp;·&nbsp; 作者 $author &nbsp;·&nbsp;
  <a href="mailto:$email">$email</a></div>
</div>$dlbox$js</body></html>"""
)


# --------------------------------------------------------------------------- 分享服务
class _QuietServer(ThreadingHTTPServer):
    """客户端断线是常态，不把连接重置等异常打到控制台。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return


class _ChunkWriter:
    """把 zip 数据以 chunked 方式直接推给浏览器，避免在磁盘上生成临时压缩包。"""

    def __init__(self, wfile, server, block=64 * 1024):
        self.wfile = wfile
        self.server = server
        self.block = block
        self.buf = bytearray()

    def write(self, data):
        if not data:
            return 0
        self.buf.extend(data)
        while len(self.buf) >= self.block:
            self._emit(self.block)
        return len(data)

    def _emit(self, n):
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        self.wfile.write(b"%X\r\n" % len(chunk))
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
        self.server.add_bytes(len(chunk))

    def flush(self):
        pass

    def finish(self):
        if self.buf:
            self._emit(len(self.buf))
        self.wfile.write(b"0\r\n\r\n")

    def tell(self):
        # 让 zipfile 认为输出流不可定位，改用数据描述符写入
        raise OSError("unseekable stream")


class ShareServer:
    """单文件或整目录的 HTTP 分享服务。"""

    def __init__(self, path, port, events=None):
        self.root = os.path.abspath(path)
        self.is_dir = os.path.isdir(self.root)
        self.name = os.path.basename(self.root.rstrip("\\/")) or self.root
        self.port = int(port)
        self.events = events or queue.Queue()
        self.bytes_sent = 0
        self.active = 0
        self.total_hits = 0
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._stopped = False

    # -- 统计 ---------------------------------------------------------------
    def add_bytes(self, n):
        with self._lock:
            self.bytes_sent += n

    def conn_open(self):
        with self._lock:
            self.active += 1
            self.total_hits += 1

    def conn_close(self):
        with self._lock:
            self.active = max(0, self.active - 1)

    def snapshot(self):
        with self._lock:
            return self.bytes_sent, self.active, self.total_hits

    def log(self, text):
        self.events.put(("log", text))

    # -- 生命周期 -----------------------------------------------------------
    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "LANSharePro/" + APP_VERSION

            def log_message(self, fmt, *args):
                pass

            def log_error(self, fmt, *args):
                pass

            def do_GET(self):
                self._handle(head_only=False)

            def do_HEAD(self):
                self._handle(head_only=True)

            def do_POST(self):
                self.send_error(405, "Method Not Allowed")

            # -- 路由 -------------------------------------------------------
            def _handle(self, head_only):
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                rel = parse_qs(parsed.query).get("p", [""])[0]

                if path == "/favicon.ico":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if server.is_dir:
                    if path in ("/", "/index.html"):
                        self._send_listing(head_only, "")
                    elif path == "/browse":
                        self._send_listing(head_only, rel)
                    elif path == "/download":
                        target = safe_join(server.root, rel)
                        if target is None or not os.path.isfile(target):
                            self.send_error(404, "Not Found")
                            return
                        self._send_file(head_only, target, os.path.basename(target), rel)
                    elif path == "/zip":
                        self._send_zip(head_only, rel)
                    else:
                        self.send_error(404, "Not Found")
                else:
                    if path in ("/", "/index.html"):
                        self._send_file_page(head_only)
                    elif path == "/download" or path.startswith("/download/"):
                        self._send_file(head_only, server.root, server.name, server.name)
                    else:
                        self.send_error(404, "Not Found")

            # -- 单文件落地页 -----------------------------------------------
            def _send_file_page(self, head_only):
                try:
                    size = human_size(os.path.getsize(server.root))
                except OSError:
                    self.send_error(404, "Not Found")
                    return
                body = FILE_PAGE.substitute(
                    app=APP_NAME,
                    version="v" + APP_VERSION,
                    author=APP_AUTHOR,
                    email=APP_EMAIL,
                    css=PAGE_CSS,
                    brand_inner=brand_html("局域网直链分享"),
                    title=escape(server.name),
                    size=size,
                    host=self.headers.get("Host", "本机IP:%d" % server.port),
                    filename=escape(server.name),
                    dlbox=DOWNLOAD_BOX,
                    js=DOWNLOAD_JS,
                ).encode("utf-8")
                self._send_html(head_only, body)
                server.log("访问主页  %s" % self.client_address[0])

            # -- 目录浏览 ---------------------------------------------------
            def _send_listing(self, head_only, rel):
                target = safe_join(server.root, rel)
                if target is None or not os.path.isdir(target):
                    self.send_error(404, "Not Found")
                    return

                try:
                    items = list(os.scandir(target))
                except OSError:
                    self.send_error(403, "Forbidden")
                    return

                def sort_key(entry):
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    return (not is_dir, entry.name.lower())

                items.sort(key=sort_key)

                rows = []
                if rel:
                    parent = "/".join(rel.strip("/").split("/")[:-1])
                    rows.append(
                        '<tr><td class="n"><a href="/browse?p=%s">'
                        '<span class="dir">↑</span>返回上级</a></td>'
                        '<td class="s">-</td><td class="t">-</td><td class="a"></td></tr>'
                        % quote(parent, safe="")
                    )

                file_count = 0
                total = 0
                for entry in items:
                    try:
                        is_dir = entry.is_dir()
                        if is_dir:
                            href = "/browse?p=%s" % quote(
                                (rel + "/" + entry.name).strip("/"), safe=""
                            )
                            dsize, dcount = dir_summary(entry.path)
                            size_text = (
                                "%s · %d 个文件" % (human_size(dsize), dcount)
                                if dcount
                                else "空文件夹"
                            )
                            rows.append(
                                '<tr><td class="n"><a href="%s">'
                                '<span class="dir">▸</span>%s</a></td>'
                                '<td class="s">%s</td><td class="t">%s</td>'
                                '<td class="a"></td></tr>'
                                % (
                                    href,
                                    escape(entry.name),
                                    size_text,
                                    time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.stat().st_mtime)),
                                )
                            )
                        else:
                            stat = entry.stat()
                            size = stat.st_size
                            file_count += 1
                            total += size
                            href = "/download?p=%s" % quote(
                                (rel + "/" + entry.name).strip("/"), safe=""
                            )
                            rows.append(
                                '<tr><td class="n"><a href="%s">%s</a></td>'
                                '<td class="s">%s</td><td class="t">%s</td>'
                                '<td class="a"><a class="dl" href="%s" data-name="%s" '
                                'onclick="return dl(event)">下载</a></td></tr>'
                                % (
                                    href,
                                    escape(entry.name),
                                    human_size(size),
                                    time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
                                    href,
                                    escape(entry.name),
                                )
                            )
                    except OSError:
                        continue

                if rows:
                    table = (
                        "<table><thead><tr><th>名称</th><th>大小</th>"
                        "<th>修改时间</th><th></th></tr></thead><tbody>%s</tbody></table>"
                        % "".join(rows)
                    )
                else:
                    table = '<div class="empty">此目录为空</div>'

                parts = ['<a href="/">%s</a>' % escape(server.name)]
                acc = ""
                for seg in rel.strip("/").split("/"):
                    if not seg:
                        continue
                    acc = (acc + "/" + seg).strip("/")
                    parts.append(
                        '<span class="sep">/</span><a href="/browse?p=%s">%s</a>'
                        % (quote(acc, safe=""), escape(seg))
                    )

                title = "共享目录" if not rel else rel.strip("/").split("/")[-1]
                dsize, dcount = dir_summary(target)
                zipbar = (
                    '<a class="zipbtn" href="/zip?p=%s" data-name="%s.zip" '
                    'onclick="return dl(event)">打包下载当前目录（ZIP · 约 %s）</a>'
                    % (quote(rel.strip("/"), safe=""), escape(title), human_size(dsize))
                )
                body = DIR_PAGE.substitute(
                    app=APP_NAME,
                    version="v" + APP_VERSION,
                    author=APP_AUTHOR,
                    email=APP_EMAIL,
                    css=PAGE_CSS,
                    brand_inner=brand_html("局域网文件共享"),
                    title=escape(title),
                    meta="当前目录共 %d 个文件 &nbsp;·&nbsp; 合计 %s（含子目录）"
                    % (dcount, human_size(dsize)),
                    crumb="".join(parts),
                    zipbar=zipbar,
                    table=table,
                    dlbox=DOWNLOAD_BOX,
                    js=DOWNLOAD_JS,
                ).encode("utf-8")
                self._send_html(head_only, body)
                server.log("浏览目录  %s  /%s" % (self.client_address[0], rel))

            def _send_html(self, head_only, body):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)

            # -- 文件传输（支持 Range） --------------------------------------
            def _send_file(self, head_only, abs_path, display_name, rel):
                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    self.send_error(404, "Not Found")
                    server.log("文件缺失，下载失败：%s" % rel)
                    return

                start, end = 0, size - 1
                status = 200
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes=") and size > 0:
                    part = rng[len("bytes="):].split(",")[0].strip()
                    s, _, e = part.partition("-")
                    try:
                        if s == "":
                            start = max(0, size - int(e))
                            end = size - 1
                        else:
                            start = int(s)
                            end = int(e) if e else size - 1
                        if start > end or start >= size:
                            raise ValueError
                        status = 206
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

                length = max(0, end - start + 1)
                ascii_name = display_name.encode("ascii", "replace").decode("ascii").replace('"', "_")

                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                    % (ascii_name, quote(display_name, safe="")),
                )
                self.send_header("Cache-Control", "no-store")
                if status == 206:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.end_headers()
                if head_only:
                    return

                server.conn_open()
                server.log("开始下载  %s  %s" % (self.client_address[0], display_name))
                sent = 0
                try:
                    with open(abs_path, "rb") as fp:
                        fp.seek(start)
                        remain = length
                        while remain > 0:
                            chunk = fp.read(min(256 * 1024, remain))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remain -= len(chunk)
                            sent += len(chunk)
                            server.add_bytes(len(chunk))
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    server.log("下载中断  %s  已发送 %s" % (self.client_address[0], human_size(sent)))
                except OSError as exc:
                    server.log("下载异常  %s  %s" % (self.client_address[0], exc))
                else:
                    server.log("下载完成  %s  %s" % (self.client_address[0], human_size(sent)))
                finally:
                    server.conn_close()

            # -- 整目录打包下载（边压边传，不落盘） --------------------------
            def _send_zip(self, head_only, rel):
                target = safe_join(server.root, rel)
                if target is None or not os.path.isdir(target):
                    self.send_error(404, "Not Found")
                    return

                base = os.path.basename(target.rstrip("\\/")) or server.name
                zip_name = base + ".zip"
                ascii_name = zip_name.encode("ascii", "replace").decode("ascii").replace('"', "_")

                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                                 % (ascii_name, quote(zip_name, safe="")))
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if head_only:
                    return

                server.conn_open()
                server.log("开始打包下载  %s  %s" % (self.client_address[0], zip_name))
                writer = _ChunkWriter(self.wfile, server)
                try:
                    with zipfile.ZipFile(writer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                        for root_dir, _dirs, files in os.walk(target):
                            for fname in files:
                                full = os.path.join(root_dir, fname)
                                arc = os.path.relpath(full, target).replace(os.sep, "/")
                                try:
                                    zf.write(full, base + "/" + arc)
                                except (OSError, ValueError):
                                    server.log("跳过文件  %s" % arc)
                    writer.finish()
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    server.log("打包下载中断  %s" % self.client_address[0])
                except OSError as exc:
                    server.log("打包异常  %s  %s" % (self.client_address[0], exc))
                else:
                    server.log("打包完成  %s  %s" % (self.client_address[0], zip_name))
                finally:
                    server.conn_close()

        self._httpd = _QuietServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.log("服务已启动  0.0.0.0:%d" % self.port)

    def stop(self):
        """关闭监听端口并释放全部连接。"""
        if self._stopped:
            return
        self._stopped = True
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        self.log("服务已停止，端口 %d 已释放" % self.port)


# --------------------------------------------------------------------------- 界面
PAGE_BG = "#F5F5F7"
CARD_BG = "#FFFFFF"
BORDER = "#E5E5EA"
FIELD_BG = "#F2F2F7"
BTN_BG = "#FFFFFF"
BTN_LINE = "#D8D8DE"
BTN_HOVER = "#F7F7F9"
BTN_PRESS = "#EDEDF0"
BTN_DISABLED_LINE = "#EEEEF1"
TEXT_MAIN = "#1D1D1F"
TEXT_MUTED = "#86868B"
TEXT_DISABLED = "#C7C7CC"

FONT_TITLE = ("Microsoft YaHei", 17, "bold")
FONT_UI = ("Microsoft YaHei", 10)
FONT_SMALL = ("Microsoft YaHei", 9)
FONT_MONO = ("Consolas", 9)


def _rounded(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（Apple 风格的基础图形）。"""
    steps = 8
    pts = []
    corners = (
        (x1 + r, y1 + r, 180, 270),
        (x2 - r, y1 + r, 270, 360),
        (x2 - r, y2 - r, 0, 90),
        (x1 + r, y2 - r, 90, 180),
    )
    for cx, cy, a0, a1 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / float(steps))
            pts.extend([cx + math.cos(a) * r, cy + math.sin(a) * r])
    return canvas.create_polygon(pts, smooth=False, **kw)


class RoundedButton(tk.Canvas):
    """圆角按钮：白底 + 浅色描边，悬停变浅灰。"""

    def __init__(self, parent, text, command, height=32, radius=8, min_width=0):
        self._font = tkfont.Font(font=FONT_UI)
        width = max(min_width, self._font.measure(text) + 36)
        super().__init__(
            parent, width=width, height=height, bg=parent.cget("bg"),
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._text = text
        self._cmd = command
        self._radius = radius
        self._enabled = True
        self._hover = False
        self._pressed = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = int(self.cget("width"))
        h = int(self.cget("height"))
        if not self._enabled:
            fill, outline, color = "#FAFAFB", BTN_DISABLED_LINE, TEXT_DISABLED
        elif self._pressed:
            fill, outline, color = BTN_PRESS, BTN_LINE, TEXT_MAIN
        elif self._hover:
            fill, outline, color = BTN_HOVER, BTN_LINE, TEXT_MAIN
        else:
            fill, outline, color = BTN_BG, BTN_LINE, TEXT_MAIN
        _rounded(self, 1, 1, w - 1, h - 1, self._radius, fill=fill, outline=outline, width=1)
        self.create_text(w / 2.0, h / 2.0, text=self._text, font=self._font, fill=color)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _on_enter(self, _=None):
        if self._enabled:
            self._hover = True
            self._draw()

    def _on_leave(self, _=None):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _=None):
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, _=None):
        if self._enabled and self._pressed:
            self._pressed = False
            self._draw()
            self._cmd()


class RoundedEntry(tk.Canvas):
    """圆角输入框：浅灰底，白卡片里也看得清。"""

    def __init__(self, parent, textvariable, font, height=34, radius=8,
                 justify="left", readonly=False, width=120):
        super().__init__(
            parent, width=width, height=height, bg=parent.cget("bg"),
            highlightthickness=0, bd=0,
        )
        self._radius = radius
        self._entry = tk.Entry(
            self, textvariable=textvariable, font=font, bd=0, relief="flat",
            bg=FIELD_BG, fg=TEXT_MAIN, insertbackground=TEXT_MAIN, justify=justify,
            readonlybackground=FIELD_BG, disabledbackground=FIELD_BG,
            disabledforeground=TEXT_MUTED, highlightthickness=0,
        )
        if readonly:
            self._entry.configure(state="readonly")
        self._win = self.create_window(12, height / 2.0, anchor="w", window=self._entry)
        self.bind("<Configure>", self._layout)
        self.bind("<Button-1>", lambda e: self._entry.focus_set())

    def _layout(self, event):
        self.delete("bg")
        _rounded(self, 0, 0, event.width, event.height, self._radius, fill=FIELD_BG, outline="")
        self.tag_lower("bg")
        self.coords(self._win, 12, event.height / 2.0)
        self.itemconfigure(self._win, width=max(20, event.width - 24))

    def set_enabled(self, enabled):
        self._entry.configure(state="normal" if enabled else "disabled")


class Card(tk.Canvas):
    """白色圆角卡片，内容放在 inner 里。"""

    def __init__(self, parent, padding=18, radius=14):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._padding = padding
        self._radius = radius
        self.inner = tk.Frame(self, bg=CARD_BG)
        self._win = self.create_window(padding, padding, anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", self._on_inner)
        self.bind("<Configure>", self._on_canvas)

    def _on_inner(self, _=None):
        height = self.inner.winfo_reqheight() + 2 * self._padding
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def _on_canvas(self, event):
        self.delete("bg")
        _rounded(self, 1, 1, event.width - 1, event.height - 1, self._radius,
                 fill=CARD_BG, outline=BORDER, width=1, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(60, event.width - 2 * self._padding))


class RoundedPanel(tk.Canvas):
    """可随窗口伸缩的圆角面板，内容放在 inner 中。"""

    def __init__(self, parent, padding=8, radius=12, fill=CARD_BG, outline=BORDER):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0, bd=0)
        self._padding = padding
        self._radius = radius
        self._fill = fill
        self._outline = outline
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(padding, padding, anchor="nw", window=self.inner)
        self.bind("<Configure>", self._on_canvas)

    def _on_canvas(self, event):
        self.delete("bg")
        _rounded(self, 1, 1, event.width - 1, event.height - 1, self._radius,
                 fill=self._fill, outline=self._outline, width=1, tags="bg")
        self.tag_lower("bg")
        self.coords(self._win, self._padding, self._padding)
        self.itemconfigure(
            self._win,
            width=max(20, event.width - 2 * self._padding),
            height=max(20, event.height - 2 * self._padding),
        )


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("%s %s" % (APP_NAME, APP_VERSION))
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        self.geometry("%dx%d" % (int(840 * scale), int(572 * scale)))
        self.minsize(int(780 * scale), int(532 * scale))
        self.configure(bg=PAGE_BG)
        self._set_icon()

        self.server = None
        self._atexit_done = False
        self.log_win = None
        self.log_text = None
        self.log_lines = [
            "%s %s  ·  作者：%s  %s" % (APP_NAME, APP_VERSION, APP_AUTHOR, APP_EMAIL)
        ]
        self.events = queue.Queue()
        self.path_var = tk.StringVar()
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.link_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="未启动")
        self.sent_var = tk.StringVar(value="0 B")
        self.speed_var = tk.StringVar(value="0 B/s")
        self.conn_var = tk.StringVar(value="0 / 0")

        self._speed_samples = collections.deque(maxlen=20)
        self._ips_urls = []
        self._flash_job = None
        self._logo = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self._tick)

    # -- 图标 ---------------------------------------------------------------
    def _set_icon(self):
        ico = os.path.join(resource_dir(), "logo.ico")
        if os.path.exists(ico):
            try:
                self.iconbitmap(ico)
            except Exception:
                pass

    # -- 布局 ---------------------------------------------------------------
    def _build(self):
        side = 26

        head = tk.Frame(self, bg=PAGE_BG)
        head.pack(fill="x", padx=side, pady=(22, 16))
        png = os.path.join(resource_dir(), "logo_64.png")
        if os.path.exists(png):
            try:
                self._logo = tk.PhotoImage(file=png).subsample(2, 2)
                tk.Label(head, image=self._logo, bg=PAGE_BG).pack(side="left", padx=(0, 14))
            except Exception:
                self._logo = None
        box = tk.Frame(head, bg=PAGE_BG)
        box.pack(side="left")
        tk.Label(box, text=APP_NAME, font=FONT_TITLE, bg=PAGE_BG, fg=TEXT_MAIN).pack(anchor="w")
        tk.Label(
            box,
            text="分享文件或整个文件夹到局域网，对方浏览器打开链接即可直接下载",
            font=FONT_SMALL, bg=PAGE_BG, fg=TEXT_MUTED,
        ).pack(anchor="w", pady=(3, 0))

        # 卡片一：共享内容 + 端口
        card = Card(self, padding=18)
        card.pack(fill="x", padx=side)
        body = card.inner

        tk.Label(body, text="共享内容", font=FONT_SMALL, bg=CARD_BG, fg=TEXT_MUTED).pack(anchor="w")
        row1 = tk.Frame(body, bg=CARD_BG)
        row1.pack(fill="x", pady=(7, 0))
        self.path_entry = RoundedEntry(row1, self.path_var, FONT_MONO, readonly=True, width=200)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.choose_btn = RoundedButton(row1, "选择", self._choose)
        self.choose_btn.pack(side="left", padx=(10, 0))

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(16, 14))

        row2 = tk.Frame(body, bg=CARD_BG)
        row2.pack(fill="x")
        tk.Label(row2, text="端口", font=FONT_SMALL, bg=CARD_BG, fg=TEXT_MUTED).pack(side="left", padx=(0, 12))
        self.port_entry = RoundedEntry(row2, self.port_var, FONT_MONO, justify="center", width=80)
        self.port_entry.pack(side="left")
        self.start_btn = RoundedButton(row2, "启动分享", self._start, min_width=90)
        self.start_btn.pack(side="left", padx=(12, 0))
        self.stop_btn = RoundedButton(row2, "停止", self._stop, min_width=70)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.log_btn = RoundedButton(row2, "传输日志", self._open_log, min_width=86)
        self.log_btn.pack(side="left", padx=(8, 0))
        self.stop_btn.set_enabled(False)

        # 卡片二：分享直链
        card2 = Card(self, padding=18)
        card2.pack(fill="x", padx=side, pady=(14, 0))
        body2 = card2.inner

        self.link_caption = tk.Label(
            body2, text="分享直链", font=FONT_SMALL, bg=CARD_BG, fg=TEXT_MUTED
        )
        self.link_caption.pack(anchor="w")
        row3 = tk.Frame(body2, bg=CARD_BG)
        row3.pack(fill="x", pady=(7, 0))
        self.link_label = tk.Label(
            row3, textvariable=self.link_var, font=("Consolas", 13), bg=CARD_BG,
            fg=TEXT_MAIN, anchor="w",
        )
        self.link_label.pack(side="left", fill="x", expand=True)
        self.copy_btn = RoundedButton(row3, "复制链接", self._copy, min_width=86)
        self.copy_btn.pack(side="left", padx=(10, 0))
        self.open_btn = RoundedButton(row3, "预览页面", self._open_page, min_width=86)
        self.open_btn.pack(side="left", padx=(8, 0))

        caprow = tk.Frame(body2, bg=CARD_BG)
        caprow.pack(fill="x", pady=(12, 0))
        self.ips_caption = tk.Label(
            caprow, text="其他网卡的地址", font=FONT_SMALL, bg=CARD_BG, fg=TEXT_MUTED
        )
        self.ips_caption.pack(side="left")
        self.copy_all_btn = RoundedButton(caprow, "复制全部", self._copy_all_ips, height=24, min_width=66)
        self.copy_all_btn.pack(side="right")
        self.copy_all_btn.set_enabled(False)

        self.ips_box = tk.Text(
            body2, height=1, width=1, font=FONT_MONO, bg=CARD_BG, fg=TEXT_MUTED,
            relief="flat", bd=0, highlightthickness=0, wrap="none", cursor="hand2",
            padx=0, pady=0, state="disabled", spacing1=2, spacing3=2,
        )
        self.ips_box.pack(fill="x", pady=(4, 0))
        self.ips_box.bind("<Button-1>", self._copy_line)

        # 状态
        stats = tk.Frame(self, bg=PAGE_BG)
        stats.pack(fill="x", padx=side, pady=(18, 0))
        self._stat(stats, "状态", self.status_var).pack(side="left", padx=(0, 30))
        self._stat(stats, "已传输", self.sent_var).pack(side="left", padx=(0, 30))
        self._stat(stats, "实时速度", self.speed_var).pack(side="left", padx=(0, 30))
        self._stat(stats, "在线/累计", self.conn_var).pack(side="left")

    def _stat(self, parent, title, var):
        box = tk.Frame(parent, bg=PAGE_BG)
        tk.Label(box, text=title, font=FONT_SMALL, bg=PAGE_BG, fg=TEXT_MUTED).pack(anchor="w")
        tk.Label(box, textvariable=var, font=("Consolas", 11), bg=PAGE_BG, fg=TEXT_MAIN).pack(anchor="w")
        return box

    # -- 选择共享内容 -------------------------------------------------------
    def _choose(self):
        """一个「选择」按钮，弹出菜单区分文件 / 文件夹。"""
        menu = tk.Menu(
            self, tearoff=0, bg=CARD_BG, fg=TEXT_MAIN, activebackground=FIELD_BG,
            activeforeground=TEXT_MAIN, relief="solid", bd=1, activeborderwidth=0,
            font=FONT_UI,
        )
        menu.add_command(label="选择文件", command=self._pick_file)
        menu.add_command(label="选择文件夹", command=self._pick_dir)
        self._choose_menu = menu
        x = self.choose_btn.winfo_rootx()
        y = self.choose_btn.winfo_rooty() + self.choose_btn.winfo_height() + 2
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _pick_file(self):
        path = filedialog.askopenfilename(title="选择要分享的文件")
        if path:
            self.path_var.set(path)
            self._write_log(
                "已选择文件  %s  (%s)" % (os.path.basename(path), human_size(os.path.getsize(path)))
            )

    def _pick_dir(self):
        path = filedialog.askdirectory(title="选择要分享的文件夹（含全部子目录）", mustexist=True)
        if path:
            path = os.path.normpath(path)
            self.path_var.set(path)
            try:
                count = sum(len(files) for _, _, files in os.walk(path))
            except OSError:
                count = 0
            self._write_log("已选择文件夹  %s  (%d 个文件)" % (path, count))

    def _start(self):
        path = self.path_var.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning(APP_NAME, "请先选择要分享的文件或文件夹")
            return
        try:
            port = int(self.port_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showwarning(APP_NAME, "端口需为 1-65535 之间的整数")
            return

        server = ShareServer(path, port, self.events)
        try:
            server.start()
        except OSError as exc:
            self._write_log("启动失败  %s" % exc)
            messagebox.showerror(
                APP_NAME, "端口 %d 启动失败：%s\n\n可能已被占用，请换一个端口。" % (port, exc)
            )
            return

        self.server = server
        if not self._atexit_done:
            atexit.register(self._release)
            self._atexit_done = True

        ads = get_lan_ads()
        host = ads[0][1] if ads else "127.0.0.1"
        if server.is_dir:
            self.link_var.set("http://%s:%d/" % (host, port))
            self.link_caption.configure(text="分享直链 · 可浏览全部内容，也可一键打包下载")
        else:
            self.link_var.set("http://%s:%d/download" % (host, port))
            self.link_caption.configure(text="分享直链 · 对方浏览器直接下载")
        others = [
            ("http://%s:%d/" % (ip, port), name) for name, ip in ads[1:]
        ]
        if others:
            self._fill_ips(
                ["%-26s %s" % (url, name) for url, name in others],
                [url for url, _ in others],
            )
        else:
            self._fill_ips(["本机地址  http://127.0.0.1:%d/" % port], ["http://127.0.0.1:%d/" % port])
        self._fit_window()
        self.status_var.set("运行中")
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.choose_btn.set_enabled(False)
        self.port_entry.set_enabled(False)
        self._speed_samples.clear()
        self._speed_samples.append((time.time(), 0))
        self._write_log("共享路径  %s" % path)

    def _stop(self):
        if self.server:
            self.server.stop()
            self.server = None
        self.status_var.set("已停止")
        self.sent_var.set("0 B")
        self.speed_var.set("0 B/s")
        self.conn_var.set("0 / 0")
        self._speed_samples.clear()
        self.start_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        self.choose_btn.set_enabled(True)
        self.port_entry.set_enabled(True)

    def _copy(self):
        url = self.link_var.get()
        if not url:
            messagebox.showinfo(APP_NAME, "请先启动分享")
            return
        self.clipboard_clear()
        self.clipboard_append(url)
        self._write_log("已复制链接  %s" % url)

    # -- 网卡地址列表（全部显示、点击即复制） --------------------------------
    def _fill_ips(self, lines, urls):
        self._ips_urls = list(urls)
        self.ips_box.configure(state="normal")
        self.ips_box.delete("1.0", "end")
        self.ips_box.insert("1.0", "\n".join(lines))
        self.ips_box.configure(state="disabled", height=max(1, min(12, len(lines))))
        self.copy_all_btn.set_enabled(bool(urls))

    def _copy_line(self, event):
        if not self._ips_urls:
            return "break"
        index = self.ips_box.index("@%d,%d" % (event.x, event.y))
        row = int(index.split(".")[0]) - 1
        if 0 <= row < len(self._ips_urls):
            self._copy_text(self._ips_urls[row])
            self._flash("已复制  " + self._ips_urls[row])
        return "break"

    def _copy_all_ips(self):
        if not self._ips_urls:
            return
        self._copy_text("\n".join(self._ips_urls))
        self._flash("已复制全部 %d 个地址" % len(self._ips_urls))

    def _copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _flash(self, msg):
        if self._flash_job:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass

        self.ips_caption.configure(text=msg, fg=TEXT_MAIN)

        def restore():
            self.ips_caption.configure(text="其他网卡的地址", fg=TEXT_MUTED)
            self._flash_job = None

        self._flash_job = self.after(1800, restore)

    def _fit_window(self):
        """按内容自动调整窗口高度，网卡多时也不会被裁掉。"""
        self.update_idletasks()
        scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
        need = self.winfo_reqheight()
        max_h = self.winfo_screenheight() - int(90 * scale)
        height = max(int(572 * scale), min(need + int(6 * scale), max_h))
        if abs(self.winfo_height() - height) > 2:
            self.geometry("%dx%d" % (self.winfo_width(), int(height)))

    def _open_page(self):
        if self.server is None:
            messagebox.showinfo(APP_NAME, "请先启动分享")
            return
        webbrowser.open("http://127.0.0.1:%d/" % self.server.port)

    def _release(self):
        """退出时释放端口与所有共享连接。"""
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

    def _on_close(self):
        self._release()
        self.destroy()

    # -- 定时刷新 -----------------------------------------------------------
    def _tick(self):
        while True:
            try:
                kind, text = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._write_log(text)

        if self.server is not None:
            sent, active, total = self.server.snapshot()
            now = time.time()
            self._speed_samples.append((now, sent))
            if len(self._speed_samples) > 1:
                t0, b0 = self._speed_samples[0]
                span = now - t0
                if span > 0.2:
                    self.speed_var.set(human_size((sent - b0) / span) + "/s")
            self.sent_var.set(human_size(sent))
            self.conn_var.set("%d / %d" % (active, total))

        self.after(300, self._tick)

    def _write_log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        self.log_lines.append(line)
        if len(self.log_lines) > 3000:
            del self.log_lines[:1000]
        if self.log_text is not None:
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", line + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except tk.TclError:
                self.log_text = None

    # -- 传输日志窗口 -------------------------------------------------------
    def _open_log(self):
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.deiconify()
            self.log_win.lift()
            self.log_win.focus_force()
            return

        win = tk.Toplevel(self)
        win.title("传输日志 · %s %s" % (APP_NAME, APP_VERSION))
        win.configure(bg=PAGE_BG)
        win.geometry(
            "700x430+%d+%d" % (self.winfo_rootx() + 40, self.winfo_rooty() + 70)
        )
        win.minsize(520, 320)
        ico = os.path.join(resource_dir(), "logo.ico")
        if os.path.exists(ico):
            try:
                win.iconbitmap(ico)
            except Exception:
                pass

        head = tk.Frame(win, bg=PAGE_BG)
        head.pack(fill="x", padx=20, pady=(16, 0))
        tk.Label(
            head, text="传输日志", font=("Microsoft YaHei", 13, "bold"), bg=PAGE_BG, fg=TEXT_MAIN
        ).pack(side="left")
        RoundedButton(head, "关闭", self._close_log, min_width=64).pack(side="right")
        RoundedButton(head, "清空", self._clear_log, min_width=64).pack(side="right", padx=(0, 8))

        panel = RoundedPanel(win, padding=8, radius=12)
        panel.pack(fill="both", expand=True, padx=20, pady=(12, 18))
        box = panel.inner
        text = tk.Text(
            box, font=FONT_MONO, bg=CARD_BG, fg="#3A3A3C", relief="flat", bd=0,
            wrap="none", padx=12, pady=10, highlightthickness=0,
        )
        sb = tk.Scrollbar(box, command=text.yview, relief="flat", bd=0, width=10)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        if self.log_lines:
            text.insert("end", "\n".join(self.log_lines) + "\n")
        text.see("end")
        text.configure(state="disabled")

        self.log_text = text
        self.log_win = win
        win.protocol("WM_DELETE_WINDOW", self._close_log)

    def _clear_log(self):
        self.log_lines.clear()
        if self.log_text is not None:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

    def _close_log(self):
        if self.log_win is not None:
            try:
                self.log_win.destroy()
            except Exception:
                pass
        self.log_win = None
        self.log_text = None


def main():
    enable_dpi_awareness()
    app = App()
    try:
        app.mainloop()
    finally:
        app._release()


if __name__ == "__main__":
    main()
