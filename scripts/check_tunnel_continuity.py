"""터널 전환 중 끊김 확인(DEV-255) — 지정한 주소들을 0.5초마다 호출해 200이 아닌 응답·오류를 세고, 하나라도 있으면 종료 코드 1.
사용: python scripts/check_tunnel_continuity.py --seconds 120 [URL ...] (명시 실행 때만 돈다)"""
import argparse
import sys
import threading
import time

import httpx

DEFAULT = ['https://dev.lomebrote.com/api/health', 'https://nightshift.lomebrote.com']


def watch(url, until, results, lock):
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        while time.monotonic() < until:
            started = time.monotonic()
            try:
                code = client.get(url).status_code
                problem = None if code == 200 else str(code)
            except httpx.HTTPError as e:
                problem = type(e).__name__
            with lock:
                results.append((time.strftime('%H:%M:%S'), url, problem))
                if problem:
                    print(f'{time.strftime("%H:%M:%S")} 실패 {url} {problem}', flush=True)
            time.sleep(max(0, 0.5 - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('urls', nargs='*')
    args = parser.parse_args()
    urls, until, results, lock = args.urls or DEFAULT, time.monotonic() + args.seconds, [], threading.Lock()
    threads = [threading.Thread(target=watch, args=(url, until, results, lock)) for url in urls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    failures = [r for r in results if r[2]]
    for url in urls:
        mine = [r for r in results if r[1] == url]
        print(f'{url}: 요청 {len(mine)}건, 실패 {sum(1 for r in mine if r[2])}건')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
