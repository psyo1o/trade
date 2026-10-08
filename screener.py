# -*- coding: utf-8 -*-
"""
한국투자 API 기반 **야간/장후 스크리너** — 당일 거래대금·시총 상위 후보를 뽑아 JSON에 저장.

실행
    * ``run_bot.start_scanner_scheduler`` 가 거래일 **14:50 KST** 에 ``run_night_screener`` 를 호출.
    * 단독 테스트 시 이 파일을 직접 실행해도 된다 (``config.json``·``kis_hts_id`` 필요).

토큰
    * ``kis_token.json`` — ``run_bot`` 과 호환되는 ``access_token`` + ``timestamp`` 형식을 사용한다.
    * **실행마다** 파일을 다시 읽고, ``EGW00123``(만료)이면 재발급 후 1회 재시도한다.
      (봇 장기 기동 시 import 시점 토큰을 붙잡지 않음.)

HTS 조건식 (최신)
    * 등록 원본: ``조건검색/v8조건검색 26.05(외국인수급제외 간결화).txt`` (동명 .xml/.tdf)
    * 로직: ``A and B and ((C and D and E and F) or (G and H))`` — README.md §8 「국장 HTS 조건검색」
    * 이 스크립트는 계정에 등록된 **모든** 조건식 결과를 합칩니다. V8만 쓰려면 HTS에 간결화 식만 두세요.
"""
import json, requests, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

appkey = config["kis_key"]
appsecret = config["kis_secret"]
hts_id = config.get("kis_hts_id", "").strip()

if not hts_id:
    print("🚨 config.json에 'kis_hts_id'를 입력해주세요!")
    exit()


def get_fresh_token(*, force: bool = False):
    """kis_token.json 재사용 또는 tokenP 발급. force=True면 무조건 재발급.

    봇 프로세스 안에서는 ``kis_api._apply_access_token`` 으로
    브로커·KIS_TOKEN·token.dat 까지 같은 토큰을 밀어 넣는다.
    """
    token_file = BASE_DIR / "kis_token.json"

    # 봇/GUI 와 동일 경로·헬퍼 사용 (가능하면)
    try:
        from utils.helpers import configure_kis_token_path, load_kis_token
        from api import kis_api as ka

        configure_kis_token_path(token_file)
        if ka._cfg is None:
            ka.configure(config)

        if not force:
            existing = load_kis_token()
            if ka._token_data_is_fresh(existing):
                tok = ka._normalize_access_token(existing.get("access_token"))
                # 메모리·token.dat 도 파일과 맞춤 (스캐너만 새 파일 보고 브로커는 옛값인 경우 방지)
                ka._apply_access_token(existing, persist=False)
                return tok

        data = ka.issue_new_kis_token()
        if data and data.get("access_token"):
            return ka._apply_access_token(data, persist=False)
        # 발급 실패 시 신선 파일만 허용
        existing = load_kis_token()
        if ka._token_data_is_fresh(existing):
            return ka._apply_access_token(existing, persist=False)
        return None
    except Exception as e:
        print(f"  ⚠️ [스캐너] kis_api 토큰 경로 실패, 로컬 폴백: {e}")

    if not force and token_file.exists():
        with open(token_file, "r", encoding="utf-8") as f:
            try:
                saved = json.load(f)
                token = saved.get("access_token") or saved.get("token")
                timestamp = saved.get("timestamp", 0)
                if token and time.time() - float(timestamp or 0) < 11.83 * 3600:
                    return token
            except Exception:
                pass

    print("🔑 한투 서버에서 보안 토큰 확인 중...")
    url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": appkey,
        "appsecret": appsecret,
    }
    res = requests.post(url, json=body)
    data = res.json()

    if "access_token" in data:
        token = data["access_token"]
        data["timestamp"] = time.time()
        with open(token_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            from api import kis_api as ka

            if ka._cfg is None:
                ka.configure(config)
            ka._apply_access_token(data, persist=False)
        except Exception:
            pass
        return token

    print(f"🚨 토큰 발급 실패: {data}")
    return None


def _is_expired_token_response(data) -> bool:
    if not isinstance(data, dict):
        return False
    msg = str(data.get("msg1", "") or "")
    code = str(data.get("msg_cd", "") or "")
    return code == "EGW00123" or "만료된 token" in msg or "만료된 토큰" in msg


def _auth_headers(token: str, tr_id: str) -> dict:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": appkey,
        "appsecret": appsecret,
        "tr_id": tr_id,
        "custtype": "P",
    }


def fetch_hts_conditions(token: str):
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/psearch-title"
    headers = _auth_headers(token, "HHKST03900300")
    params = {"user_id": hts_id}
    res = requests.get(url, headers=headers, params=params)
    return res.json()


def get_condition_stocks(token: str, seq):
    url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/psearch-result"
    headers = _auth_headers(token, "HHKST03900400")
    params = {"user_id": hts_id, "seq": seq}
    res = requests.get(url, headers=headers, params=params)
    return res.json()


def run_night_screener():
    print(f"🌙 [야간 발굴기] HTS({hts_id}) 조건검색 연동을 시작합니다...")
    token = get_fresh_token(force=False)
    if not token:
        print("⚠️ [에러 발생] 토큰을 확보하지 못해 스캐너를 중단합니다.")
        return

    cond_list_data = fetch_hts_conditions(token)
    if _is_expired_token_response(cond_list_data):
        print("  🔑 [스캐너] 토큰 만료(EGW00123) — 재발급 후 1회 재시도")
        token = get_fresh_token(force=True)
        if not token:
            print("⚠️ [에러 발생] 토큰 재발급 실패 — 스캐너 중단")
            return
        cond_list_data = fetch_hts_conditions(token)

    if "output2" not in cond_list_data:
        print("\n⚠️ [에러 발생] 조건식 목록을 가져오지 못했습니다.")
        print(f"👉 서버 원본 응답: {cond_list_data}")
        return

    target_stocks = []

    for cond in cond_list_data["output2"]:
        seq = cond.get("seq", cond.get("SEQ", ""))
        name = cond.get("condition_nm", cond.get("CONDITION_NM", "이름모름"))

        print(f"  -> 🔍 [{name}] (번호:{seq}) 조건식 스캔 중...")

        data = get_condition_stocks(token, seq)
        if _is_expired_token_response(data):
            print("  🔑 [스캐너] 조회 중 토큰 만료 — 재발급 후 재시도")
            token = get_fresh_token(force=True)
            if not token:
                print(f"     ❌ 토큰 재발급 실패. 응답: {data}")
                continue
            data = get_condition_stocks(token, seq)

        if data.get("rt_cd") == "0" and "output2" in data:
            stocks = [item["code"] for item in data["output2"]]
            print(f"     ✅ {len(stocks)}개 종목 포착 완료!")
            target_stocks.extend(stocks)
        else:
            print(f"     ❌ 종목을 가져오지 못했습니다. 응답: {data}")

        time.sleep(0.5)

    target_stocks = list(set(target_stocks))

    with open(BASE_DIR / "kr_targets.json", "w", encoding="utf-8") as f:
        json.dump(target_stocks, f)

    print(f"\n🎉 [발굴 완료] 총 {len(target_stocks)}개의 최정예 타겟이 kr_targets.json에 꽂혔습니다!")


if __name__ == "__main__":
    run_night_screener()
