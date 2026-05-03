#!/usr/bin/env python3
"""
WiiFlow Manager — servidor backend
Ejecuta: python3 wii-manager-server.py
Luego abre: http://localhost:8765
"""

import http.server
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

PORT = 8765

# ─── Utilidades ──────────────────────────────────────────────

def which(cmd):
    return shutil.which(cmd)

def run(cmd, timeout=120):
    """Ejecuta un comando y devuelve (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding='utf-8', errors='replace'
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return '', 'Timeout: el comando tardó demasiado', 1
    except Exception as e:
        return '', str(e), 1

def part_flag(part):
    """Devuelve '-p "<part>"' si hay ruta, o '--auto' si no."""
    if part and part.strip():
        return f'-p "{part.strip()}"'
    return '--auto'

# ─── Parser de salida de wwt LIST ────────────────────────────

def parse_wwt_list(output):
    """
    Parsea la salida de: wwt LIST -p <part> --long
    Formato típico:
      1  RMCP01  4.37 GiB  Mario Kart Wii
    """
    games = []

    pattern = re.compile(
        r'^\s*\d+\s+'
        r'([A-Z0-9]{4,6})\s+'
        r'(?:[^\s]+\s+)?'
        r'([\d.]+)\s*(GiB|MiB|GB|MB)\s+'
        r'(.+?)\s*$',
        re.MULTILINE
    )
    for m in pattern.finditer(output):
        game_id  = m.group(1).strip()
        size_raw = float(m.group(2))
        unit     = m.group(3).upper()
        title    = m.group(4).strip()
        if not title or title.startswith('#'):
            continue
        if 'MIB' in unit or unit == 'MB':
            size_raw /= 1024
        games.append({
            'id':     game_id,
            'title':  title,
            'size':   round(size_raw, 2),
            'fmt':    'wbfs',
            'region': region_from_id(game_id),
            'year':   0,
        })

    # Fallback: formato simplificado sin tamaño
    if not games:
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('='):
                continue
            m2 = re.match(r'^([A-Z0-9]{4,6})\s+(.+)$', line)
            if m2:
                games.append({
                    'id':     m2.group(1),
                    'title':  m2.group(2).strip(),
                    'size':   0,
                    'fmt':    'wbfs',
                    'region': region_from_id(m2.group(1)),
                    'year':   0,
                })
    return games

def region_from_id(game_id):
    if len(game_id) < 4:
        return '?'
    return {
        'P': 'PAL',    'E': 'NTSC-U', 'J': 'NTSC-J',
        'K': 'NTSC-K', 'W': 'NTSC-T', 'D': 'PAL-DE',
        'F': 'PAL-FR', 'S': 'PAL-ES', 'I': 'PAL-IT',
    }.get(game_id[3], 'UNK')

# ─── Parser de wwt SPACE ─────────────────────────────────────

def to_gib(value, unit):
    v    = float(value)
    unit = unit.strip().upper()
    if unit in ('MIB', 'MB'): return v / 1024
    if unit in ('KIB', 'KB'): return v / (1024 * 1024)
    if unit in ('TIB', 'TB'): return v * 1024
    return v  # GiB / GB

def parse_wwt_space(output):
    """
    Acepta múltiples formatos de salida de wwt SPACE:

      Clave = valor:
        total = 120.00 GiB
        used  =  45.32 GiB
        free  =  74.68 GiB

      Valor + etiqueta inline:
        45.32 GiB used   74.68 GiB free   120.00 GiB total

      Devuelve (used_gib, free_gib, total_gib).
    """
    total = used = free = 0.0

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # "total = 120.00 GiB" / "total: 120.00 GiB"
        m = re.search(r'\btotal\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)', line, re.I)
        if m: total = to_gib(m.group(1), m.group(2))

        m = re.search(r'\bused\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)', line, re.I)
        if m: used = to_gib(m.group(1), m.group(2))

        m = re.search(r'\bfree\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)', line, re.I)
        if m: free = to_gib(m.group(1), m.group(2))

        # "45.32 GiB used" / "45.32 GiB free" / "120.00 GiB total"
        for val, unit, label in re.findall(
                r'([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)\s+(used|free|total)', line, re.I):
            g = to_gib(val, unit)
            lbl = label.lower()
            if   lbl == 'used':  used  = g
            elif lbl == 'free':  free  = g
            elif lbl == 'total': total = g

    # Derivar el que falte
    if total and not free and used:  free  = total - used
    if total and not used and free:  used  = total - free
    if used  and free  and not total: total = used + free

    return round(used, 2), round(free, 2), round(total, 2)

# ─── API handlers ─────────────────────────────────────────────

def api_status(params):
    wwt = which('wwt') or ''
    wit = which('wit')  or ''
    return {
        'wwt':     wwt,
        'wit':     wit,
        'wwt_ok':  bool(wwt),
        'wit_ok':  bool(wit),
    }

def api_list(params):
    part = params.get('part', '').strip()
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    flag = part_flag(part)

    # LIST con --long para obtener tamaños
    cmd = f'{wwt} LIST {flag} --long'
    stdout, stderr, rc = run(cmd)

    # Si falla con --long, intentar sin
    if rc != 0:
        cmd = f'{wwt} LIST {flag}'
        stdout, stderr, rc = run(cmd)

    games = parse_wwt_list(stdout)

    # SPACE para espacio en disco
    cmd_sp  = f'{wwt} SPACE {flag}'
    sp_out, sp_err, _ = run(cmd_sp)
    used, free, total = parse_wwt_space(sp_out)

    return {
        'games':      games,
        'used_gb':    used,
        'free_gb':    free,
        'total_gb':   total,
        'stdout':     stdout,
        'stderr':     stderr,
        'rc':         rc,
        'cmd':        cmd,
        # Incluir salida raw de SPACE para depuración desde el terminal de la GUI
        'space_raw':  sp_out.strip() or sp_err.strip(),
    }

def api_add(params):
    part = params.get('part', '').strip()
    src  = params.get('src',  '')
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    opts = params.get('opts', '')
    if not src:
        return {'error': 'Falta el parámetro src'}
    flag = part_flag(part)
    cmd  = f'{wwt} ADD {flag} {opts} "{src}"'
    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

def api_remove(params):
    part    = params.get('part', '').strip()
    game_id = params.get('id',   '')
    wwt     = params.get('wwt',  which('wwt') or 'wwt')
    if not game_id:
        return {'error': 'Falta el parámetro id'}
    flag = part_flag(part)
    cmd  = f'{wwt} REMOVE {flag} {game_id}'
    stdout, stderr, rc = run(cmd)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

def api_extract(params):
    part    = params.get('part', '').strip()
    game_id = params.get('id',   '')
    dest    = params.get('dest', '/tmp/')
    wwt     = params.get('wwt',  which('wwt') or 'wwt')
    opts    = params.get('opts', '')
    if not game_id:
        return {'error': 'Falta el parámetro id'}
    flag = part_flag(part)
    cmd  = f'{wwt} EXTRACT {flag} {opts} {game_id} --dest "{dest}"'
    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

def api_verify(params):
    part = params.get('part', '').strip()
    src  = params.get('src',  '')
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    wit  = params.get('wit',  which('wit')  or 'wit')
    if src:
        cmd = f'{wit} VERIFY "{src}"'
    else:
        flag = part_flag(part)
        cmd  = f'{wwt} VERIFY {flag}'
    stdout, stderr, rc = run(cmd, timeout=300)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

def api_run(params):
    """Ejecuta un comando arbitrario (solo wwt/wit por seguridad)."""
    cmd   = params.get('cmd', '').strip()
    if not cmd:
        return {'error': 'Sin comando'}
    first = cmd.split()[0].lower()
    if not any(first.endswith(t) for t in ('wwt', 'wit', 'wdf')):
        return {'error': f'Solo se permiten comandos wwt/wit, recibido: {first}'}
    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

ROUTES = {
    '/api/status':  api_status,
    '/api/list':    api_list,
    '/api/add':     api_add,
    '/api/remove':  api_remove,
    '/api/extract': api_extract,
    '/api/verify':  api_verify,
    '/api/run':     api_run,
}

# ─── HTTP Handler ─────────────────────────────────────────────

HTML_FILE = Path(__file__).parent / 'wii-manager.html'

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f'  {self.address_string()} {fmt % args}')

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path in ROUTES:
            result = ROUTES[path](params)
            self.send_json(result)
            return

        if path in ('/', '/index.html', '/wii-manager.html'):
            if HTML_FILE.exists():
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({'error': 'wii-manager.html no encontrado'}, 404)
            return

        self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'
        try:
            params = json.loads(body)
        except Exception:
            params = dict(urllib.parse.parse_qsl(body.decode()))

        if path in ROUTES:
            result = ROUTES[path](params)
            self.send_json(result)
            return

        self.send_json({'error': 'Not found'}, 404)

# ─── Main ─────────────────────────────────────────────────────

def main():
    print('=' * 54)
    print('  WiiFlow Manager — Backend')
    print(f'  http://localhost:{PORT}')
    print('  Ctrl+C para detener')
    print('=' * 54)

    wwt = which('wwt')
    wit = which('wit')
    print(f'  wit : {wit  or "⚠ NO ENCONTRADO"}')
    print(f'  wwt : {wwt or "⚠ NO ENCONTRADO"}')
    print()

    if not HTML_FILE.exists():
        print(f'  ⚠  No se encuentra {HTML_FILE}')
        print(f'     Asegúrate de que wii-manager.html está en el mismo directorio.')
        print()

    server = http.server.HTTPServer(('127.0.0.1', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Servidor detenido.')
        server.server_close()

if __name__ == '__main__':
    main()
