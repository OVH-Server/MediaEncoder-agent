import os
import signal
import socket
import time
import logging
import threading
from base64 import b64encode

import requests

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s')
log = logging.getLogger(__name__)

import converter

SERVER_URL    = os.getenv('SERVER_URL', '').rstrip('/')
API_KEY       = os.getenv('API_KEY', '')
AGENT_ID      = os.getenv('AGENT_ID', socket.gethostname())
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '5'))

HEADERS = {'X-Agent-Key': API_KEY}
_basic = os.getenv('BASIC_AUTH', '')  # format user:pass
if _basic:
    HEADERS['Authorization'] = 'Basic ' + b64encode(_basic.encode()).decode()

_shutdown = threading.Event()


def _heartbeat_loop():
    while not _shutdown.is_set():
        try:
            requests.post(
                f'{SERVER_URL}/api/agent/heartbeat',
                headers=HEADERS,
                json={
                    'agent_id': AGENT_ID,
                    'gpu':      converter.GPU_NAME,
                    'encoders': converter.ENCODERS,
                },
                timeout=10,
            )
        except Exception as e:
            log.warning('Heartbeat : %s', e)
        _shutdown.wait(30)


def _report_loop(job_id):
    while True:
        st = converter.get_state(job_id)
        if st is None or st['state'] == 'idle':
            break
        if st['state'] == 'done':
            break
        if st['state'] == 'canceled':
            break
        if st['state'] == 'error':
            try:
                requests.post(
                    f'{SERVER_URL}/api/agent/jobs/{job_id}/error',
                    headers=HEADERS,
                    json={'agent_id': AGENT_ID, 'message': st.get('message', '')},
                    timeout=10,
                )
            except Exception as e:
                log.warning('Rapport erreur job %s : %s', job_id, e)
            break
        try:
            body = {**st, 'agent_id': AGENT_ID}
            pf = converter.get_prefetch_progress()
            if pf:
                body['prefetch_progress'] = {'job_id': pf[0], 'progress': pf[1]}
            r = requests.post(
                f'{SERVER_URL}/api/agent/jobs/{job_id}/progress',
                headers=HEADERS,
                json=body,
                timeout=8,
            )
            if r.ok:
                resp = r.json()
                if resp.get('cancel'):
                    log.info('Annulation reçue pour job %s', job_id)
                    converter.cancel(job_id)
                    break
                prefetch = resp.get('prefetch')
                if prefetch and not converter.has_prefetch(prefetch.get('job_id')):
                    log.info('Préchargement job %s en arrière-plan', prefetch.get('job_id'))
                    converter.start_prefetch(prefetch, HEADERS)
        except Exception as e:
            log.warning('Rapport progression job %s : %s', job_id, e)
        _shutdown.wait(3)


def _send_disconnect():
    if not SERVER_URL:
        return
    try:
        requests.post(
            f'{SERVER_URL}/api/agent/disconnect',
            headers=HEADERS,
            json={'agent_id': AGENT_ID},
            timeout=5,
        )
        log.info('Déconnexion signalée au serveur')
    except Exception as e:
        log.warning('Erreur lors de la déconnexion : %s', e)


def _main_loop():
    if not SERVER_URL:
        log.error('SERVER_URL non configuré — arrêt.')
        return
    log.info('Agent démarré — serveur : %s', SERVER_URL)
    while not _shutdown.is_set():
        if not converter.is_busy():
            try:
                r = requests.post(
                    f'{SERVER_URL}/api/agent/jobs/claim',
                    headers=HEADERS,
                    json={'agent_id': AGENT_ID},
                    timeout=10,
                )
                if r.ok and r.status_code != 204:
                    job = r.json()
                    if job.get('job_id'):
                        log.info('Job %s réclamé : %s', job['job_id'], job.get('name', '?'))
                        converter.start_job(job, HEADERS)
                        threading.Thread(
                            target=_report_loop, args=(job['job_id'],), daemon=True
                        ).start()
            except Exception as e:
                log.warning('Sondage : %s', e)
        _shutdown.wait(POLL_INTERVAL)


if __name__ == '__main__':
    def _on_signal(signum, frame):
        log.info('Signal %s reçu — arrêt propre…', signum)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    os.makedirs(converter.WORK_DIR, exist_ok=True)
    converter.clean_workdir()
    converter.detect()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _main_loop()
    _send_disconnect()
