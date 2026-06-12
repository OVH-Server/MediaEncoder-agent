import hashlib
import os
import json
import logging
import shutil
import subprocess
import threading
import time

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
          'eta': None, 'message': '', 'out_size': 0,
          'frame': 0, 'total_frames': 0, 'streams': [], 'speed': 0.0}

# État du préchargement pipeline (téléchargement du job N+1 pendant l'encodage du job N)
_prefetch_lock = threading.Lock()
_prefetch_cancel = threading.Event()
_prefetch = {'job_id': None, 'path': None, 'done': False, 'error': None,
             'thread': None, 'progress': 0.0, 'speed': 0.0}


class _Canceled(Exception):
    pass


def clean_workdir():
    """Vide WORK_DIR au démarrage pour repartir propre : tout fichier restant est
    le résidu d'un job interrompu (l'agent ne reprend jamais un transfert à chaud,
    le serveur remet ces jobs en attente). Garde le dossier lui-même."""
    if not os.path.isdir(WORK_DIR):
        return
    removed = 0
    for entry in os.listdir(WORK_DIR):
        path = os.path.join(WORK_DIR, entry)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed += 1
        except OSError as e:
            log.warning('Nettoyage WORK_DIR : %s non supprimé (%s)', path, e)
    if removed:
        log.info('WORK_DIR nettoyé au démarrage : %d élément(s) supprimé(s)', removed)


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


def start_job(payload, headers):
    with _lock:
        if _state['state'] in BUSY_STATES:
            return False
        _cancel.clear()
        _state.update(job_id=payload['job_id'], state='starting', progress=0.0,
                      fps=0.0, eta=None, message='', out_size=0,
                      frame=0, total_frames=0, streams=[], speed=0.0)
    # Préserve le préchargement si c'est bien ce job, sinon réinitialise
    with _prefetch_lock:
        if _prefetch['job_id'] != payload['job_id']:
            _prefetch_cancel.set()
            _prefetch.update(job_id=None, path=None, done=False, error=None,
                             thread=None, progress=0.0)
    threading.Thread(target=_run, args=(payload, headers), daemon=True).start()
    return True


def start_prefetch(payload, headers):
    """Lance le téléchargement du prochain job en arrière-plan."""
    jid = payload['job_id']
    with _prefetch_lock:
        if _prefetch['job_id'] == jid:
            return  # Déjà en cours pour ce job
        _prefetch_cancel.clear()
        _prefetch.update(job_id=jid, path=None, done=False, error=None,
                         thread=None, progress=0.0, speed=0.0, error_reported=False)
    t = threading.Thread(target=_prefetch_run, args=(payload, headers), daemon=True)
    with _prefetch_lock:
        _prefetch['thread'] = t
    t.start()


def _prefetch_run(payload, headers):
    jid = payload['job_id']
    ext = (payload.get('ext') or '.mkv').lower()
    path = os.path.join(WORK_DIR, f'in_{jid}{ext}')
    with _prefetch_lock:
        _prefetch['path'] = path
    try:
        h = hashlib.sha256()
        with requests.get(payload['download_url'], headers=headers,
                          stream=True, timeout=(10, 120)) as r:
            if not r.ok:
                raise RuntimeError(f'Préchargement refusé : HTTP {r.status_code}')
            total = int(r.headers.get('Content-Length') or payload.get('size') or 0)
            done_bytes = 0
            last_t = time.time()
            last_done = 0
            with open(path, 'wb') as fh:
                for chunk in r.iter_content(1024 * 1024):
                    if _prefetch_cancel.is_set():
                        return
                    h.update(chunk)
                    fh.write(chunk)
                    done_bytes += len(chunk)
                    now = time.time()
                    if now - last_t >= 1.0:
                        speed = (done_bytes - last_done) / (now - last_t)
                        last_t, last_done = now, done_bytes
                        with _prefetch_lock:
                            if _prefetch['job_id'] == jid:
                                _prefetch['speed'] = speed
                                if total:
                                    _prefetch['progress'] = min(99.9, done_bytes * 100.0 / total)
        # Fichier complètement écrit : vérifie l'intégrité avant de le marquer prêt.
        # En cas de mismatch → error → _wait_for_prefetch retournera None et le
        # job retombera sur un _download normal (re-téléchargement).
        _verify_source_checksum(payload, h.hexdigest(), headers)
        with _prefetch_lock:
            if _prefetch['job_id'] == jid:
                _prefetch['done'] = True
                _prefetch['progress'] = 100.0
        log.info('Préchargement job %s terminé', jid)
    except Exception as e:
        with _prefetch_lock:
            if _prefetch['job_id'] == jid:
                _prefetch['error'] = str(e)
                _prefetch['done'] = True
        log.warning('Préchargement job %s échoué : %s', jid, e)
        try:
            os.remove(path)
        except OSError:
            pass


def has_prefetch(job_id):
    """Retourne True si un préchargement est en cours ou terminé pour ce job."""
    with _prefetch_lock:
        return _prefetch['job_id'] == job_id


def get_prefetch_progress():
    """Retourne (job_id, progress%, speed) si un préchargement est actif, sinon None."""
    with _prefetch_lock:
        jid   = _prefetch['job_id']
        prog  = _prefetch['progress']
        speed = _prefetch['speed']
        done  = _prefetch['done']
    if jid is not None and not done:
        return jid, prog, speed
    return None


def get_prefetch_error():
    """Retourne (job_id, message) si le dernier préchargement a échoué (une fois),
    puis efface l'erreur pour ne pas la re-signaler. Sinon None."""
    with _prefetch_lock:
        jid = _prefetch['job_id']
        err = _prefetch['error']
        if jid is not None and err and not _prefetch.get('error_reported'):
            _prefetch['error_reported'] = True
            return jid, err
    return None


def _wait_for_prefetch(job_id):
    """Joint le thread de préchargement et retourne le chemin si succès, sinon None.
    Pendant l'attente, relaie la progression du téléchargement dans _state pour
    que l'UI affiche la suite du download au lieu d'un job figé sur 'sent'."""
    with _prefetch_lock:
        if _prefetch['job_id'] != job_id:
            return None
        t    = _prefetch['thread']
        path = _prefetch['path']

    if t and t.is_alive():
        log.info('Job %s : attente fin préchargement…', job_id)
        _set(state='downloading')
        deadline = time.time() + 600
        while t.is_alive() and time.time() < deadline:
            with _prefetch_lock:
                prog = _prefetch['progress']
            _set(progress=prog)
            t.join(timeout=1)
        if t.is_alive():
            log.warning('Job %s : préchargement trop long, abandon', job_id)
            _prefetch_cancel.set()
            t.join(timeout=30)
            # Nettoie le fichier partiel avant que _download ne reprenne
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return None

    with _prefetch_lock:
        if _prefetch['job_id'] != job_id:
            return None
        done  = _prefetch['done']
        error = _prefetch['error']
        path  = _prefetch['path']

    if not done or error or not path or not os.path.exists(path):
        return None
    return path


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


def _run(payload, headers):
    jid = payload['job_id']
    ext = (payload.get('ext') or '.mkv').lower()
    out_ext = ext if ext in KEEP_EXTS else '.mkv'
    inp = os.path.join(WORK_DIR, f'in_{jid}{ext}')
    out = os.path.join(WORK_DIR, f'out_{jid}{out_ext}')
    try:
        # Utilise le fichier préchargé si disponible, sinon télécharge normalement
        # (le préchargement a déjà vérifié son checksum dans _prefetch_run)
        prefetched = _wait_for_prefetch(jid) if has_prefetch(jid) else None
        if prefetched:
            log.info('Job %s : fichier préchargé utilisé (%s)', jid, prefetched)
            _set(state='probing', progress=0.0)
        else:
            digest = _download(payload['download_url'], inp, headers,
                               payload.get('size') or 0)
            _check_cancel()
            _verify_source_checksum(payload, digest, headers)
            _set(state='probing', progress=0.0)
        duration, total_frames, streams = _probe_streams(inp)
        _set(state='converting', progress=0.0, total_frames=total_frames, streams=streams, speed=0.0)
        _convert(payload['options'], inp, out, out_ext, duration)
        _check_cancel()
        _set(state='uploading', progress=0.0, out_size=os.path.getsize(out), speed=0.0)
        _upload(payload['upload_url'], out, out_ext, headers)
        _set(state='done', progress=100.0, eta=None, speed=0.0)
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
    """Télécharge la source en hashant les octets écrits. Retourne le sha256 hex."""
    _set(state='downloading', progress=0.0, speed=0.0)
    h = hashlib.sha256()
    with requests.get(url, headers=headers, stream=True, timeout=(10, 120)) as r:
        if not r.ok:
            raise RuntimeError(f'Téléchargement refusé : HTTP {r.status_code}')
        total = int(r.headers.get('Content-Length') or expected_size or 0)
        done = 0
        t0 = last_t = time.time()
        last_done = 0
        with open(dest, 'wb') as fh:
            for chunk in r.iter_content(1024 * 1024):
                _check_cancel()
                h.update(chunk)
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_t >= 1.0:
                    speed = (done - last_done) / (now - last_t)
                    last_t, last_done = now, done
                    upd = {'speed': speed}
                    if total:
                        upd['progress'] = min(99.9, done * 100.0 / total)
                    _set(**upd)
    _set(progress=100.0, speed=0.0)
    return h.hexdigest()


def _verify_source_checksum(payload, digest, headers):
    """Compare le hash local (calculé sur le fichier complètement reçu) au
    sha256 de la source retourné par le serveur. Serveur ancien : skip."""
    url = payload.get('checksum_url')
    if not url or not digest:
        return
    r = requests.get(url, headers=headers, timeout=(10, 600))
    if not r.ok:
        log.warning('Checksum source indisponible (HTTP %s) — vérification sautée',
                    r.status_code)
        return
    expected = (r.json().get('sha256') or '').lower()
    if expected and expected != digest:
        raise RuntimeError(
            f'Checksum source invalide après téléchargement '
            f'({digest[:12]}… ≠ {expected[:12]}…)')
    log.info('Checksum source vérifié (%s…)', digest[:12])


def _probe_streams(path):
    """Retourne (durée en s, total_frames vidéo, liste de flux détaillés)."""
    out = subprocess.run(
        [FFPROBE, '-v', 'error', '-print_format', 'json',
         '-show_format', '-show_streams', path],
        capture_output=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError('ffprobe a échoué sur le fichier téléchargé')
    data = json.loads(out.stdout)
    try:
        duration = float(data['format'].get('duration') or 0)
    except (KeyError, TypeError, ValueError):
        duration = 0.0

    IMAGE_CODECS = {'mjpeg', 'png', 'bmp', 'gif'}
    total_frames = 0
    streams = []
    for s in data.get('streams', []):
        ctype = s.get('codec_type')
        codec = s.get('codec_name', '?')
        lang = (s.get('tags') or {}).get('language', '')
        title = (s.get('tags') or {}).get('title', '')
        if ctype == 'video':
            if codec in IMAGE_CODECS or s.get('disposition', {}).get('attached_pic'):
                continue
            try:
                total_frames = int(s.get('nb_frames') or 0)
            except (TypeError, ValueError):
                total_frames = 0
            if not total_frames and duration > 0:
                num, _, den = (s.get('avg_frame_rate') or '0/1').partition('/')
                try:
                    total_frames = int(duration * float(num) / float(den or 1))
                except (ValueError, ZeroDivisionError):
                    pass
            streams.append({
                'type': 'video', 'codec': codec,
                'width': int(s.get('width') or 0),
                'height': int(s.get('height') or 0),
                'total_frames': total_frames,
            })
        elif ctype == 'audio':
            streams.append({
                'type': 'audio', 'codec': codec,
                'lang': lang, 'title': title,
                'channels': int(s.get('channels') or 0),
            })
        elif ctype == 'subtitle':
            streams.append({
                'type': 'subtitle', 'codec': codec,
                'lang': lang, 'title': title,
            })
    return duration, total_frames, streams


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
    for line in _proc.stdout:
        if _cancel.is_set():
            _proc.terminate()
            break
        key, _, val = line.strip().partition('=')
        if key == 'frame':
            try:
                frame = int(val)
                with _lock:
                    tf = _state['total_frames']
                    fps = _state['fps']
                updates = {'frame': frame}
                if tf > 0:
                    updates['progress'] = min(99.9, frame * 100.0 / tf)
                    if fps > 0:
                        updates['eta'] = int(max(0, tf - frame) / fps)
                _set(**updates)
            except ValueError:
                pass
        elif key == 'fps':
            try:
                _set(fps=float(val))
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
        self._t0 = self._last_t = time.time()
        self._last_sent = 0

    def __len__(self):
        return self._size

    def read(self, n=-1):
        _check_cancel()
        chunk = self._fh.read(n)
        self._sent += len(chunk)
        now = time.time()
        if now - self._last_t >= 1.0:
            speed = (self._sent - self._last_sent) / (now - self._last_t)
            self._last_t, self._last_sent = now, self._sent
            upd = {'speed': speed}
            if self._size:
                upd['progress'] = min(99.9, self._sent * 100.0 / self._size)
            _set(**upd)
        return chunk

    def close(self):
        self._fh.close()


def _upload(url, path, out_ext, headers):
    # Hash du fichier encodé (complet sur disque local, ffmpeg terminé) envoyé
    # en header : le serveur compare au hash des octets reçus une fois le flux
    # terminé pour valider l'intégrité du transfert retour.
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            _check_cancel()
            h.update(chunk)
    pf = _ProgressFile(path)
    try:
        r = requests.post(f'{url}?ext={out_ext}', data=pf,
                          headers={**headers,
                                   'Content-Type': 'application/octet-stream',
                                   'X-Content-Sha256': h.hexdigest()},
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
