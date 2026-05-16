# chapter2 / example2-3.py
# 목표: opt10080 주식분봉차트조회요청 - 분봉 데이터 수신 후 DataFrame 출력
#
# opt10080 입력값:
#   종목코드    - 시세별 종목코드 (KRX:039490, NXT:039490_NX, 통합:039490_AL)
#   틱범위      - 1:1분, 3:3분, 5:5분, 10:10분, 15:15분, 30:30분, 45:45분, 60:60분
#   수정주가구분 - 0 or 1 (1: 수정주가 사용)
#   종가매매분봉 - 0:종가매매(15시 35분 분봉) 표시, 1:표시안함
#
# opt10080 출력값 (멀티데이터):
#   현재가, 거래량, 체결시간, 시가, 고가, 저가, 수정주가구분 ...
# 최대 900개 조회

import sys
import pandas as pd
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.kiwoom.dynamicCall("CommConnect()")

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
        # _AL: 통합시세, _NX: NXT 시세, 아무것도 없으면 KRX 시세
        self.request_opt10080(code="005930_AL", tick_range="1:1분")

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def request_opt10080(self, code, tick_range="1:1분"):
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="틱범위", value=tick_range)
        self.set_input_value(id="수정주가구분", value=1)
        self.set_input_value(id="종가매매분봉", value=0)
        self.comm_rq_data(rqname="opt10080_req", trcode="opt10080", next=0, screen_no="5000")

    def _receive_tr_data(self, sScrnNo, sRQName, sTrCode, sRecordName, sPrevNext,
                         nDataLength, sErrorCode, sMessage, sSplmMsg):
        if sRQName == "opt10080_req":
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
        rows = []
        for i in range(data_cnt):
            _time  = self.get_comm_data(trcode, rqname, i, strItemName="체결시간")
            open   = self.get_comm_data(trcode, rqname, i, strItemName="시가")
            high   = self.get_comm_data(trcode, rqname, i, strItemName="고가")
            low    = self.get_comm_data(trcode, rqname, i, strItemName="저가")
            close  = self.get_comm_data(trcode, rqname, i, strItemName="현재가")
            volume = self.get_comm_data(trcode, rqname, i, strItemName="거래량")
            rows.append([_time, abs(float(open)), abs(float(high)), abs(float(low)), abs(float(close)), abs(float(volume))])

        minute_data_df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        minute_data_df = minute_data_df[::-1].reset_index(drop=True)  # 역순으로 해야 일반적 df
        print(f"종목코드: {stock_code}")
        print(minute_data_df)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
