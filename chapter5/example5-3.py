# chapter5 / example5-3.py
# 목표: 실시간 일봉 차트 데이터 유지 관리
#
# 일봉 데이터 실시간 업데이트 흐름:
#   로그인 → register_code_to_realtime_list (SetRealReg)
#   → request_opt10081 (일봉 TR 요청) → OnReceiveTrData → DataFrame 생성
#   → OnReceiveRealData (실시간 체결) → 마지막 행 Close/High/Low/Volume 갱신
#   → QTimer(2초) → print_dfs() 주기 출력

import sys
import datetime

import pandas as pd
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.realtime_data_scrnum = 5000
        self.realtime_registered_codes_set = set()
        self.stock_code_to_df_dict = dict()

        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self._login()

        self.timer1 = QTimer()
        self.timer1.timeout.connect(self.print_dfs)
        self.timer1.start(2000)  # 2초 마다 실행

    def print_dfs(self):
        for code, df in self.stock_code_to_df_dict.items():
            print(code)
            print(df)

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveRealData.connect(self._receive_realdata)
        self.kiwoom.OnReceiveTrData.connect(self._receive_tr_data)

    def _login(self):
        ret = self.kiwoom.dynamicCall("CommConnect()")
        if ret == 0:
            print("로그인 창 열기 성공!")

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공!")
            self._after_login()
        else:
            raise Exception("로그인 실패!")

    def _after_login(self):
        print("실시간 등록/일봉 요청")
        self.register_code_to_realtime_list("005930")
        self.request_opt10081(code="005930_AL", date=datetime.datetime.now().strftime('%Y%m%d'))

    def request_opt10081(self, code, date=datetime.datetime.now().strftime('%Y%m%d')):
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="기준일자", value=date)
        self.set_input_value(id="수정주가구분", value=1)
        self.comm_rq_data(rqname="opt10081_req", trcode="opt10081", next=0, screen_no="5000")

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, trcode, next, screen_no)

    def set_real(self, scrNum, strCodeList, strFidList, strRealType):
        self.kiwoom.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            scrNum, strCodeList, strFidList, strRealType
        )

    def register_code_to_realtime_list(self, code, register_NXT=True):
        fid_list = "10;12;20"
        if register_NXT:
            code += "_AL"
        if len(code) != 0 and code not in self.realtime_registered_codes_set:
            self.set_real(self._get_realtime_data_screen_num(), code, fid_list, strRealType="1")
            print(f"{code}, 실시간 등록 완료!")
            self.realtime_registered_codes_set.add(code)

    def _get_realtime_data_screen_num(self):
        self.realtime_data_scrnum += 1
        if self.realtime_data_scrnum > 5150:
            self.realtime_data_scrnum = 5000
        return str(self.realtime_data_scrnum)

    def _get_comm_realdata(self, strCode, nFid):
        return self.kiwoom.dynamicCall("GetCommRealData(QString, int)", strCode, nFid)

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
            open_  = self.get_comm_data(trcode, rqname, i, strItemName="시가")
            high   = self.get_comm_data(trcode, rqname, i, strItemName="고가")
            low    = self.get_comm_data(trcode, rqname, i, strItemName="저가")
            close  = self.get_comm_data(trcode, rqname, i, strItemName="현재가")
            volume = self.get_comm_data(trcode, rqname, i, strItemName="거래량")
            rows.append([date, float(open_), float(high), float(low), float(close), float(volume)])
        daily_data_df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        daily_data_df = daily_data_df[::-1].reset_index(drop=True)
        self.stock_code_to_df_dict[stock_code] = daily_data_df
        print(stock_code, "일봉 획득 완료!")

    def _receive_realdata(self, sJongmokCode, sRealType, sRealData):
        종목코드 = sJongmokCode.replace("_AL", "")
        if sRealType == "주식체결":
            현재가       = int(self._get_comm_realdata(sRealType, nFid=10).replace('-', ''))
            당일누적거래량  = int(self._get_comm_realdata(sRealType, nFid=13))
            df = self.stock_code_to_df_dict.get(종목코드)
            if df is None or len(df) == 0:
                return
            df["Close"].iat[-1] = 현재가
            df["High"].iat[-1]  = max(df["High"].iat[-1], 현재가)
            df["Low"].iat[-1]   = min(df["Low"].iat[-1], 현재가)
            df["Volume"].iat[-1] = 당일누적거래량


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
