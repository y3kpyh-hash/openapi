# chapter2 / example2-5.py
# 목표: TR 요청 제한 + 연속 조회
#
# 핵심 구조:
#   - Queue: TR 요청을 큐에 넣고 순차 처리
#   - deque(maxlen=4): 최근 4회 TR 전송 시각 기록 → 초당 4회 제한
#   - QTimer(100ms): 0.1초마다 send_tr_reqeust() 호출
#   - has_next_data: sPrevNext == "2" → 연속 조회 여부 판단
#   - 3000행 or 연속조회 끝 → DataFrame 역순 정렬 후 출력

import sys
import datetime
from collections import deque
from queue import Queue

from loguru import logger
import pandas as pd
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtCore import QTimer
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.kiwoom.dynamicCall("CommConnect()")
        self.has_next_data = False
        self.max_send_per_sec: int = 4  # 초당 TR 호출 최대 4번
        self.last_tr_send_times = deque(maxlen=self.max_send_per_sec)
        self.tr_req_queue = Queue()
        self.minute_data_df = pd.DataFrame(columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])

        self.timer1 = QTimer()
        self.timer1.timeout.connect(self.send_tr_reqeust)
        self.timer1.start(100)  # 0.1초 마다 실행

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveTrData.connect(self._receive_tr_data)

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공")
        else:
            print("로그인 실패")
        self.after_login()

    def after_login(self):
        self.tr_req_queue.put([self.request_opt10080, "005930_AL", "1:1분", 0])

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def request_opt10080(self, code, tick_range="1:1분", next=0):
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="틱범위", value=tick_range)
        self.set_input_value(id="수정주가구분", value=1)
        self.comm_rq_data(rqname="opt10080_req", trcode="opt10080", next=next, screen_no="5000")

    def _receive_tr_data(self, sScrnNo, sRQName, sTrCode, sRecordName, sPrevNext,
                         nDataLength, sErrorCode, sMessage, sSplmMsg):
        # sPrevNext == "2" 이면 연속 조회 데이터 존재
        if sRQName == "opt10080_req":
            self.has_next_data = sPrevNext == "2"
            self._on_opt10080_req(sTrCode, sRQName)

    def get_comm_data(self, strTrCode, strRecordName, nIndex, strItemName):
        ret = self.kiwoom.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            strTrCode, strRecordName, nIndex, strItemName
        )
        return ret.strip()

    def _get_repeat_cnt(self, trcode, rqname):
        ret = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        return ret

    def _on_opt10080_req(self, trcode, rqname):
        stock_code = self.get_comm_data(trcode, rqname, nIndex=0, strItemName="종목코드").replace("_AL", "")
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        for i in range(data_cnt):
            _time  = self.get_comm_data(trcode, rqname, i, strItemName="체결시간")
            open   = self.get_comm_data(trcode, rqname, i, strItemName="시가")
            high   = self.get_comm_data(trcode, rqname, i, strItemName="고가")
            low    = self.get_comm_data(trcode, rqname, i, strItemName="저가")
            close  = self.get_comm_data(trcode, rqname, i, strItemName="현재가")
            volume = self.get_comm_data(trcode, rqname, i, strItemName="거래량")
            self.minute_data_df.loc[len(self.minute_data_df)] = [
                _time, abs(float(open)), abs(float(high)), abs(float(low)), abs(float(close)), abs(float(volume))
            ]
        if not self.has_next_data or len(self.minute_data_df) >= 3000:  # 연속조회 끝 / 또는 df length 3000개 이상
            logger.info(f"{stock_code} 연속 조회 끝!")
            self.minute_data_df = self.minute_data_df[::-1].reset_index(drop=True)
            print(self.minute_data_df)
        elif self.has_next_data:
            self.tr_req_queue.put([self.request_opt10080, "005930_AL", "1:1분", 2])
            logger.info(f"{stock_code} 연속 조회 요청!")

    def send_tr_reqeust(self):
        self.now_time = datetime.datetime.now()
        if self.is_check_tr_req_condition() and not self.tr_req_queue.empty():
            request_func, *func_args = self.tr_req_queue.get()
            logger.info(f"Executing TR request fuction: {request_func}, func_args: {func_args}")
            request_func(*func_args) if func_args else request_func()
            self.last_tr_send_times.append(self.now_time)

    def is_check_tr_req_condition(self):
        if len(self.last_tr_send_times) >= self.max_send_per_sec and \
                self.now_time - self.last_tr_send_times[-self.max_send_per_sec] < datetime.timedelta(milliseconds=1000):
            logger.info(f"초 단위 TR 요청 제한! Wait for time to send! length: {len(self.last_tr_send_times)}")
            return False
        else:
            return True


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
