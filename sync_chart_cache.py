#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""매일 오후 11시에 차트 캐시를 GitHub에 동기화"""

import json
import subprocess
import os
import datetime
import sys

sys.path.insert(0, '/home/ubuntu/-claude-test-')

try:
    from config import store

    # 메모리 캐시 → 파일로 저장
    cache = {k: v for k, v in store.items() if '__last_chart_' in k}
    os.makedirs('/home/ubuntu/-claude-test-/.cache', exist_ok=True)

    cache_file = '/home/ubuntu/-claude-test-/.cache/chart_cache.json'
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

    # git 동기화
    os.chdir('/home/ubuntu/-claude-test-')
    subprocess.run(['git', 'add', '.cache/chart_cache.json'], capture_output=True)
    subprocess.run(['git', 'commit', '-m', f'cache: chart analysis auto-sync {datetime.date.today()}'],
                   capture_output=True)
    subprocess.run(['git', 'push'], capture_output=True)

    print(f'✅ {datetime.datetime.now()} 캐시 동기화 완료')

except Exception as e:
    print(f'❌ {datetime.datetime.now()} 캐시 동기화 실패: {e}')
