#!/usr/bin/env python3
import requests, time, subprocess
import logging

logging.basicConfig(filename='/home/ubuntu/health.log', level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

def check_proxy():
    try:
        r = requests.get('http://localhost:11435/health', timeout=5)
        return r.status_code == 200
    except:
        return False

def restart_proxy():
    subprocess.run(['pkill', '-f', 'proxy_v53.py'], capture_output=True)
    time.sleep(2)
    subprocess.run(
        'nohup python3 /home/ubuntu/-claude-test-/proxy_v53.py > /home/ubuntu/proxy.log 2>&1 &',
        shell=True
    )

while True:
    if not check_proxy():
        logging.warning("Proxy down! Restarting...")
        restart_proxy()
        time.sleep(30)
    else:
        time.sleep(5)
