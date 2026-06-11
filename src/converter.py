import os
import json
import logging
import shutil
import subprocess
import threading

import requests

log = logging.getLogger(__name__)

WORK_DIR = os.getenv('WORK_DIR', '/work')
FFMPEG = shutil.which('ffmpeg') or '/usr/local/bin/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/usr/local/bin/ffprobe'

# extensions de sortie sûres ; tout le reste (avi, wmv, ts…) est remuxé en .mkv
KEEP_EXTS = {'.mkv', '.mp4', '.m4v', '.mov'}
BUSY_STATES = ('starting', 'downloading', 'probing', 'converting', 'uploading')

ENCODERS = []
GPU_NAME = ''

_lock = threading.Lock()
_cancel = threading.Event()
_proc = None
_state = {'job_id': None, 'state': 'idle', 'progress': 0.0, 'fps': 0.0,
          'eta': None, 'message': '', 'out_size': 0}


class _Canceled(Exception):
    pass


def detect():
    """Détecte les encodeurs disponibles (NVENC, avec repli CPU) et le GPU."""
    global ENCODERS, GPU_NAME
    try:
        out = subprocess.run([FFMPEG, '-hide_banner', '-encoders'],
                             capture_output=True, text=True, timeout=20).stdout
        found = [e for e in ('hevc_nvenc', 'h264_nvenc', 'libx265', 'libx264')
                 if e in out]
        ENCODERS = []
        for e in found:
            if e.endswith('_nvenc'):
                # compilé ≠ utilisable : vérifie que l'encodeur s'initialise (GPU présent)
                t = subprocess.run(
                    [FFMPEG, '-v', 'error', '-f', 'lavfi',
                     '-i', 'nullsrc=s=256x256:r=10', '-frames:v', '2',
                     '-c:v', e, '-f', 'null', '-'],
                    capture_output=True, timeout=60)
                if t.returncode != 0:
                    log.warning('%s présent mais inutilisable (pas de GPU ?)', e)
                    continue
            ENCODERS.append(e)
    except Exception as e:
        log.error('Détection des encodeurs impossible : %s', e)
    try:
        GPU_NAME = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=10).stdout.strip().split('\n')[0]
    except Exception:
        GPU_NAME = ''
    log.info('Encodeurs NVENC : %s — GPU : %s', ENCODERS or 'aucun', GPU_NAME or 'inconnu')


def is_busy():
    return _state['state'] in BUSY_STATES


def get_state(job_id):
    with _lock:
        if _state['job_id'] != job_id:
            return None
        return dict(_state)


def _set(**kw):
    with _lock:
        _state.update(kw)


def start_job(payload, api_key):
    with _lock:
        if _state['state'] in BUSY_STATES:
            return False
        _cancel.clear()
        _state.update(job_id=payload['job_id'], state='starting', progress=0.0,
                      fps=0.0, eta=None, message='', out_size=0)
    threading.Thread(target=_run, args=(payload, api_key), daemon=True).start()
    return True


def cancel(job_id):
    with _lock:
        if _state['job_id'] != job_id or _state['state'] not in BUSY_STATES:
            return False
    _cancel.set()
    if _proc and _proc.poll() is None:
        _proc.terminate()
    return True


def _check_cancel():
    if _cancel.is_set():
        raise _Canceled()


def _run(payload, api_key):
    jid = payload['job_id']
    ext = (payload.get('ext') or '.mkv').lower()
    out_ext = ext if ext in KEEP_EXTS else '.mkv'
    inp = os.path.join(WORK_DIR, f'in_{jid}{ext}')
    out = os.path.join(WORK_DIR, f'out_{jid}{out_ext}')
    headers = {'Authorization': f'Bearer {api_key}'}
    try:
        _download(payload['download_url'], inp, headers,
                  payload.get('size') or 0)
        _check_cancel()
        _set(state='probing', progress=0.0)
        duration = _probe_duration(inp)
        _set(state='converting', progress=0.0)
        _convert(payload['options'], inp, out, out_ext, duration)
        _check_cancel()
        _set(state='uploading', progress=0.0, out_size=os.path.getsize(out))
        _upload(payload['upload_url'], out, out_ext, headers)
        _set(state='done', progress=100.0, eta=None)
        log.info('Job %s terminé', jid)
    except _Canceled:
        _set(state='canceled', message='Annulé par le serveur')
        log.info('Job %s annulé', jid)
    except Exception as e:
        log.exception('Job %s en erreur', jid)
        _set(state='error', message=str(e)[:500])
    finally:
        for f in (inp, out):
            try:
                os.remove(f)
            except OSError:
                pass


def _download(url, dest, headers, expected_size):
    _set(state='downloading', progress=0.0)
    with requests.get(url, headers=headers, stream=True, timeout=(10, 120)) as r:
        if not r.ok:
            raise RuntimeError(f'Téléchargement refusé : HTTP {r.status_code}')
        total = int(r.headers.get('Content-Length') or expected_size or 0)
        done = 0
        with open(dest, 'wb') as fh:
            for chunk in r.iter_content(1024 * 1024):
                _check_cancel()
                fh.write(chunk)
                done += len(chunk)
                if total:
                    _set(progress=min(99.9, done * 100.0 / total))
    _set(progress=100.0)


def _probe_duration(path):
    out = subprocess.run(
        [FFPROBE, '-v', 'error', '-print_format', 'json', '-show_format', path],
        capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError('ffprobe a échoué sur le fichier téléchargé')
    try:
        return float(json.loads(out.stdout)['format'].get('duration') or 0)
    except (KeyError, TypeError, ValueError):
        return 0.0


def _build_cmd(opts, inp, out, out_ext):
    codec = opts.get('codec', 'hevc')
    nvenc, cpu = ('hevc_nvenc', 'libx265') if codec == 'hevc' else ('h264_nvenc', 'libx264')
    enc = nvenc if nvenc in ENCODERS else (cpu if cpu in ENCODERS else None)
    if enc is None:
        raise RuntimeError(f'Aucun encodeur {codec} disponible sur cet agent')
    cq = str(max(10, min(40, int(opts.get('cq', 24)))))
    preset = opts.get('preset', 'p5')
    if preset not in ('p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'):
        preset = 'p5'

    cmd = [FFMPEG, '-y', '-hide_banner', '-loglevel', 'error', '-nostats',
           '-progress', 'pipe:1', '-i', inp, '-map', '0:v:0']
    if opts.get('audio', 'copy') == 'copy':
        cmd += ['-map', '0:a?', '-c:a', 'copy']
    if opts.get('subs', 'copy') == 'copy':
        cmd += ['-map', '0:s?', '-c:s', 'copy']
    if out_ext == '.mkv':
        cmd += ['-map', '0:t?', '-c:t', 'copy']
    cmd += ['-map_chapters', '0', '-dn', '-c:v', enc]
    if enc == nvenc:
        cmd += ['-preset', preset, '-rc', 'vbr', '-cq', cq, '-b:v', '0',
                '-spatial_aq', '1']
    else:
        # repli CPU si pas de NVENC : CQ ≈ CRF
        cmd += ['-preset', 'medium', '-crf', cq]
    if codec == 'hevc' and out_ext in ('.mp4', '.m4v', '.mov'):
        cmd += ['-tag:v', 'hvc1']
    cmd += [out]
    return cmd


def _convert(opts, inp, out, out_ext, duration):
    global _proc
    cmd = _build_cmd(opts, inp, out, out_ext)
    log.info('ffmpeg : %s', ' '.join(cmd))
    _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    speed = 0.0
    for line in _proc.stdout:
        if _cancel.is_set():
            _proc.terminate()
            break
        key, _, val = line.strip().partition('=')
        if key in ('out_time_us', 'out_time_ms'):  # les deux sont en microsecondes
            try:
                t = int(val) / 1_000_000
            except ValueError:
                continue
            if duration > 0:
                _set(progress=min(99.9, t * 100.0 / duration))
                if speed > 0:
                    _set(eta=int(max(0, duration - t) / speed))
        elif key == 'fps':
            try:
                _set(fps=float(val))
            except ValueError:
                pass
        elif key == 'speed':
            try:
                speed = float(val.rstrip('x'))
            except ValueError:
                pass
    _proc.wait()
    stderr_tail = (_proc.stderr.read() or '')[-1000:]
    rc = _proc.returncode
    _proc = None
    _check_cancel()
    if rc != 0:
        raise RuntimeError(f'ffmpeg a échoué (code {rc}) : {stderr_tail.strip()}')
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError('ffmpeg n’a produit aucun fichier de sortie')


class _ProgressFile:
    """Fichier lisible par requests, avec Content-Length et suivi de progression."""

    def __init__(self, path):
        self._fh = open(path, 'rb')
        self._size = os.path.getsize(path)
        self._sent = 0

    def __len__(self):
        return self._size

    def read(self, n=-1):
        _check_cancel()
        chunk = self._fh.read(n)
        self._sent += len(chunk)
        if self._size:
            _set(progress=min(99.9, self._sent * 100.0 / self._size))
        return chunk

    def close(self):
        self._fh.close()


def _upload(url, path, out_ext, headers):
    pf = _ProgressFile(path)
    try:
        r = requests.post(f'{url}?ext={out_ext}', data=pf,
                          headers={**headers,
                                   'Content-Type': 'application/octet-stream'},
                          timeout=(10, 600))
    finally:
        pf.close()
    if not r.ok:
        detail = ''
        try:
            detail = r.json().get('error', '')
        except Exception:
            pass
        raise RuntimeError(f'Renvoi refusé par le serveur : HTTP {r.status_code} {detail}')
