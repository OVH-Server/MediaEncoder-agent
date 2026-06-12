import os
import time
import logging
import threading

import requests

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s')
log = logging.getLogger(__name__)

import converter

SERVER_URL    = os.getenv('SERVER_URL', '').rstrip('/')
API_KEY       = os.getenv('API_KEY', '')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '5'))
HEADERS       = {'Authorization': f'Bearer {API_KEY}'}


def _heartbeat_loop():
    while True:
        try:
            requests.post(
                f'{SERVER_URL}/api/agent/heartbeat',
                headers=HEADERS,
                json={
                    'gpu': converter.GPU_NAME,
                    'encoders': converter.ENCODERS,
                },
                timeout=10,
            )
        except Exception as e:
            log.warning('Heartbeat : %s', e)
        time.sleep(30)


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
                    json={'message': st.get('message', '')},
                    timeout=10,
                )
            except Exception as e:
                log.warning('Rapport erreur job %s : %s', job_id, e)
            break
        try:
            r = requests.post(
                f'{SERVER_URL}/api/agent/jobs/{job_id}/progress',
                headers=HEADERS,
                json=st,
                timeout=8,
            )
            if r.ok and r.json().get('cancel'):
                log.info('Annulation reçue pour job %s', job_id)
                converter.cancel(job_id)
                break
        except Exception as e:
            log.warning('Rapport progression job %s : %s', job_id, e)
        time.sleep(3)


def _main_loop():
    if not SERVER_URL:
        log.error('SERVER_URL non configuré — arrêt.')
        return
    log.info('Agent démarré — serveur : %s', SERVER_URL)
    while True:
        if not converter.is_busy():
            try:
                r = requests.post(
                    f'{SERVER_URL}/api/agent/jobs/claim',
                    headers=HEADERS,
                    json={},
                    timeout=10,
                )
                if r.ok and r.status_code != 204:
                    job = r.json()
                    if job.get('job_id'):
                        log.info('Job %s réclamé : %s', job['job_id'], job.get('name', '?'))
                        converter.start_job(job, API_KEY)
                        threading.Thread(
                            target=_report_loop, args=(job['job_id'],), daemon=True
                        ).start()
            except Exception as e:
                log.warning('Sondage : %s', e)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    os.makedirs(converter.WORK_DIR, exist_ok=True)
    converter.detect()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _main_loop()
