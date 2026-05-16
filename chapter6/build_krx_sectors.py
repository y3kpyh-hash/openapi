# -*- coding: utf-8 -*-
"""
네이버 금융 업종별 종목 데이터를 수집하여 krx_sector.json 생성.

실행 방법 (인터넷 연결 필요):
    python build_krx_sectors.py

결과물:
    chapter6/krx_sector.json  ← 메인 앱이 시작 시 자동 로드
"""

import json
import re
import time
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("requests 모듈이 없습니다. pip install requests 실행 후 다시 시도하세요.")

BASE_URL = "https://finance.naver.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "krx_sector.json")


def get_text(url: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            return r.content.decode("euc-kr", errors="replace")
        except Exception as e:
            print(f"  [재시도 {attempt+1}/{retries}] {url}: {e}")
            time.sleep(2)
    return ""


def fetch_sector_list() -> list[tuple[str, str]]:
    """(no, 업종명) 쌍 목록 반환"""
    text = get_text(f"{BASE_URL}/sise/sise_group.naver?type=upjong")
    pairs = re.findall(r'no=(\d+)"[^>]*>([^<]+)</a>', text)
    result = []
    for no, name in pairs:
        name = name.strip()
        if name and name != "&nbsp;":
            result.append((no, name))
    return result


def fetch_sector_stocks(no: str) -> list[str]:
    """해당 업종 번호의 종목코드 6자리 목록 반환"""
    url = f"{BASE_URL}/sise/sise_group_detail.naver?type=upjong&no={no}"
    text = get_text(url)
    codes = re.findall(r"code=(\d{6})", text)
    return list(dict.fromkeys(codes))   # 순서 유지 중복 제거


def main():
    print("=== 네이버 금융 업종분류 데이터 수집 ===")
    print(f"출력: {OUTPUT_PATH}\n")

    sectors = fetch_sector_list()
    if not sectors:
        sys.exit("[오류] 업종 목록을 가져오지 못했습니다. 네트워크 상태를 확인하세요.")
    print(f"업종 수: {len(sectors)}개\n")

    # 기존 파일이 있으면 병합 (user_sector.json 내용 보존)
    existing: dict[str, list] = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, encoding="utf-8") as f:
                existing = json.load(f)
            print(f"기존 {OUTPUT_PATH} 로드 ({len(existing)}개 섹터 병합)\n")
        except Exception:
            pass

    result: dict[str, list] = {}
    total_codes = 0

    for idx, (no, sector_name) in enumerate(sectors, 1):
        codes = fetch_sector_stocks(no)
        if codes:
            result[sector_name] = codes
            total_codes += len(codes)
        status = "O" if codes else "-"
        print(f"  [{idx:3d}/{len(sectors)}] {status} {sector_name:20s} {len(codes)}개")
        time.sleep(0.3)   # 네이버 서버 부하 방지

    # KRX 업종 데이터가 기존 파일보다 우선 (KRX가 베이스), 기존 항목은 병합
    merged: dict[str, list] = dict(result)
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v
        else:
            # 기존에만 있는 코드 추가
            for code in v:
                if code not in merged[k]:
                    merged[k].append(code)

    merged["_출처"] = "네이버금융 업종분류 (자동 수집)"
    merged["_수집일"] = __import__("datetime").date.today().isoformat()

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {len(result)}개 업종 / {total_codes}개 종목코드 → {OUTPUT_PATH}")
    print("프로그램을 재시작하면 자동으로 로드됩니다.")


if __name__ == "__main__":
    main()
