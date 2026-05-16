# chapter2 / example2-2.py
# 목표: opt10081 주식일봉차트조회요청 - 멀티데이터 수신 후 DataFrame 출력
#
# opt10081 입력값:
#   종목코드    - 시세별 종목코드 (KRX:039490, NXT:039490_NX, 통합:039490_AL)
#   기준일자    - YYYYMMDD (연도 4자리, 월 2자리, 일 2자리)
#   수정주가구분 - 0 or 1 (1: 수정주가 사용)
#
# opt10081 출력값 (멀티데이터):
#   종목코드, 현재가, 거래량, 거래대금, 일자, 시가, 고가, 저가, 수정주가구분 ...

import sys
import datetime
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
        # _AL: 통합시세, _NX: NXT 시세
        self.request_opt10081(code="005930_AL", date=datetime.datetime.now().strftime('%Y%m%d'))

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def request_opt10081(self, code, date=datetime.datetime.now().strftime('%Y%m%d')):
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="기준일자", value=date)
        self.set_input_value(id="수정주가구분", value=1)  # 수정주가 사용
        self.comm_rq_data(rqname="opt10081_req", trcode="opt10081", next=0, screen_no="5000")

    def _receive_tr_data(self, sScrnNo, sRQName, sTrCode, sRecordName, sPrevNext,
                         nDataLength, sErrorCode, sMessage, sSplmMsg):
        if sRQName == "opt10081_req":
            self._on_opt10081_req(sTrCode, sRQName)

    def get_comm_data(self, strTrCode, strRecordName, nIndex, strItemName):
        ret = self.kiwoom.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            strTrCode, strRecordName, nIndex, strItemName
        )
        return ret.strip()

    def _get_repeat_cnt(self, trcode, rqname):
        ret = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        return ret

    def _on_opt10081_req(self, trcode, rqname):
        stock_code = self.get_comm_data(trcode, rqname, nIndex=0, strItemName="종목코드").replace("_AL", "")
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            date   = self.get_comm_data(trcode, rqname, i, strItemName="일자")
            open   = self.get_comm_data(trcode, rqname, i, strItemName="시가")
            high   = self.get_comm_data(trcode, rqname, i, strItemName="고가")
            low    = self.get_comm_data(trcode, rqname, i, strItemName="저가")
            close  = self.get_comm_data(trcode, rqname, i, strItemName="현재가")
            volume = self.get_comm_data(trcode, rqname, i, strItemName="거래량")
            rows.append([date, float(open), float(high), float(low), float(close), float(volume)])

        daily_data_df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        # daily_data_df = daily_data_df[::-1].reset_index(drop=True)  # 역순으로 해야 일반적 df
        print(f"종목코드: {stock_code}")
        print(daily_data_df)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
