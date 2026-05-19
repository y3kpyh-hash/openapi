import os
import sys
import json
import datetime
from collections import deque
from queue import Queue

from loguru import logger
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QSettings, QTimer, QTime, Qt
from PyQt5 import QtGui, uic
from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QStyledItemDelegate
import pandas as pd
from telegram import Bot

from utils import (
    log_exceptions, resource_path, ConfirmDialog,
    set_pw_msg, reset_pw_msg, MaskedComboBoxDelegate, mask_account_number,
)

# ========================
# 모듈 레벨 경로 설정
# ========================
if getattr(sys, 'frozen', False):
    cur_path = os.path.dirname(sys.executable)
elif __file__:
    cur_path = os.path.dirname(__file__)
else:
    raise FileNotFoundError

data_save_path = os.path.join(cur_path, 'data')
os.makedirs(data_save_path, exist_ok=True)

internal_resource_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

today = datetime.datetime.now().strftime('%Y%m%d')
data_path = os.path.join(cur_path, "Log")
os.makedirs(data_path, exist_ok=True)
logger.add(os.path.join(data_path, today + 'log.log'))

form_class = uic.loadUiType(resource_path("main.ui"))[0]


class KiwoomAPI(QMainWindow, form_class):
    def __init__(self):
        super().__init__()
        self.setupUi(self)
        self.show()

        # 버튼 시그널
        self.autoOnPushButton.clicked.connect(self.auto_trade_on)
        self.autoOffPushButton.clicked.connect(self.auto_trade_off)
        self.saveSettingsPushButton.clicked.connect(self.save_settings)
        self.convertServerPushButton.clicked.connect(self.convert_server)
        self.sendSampleTelegramPushButton.clicked.connect(self.sample_telegram)
        self.taxDoubleSpinBox.valueChanged.connect(self.update_transaction_cost)
        self.transactionFeeDoubleSpinBox.valueChanged.connect(self.update_transaction_cost)
        self.alwaysOnTopCheckBox.stateChanged.connect(self.toggle_always_on_top)
        self.maskAccountCheckBox.stateChanged.connect(self.custom_mask_checkbox_changed)
        self.customAccountNumComboBox.currentIndexChanged.connect(self.update_custom_line_edit_masking)
        self.customAccountNumComboBox.setEditable(True)
        self.customAccountNumComboBox.lineEdit().setReadOnly(True)

        self.settings = QSettings('MyAPP20251013', 'myApp20251013')
        self.setWindowIcon(QtGui.QIcon(resource_path('icon.ico')))

        # TR 요청 제한
        self.max_send_per_sec: int    = 4
        self.max_send_per_minute: int = 9999999   # 본당 제한 없어짐 (20251116 업데이트)
        self.max_send_per_hour: int   = 990

        self.last_tr_send_times       = deque(maxlen=self.max_send_per_sec)
        self.last_order_tr_send_times = deque(maxlen=self.max_send_per_sec)
        self.last_hour_tr_times       = deque()   # 시간당 사용량 추적 (무제한, 직접 prune)
        self.tr_req_queue             = Queue()
        self.orders_queue             = Queue()

        self.stock_name_list                   = []
        self.stock_code_to_stock_name_dict     = dict()
        self.stock_name_to_stock_code_dict     = dict()
        self.stock_code_to_realtime_price_dict = dict()
        self.condition_name_to_condition_idx_dict = dict()
        self.stock_code_to_sector: dict[str, str] = {}
        self._theme_sector_queue: list[str] = []   # opt90002_sector_req 종목명 순서 큐
        self._theme_sector_total: int = 0           # 요청한 전체 테마 수
        self._theme_sector_done:  int = 0           # 응답 완료된 테마 수
        self.stock_code_to_info_dict           = dict()

        self.index_code_to_name_map = {
            "001": "KOSPI",
            "101": "KOSDAQ",
            "201": "KOSPI200",
            "150": "KOSDAQ150",
        }
        self.index_name_to_code_map = {v: k for k, v in self.index_code_to_name_map.items()}

        self.available_credit_order_codes_set = set()
        self.realtime_registered_codes        = set()
        self.NXT_code_set                     = set()

        self.update_transaction_cost()

        self.screen_num               = 5000
        self.last_auto_trade_on_unix_time = 0
        self.can_push_auto_trade_on_btn   = True
        self.account_list                 = []
        self.using_account_num            = ""
        self.has_done_loading             = False
        self.is_no_transaction            = True
        self.has_remained_data99018       = False
        self.is_paper_trading             = False
        self.transaction_cost             = 0.0

        # 계좌 잔고 DataFrame
        self.account_info_df = pd.DataFrame(
            columns=["계좌번호", "종목코드", "종목명", "보유수량", "매매가능수량", "평균단가",
                     "현재가", "전일대비(%)", "수익률(%)"]
        )
        self.account_info_df.set_index(["계좌번호", "종목코드"], inplace=True)

        # 신용 잔고 DataFrame
        self.credit_account_info_df = pd.DataFrame(
            columns=["계좌번호", "종목코드", "종목명", "보유수량", "매매가능수량", "평균단가",
                     "현재가", "전일대비(%)", "수익률(%)"]
        )
        self.credit_account_info_df.set_index(["계좌번호", "종목코드"], inplace=True)

        # 미체결 주문 DataFrame
        self.unfinished_orders_df = pd.DataFrame(
            columns=["종목코드", "종목명", "주문체결시간", "미체결수량", "주문구분"]
        )

        # OCX
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self._login()

    # ========================
    # 시그널-슬롯 연결
    # ========================

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveRealData.connect(self._receive_realdata)
        self.kiwoom.OnReceiveTrData.connect(self._receive_tr_data)
        self.kiwoom.OnReceiveChejanData.connect(self._receive_chejandata)
        self.kiwoom.OnReceiveConditionVer.connect(self._receive_condition_ver)
        self.kiwoom.OnReceiveTrCondition.connect(self._receive_tr_condition)
        self.kiwoom.OnReceiveRealCondition.connect(self._receive_real_condition)
        self.kiwoom.OnReceiveMsg.connect(self._receive_msg)

    # ========================
    # 로그인
    # ========================

    def _login(self):
        ret = self.kiwoom.dynamicCall("CommConnect()")
        if ret == 0:
            self.common_log("로그인 창 열기 성공!")

    def _event_connect(self, err_code):
        if err_code == 0:
            self.common_log("로그인 성공!")
            self._after_login()
        else:
            raise Exception("로그인 실패!")

    def _after_login(self):
        self.get_is_paper_trading()
        self.get_account_info()
        self.get_stock_code_name_dict()
        self.load_settings()

    # ========================
    # 서버 구분
    # ========================

    def get_is_paper_trading(self):
        server_class_num = self.kiwoom.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")
        if server_class_num == "1":
            self.is_paper_trading = True
            logger.info("모의투자 서버에 접속 중입니다.")
            self.serverLineEdit.setText("접속 서버: 모의투자")
        else:
            self.is_paper_trading = False
            logger.info("실서버에 접속 중입니다.")
            self.serverLineEdit.setText("접속 서버: 실전투자")

    # ========================
    # 계좌 정보
    # ========================

    @log_exceptions
    def get_account_info(self):
        account_nums = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCNO"]).rstrip(';'))
        self.account_list = [
            x for x in account_nums.split(';')
            if x != '' and not (x.endswith('72') or x.endswith('32'))
        ]
        self.using_account_num = self.account_list[0]

    def get_account_balance(self, next=0):
        if len(self.using_account_num) > 0:
            self.set_input_value(id="계좌번호",              value=self.using_account_num)
            self.set_input_value(id="비밀번호",              value="")
            self.set_input_value(id="비밀번호입력매체구분",   value="00")
            self.set_input_value(id="조회구분",              value="1")
            self.set_input_value(id="거래소구분",            value="")
            self.comm_rq_data(rqname="opw00018_req", trcode="opw00018", next=next, screen_no=self._get_screen_num())

    def get_credit_date(self, next=0):
        if len(self.using_account_num) > 0:
            self.set_input_value(id="계좌번호",              value=self.using_account_num)
            self.set_input_value(id="비밀번호",              value="")
            self.set_input_value(id="상장폐지조회구분",       value="1")
            self.set_input_value(id="비밀번호입력매체구분",   value="00")
            self.set_input_value(id="거래소구분",            value="")
            self.comm_rq_data(rqname="opw00004_req", trcode="opw00004", next=next, screen_no=self._get_screen_num())

    def get_credit_info(self, next=0):
        self.set_input_value(id="신용종목등록글구분", value="%")
        self.set_input_value(id="시장거래구분",       value="%")
        self.set_input_value(id="종목번호",           value="")
        self.comm_rq_data(rqname="opt10099_req", trcode="opt10099", next=next, screen_no=self._get_screen_num())

    def request_get_account_balance(self, is_next=False):
        self.tr_req_queue.put([self.get_account_balance, 2 if is_next else 0])

    def request_get_credit_date(self, is_next=False):
        self.tr_req_queue.put([self.get_credit_date, 2 if is_next else 0])

    def request_get_credit_info(self, is_next=False):
        self.tr_req_queue.put([self.get_credit_info, 2 if is_next else 0])

    # ========================
    # 종목 코드/이름 목록
    # ========================

    @log_exceptions
    def get_stock_code_name_dict(self):
        KOSPI_list  = self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", '0').split(';')[:-1]
        KOSDAQ_list = self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", '10').split(';')[:-1]
        # NXT 코드는 "005930_AL" 형태로 반환됨 → _AL 제거하여 일반 코드와 비교 가능하게 정규화
        self.NXT_code_set = set(
            code.replace('_AL', '').strip()
            for code in self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", 'NXT').split(';')
            if code.strip()
        )
        logger.debug(f"NXT 대체거래소 가능 종목 수: {len(self.NXT_code_set)}")
        total_stock_code_list = KOSPI_list + KOSDAQ_list
        for stock_code in total_stock_code_list:
            name = self.kiwoom.dynamicCall("GetMasterCodeName(QString)", [stock_code])
            self.stock_code_to_stock_name_dict[stock_code] = name
            self.stock_name_to_stock_code_dict[name]       = stock_code
            self.stock_name_list.append(name)

        self._build_sector_map(KOSPI_list, KOSDAQ_list)

    def _build_sector_map(self, kospi_list: list, kosdaq_list: list) -> None:
        """업종 맵 구축: sector_config.json 커스텀 분류 → opt90001/90002 테마로 갱신"""
        # GetCodeListByMarket은 시장코드("0","10" 등) 전용으로 업종코드("001"~)를 지원하지 않음.
        # 미분류 종목은 빈 문자열로 두고, sector_config.json + opt90001/90002 테마가 채움.

        base = os.path.dirname(__file__)

        # 1단계: krx_sector.json (build_krx_sectors.py로 수집한 네이버 금융 업종 분류 — 베이스)
        self._load_sector_json(os.path.join(base, "krx_sector.json"), "krx_sector.json")

        # 2단계: sector_config.json (관리자 정의 커스텀 섹터 — KRX 분류 위에 덮어씌움)
        self._load_sector_json(os.path.join(base, "sector_config.json"), "sector_config.json")

        # 3단계: user_sector.json (엑셀 가져오기로 생성된 사용자 정의)
        self._load_sector_json(os.path.join(base, "user_sector.json"), "user_sector.json")

        # 4단계: sector_list.xlsx (사용자가 직접 편집한 Excel — 최우선)
        self._load_sector_xlsx(os.path.join(base, "sector_list.xlsx"))

        logger.info(f"[업종맵] 최종 {len(self.stock_code_to_sector)}개 종목 매핑 완료")

    def _load_sector_xlsx(self, path: str) -> None:
        """sector_list.xlsx의 종목코드+업종 컬럼을 읽어 업종맵 갱신 (최우선 적용)"""
        if not os.path.exists(path):
            return
        try:
            import pandas as pd
            df = pd.read_excel(path, dtype=str, usecols=["종목코드", "업종"])
            count = 0
            for _, row in df.iterrows():
                code   = str(row.get("종목코드", "")).strip().zfill(6)
                sector = str(row.get("업종", "")).strip()
                if code and sector and sector not in ("nan", ""):
                    self.stock_code_to_sector[code] = sector
                    count += 1
            logger.info(f"[업종맵] sector_list.xlsx {count}개 적용 완료")
        except Exception as e:
            logger.warning(f"[업종맵] sector_list.xlsx 로드 실패: {e}")

    def _load_sector_json(self, path: str, label: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            count = 0
            for sector_name, codes in data.items():
                if sector_name.startswith("_"):
                    continue
                for code in codes:
                    self.stock_code_to_sector[code.strip()] = sector_name
                    count += 1
            logger.info(f"[업종맵] {label} {count}개 적용 완료")
        except Exception as e:
            logger.warning(f"[업종맵] {label} 로드 실패: {e}")

    # ========================
    # TR 요청 큐
    # ========================

    def send_tr_request(self):
        self.now_time = datetime.datetime.now()
        if self.is_check_tr_req_condition() and not self.tr_req_queue.empty():
            request_func, *func_args = self.tr_req_queue.get()
            logger.info(f"Executing TR request fuction: {request_func}, func_args: {func_args}")
            request_func(*func_args) if func_args else request_func()
            self.last_tr_send_times.append(self.now_time)
            self.last_hour_tr_times.append(self.now_time)
            self.on_tr_usage_updated(*self.get_tr_usage_this_hour())

    def is_check_tr_req_condition(self) -> bool:
        now = datetime.datetime.now()
        if len(self.last_tr_send_times) >= self.max_send_per_sec:
            oldest = self.last_tr_send_times[0]
            if (now - oldest).total_seconds() < 1.0:
                return False
        return True

    def get_tr_usage_this_hour(self) -> tuple[int, int]:
        """(사용량, 잔여량) — 직전 60분 롤링 윈도우 기준"""
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(hours=1)
        while self.last_hour_tr_times and self.last_hour_tr_times[0] < cutoff:
            self.last_hour_tr_times.popleft()
        used      = len(self.last_hour_tr_times)
        remaining = max(0, self.max_send_per_hour - used)
        return used, remaining

    # ========================
    # 조건 검색
    # ========================

    def get_condition_name_list(self):
        self.kiwoom.dynamicCall("GetConditionLoad()")
        return []

    def _receive_condition_ver(self, ret, msg):
        logger.info(f"Received condition ver: ret={ret}, msg={msg}")

    def _receive_tr_condition(self, scrNum, strCodeList, strConditionName, nIndex, nNext):
        logger.info(
            f"Received TR Condition, strCodeList: {strCodeList}, strConditionName: {strConditionName}, "
            f"nIndex: {nIndex}, nNext: {nNext}, scrNum: {scrNum}"
        )
        search_code_list = [
            stock_code for stock_code in strCodeList.split(';') if len(stock_code) == 6
        ]
        self.on_receive_condition_search_data(
            dict(
                조건명=strConditionName,
                종리스트=search_code_list,
            )
        )

    def _receive_real_condition(self, strCode, strType, strConditionName, strConditionIndex):
        logger.info(
            f"Received Real Condition: strCode={strCode}, strType={strType}, "
            f"strConditionName={strConditionName}"
        )

    def on_receive_condition_search_data(self, data):
        pass

    # ========================
    # 캔들 데이터 요청
    # ========================

    def request_candle_data(self, stock_code, candle_type="3분", include_pre_post=True):
        if include_pre_post and not self.is_paper_trading:
            stock_code += "_AL"
        if '분' in candle_type:
            # opt10080 틱범위는 숫자만 허용 (1, 3, 5, 10, 15, 30, 60)
            # "1분봉", "3분봉", "30분봉" 등에서 숫자만 추출
            import re as _re
            _m = _re.search(r'\d+', candle_type)
            tick_range = _m.group() if _m else "1"
            self.tr_req_queue.put([self.request_opt10080, stock_code, tick_range])
        elif candle_type == '일봉':
            self.tr_req_queue.put([self.request_opt10081, stock_code])
        elif candle_type == '주봉':
            self.tr_req_queue.put([self.request_opt10082, stock_code])
        elif candle_type == '월봉':
            self.tr_req_queue.put([self.request_opt10083, stock_code])

    def request_index_candle_data_by_name(self, name="KOSPI", candle_type="3분"):
        code = self.index_name_to_code_map.get(name)
        if '분' in candle_type:
            self.tr_req_queue.put([self.request_opt20005, code, f"{candle_type}:{candle_type}"])
        elif candle_type == '일봉':
            self.tr_req_queue.put([self.request_opt20006, code])

    def request_opt10080(self, code, tick_range="1:1분"):
        self.set_input_value(id="종목코드",   value=code)
        self.set_input_value(id="틱범위",    value=tick_range)
        self.set_input_value(id="수정주가구분", value=1)
        self.set_input_value(id="증가매매분봉", value=0)
        self.comm_rq_data(rqname="opt10080_req", trcode="opt10080", next=0, screen_no=self._get_screen_num())

    def request_opt10063(self, code: str):
        """장중투자자별매매요청 — 종목별 1차~5차 시간대 집계."""
        self.set_input_value(id="종목코드", value=code.replace("_AL", "").strip())
        self.comm_rq_data(rqname="opt10063_req", trcode="opt10063", next=0,
                          screen_no=self._get_screen_num())

    def request_opt50028(self, code, tick_range="1"):
        """해외선물 분봉차트 조회 (CME 등)."""
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="틱범위",   value=str(tick_range))
        self.comm_rq_data(rqname="opt50028_req", trcode="opt50028", next=0, screen_no=self._get_screen_num())

    def request_opt10081(self, code):
        self.set_input_value(id="종목코드",   value=code)
        self.set_input_value(id="기준일자",   value=datetime.datetime.now().strftime('%Y%m%d'))
        self.set_input_value(id="수정주가구분", value=1)
        self.comm_rq_data(rqname="opt10081_req", trcode="opt10081", next=0, screen_no=self._get_screen_num())

    def request_opt10082(self, code):
        self.set_input_value(id="종목코드",   value=code)
        self.set_input_value(id="기준일자",   value=datetime.datetime.now().strftime('%Y%m%d'))
        self.set_input_value(id="수정주가구분", value=1)
        self.comm_rq_data(rqname="opt10082_req", trcode="opt10082", next=0, screen_no=self._get_screen_num())

    def request_opt10083(self, code):
        self.set_input_value(id="종목코드",   value=code)
        self.set_input_value(id="기준일자",   value=datetime.datetime.now().strftime('%Y%m%d'))
        self.set_input_value(id="수정주가구분", value=1)
        self.comm_rq_data(rqname="opt10083_req", trcode="opt10083", next=0, screen_no=self._get_screen_num())

    def request_opt20005(self, code, tick_range="3분:3분"):
        self.set_input_value(id="업종코드", value=code)
        self.set_input_value(id="틱범위",  value=tick_range)
        self.comm_rq_data(rqname="opt20005_req", trcode="opt20005", next=0, screen_no=self._get_screen_num())

    def request_opt20006(self, code):
        self.set_input_value(id="업종코드", value=code)
        self.set_input_value(id="기준일자", value=datetime.datetime.now().strftime('%Y%m%d'))
        self.comm_rq_data(rqname="opt20006_req", trcode="opt20006", next=0, screen_no=self._get_screen_num())

    # ========================
    # 거래원 분석 요청
    # ========================

    def request_broker_analysis(self, stock_code: str):
        """
        opt10070 (당일주요거래원요청)
        INPUT: 종목코드 하나만 (싱글데이터 — 오늘 누적 주요 거래원 스냅샷)
        """
        stock_code = stock_code.replace('_AL', '').strip()
        self.set_input_value(id="종목코드", value=stock_code)
        self.comm_rq_data(rqname="opt10070_req", trcode="opt10070", next=0, screen_no=self._get_screen_num())

    def subscribe_broker_realtime(self, stock_code: str):
        """주식거래원 실시간 구독 — 시간별 거래원 분석용 (FID 72~91)"""
        clean = stock_code.replace('_AL', '').strip()
        self._broker_realtime_code = clean
        fid_list = "72;73;74;75;76;77;78;79;80;81;82;83;84;85;86;87;88;89;90;91"
        self.set_real(self._get_screen_num(), clean, fid_list, "1")
        logger.debug(f"[거래원] 시간별 실시간 구독 시작: {clean}")

    # ========================
    # 종목별투자자 요청 (opt10059)
    # ========================

    def request_investor_data(self, stock_code: str, start_date: str, end_date: str,
                              amount_qty: str = "1", trade_type: str = "0", unit: str = "1"):
        """
        opt10059 (종목별투자자기관별요청) — TR 큐를 통해 요청 (속도 제한 준수)
          amount_qty : 1=금액, 2=수량
          trade_type : 0=순매수, 1=매수, 2=매도
          unit       : 1000=천주, 1=단주
        """
        # _AL 접미사 유지 — 000660_AL로 조회해야 KRX+NXT 통합 데이터 반환
        # (strip 하면 000660=KRX 단독이 되어 HTS 기준값과 거래량/투자자 모두 불일치)
        stock_code = stock_code.strip()
        self._investor_trade_type = trade_type   # on_opt10059_req에서 참조
        self._investor_volume_cache = {}          # 이전 캐시 초기화
        # opt10081 먼저 — 일봉 거래량 캐시 구성 (opt10059 거래량 공백 행 보완)
        self.tr_req_queue.put([self._send_opt10081_investor_vol, stock_code])
        self.tr_req_queue.put([self._send_opt10059, stock_code, end_date, amount_qty, trade_type, unit])

    def _send_opt10081_investor_vol(self, stock_code):
        """opt10081 일봉 거래량 조회 — 투자자 테이블 거래량 보완용 (실제거래량, 통합)"""
        self.set_input_value(id="종목코드",   value=stock_code)
        self.set_input_value(id="기준일자",   value=datetime.datetime.now().strftime('%Y%m%d'))
        self.set_input_value(id="수정주가구분", value=0)   # 비수정: 실제 거래량
        self.set_input_value(id="거래소구분",  value="3") # KRX+NXT 통합
        self.comm_rq_data(rqname="opt10081_investor_vol_req", trcode="opt10081",
                          next=0, screen_no=self._get_screen_num())
        logger.info(f"[opt10081 vol] 요청: {stock_code}")

    def _send_opt10059(self, stock_code, end_date, amount_qty, trade_type, unit):
        self.set_input_value(id="일자",       value=end_date)
        self.set_input_value(id="종목코드",    value=stock_code)
        self.set_input_value(id="금액수량구분", value=amount_qty)
        self.set_input_value(id="매매구분",    value=trade_type)
        self.set_input_value(id="단위구분",    value=unit)
        self.set_input_value(id="거래소구분",  value="3")   # KRX+NXT 통합
        self.comm_rq_data(rqname="opt10059_req", trcode="opt10059", next=0, screen_no=self._get_screen_num())
        logger.info(f"[opt10059] 요청 전송: 종목={stock_code} 금액수량={amount_qty} 매매={trade_type} 거래소=통합")

    def request_investor_data_flow_only(self, stock_code: str, end_date: str) -> None:
        """수급흐름 배치 전용 — opt10059만 요청 (opt10081 생략으로 TR 절반 절감).
        응답은 기존 on_opt10059_req → on_receive_investor_data 경로 공유."""
        stock_code = stock_code.strip()
        today = end_date
        self._investor_trade_type = "0"   # 순매수
        self.tr_req_queue.put([self._send_opt10059, stock_code, today, "1", "0", "1"])

    # ── opt90013 종목일별프로그램매매추이 ────────────────────────

    def request_prog_trade_daily(self, stock_code: str, date: str) -> None:
        """opt90013 종목일별프로그램매매추이 — 오늘 프로그램 순매수금액 조회.
        배치당 1TR만 사용 (opt10081 없음)."""
        self.tr_req_queue.put([self._send_opt90013, stock_code.strip(), date])

    def _send_opt90013(self, stock_code: str, date: str) -> None:
        self.set_input_value(id="시간일자구분", value="1")   # 1=일별
        self.set_input_value(id="금액수량구분", value="1")   # 1=금액
        self.set_input_value(id="종목코드",     value=stock_code)
        self.set_input_value(id="날짜",         value=date)
        self.comm_rq_data(rqname="opt90013_prog_req", trcode="opt90013",
                          next=0, screen_no=self._get_screen_num())
        logger.info(f"[opt90013] 요청: 종목={stock_code} 날짜={date}")

    def _on_opt90013_prog_req(self, trcode: str, rqname: str) -> None:
        """opt90013 응답 파싱 → on_receive_program_trade_data(code, prog_net) 콜백."""
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        if data_cnt == 0:
            self.on_receive_program_trade_data("", 0.0)
            return

        today_str = datetime.datetime.now().strftime("%Y%m%d")
        prog_net  = 0.0
        code_used = getattr(self, '_auto_investor_code', '')

        for i in range(min(data_cnt, 3)):   # 최대 3행만 탐색
            일자 = self._comm_get_data(trcode, "", rqname, i, "일자").strip()
            if 일자 == today_str:
                raw = self._comm_get_data(trcode, "", rqname, i, "프로그램순매수금액").strip()
                try:
                    prog_net = float(raw.replace(",", "").replace("+", "") or 0)
                except ValueError:
                    prog_net = 0.0
                break
            elif i == 0 and 일자:
                # 오늘 날짜가 row0에 없으면 row0을 당일 최신값으로 사용
                raw = self._comm_get_data(trcode, "", rqname, 0, "프로그램순매수금액").strip()
                try:
                    prog_net = float(raw.replace(",", "").replace("+", "") or 0)
                except ValueError:
                    prog_net = 0.0
                break

        logger.debug(f"[opt90013] {code_used} 프로그램순매수={prog_net:+,.0f}")
        self.on_receive_program_trade_data(code_used, prog_net)

    def on_receive_program_trade_data(self, code: str, prog_net: float) -> None:
        """opt90013 콜백 — 하위 클래스에서 오버라이드."""
        pass

    def request_top_trading_value(self, market_type='000', filter_mode=0):
        # filter_mode: 0=전체, 1=ETF+ETN 제외, 2=ETF 제외, 3=ETN 제외
        self._top_trading_filter_mode = filter_mode
        self._opt10032_acc = []          # 페이지 누적 초기화
        self._top_trading_market = market_type
        self.set_input_value(id="시장구분",    value=market_type)
        self.set_input_value(id="관리종목포함", value="0")
        self.set_input_value(id="거래소구분",  value="3")   # 3=통합(KRX+NXT)
        self.comm_rq_data(rqname="opt10032_req", trcode="opt10032", next=0, screen_no=self._get_screen_num())

    def request_theme_group(self, date_offset: int = 1, sort_mode: int = 3):
        """opt90001 테마그룹별조회
        sort_mode: 1=상위기간수익률, 2=하위기간수익률, 3=상위등락률, 4=하위등락률
        """
        self.set_input_value(id="검색구분",    value="0")          # 0=전체검색
        self.set_input_value(id="날짜구분",    value=str(date_offset))
        self.set_input_value(id="등락수익구분", value=str(sort_mode))
        self.set_input_value(id="거래소구분",  value="3")           # 3=통합
        self.comm_rq_data(rqname="opt90001_req", trcode="opt90001", next=0, screen_no=self._get_screen_num())

    def request_theme_stocks(self, theme_name: str):
        """opt90002 테마구성종목요청 (클릭 → 화면 표시용)"""
        self.set_input_value(id="테마명", value=theme_name)
        self.set_input_value(id="거래소구분", value="3")
        self.comm_rq_data(rqname="opt90002_req", trcode="opt90002", next=0, screen_no=self._get_screen_num())

    def request_theme_stocks_for_sector(self, theme_name: str):
        """opt90002 테마구성종목요청 (업종맵 자동 갱신 전용, TR 큐 경유)"""
        self._theme_sector_queue.append(theme_name)
        self.set_input_value(id="테마명", value=theme_name)
        self.set_input_value(id="거래소구분", value="3")
        self.comm_rq_data(rqname="opt90002_sector_req", trcode="opt90002", next=0, screen_no=self._get_screen_num())

    # ========================
    # TR 응답 핸들러
    # ========================

    def _receive_tr_data(self, sScrnNo, sRQName, sTrCode, sRecordName, sPrevNext,
                         nDataLength, sErrorCode, sMessage, sSplmMsg):
        try:
            if sRQName == "opt10080_req":  # noqa
                self.on_opt10080_req(sTrCode, sRQName)
            elif sRQName == "opt10081_req":
                self.on_opt10081_req(sTrCode, sRQName)
            elif sRQName == "opt10082_req":
                self.on_opt10082_req(sTrCode, sRQName)
            elif sRQName == "opt10083_req":
                self.on_opt10083_req(sTrCode, sRQName)
            elif sRQName == "opw00018_req":
                self.on_opw00018_req(sTrCode, sRQName)
            elif sRQName == "opw00016_req":
                self.on_opw00016_req(sTrCode, sRQName)
            elif sRQName == "opw00004_req":
                self.on_opw00004_req(sTrCode, sRQName)
            elif sRQName == "opt10081_investor_vol_req":
                self._on_opt10081_investor_vol_req(sTrCode, sRQName)
            elif sRQName == "opt10059_req":
                self.on_opt10059_req(sTrCode, sRQName)
            elif sRQName == "opt90013_prog_req":
                self._on_opt90013_prog_req(sTrCode, sRQName)
            elif sRQName == "opt10070_req":
                self.on_opt10070_req(sTrCode, sRQName)
            elif sRQName == "opt10032_req":
                self.on_opt10032_req(sTrCode, sRQName, sPrevNext)
            elif sRQName == "opt90001_req":
                self.on_opt90001_req(sTrCode, sRQName)
            elif sRQName == "opt90002_req":
                self.on_opt90002_req(sTrCode, sRQName)
            elif sRQName == "opt90002_sector_req":
                self.on_opt90002_sector_req(sTrCode, sRQName)
            elif sRQName == "opt10063_req":
                self.on_opt10063_req(sTrCode, sRQName)
            elif sRQName == "opt50028_req":
                logger.info(f"[opt50028] _receive_tr_data: errCode={sErrorCode!r} msg={sMessage!r} prevNext={sPrevNext!r} cnt={self._get_repeat_cnt(sTrCode, sRQName)}")
                self.on_opt50028_req(sTrCode, sRQName)
        except Exception as e:
            logger.exception(e)

    def on_opt10080_req(self, trcode, rqname):
        종목코드 = self._comm_get_data(trcode, "", rqname, 0, "종목코드").replace("_AL", "").replace("A", "")
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            date   = self._comm_get_data(trcode, "", rqname, i, "체결시간")
            close  = self._comm_get_data(trcode, "", rqname, i, "현재가")
            open_  = self._comm_get_data(trcode, "", rqname, i, "시가")
            high   = self._comm_get_data(trcode, "", rqname, i, "고가")
            low    = self._comm_get_data(trcode, "", rqname, i, "저가")
            volume = self._comm_get_data(trcode, "", rqname, i, "거래량")
            rows.append([date, abs(int(open_)), abs(int(high)), abs(int(low)), abs(int(close)), abs(int(volume))])
        df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])[::-1]
        df = df.reset_index(drop=True)
        self.on_receive_candle_data(종목코드, df, chart_type="분봉")

    def on_opt50028_req(self, trcode, rqname):
        """해외선물 분봉차트 수신 → on_receive_overseas_futures_data 콜백."""
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        # 헤더 단일데이터에서 종목코드 시도
        code = (self._comm_get_data(trcode, "", rqname, 0, "종목코드").strip() or
                self._comm_get_data(trcode, "", rqname, 0, "단축코드").strip())
        # 데이터가 0인 경우 진단용 필드명 탐색 로깅
        if data_cnt == 0:
            probe_fields = ["체결시간", "현재가", "시가", "고가", "저가", "거래량",
                            "일자", "시가1", "종가", "틱시간", "분봉시간"]
            for f in probe_fields:
                val = self._comm_get_data(trcode, "", rqname, 0, f)
                if val and val.strip():
                    logger.info(f"[opt50028] probe field '{f}' = {val!r}")
            logger.warning(f"[opt50028] 데이터 0행 — 종목코드={code!r} trcode={trcode!r}")
            return
        rows = []
        for i in range(data_cnt):
            date   = self._comm_get_data(trcode, "", rqname, i, "체결시간")
            close  = self._comm_get_data(trcode, "", rqname, i, "현재가").replace(',', '').strip()
            open_  = self._comm_get_data(trcode, "", rqname, i, "시가").replace(',', '').strip()
            high   = self._comm_get_data(trcode, "", rqname, i, "고가").replace(',', '').strip()
            low    = self._comm_get_data(trcode, "", rqname, i, "저가").replace(',', '').strip()
            volume = self._comm_get_data(trcode, "", rqname, i, "거래량").replace(',', '').strip()
            try:
                rows.append([date,
                              float(open_)  if open_  else 0.0,
                              float(high)   if high   else 0.0,
                              float(low)    if low    else 0.0,
                              float(close)  if close  else 0.0,
                              abs(int(float(volume))) if volume else 0])
            except ValueError:
                continue
        df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])[::-1]
        df = df.reset_index(drop=True)
        logger.info(f"[opt50028] {code} 수신 {len(df)}행")
        self.on_receive_overseas_futures_data(code, df)

    def on_opt10063_req(self, trcode, rqname):
        """장중투자자별매매 응답 파싱 → on_receive_intraday_investor 콜백.
        단위: 백만원 (÷100 하면 억원)
        """
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        code = (self._comm_get_data(trcode, "", rqname, 0, "종목코드").strip() or
                self._comm_get_data(trcode, "", rqname, 0, "단축코드").strip())

        if data_cnt == 0:
            probe_fields = ["집계시간", "집계구분", "시간구분", "구분", "시간",
                            "외국인", "외인", "기관계", "보험", "투신", "은행", "연기금등", "기타법인"]
            for f in probe_fields:
                val = self._comm_get_data(trcode, "", rqname, 0, f)
                if val and val.strip():
                    logger.info(f"[opt10063] probe '{f}' = {val!r}")
            logger.warning(f"[opt10063] 데이터 0행 — code={code!r}")
            self.on_receive_intraday_investor(code, [])
            return

        def _probe(candidates):
            for row_idx in range(min(5, data_cnt)):
                for cname in candidates:
                    v = self._comm_get_data(trcode, "", rqname, row_idx, cname).strip()
                    if v:
                        return cname
            return candidates[0]

        period_field  = _probe(["집계시간", "집계구분", "시간구분", "구분", "시간"])
        foreign_field = _probe(["외국인", "외인"])
        inst_field    = _probe(["기관계", "기관"])
        insure_field  = _probe(["보험", "보험사"])
        trust_field   = _probe(["투신", "투신사"])
        bank_field    = _probe(["은행"])
        pension_field = _probe(["연기금등", "연기금"])
        other_field   = _probe(["기타법인"])

        logger.debug(
            f"[opt10063] {code!r} cnt={data_cnt} "
            f"period={period_field!r} 외인={foreign_field!r} 기관={inst_field!r}"
        )

        rows = []
        for i in range(data_cnt):
            period = self._comm_get_data(trcode, "", rqname, i, period_field).strip()
            rows.append({
                "집계시간": period,
                "외국인":  self._to_int(self._comm_get_data(trcode, "", rqname, i, foreign_field)),
                "기관계":  self._to_int(self._comm_get_data(trcode, "", rqname, i, inst_field)),
                "보험":    self._to_int(self._comm_get_data(trcode, "", rqname, i, insure_field)),
                "투신":    self._to_int(self._comm_get_data(trcode, "", rqname, i, trust_field)),
                "은행":    self._to_int(self._comm_get_data(trcode, "", rqname, i, bank_field)),
                "연기금등": self._to_int(self._comm_get_data(trcode, "", rqname, i, pension_field)),
                "기타법인": self._to_int(self._comm_get_data(trcode, "", rqname, i, other_field)),
            })
        self.on_receive_intraday_investor(code, rows)

    def on_opt10081_req(self, trcode, rqname):
        종목코드 = self._comm_get_data(trcode, "", rqname, 0, "종목코드").replace("_AL", "").replace("A", "")
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            date   = self._comm_get_data(trcode, "", rqname, i, "일자")
            open_  = self._comm_get_data(trcode, "", rqname, i, "시가")
            high   = self._comm_get_data(trcode, "", rqname, i, "고가")
            low    = self._comm_get_data(trcode, "", rqname, i, "저가")
            close  = self._comm_get_data(trcode, "", rqname, i, "현재가")
            volume = self._comm_get_data(trcode, "", rqname, i, "거래량")
            rows.append([date, abs(int(open_)), abs(int(high)), abs(int(low)), abs(int(close)), abs(int(volume))])
        df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])[::-1]
        df = df.reset_index(drop=True)
        self.on_receive_candle_data(종목코드, df, chart_type="일봉")

    def on_opt10082_req(self, trcode, rqname):
        pass

    def on_opt10083_req(self, trcode, rqname):
        pass

    def on_opw00018_req(self, trcode, rqname):
        pass

    def on_opw00016_req(self, trcode, rqname):
        pass

    def on_opw00004_req(self, trcode, rqname):
        pass

    # ETF 판별용 브랜드명 (대문자 비교)
    _ETF_BRANDS = {
        "KODEX", "TIGER", "KINDEX", "KOSEF", "ARIRANG", "HANARO",
        "SOL", "ACE", "RISE", "TIMEFOLIO", "TREX", "PLUS", "MASTER",
    }
    # 채권/파생 상품 식별 키워드 (일반 주식에는 없는 단어)
    _BOND_KEYWORDS = {"액티브", "회사채", "국채", "채권", "머니마켓"}

    def _is_etf(self, name: str) -> bool:
        u = name.upper()
        if "ETF" in u:
            return True
        if any(kw in name for kw in self._BOND_KEYWORDS):
            return True
        return any(u.startswith(b) or f" {b}" in u for b in self._ETF_BRANDS)

    def _is_etn(self, name: str) -> bool:
        return "ETN" in name.upper()

    def on_opt10032_req(self, trcode, rqname, prev_next="0"):
        # opt10032 출력 필드: 종목코드, 현재순위, 전일순위, 종목명,
        #   현재가, 전일대비기호, 전일대비, 등락률, 매도호가, 매수호가,
        #   현재거래량, 전일거래량, 거래대금   (시가총액 없음)
        filter_mode = getattr(self, '_top_trading_filter_mode', 0)
        data_cnt    = self._get_repeat_cnt(trcode, rqname)

        if not hasattr(self, '_opt10032_acc'):
            self._opt10032_acc = []

        for i in range(data_cnt):
            종목코드 = self._comm_get_data(trcode, "", rqname, i, "종목코드").strip()
            전일순위 = self._comm_get_data(trcode, "", rqname, i, "전일순위").strip()
            종목명   = self._comm_get_data(trcode, "", rqname, i, "종목명").strip()
            현재가   = self._comm_get_data(trcode, "", rqname, i, "현재가").strip()
            전일대비 = self._comm_get_data(trcode, "", rqname, i, "전일대비").strip()
            등락률   = self._comm_get_data(trcode, "", rqname, i, "등락률").strip()
            매도호가 = self._comm_get_data(trcode, "", rqname, i, "매도호가").strip()
            매수호가 = self._comm_get_data(trcode, "", rqname, i, "매수호가").strip()
            거래량   = self._comm_get_data(trcode, "", rqname, i, "현재거래량").strip()
            거래대금 = self._comm_get_data(trcode, "", rqname, i, "거래대금").strip()

            # 종목조건 필터
            if filter_mode == 1 and (self._is_etf(종목명) or self._is_etn(종목명)):
                continue
            if filter_mode == 2 and self._is_etf(종목명):
                continue
            if filter_mode == 3 and self._is_etn(종목명):
                continue

            # 시가총액: TR에 없으므로 OCX 마스터 데이터로 직접 계산 (억원)
            try:
                _price  = abs(int(self.kiwoom.dynamicCall("GetMasterLastPrice(QString)", 종목코드)))
                _shares = abs(int(self.kiwoom.dynamicCall("GetMasterListedStockCnt(QString)", 종목코드)))
                시가총액_억 = _price * _shares // 100_000_000
            except Exception:
                시가총액_억 = 0

            clean_for_sector = 종목코드.replace('_AL', '').strip()
            업종명 = self.stock_code_to_sector.get(clean_for_sector, "")

            clean_code = 종목코드.replace('_AL', '').strip()
            is_nxt = clean_code in self.NXT_code_set and bool(self.NXT_code_set)

            try:
                self._opt10032_acc.append({
                    "전일순위": int(전일순위)     if 전일순위 else 0,
                    "종목코드": 종목코드,
                    "종목명":  종목명,
                    "업종":    업종명,
                    "현재가":  abs(int(현재가))  if 현재가  else 0,
                    "전일대비": int(전일대비)     if 전일대비 else 0,
                    "등락률":  float(등락률)     if 등락률  else 0.0,
                    "매도호가": abs(int(매도호가)) if 매도호가 else 0,
                    "매수호가": abs(int(매수호가)) if 매수호가 else 0,
                    "거래량":  abs(int(거래량))  if 거래량  else 0,
                    "시가총액": 시가총액_억,
                    "거래대금": abs(int(거래대금)) if 거래대금 else 0,
                    "NXT":     is_nxt,
                })
            except (ValueError, TypeError):
                continue

        # 50개 미만이고 다음 페이지가 있으면 계속 요청
        if str(prev_next) == '2' and len(self._opt10032_acc) < 50:
            market = getattr(self, '_top_trading_market', '000')
            self.set_input_value(id="시장구분",    value=market)
            self.set_input_value(id="관리종목포함", value="0")
            self.set_input_value(id="거래소구분",  value="3")
            self.comm_rq_data(rqname="opt10032_req", trcode="opt10032", next=2, screen_no=self._get_screen_num())
            return

        # 완료 — 최대 50개로 자르고 순위 부여 후 emit
        final = self._opt10032_acc[:50]
        self._opt10032_acc = []
        for idx, row in enumerate(final):
            row["순위"] = idx + 1
        df = pd.DataFrame(final)
        self.on_receive_top_trading_value(df)

    def on_receive_top_trading_value(self, df):
        pass

    # ── opt90001 테마그룹별조회 핸들러 ───────────────────────────

    def on_opt90001_req(self, trcode, rqname):
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            테마명    = self._comm_get_data(trcode, "", rqname, i, "테마명").strip()
            종목수    = self._comm_get_data(trcode, "", rqname, i, "종목수").strip()
            등락기호  = self._comm_get_data(trcode, "", rqname, i, "등락기호").strip()
            등락률    = self._comm_get_data(trcode, "", rqname, i, "등락률").strip()
            상승종목수 = self._comm_get_data(trcode, "", rqname, i, "상승종목수").strip()
            하락종목수 = self._comm_get_data(trcode, "", rqname, i, "하락종목수").strip()
            기간수익률 = self._comm_get_data(trcode, "", rqname, i, "기간수익률").strip()
            주요종목  = self._comm_get_data(trcode, "", rqname, i, "주요종목").strip()
            if not 테마명:
                continue
            try:
                pct = float(등락률) if 등락률 else 0.0
                sign = 1 if 등락기호 in ("2", "1") else -1
                rows.append({
                    "테마명":    테마명,
                    "종목수":    int(종목수)    if 종목수    else 0,
                    "등락률":    sign * abs(pct),
                    "상승종목수": int(상승종목수) if 상승종목수 else 0,
                    "하락종목수": int(하락종목수) if 하락종목수 else 0,
                    "기간수익률": float(기간수익률) if 기간수익률 else 0.0,
                    "주요종목":  주요종목,
                })
            except (ValueError, TypeError):
                continue
        df = pd.DataFrame(rows)
        self.on_receive_theme_data(df)

    def on_receive_theme_data(self, df: pd.DataFrame):
        pass

    # ── opt90002 테마구성종목요청 핸들러 ─────────────────────────

    def on_opt90002_req(self, trcode, rqname):
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            종목코드 = self._comm_get_data(trcode, "", rqname, i, "종목코드").strip()
            종목명   = self._comm_get_data(trcode, "", rqname, i, "종목명").strip()
            현재가   = self._comm_get_data(trcode, "", rqname, i, "현재가").strip()
            전일대비 = self._comm_get_data(trcode, "", rqname, i, "전일대비").strip()
            등락률   = self._comm_get_data(trcode, "", rqname, i, "등락률").strip()
            거래량   = self._comm_get_data(trcode, "", rqname, i, "거래량").strip()
            if not 종목코드:
                continue
            try:
                rows.append({
                    "종목코드": 종목코드,
                    "종목명":  종목명,
                    "현재가":  abs(int(현재가))  if 현재가  else 0,
                    "전일대비": int(전일대비)     if 전일대비 else 0,
                    "등락률":  float(등락률)     if 등락률  else 0.0,
                    "거래량":  abs(int(거래량))  if 거래량  else 0,
                })
            except (ValueError, TypeError):
                continue
        df = pd.DataFrame(rows)
        self.on_receive_theme_stocks(df)

    def on_receive_theme_stocks(self, df: pd.DataFrame):
        pass

    # ── opt90002_sector_req: 업종맵 자동 갱신 전용 핸들러 ────────

    def on_opt90002_sector_req(self, trcode, rqname):
        theme_name = self._theme_sector_queue.pop(0) if self._theme_sector_queue else ""
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        for i in range(data_cnt):
            code = self._comm_get_data(trcode, "", rqname, i, "종목코드").strip()
            # opt90002가 'A005930' 형식으로 반환하는 경우 접두사 제거
            if len(code) == 7 and code[0] == 'A':
                code = code[1:]
            if code and theme_name:
                self.stock_code_to_sector[code] = theme_name
        self._theme_sector_done += 1
        remaining = self._theme_sector_total - self._theme_sector_done
        logger.info(f"[업종맵] ({self._theme_sector_done}/{self._theme_sector_total}) "
                    f"'{theme_name}' {data_cnt}개 / 잔여 {remaining}개 "
                    f"/ 누적 {len(self.stock_code_to_sector)}개")
        # 카운터 기반 완료 체크 (큐 기반 조기 완료 버그 방지)
        if self._theme_sector_total > 0 and self._theme_sector_done >= self._theme_sector_total:
            self._theme_sector_total = 0
            self._theme_sector_done = 0
            self.on_theme_sector_map_updated()

    def on_theme_sector_map_updated(self):
        pass

    def on_tr_usage_updated(self, used: int, remaining: int):
        pass

    # ── opt10059 종목별투자자기관별 핸들러 ────────────────────
    # KOA Studio 확인 필드: 전일대비, 매도량, 매수량, 순매수수량, 거래량합, 거래비중
    # 멀티데이터: 투자자 유형별 행(개인/외국인/기관계...순서 고정)
    _INVESTOR_ORDER = [
        "개인", "외국인", "기관계", "금융투자", "보험", "투신",
        "기타금융", "은행", "연기금등", "사모펀드", "국가", "기타법인", "내외국인",
    ]

    @staticmethod
    def _to_int(s: str) -> int:
        """공백·부호·쉼표 포함 문자열을 정수로 변환. 빈 문자열이면 0."""
        s = s.strip().replace(",", "")
        if not s:
            return 0
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return 0

    def _on_opt10081_investor_vol_req(self, trcode, rqname):
        """opt10081 일봉 거래량을 dict(일자→거래량)로 캐시"""
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        cache = {}
        for i in range(data_cnt):
            date   = self._comm_get_data(trcode, "", rqname, i, "일자").strip()
            volume = self._comm_get_data(trcode, "", rqname, i, "거래량").strip()
            if date and volume:
                try:
                    cache[date] = abs(int(volume))
                except ValueError:
                    pass
        self._investor_volume_cache = cache
        logger.info(f"[opt10081 vol] {len(cache)}건 캐시 완료")

    def on_opt10059_req(self, trcode, rqname):
        data_cnt = self._get_repeat_cnt(trcode, rqname)

        if data_cnt == 0:
            self.common_log("[투자자] 수신 데이터 없음 (data_cnt=0)")
            self.on_receive_investor_data(pd.DataFrame())
            return

        # ── 실제 API 필드명 탐색 ──────────────────────────────────
        # KOA Studio 표기와 실제 CommGetData 필드명이 다를 수 있으므로
        # row 0 기준으로 후보 이름을 순서대로 시도해 첫 번째 비어있지 않은 것을 사용
        # 최근 row가 당일 미집계이므로 최대 10행까지 확인
        def _probe(candidates):
            for row_idx in range(min(10, data_cnt)):
                for cname in candidates:
                    v = self._comm_get_data(trcode, "", rqname, row_idx, cname).strip()
                    if v:
                        return cname
            return candidates[0]

        개인_field  = _probe(["개인", "개인투자자", "개인계"])
        외국인_field = _probe(["외국인", "외국인투자자", "외국인계"])
        거래량_field = _probe(["거래량합", "거래량", "현재거래량", "거래대금"])
        종가_field  = _probe(["현재가", "종가"])   # 현재가: 오늘=실시간, 과거=종가
        today_str   = datetime.datetime.now().strftime('%Y%m%d')

        # 거래량 probe 결과 확인 (row0=오늘, row1=어제)
        _r0_거래량 = self._comm_get_data(trcode, "", rqname, 0, 거래량_field).strip() if data_cnt > 0 else ""
        _r1_거래량 = self._comm_get_data(trcode, "", rqname, 1, 거래량_field).strip() if data_cnt > 1 else ""

        row0_일자   = self._comm_get_data(trcode, "", rqname, 0, "일자").strip()
        row1_일자   = self._comm_get_data(trcode, "", rqname, 1, "일자").strip() if data_cnt > 1 else ""
        row0_순매수  = self._comm_get_data(trcode, "", rqname, 0, "순매수수량").strip()
        row0_개인    = self._comm_get_data(trcode, "", rqname, 0, 개인_field).strip()

        _r0_종가 = self._comm_get_data(trcode, "", rqname, 0, 종가_field).strip() if data_cnt > 0 else ""
        _r1_종가 = self._comm_get_data(trcode, "", rqname, 1, 종가_field).strip() if data_cnt > 1 else ""
        _r0_개인 = self._comm_get_data(trcode, "", rqname, 0, 개인_field).strip() if data_cnt > 0 else ""
        logger.info(
            f"[opt10059] cnt={data_cnt} 거래량필드={repr(거래량_field)} 종가필드={repr(종가_field)} "
            f"r0거래량={repr(_r0_거래량)} r1거래량={repr(_r1_거래량)} "
            f"r0종가={repr(_r0_종가)} r1종가={repr(_r1_종가)} r0개인={repr(_r0_개인)}"
        )

        trade_type = getattr(self, '_investor_trade_type', '0')
        val_key = {"1": "매수량", "2": "매도량"}.get(trade_type, "순매수수량")

        # 실제 API 필드명 → DataFrame 컬럼명(= _INVESTOR_ORDER) 매핑
        # 처음 두 항목(개인/외국인)만 다를 수 있음
        api_field_names = [개인_field, 외국인_field] + list(self._INVESTOR_ORDER[2:])

        df_rows: list = []

        # ── 구조 A: 날짜당 1행 — 투자자 데이터를 필드명으로 직접 읽기 ──
        one_per_date    = (row1_일자 != row0_일자)
        use_field_names = bool(row0_개인)

        if one_per_date or use_field_names:
            for i in range(data_cnt):
                일자 = self._comm_get_data(trcode, "", rqname, i, "일자").strip()
                if len(일자) != 8 or not 일자.isdigit():
                    continue
                종가  = abs(self._to_int(self._comm_get_data(trcode, "", rqname, i, 종가_field)))
                대비  = self._to_int(self._comm_get_data(trcode, "", rqname, i, "전일대비"))
                거래량 = abs(self._to_int(self._comm_get_data(trcode, "", rqname, i, 거래량_field)))
                if not 거래량:
                    for _alt in ["거래량합", "거래량", "현재거래량", "거래대금", "체결량"]:
                        if _alt == 거래량_field:
                            continue
                        거래량 = abs(self._to_int(self._comm_get_data(trcode, "", rqname, i, _alt)))
                        if 거래량:
                            break
                pivot: dict = {"일자": 일자, "종가": 종가, "대비": 대비, "거래량": 거래량}
                for col_name, field_name in zip(self._INVESTOR_ORDER, api_field_names):
                    v = self._comm_get_data(trcode, "", rqname, i, field_name).strip()
                    pivot[col_name] = self._to_int(v)
                df_rows.append(pivot)
            self.common_log(
                f"[투자자] 구조A(날짜별1행) → {len(df_rows)}건 파싱 "
                f"| 개인0={df_rows[0].get('개인') if df_rows else 'N/A'}"
            )

        # ── 구조 B: 날짜당 13행 — 투자자유형별 행으로 피벗 ────────
        if not df_rows:
            raw_rows = []
            for i in range(data_cnt):
                일자     = self._comm_get_data(trcode, "", rqname, i, "일자").strip()
                종가     = abs(self._to_int(self._comm_get_data(trcode, "", rqname, i, 종가_field)))
                전일대비  = self._comm_get_data(trcode, "", rqname, i, "전일대비").strip()
                _거래량합 = self._comm_get_data(trcode, "", rqname, i, 거래량_field).strip()
                if not _거래량합:
                    for _alt in ["거래량합", "거래량", "현재거래량", "거래대금"]:
                        if _alt == 거래량_field:
                            continue
                        _거래량합 = self._comm_get_data(trcode, "", rqname, i, _alt).strip()
                        if _거래량합:
                            break
                거래량합 = _거래량합
                매도량    = self._comm_get_data(trcode, "", rqname, i, "매도량").strip()
                매수량    = self._comm_get_data(trcode, "", rqname, i, "매수량").strip()
                순매수수량  = self._comm_get_data(trcode, "", rqname, i, "순매수수량").strip()
                raw_rows.append({
                    "일자": 일자, "종가": 종가, "대비": self._to_int(전일대비),
                    "거래량": abs(self._to_int(거래량합)),
                    "매도량": self._to_int(매도량), "매수량": self._to_int(매수량),
                    "순매수수량": self._to_int(순매수수량),
                })

            n_types = len(self._INVESTOR_ORDER)
            n_dates = data_cnt // n_types
            for d in range(n_dates):
                base  = raw_rows[d * n_types]
                date  = base["일자"] or f"row{d*n_types}"
                pivot = {"일자": date, "종가": base["종가"],
                         "대비": base["대비"], "거래량": base["거래량"]}
                for t, name in enumerate(self._INVESTOR_ORDER):
                    r = raw_rows[d * n_types + t]
                    pivot[name] = r[val_key]
                df_rows.append(pivot)
            self.common_log(
                f"[투자자] 구조B(13행/날짜) → {len(df_rows)}건 파싱 "
                f"| 개인0={df_rows[0].get('개인') if df_rows else 'N/A'}"
            )

        df = pd.DataFrame(df_rows)

        # opt10081 캐시에서 거래량 보완 (opt10059가 최근 1~2일 거래량합 미반환 시)
        vol_cache = getattr(self, '_investor_volume_cache', {})
        if vol_cache and not df.empty:
            patched = 0
            for idx, row in df.iterrows():
                if not row.get('거래량'):
                    cached_vol = vol_cache.get(row['일자'], 0)
                    if cached_vol:
                        df.at[idx, '거래량'] = cached_vol
                        patched += 1
            if patched:
                logger.info(f"[opt10059] opt10081 캐시로 거래량 {patched}행 보완")

        _r0 = df["거래량"].iloc[0] if not df.empty else "N/A"
        _r1 = df["거래량"].iloc[1] if len(df) > 1 else "N/A"
        self.common_log(
            f"[투자자] {len(df)}건 파싱완료 | 거래량필드={repr(거래량_field)} "
            f"row0거래량={_r0} row1거래량={_r1}"
        )
        self.on_receive_investor_data(df)

    def on_receive_investor_data(self, df: pd.DataFrame):
        pass

    # ── opt10070 당일주요거래원 핸들러 (싱글데이터) ────────────
    def on_opt10070_req(self, trcode, rqname):
        """
        opt10070 싱글데이터: 오늘 주요 거래원 스냅샷 (index=0 고정)
        Output 필드명은 KOA Studio 확인 기반 — 다를 경우 로그로 확인
        """
        snapshot: dict = {}
        for n in range(1, 6):
            매수사 = self._comm_get_data(trcode, "", rqname, 0, f"매수거래원{n}").strip()
            매수량 = self._comm_get_data(trcode, "", rqname, 0, f"매수거래원수량{n}").strip()
            매도사 = self._comm_get_data(trcode, "", rqname, 0, f"매도거래원{n}").strip()
            매도량 = self._comm_get_data(trcode, "", rqname, 0, f"매도거래원수량{n}").strip()
            logger.debug(
                f"[opt10070] n={n}: "
                f"매수거래원={repr(매수사)} 매수거래원수량={repr(매수량)} | "
                f"매도거래원={repr(매도사)} 매도거래원수량={repr(매도량)}"
            )
            snapshot[f"매수거래원{n}"]     = 매수사
            snapshot[f"매수거래원수량{n}"] = abs(int(매수량)) if 매수량 else 0
            snapshot[f"매도거래원{n}"]     = 매도사
            snapshot[f"매도거래원수량{n}"] = abs(int(매도량)) if 매도량 else 0
        self.on_receive_broker_analysis_data(snapshot)

    def on_receive_broker_analysis_data(self, df):
        pass

    def on_receive_broker_realtime(self, stock_code: str, data: dict):
        pass

    def on_receive_intraday_investor(self, code: str, rows: list):
        """장중투자자별매매 수신 콜백.
        rows: [{"집계시간": "1차", "외국인": 백만원, "기관계": 백만원, ...}, ...]
        """
        pass

    def on_receive_candle_data(self, stock_code, df, chart_type=''):
        pass

    def on_receive_index_candle_data(self, code, name, df, chart_type=''):
        pass

    # ========================
    # 실시간 데이터
    # ========================

    def register_code_to_realtime_list(self, code, is_register_nxt=True):
        fid_list = "10;12;20;28"
        if len(code) != 0 and code not in self.realtime_registered_codes:
            self.realtime_registered_codes.add(code)
            if is_register_nxt and not self.is_paper_trading:
                code += "_AL"
            self.set_real(self._get_screen_num(), code, fid_list, strRealType="1")
            logger.info(f"{code}, 실시간 등록 완료!")

    def register_index_to_realtime_list_by_name(self, target_name="KOSPI"):
        code = self.index_name_to_code_map.get(target_name, "")
        fid_list = "10;12;20;28"
        if len(code) != 0 and code not in self.realtime_registered_codes:
            self.realtime_registered_codes.add(code)
            self.set_real(self._get_screen_num(), code, fid_list, strRealType="1")
            logger.info(f"{target_name}({code}), 실시간 등록 완료!")

    def _receive_realdata(self, sJongmokCode, sRealType, sRealData):
        try:
            종목코드 = sJongmokCode.replace("_AL", "")
            if sRealType == "주식체결":
                현재가   = int(self.get_comm_realdata(sRealType, nFid=10).replace('-', ''))
                전일대비  = float(self.get_comm_realdata(sRealType, nFid=11) or 0)
                등락률   = float(self.get_comm_realdata(sRealType, nFid=12) or 0)
                거래량   = abs(int(self.get_comm_realdata(sRealType, nFid=13) or 0))
                체결시간  = self.get_comm_realdata(sRealType, nFid=20).zfill(6)
                _es_raw  = self.get_comm_realdata(sRealType, nFid=228)
                체결강도  = float(_es_raw) if _es_raw and _es_raw not in ('', '-') else 100.0
                data = dict(
                    종목코드=종목코드, 현재가=현재가, 전일대비=전일대비,
                    등락률=등락률, 거래량=거래량, 체결강도=체결강도,
                )
                self.on_receive_realtime_tick_data(data)

            elif sRealType == "주식거래원":
                watching = getattr(self, '_broker_realtime_code', '')
                if 종목코드 != watching:
                    return
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                row = {"시간": now_str}
                for n, (sell_fid, buy_fid) in enumerate(
                    zip(range(72, 82, 2), range(82, 92, 2)), start=1
                ):
                    row[f"매도거래원{n}"] = self.get_comm_realdata(sRealType, sell_fid).strip()
                    qty = self.get_comm_realdata(sRealType, sell_fid + 1).strip()
                    row[f"매도수량{n}"] = abs(int(qty)) if qty else 0
                    row[f"매수거래원{n}"] = self.get_comm_realdata(sRealType, buy_fid).strip()
                    qty = self.get_comm_realdata(sRealType, buy_fid + 1).strip()
                    row[f"매수수량{n}"] = abs(int(qty)) if qty else 0
                logger.debug(
                    f"[주식거래원] {종목코드} 매수1={row['매수거래원1']}:{row['매수수량1']} "
                    f"매도1={row['매도거래원1']}:{row['매도수량1']}"
                )
                self.on_receive_broker_realtime(종목코드, row)

            elif sRealType == "업종지수":
                idx_name = self.index_code_to_name_map.get(종목코드, 종목코드)
                try:
                    등락률 = float(self.get_comm_realdata(sRealType, nFid=12).replace(',', ''))
                    t_str  = self.get_comm_realdata(sRealType, nFid=20).zfill(6)
                    self.on_receive_market_chart_data(idx_name, t_str, 등락률)
                except Exception:
                    pass

            elif sRealType == "선물시세":
                try:
                    등락률 = float(self.get_comm_realdata(sRealType, nFid=12).replace(',', ''))
                    t_str  = self.get_comm_realdata(sRealType, nFid=20).zfill(6)
                    self.on_receive_market_chart_data("선물", t_str, 등락률)
                except Exception:
                    pass

            elif sRealType == "FCCME시세":
                try:
                    현재가    = float(self.get_comm_realdata(sRealType, nFid=10).replace(',', '') or 0)
                    전일대비   = float(self.get_comm_realdata(sRealType, nFid=11).replace(',', '') or 0)
                    등락률    = float(self.get_comm_realdata(sRealType, nFid=12).replace(',', '') or 0)
                    거래량    = abs(int(self.get_comm_realdata(sRealType, nFid=15) or 0))
                    누적거래량  = abs(int(self.get_comm_realdata(sRealType, nFid=13) or 0))
                    체결시간   = self.get_comm_realdata(sRealType, nFid=20).strip()
                    시가      = float(self.get_comm_realdata(sRealType, nFid=182).replace(',', '') or 0)
                    고가      = float(self.get_comm_realdata(sRealType, nFid=184).replace(',', '') or 0)
                    저가      = float(self.get_comm_realdata(sRealType, nFid=183).replace(',', '') or 0)
                    data = dict(
                        종목코드=종목코드, 현재가=현재가, 전일대비=전일대비,
                        등락률=등락률, 거래량=거래량, 누적거래량=누적거래량,
                        체결시간=체결시간, 시가=시가, 고가=고가, 저가=저가,
                    )
                    self.on_receive_overseas_futures_realtime(종목코드, data)
                except Exception:
                    pass

        except Exception as e:
            logger.exception(e)

    # ========================
    # 체결 데이터
    # ========================

    def _receive_chejandata(self, sGubun, nItemCnt, sFIdList):
        try:
            if sGubun == "0":
                종목코드   = self.get_chejandata(9001).replace("A", "").strip()
                종목명    = self.get_chejandata(302).strip()
                주문체결시간 = self.get_chejandata(908).strip()
                체결수량   = 0 if len(self.get_chejandata(911)) == 0 else int(self.get_chejandata(911))
                체결가격   = 0 if len(self.get_chejandata(910)) == 0 else int(self.get_chejandata(910))
                미체결수량  = 0 if len(self.get_chejandata(902)) == 0 else int(self.get_chejandata(902))
                주문구분   = self.get_chejandata(905).replace("+", "").replace("-", "").strip()
                매매구분   = self.get_chejandata(906).strip()
                단위체결가  = 0 if len(self.get_chejandata(914)) == 0 else int(self.get_chejandata(914))
                단위체결량  = 0 if len(self.get_chejandata(915)) == 0 else int(self.get_chejandata(915))
                주문번호   = self.get_chejandata(9203).strip()
                계좌번호   = self.get_chejandata(9201).strip()

                logger.info(
                    f"체결: 종목={종목코드}, 주문구분={주문구분}, 체결수량={체결수량}, "
                    f"체결가={체결가격}, 미체결={미체결수량}, 주문번호={주문번호}"
                )

                # 미체결 주문 DataFrame 업데이트
                if 주문번호:
                    self.unfinished_orders_df.loc[주문번호] = {
                        "종목코드":   종목코드,
                        "종목명":    종목명,
                        "주문체결시간": 주문체결시간,
                        "미체결수량":  미체결수량,
                        "주문구분":   주문구분,
                    }
                    if 미체결수량 == 0:
                        self.unfinished_orders_df.drop(주문번호, inplace=True)

                data = dict(
                    계좌번호=계좌번호, 주문구분=주문구분, 종목코드=종목코드, 종목명=종목명,
                    미체결수량=미체결수량, 체결수량=체결수량, 체결가격=체결가격,
                    단위체결수량=단위체결량, 단위체결가격=단위체결가,
                )
                self.on_receive_order_data(data)
        except Exception as e:
            logger.exception(e)

    def _receive_msg(self, sScrNo, sRQName, sTrCode, sMsg):
        logger.info(f"Received MSG! 화면번호: {sScrNo}, 사용자 구분명: {sRQName}, TR이름: {sTrCode}, 메세지: {sMsg}")
        self.common_log(sMsg)

    # ========================
    # 종목 기본 정보
    # ========================

    def request_stock_basic_info(self, stock_code):
        self.tr_req_queue.put([self.get_basic_stock_info, stock_code])

    def get_basic_stock_info(self, stock_code):
        if stock_code is not None and len(stock_code) == 6:
            self.set_input_value(id="종목코드", value=stock_code)
            self.comm_rq_data(rqname="opt10001_req", trcode="opt10001", next=0, screen_no=self._get_screen_num())

    # ========================
    # 설정 저장/불러오기
    # ========================

    def save_settings(self):
        self.settings.setValue('telegramAPIKEYLineEdit',        self.telegramAPIKEYLineEdit.text())
        self.settings.setValue('telegramChatIDLineEdit',         self.telegramChatIDLineEdit.text())
        self.settings.setValue('telegramOnlyLoginRadioButton',   self.telegramOnlyLoginRadioButton.isChecked())
        self.settings.setValue('telegramSendAllRadioButton',     self.telegramSendAllRadioButton.isChecked())
        self.settings.setValue('maskAccountCheckBox',            self.maskAccountCheckBox.isChecked())
        self.settings.setValue('taxDoubleSpinBox',               self.taxDoubleSpinBox.value())
        self.settings.setValue('transactionFeeDoubleSpinBox',    self.transactionFeeDoubleSpinBox.value())
        self.on_save_settings()

    def on_save_settings(self):
        pass

    def load_settings(self):
        self.telegramAPIKEYLineEdit.setText(
            self.settings.value('telegramAPIKEYLineEdit', defaultValue='', type=str))
        self.telegramChatIDLineEdit.setText(
            self.settings.value('telegramChatIDLineEdit', defaultValue='', type=str))
        self.telegramOnlyLoginRadioButton.setChecked(
            self.settings.value('telegramOnlyLoginRadioButton', defaultValue=True, type=bool))
        self.telegramSendAllRadioButton.setChecked(
            self.settings.value('telegramSendAllRadioButton', defaultValue=False, type=bool))
        self.autoShutDownTimeEdit.setTime(
            QTime.fromString(self.settings.value('autoShutDownTimeEdit', "210000"), "HHmmss"))
        self.autoOnCheckBox.setChecked(
            self.settings.value('autoOnCheckBox', defaultValue=False, type=bool))
        self.taxDoubleSpinBox.setValue(
            self.settings.value("taxDoubleSpinBox", 0.15, float))
        self.transactionFeeDoubleSpinBox.setValue(
            self.settings.value("transactionFeeDoubleSpinBox", 0.015, float))
        if not self.settings.value('hasInit', defaultValue=False, type=bool):
            self.show_pending_message_box(
                title='초기 계좌 설정을 진행합니다!',
                message=set_pw_msg,
            )
            self.open_password_window()
            self.settings.setValue('hasInit', True)
        self.on_finished_password_settings()

    def on_finished_password_settings(self):
        pass

    # ========================
    # UI 헬퍼
    # ========================

    def common_log(self, msg: str):
        logger.info(msg)
        try:
            self.statusLineEdit.setText(msg)
        except Exception:
            pass

    def show_pending_message_box(self, title='', message=''):
        QMessageBox.information(self, title, message)

    def open_password_window(self):
        self.kiwoom.dynamicCall("KOA_Functions(QString, QString)", "ShowAccountWindow", "")

    def toggle_always_on_top(self, state):
        if state == Qt.Checked:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
        self.show()

    def custom_mask_checkbox_changed(self, state):
        self.update_custom_line_edit_masking()

    def update_custom_line_edit_masking(self):
        pass

    def update_transaction_cost(self):
        try:
            tax  = self.taxDoubleSpinBox.value()
            fee  = self.transactionFeeDoubleSpinBox.value()
            self.transaction_cost = tax + fee
        except AttributeError:
            self.transaction_cost = 0.165

    def convert_server(self):
        self.kiwoom.dynamicCall("KOA_Functions(QString, QString)", "ShowAccountWindow", "")

    def sample_telegram(self):
        pass

    def auto_trade_on(self):
        pass

    def auto_trade_off(self):
        pass

    # ========================
    # 시장 지수 실시간 등록
    # ========================

    def register_market_indices(self, futures_code: str = ""):
        fid_list = "10;12;20"
        for code in ("001", "101"):
            if code not in self.realtime_registered_codes:
                self.realtime_registered_codes.add(code)
                self.set_real(self._get_screen_num(), code, fid_list, "1")
                logger.info(f"[시장차트] {self.index_code_to_name_map.get(code, code)} 실시간 등록")
        if futures_code:
            self._market_futures_code = futures_code
            if futures_code not in self.realtime_registered_codes:
                self.realtime_registered_codes.add(futures_code)
                self.set_real(self._get_screen_num(), futures_code, fid_list, "1")
                logger.info(f"[시장차트] 선물({futures_code}) 실시간 등록")

    def on_receive_market_chart_data(self, name: str, time_str: str, pct_change: float):
        pass

    def register_overseas_futures(self, code: str):
        """CME 해외선물 실시간 등록 (FCCME시세 타입)."""
        fid_list = "20;10;11;12;15;13;182;184;183;186"
        if code and code not in self.realtime_registered_codes:
            self.realtime_registered_codes.add(code)
            self.set_real(self._get_screen_num(), code, fid_list, "1")
            logger.info(f"[해외선물] {code} 실시간 등록")

    def on_receive_overseas_futures_data(self, code: str, df: "pd.DataFrame"):
        """opt50028 분봉 수신 콜백 — 서브클래스에서 오버라이드."""
        pass

    def on_receive_overseas_futures_realtime(self, code: str, data: dict):
        """FCCME체결 실시간 수신 콜백 — 서브클래스에서 오버라이드."""
        pass

    # ========================
    # API 래퍼 메소드들
    # ========================

    def set_real(self, scrNum, strCodeList, strFidList, strRealType):
        self.kiwoom.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            scrNum, strCodeList, strFidList, strRealType
        )

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def get_chejandata(self, nFid):
        return self.kiwoom.dynamicCall("GetChejanData(int)", nFid)

    def _get_repeat_cnt(self, trcode, rqname):
        return self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)

    def get_comm_realdata(self, strCode, nFid):
        return self.kiwoom.dynamicCall("GetCommRealData(QString, int)", strCode, nFid)

    def _comm_get_data(self, code, real_type, field_name, index, item_name):
        ret = self.kiwoom.dynamicCall(
            "CommGetData(QString, QString, QString, int, QString)",
            code, real_type, field_name, index, item_name
        )
        return ret.strip()

    def _get_screen_num(self) -> str:
        self.screen_num += 1
        if self.screen_num > 5999:
            self.screen_num = 5000
        return str(self.screen_num)

    def send_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo):
        logger.info("Sending order")
        return self.kiwoom.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo]
        )

    def send_credit_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode,
                          nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, sOrgOrderNo):
        logger.info("Sending credit order")
        return self.kiwoom.dynamicCall(
            "SendOrderCredit(QString, QString, QString, int, QString, int, int, QString, QString, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice,
             sHogaGb, sCreditGb, sLoanDate, sOrgOrderNo]
        )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
