#!/usr/bin/env python3
"""
wii-manager-server.py
=====================
Servidor HTTP local que actúa como backend de la GUI WiiFlow Manager.

Expone una API REST minimalista en http://localhost:8765 que traduce las
peticiones del frontend en llamadas reales a los ejecutables `wit` y `wwt`
de Wiimm (https://wit.wiimm.de/).

Uso:
    python3 wii-manager-server.py

El archivo wii-manager.html debe estar en el mismo directorio; el servidor
lo sirve directamente en la ruta raíz (/).

Rutas de la API:
    GET  /api/status            Detecta rutas de wit y wwt en el sistema
    GET  /api/list?part=...     Lista juegos y espacio en la partición WBFS
    POST /api/add               Añade un ISO a la partición WBFS
    POST /api/remove            Elimina un juego por su Game ID
    POST /api/extract           Extrae un juego a fichero ISO
    POST /api/verify            Verifica la integridad de juegos o archivos
    POST /api/run               Ejecuta un comando wwt/wit arbitrario

Seguridad:
    El servidor escucha solo en 127.0.0.1 (localhost), no es accesible
    desde la red. El endpoint /api/run rechaza cualquier comando que no
    empiece por 'wwt', 'wit' o 'wdf'.

Licencia: GNU GPL v3
"""

import http.server
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

# Puerto en el que escucha el servidor
PORT = 8765

# Ruta al HTML del frontend (mismo directorio que este script)
HTML_FILE = Path(__file__).parent / 'wii-manager.html'


# ══════════════════════════════════════════════════════════════
#  UTILIDADES GENERALES
# ══════════════════════════════════════════════════════════════

def which(cmd):
    """
    Devuelve la ruta absoluta de un ejecutable buscándolo en el PATH,
    o None si no existe. Equivale a `which <cmd>` en bash.
    """
    return shutil.which(cmd)


def run(cmd, timeout=120):
    """
    Ejecuta un comando de shell y captura su salida.

    Parámetros:
        cmd     -- comando completo como string (se ejecuta con shell=True)
        timeout -- segundos máximos de espera (default: 120)

    Devuelve:
        (stdout, stderr, returncode)
        En caso de timeout o excepción devuelve returncode=1 y el error
        en stderr para que el frontend pueda mostrarlo en el terminal.
    """
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding='utf-8',
            errors='replace'    # caracteres inválidos → replacement char
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return '', f'Timeout: el comando tardó más de {timeout}s', 1
    except Exception as e:
        return '', str(e), 1


def part_flag(part):
    """
    Construye el flag de partición para los comandos wwt.

    Si se ha configurado una ruta (ej. '/dev/sdb1' o '/ruta/disco.wbfs')
    devuelve '-p "<ruta>"'. Si la ruta está vacía, devuelve '--auto' para
    que wwt detecte automáticamente la partición WBFS montada.

    Ejemplos:
        part_flag('/dev/sdb1')  →  '-p "/dev/sdb1"'
        part_flag('')           →  '--auto'
        part_flag(None)         →  '--auto'
    """
    if part and part.strip():
        return f'-p "{part.strip()}"'
    return '--auto'


# ══════════════════════════════════════════════════════════════
#  PARSERS DE SALIDA DE wwt
# ══════════════════════════════════════════════════════════════

def parse_wwt_list(output):
    """
    Parsea la salida de texto de 'wwt LIST --long' y devuelve una lista
    de diccionarios con la información de cada juego.

    Formato esperado (--long):
        1  RMCP01  4.37 GiB  Mario Kart Wii
        2  SOUE01  7.92 GiB  Super Smash Bros. Brawl

    Si el formato largo falla (algunos builds de wwt omiten el tamaño),
    se intenta un formato simplificado:
        RMCP01  Mario Kart Wii

    Cada juego se devuelve como:
        {
            'id':     'RMCP01',       # Game ID de 4-6 caracteres
            'title':  'Mario Kart Wii',
            'size':   4.37,           # Tamaño en GiB (0 si no disponible)
            'fmt':    'wbfs',         # Siempre 'wbfs' (formato de partición)
            'region': 'PAL',          # Derivado del 4º carácter del ID
            'year':   0,              # wwt no devuelve el año
        }
    """
    games = []

    # Regex para formato largo: índice, ID, tamaño+unidad, título
    pattern = re.compile(
        r'^\s*\d+\s+'               # número de índice (1, 2, 3…)
        r'([A-Z0-9]{4,6})\s+'       # Game ID (ej. RMCP01)
        r'(?:[^\s]+\s+)?'           # ruta opcional (algunos builds la incluyen)
        r'([\d.]+)\s*(GiB|MiB|GB|MB)\s+'  # tamaño y unidad
        r'(.+?)\s*$',               # título hasta fin de línea
        re.MULTILINE
    )
    for m in pattern.finditer(output):
        game_id  = m.group(1).strip()
        size_raw = float(m.group(2))
        unit     = m.group(3).upper()
        title    = m.group(4).strip()

        # Ignorar líneas de cabecera o comentarios
        if not title or title.startswith('#'):
            continue

        # Normalizar a GiB si viene en MiB o MB
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

    # Fallback: formato simplificado sin tamaño (wwt sin --long)
    if not games:
        for line in output.splitlines():
            line = line.strip()
            # Saltar líneas vacías, comentarios y separadores
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
    """
    Deriva la región de un juego Wii a partir del 4º carácter de su Game ID.

    El sistema de IDs de Nintendo codifica la región en la posición 3 (0-based):
        RMCP01  →  'P'  →  PAL
        RMCE01  →  'E'  →  NTSC-U (USA)
        RMCJ01  →  'J'  →  NTSC-J (Japón)

    Devuelve 'UNK' si el carácter no está en la tabla conocida,
    o '?' si el ID es demasiado corto.
    """
    if len(game_id) < 4:
        return '?'
    return {
        'P': 'PAL',     # Europa
        'E': 'NTSC-U',  # USA
        'J': 'NTSC-J',  # Japón
        'K': 'NTSC-K',  # Corea
        'W': 'NTSC-T',  # Taiwan
        'D': 'PAL-DE',  # Alemania
        'F': 'PAL-FR',  # Francia
        'S': 'PAL-ES',  # España
        'I': 'PAL-IT',  # Italia
    }.get(game_id[3], 'UNK')


def to_gib(value, unit):
    """
    Convierte un valor de tamaño a GiB independientemente de la unidad
    de origen.

    Parámetros:
        value -- string o número con el valor numérico
        unit  -- string con la unidad (GiB, MiB, GB, MB, TiB, TB, KiB, KB)

    Devuelve:
        float en GiB
    """
    v    = float(value)
    unit = unit.strip().upper()
    if unit in ('MIB', 'MB'):  return v / 1024
    if unit in ('KIB', 'KB'):  return v / (1024 * 1024)
    if unit in ('TIB', 'TB'):  return v * 1024
    return v  # GiB o GB, ya en la unidad correcta


def parse_wwt_space(output):
    """
    Parsea la salida de 'wwt SPACE' y extrae el espacio usado, libre y total
    de la partición WBFS.

    wwt puede producir varios formatos según la versión:

        Formato clave = valor:
            total = 120.00 GiB
            used  =  45.32 GiB
            free  =  74.68 GiB

        Formato valor + etiqueta inline (en la misma línea):
            45.32 GiB used   74.68 GiB free   120.00 GiB total

    El parser intenta ambos formatos en cada línea y acepta cualquier
    combinación de unidades (GiB, MiB, TiB, GB, MB, TB, KiB, KB).

    Si alguno de los tres valores no aparece en la salida, lo deriva
    matemáticamente de los otros dos.

    Devuelve:
        (used_gib, free_gib, total_gib)  — floats redondeados a 2 decimales
    """
    total = used = free = 0.0

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # ── Formato "clave = valor unidad" ──────────────────────
        m = re.search(
            r'\btotal\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: total = to_gib(m.group(1), m.group(2))

        m = re.search(
            r'\bused\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: used = to_gib(m.group(1), m.group(2))

        m = re.search(
            r'\bfree\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: free = to_gib(m.group(1), m.group(2))

        # ── Formato "valor unidad etiqueta" ─────────────────────
        for val, unit, label in re.findall(
                r'([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)\s+(used|free|total)',
                line, re.I):
            g   = to_gib(val, unit)
            lbl = label.lower()
            if   lbl == 'used':  used  = g
            elif lbl == 'free':  free  = g
            elif lbl == 'total': total = g

    # Derivar el valor que falte si solo hay dos
    if total and not free and used:   free  = total - used
    if total and not used and free:   used  = total - free
    if used  and free  and not total: total = used  + free

    return round(used, 2), round(free, 2), round(total, 2)


# ══════════════════════════════════════════════════════════════
#  HANDLERS DE LA API REST
# ══════════════════════════════════════════════════════════════

def api_status(params):
    """
    GET /api/status

    Detecta si wit y wwt están instalados en el sistema usando `which`.

    Respuesta JSON:
        {
            "wwt":     "/usr/bin/wwt",  # ruta o "" si no se encuentra
            "wit":     "/usr/bin/wit",
            "wwt_ok":  true,
            "wit_ok":  true
        }
    """
    wwt = which('wwt') or ''
    wit = which('wit')  or ''
    return {
        'wwt':    wwt,
        'wit':    wit,
        'wwt_ok': bool(wwt),
        'wit_ok': bool(wit),
    }


def api_list(params):
    """
    GET /api/list?part=<ruta>&wwt=<ruta_wwt>

    Lista todos los juegos de la partición WBFS y el espacio en disco.
    Ejecuta 'wwt LIST --long' (con fallback sin --long) y 'wwt SPACE'.

    Parámetros GET:
        part  -- ruta de la partición (ej. '/dev/sdb1'). Vacío = --auto
        wwt   -- ruta al ejecutable wwt (opcional, por defecto detectado)

    Respuesta JSON:
        {
            "games":     [ { id, title, size, fmt, region, year }, ... ],
            "used_gb":   45.32,
            "free_gb":   74.68,
            "total_gb":  120.0,
            "stdout":    "...",   # salida raw de wwt LIST (para el terminal)
            "stderr":    "...",
            "rc":        0,       # código de retorno de wwt
            "cmd":       "wwt LIST -p /dev/sdb1 --long",
            "space_raw": "..."    # salida raw de wwt SPACE (para depuración)
        }
    """
    part = params.get('part', '').strip()
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    flag = part_flag(part)

    # Intentar primero con --long para obtener tamaños
    cmd = f'{wwt} LIST {flag} --long'
    stdout, stderr, rc = run(cmd)

    # Si falla (código de error), reintentar sin --long
    if rc != 0:
        cmd = f'{wwt} LIST {flag}'
        stdout, stderr, rc = run(cmd)

    games = parse_wwt_list(stdout)

    # Obtener espacio en disco de la partición
    cmd_sp = f'{wwt} SPACE {flag}'
    sp_out, sp_err, _ = run(cmd_sp)
    used, free, total = parse_wwt_space(sp_out)

    return {
        'games':     games,
        'used_gb':   used,
        'free_gb':   free,
        'total_gb':  total,
        'stdout':    stdout,
        'stderr':    stderr,
        'rc':        rc,
        'cmd':       cmd,
        'space_raw': sp_out.strip() or sp_err.strip(),
    }


def api_add(params):
    """
    POST /api/add

    Añade un archivo ISO/WBFS a la partición WBFS usando 'wwt ADD'.
    Este comando puede tardar varios minutos para ISOs grandes;
    el timeout está fijado en 600 segundos (10 minutos).

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",        # partición destino (vacío = --auto)
            "src":  "/ruta/juego.iso",  # archivo origen (obligatorio)
            "opts": "",                 # opciones adicionales para wwt
            "wwt":  "/usr/bin/wwt"      # ruta al ejecutable (opcional)
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
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
    """
    POST /api/remove

    Elimina un juego de la partición WBFS por su Game ID usando 'wwt REMOVE'.

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",  # partición (vacío = --auto)
            "id":   "RMCP01",     # Game ID del juego a eliminar (obligatorio)
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
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
    """
    POST /api/extract

    Extrae un juego de la partición WBFS a un archivo ISO usando 'wwt EXTRACT'.
    El timeout es de 600 segundos por el tamaño de los ISOs de Wii.

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",    # partición origen (vacío = --auto)
            "id":   "RMCP01",       # Game ID a extraer (obligatorio)
            "dest": "/tmp/",        # directorio destino (default: /tmp/)
            "opts": "",             # opciones adicionales (ej. "--wbfs")
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
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
    """
    POST /api/verify

    Verifica la integridad de juegos. Tiene dos modos:

    - Si se proporciona 'src' (ruta a un archivo): usa 'wit VERIFY <src>'
      para verificar un ISO o WBFS individual.
    - Si no hay 'src' pero sí 'part': usa 'wwt VERIFY <flag>' para
      verificar todos los juegos de la partición WBFS.

    Cuerpo JSON:
        {
            "src":  "/ruta/juego.iso",  # archivo a verificar (opcional)
            "part": "/dev/sdb1",        # partición (usado si no hay src)
            "wit":  "/usr/bin/wit",
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
    part = params.get('part', '').strip()
    src  = params.get('src',  '')
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    wit  = params.get('wit',  which('wit')  or 'wit')

    if src:
        # Verificar un archivo concreto con wit
        cmd = f'{wit} VERIFY "{src}"'
    else:
        # Verificar toda la partición con wwt
        flag = part_flag(part)
        cmd  = f'{wwt} VERIFY {flag}'

    stdout, stderr, rc = run(cmd, timeout=300)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_run(params):
    """
    POST /api/run

    Ejecuta un comando wwt/wit arbitrario construido por el frontend
    (usado principalmente para conversiones en lote).

    Por seguridad, solo se permiten comandos cuyo primer token termine
    en 'wwt', 'wit' o 'wdf'. Cualquier otro ejecutable es rechazado
    con un error sin ejecutar nada.

    Cuerpo JSON:
        { "cmd": "wwt ADD -p /dev/sdb1 /ruta/*.iso" }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
        o
        { "error": "Solo se permiten comandos wwt/wit" }
    """
    cmd = params.get('cmd', '').strip()
    if not cmd:
        return {'error': 'Sin comando'}

    # Validación de seguridad: solo wwt, wit o wdf
    first = cmd.split()[0].lower()
    if not any(first.endswith(t) for t in ('wwt', 'wit', 'wdf')):
        return {'error': f'Solo se permiten comandos wwt/wit, recibido: {first}'}

    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


# Tabla de enrutamiento: ruta URL → función handler
ROUTES = {
    '/api/status':  api_status,
    '/api/list':    api_list,
    '/api/add':     api_add,
    '/api/remove':  api_remove,
    '/api/extract': api_extract,
    '/api/verify':  api_verify,
    '/api/run':     api_run,
}


# ══════════════════════════════════════════════════════════════
#  SERVIDOR HTTP
# ══════════════════════════════════════════════════════════════

class Handler(http.server.BaseHTTPRequestHandler):
    """
    Manejador HTTP para el servidor local de WiiFlow Manager.

    Gestiona tres tipos de peticiones:
        - GET  /api/*  → llamada a la API (parámetros en query string)
        - POST /api/*  → llamada a la API (parámetros en body JSON)
        - GET  /       → sirve el archivo wii-manager.html
        - OPTIONS *    → responde headers CORS para peticiones preflight
    """

    def log_message(self, fmt, *args):
        """Sobreescribe el log por defecto para formato más limpio."""
        print(f'  {self.address_string()} {fmt % args}')

    def send_json(self, data, status=200):
        """
        Serializa 'data' a JSON y lo envía como respuesta HTTP.
        Incluye headers CORS para permitir peticiones desde el frontend.
        """
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """
        Responde a peticiones OPTIONS (preflight CORS).
        Necesario para que el navegador permita las llamadas fetch() al API.
        """
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        """
        Maneja peticiones GET.
        - Rutas /api/* → ejecuta el handler correspondiente de ROUTES.
        - Ruta raíz /  → sirve wii-manager.html.
        - Resto        → 404.
        """
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path in ROUTES:
            result = ROUTES[path](params)
            self.send_json(result)
            return

        # Servir el frontend HTML
        if path in ('/', '/index.html', '/wii-manager.html'):
            if HTML_FILE.exists():
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(
                    {'error': f'No se encuentra {HTML_FILE.name}. '
                              f'Asegúrate de que está en el mismo directorio.'},
                    404
                )
            return

        self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        """
        Maneja peticiones POST.
        Lee el body como JSON (o como form-urlencoded como fallback)
        y llama al handler correspondiente de ROUTES.
        """
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'

        # Intentar parsear como JSON, fallback a form-urlencoded
        try:
            params = json.loads(body)
        except Exception:
            params = dict(urllib.parse.parse_qsl(body.decode()))

        if path in ROUTES:
            result = ROUTES[path](params)
            self.send_json(result)
            return

        self.send_json({'error': 'Not found'}, 404)


# ══════════════════════════════════════════════════════════════
#  PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════════════

def main():
    """
    Arranca el servidor HTTP y muestra información de diagnóstico:
    - URL de acceso
    - Rutas detectadas de wit y wwt
    - Advertencia si el HTML no se encuentra en el directorio
    """
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

    # Escuchar solo en localhost por seguridad
    server = http.server.HTTPServer(('127.0.0.1', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Servidor detenido.')
        server.server_close()


if __name__ == '__main__':
    main()
