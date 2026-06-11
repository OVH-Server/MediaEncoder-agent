import os
import logging

from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s')
log = logging.getLogger(__name__)

import converter

app = Flask(__name__)
API_KEY = os.getenv('API_KEY', '')


@app.before_request
def check_auth():
    auth = request.headers.get('Authorization', '')
    if not API_KEY or auth != f'Bearer {API_KEY}':
        return jsonify({'error': 'unauthorized'}), 401


@app.route('/api/health')
def health():
    return jsonify({
        'ok': True,
        'busy': converter.is_busy(),
        'job_id': converter._state['job_id'] if converter.is_busy() else None,
        'encoders': converter.ENCODERS,
        'gpu': converter.GPU_NAME,
    })


@app.route('/api/jobs', methods=['POST'])
def create_job():
    payload = request.get_json(force=True, silent=True) or {}
    required = ('job_id', 'download_url', 'upload_url', 'options')
    if any(k not in payload for k in required):
        return jsonify({'error': 'payload incomplet'}), 400
    if not converter.start_job(payload, API_KEY):
        return jsonify({'error': 'busy'}), 409
    log.info('Job %s accepté : %s', payload['job_id'], payload.get('name', '?'))
    return jsonify({'accepted': True}), 202


@app.route('/api/jobs/<int:job_id>')
def job_status(job_id):
    st = converter.get_state(job_id)
    if st is None:
        return jsonify({'error': 'inconnu'}), 404
    return jsonify(st)


@app.route('/api/jobs/<int:job_id>/cancel', methods=['POST'])
def job_cancel(job_id):
    if not converter.cancel(job_id):
        return jsonify({'error': 'aucun job actif avec cet id'}), 404
    return jsonify({'ok': True})


if __name__ == '__main__':
    os.makedirs(converter.WORK_DIR, exist_ok=True)
    converter.detect()
    app.run(host='0.0.0.0', port=int(os.getenv('FLASK_PORT', 5401)),
            debug=False, threaded=True)
