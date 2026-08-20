#!/usr/bin/env python3
"""dev farmos 테스트 농가 계정으로 로그인해 accessToken 을 출력 (드라이런 --farmos-token 용).

    python scripts/farmos_login.py --user-id <id> [--base-url https://dev.jinongservice.co.kr]
    (비밀번호는 프롬프트로 입력하거나 FARMOS_PASSWORD env)

로그인 API: POST /m/auths/login {userId, password, fcmDeviceToken} → data.accessToken (30일).
이 서비스의 farmos 사용은 읽기 전용이므로 dev 에서 반복 실행해도 안전하다.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--base-url", default=os.environ.get("FARMOS_BASE_URL", "https://dev.jinongservice.co.kr"))
    ap.add_argument("--json", action="store_true", help="응답 전체 출력")
    args = ap.parse_args()
    pw = os.environ.get("FARMOS_PASSWORD") or getpass.getpass("password: ")
    body = json.dumps({"userId": args.user_id, "password": pw, "fcmDeviceToken": "agent-dryrun"}).encode()
    req = urllib.request.Request(f"{args.base_url.rstrip('/')}/m/auths/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0
    tok = (data.get("data") or {}).get("accessToken")
    if not tok:
        print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
        return 1
    print(tok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
