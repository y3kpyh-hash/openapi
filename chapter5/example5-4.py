import sys
import datetime

from loguru import logger
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
        self.candle_interval = 1  # 1분

        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()  # 키움증권 API와 내부 메소드를 연동
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
        print("실시간 등록/분봉 요청")
        self.register_code_to_realtime_list("005930")  # 5002
        self.request_opt10080(code="005930_AL", tick_range=f"{self.candle_interval}:{self.candle_interval}분")  # _AL: 통합시세, _NX: NXT 시세

    def request_opt10080(self, code, tick_range="1:1분"):
        self.set_input_value(id="종목코드", value=code)
        self.set_input_value(id="틱범위", value=tick_range)
        self.set_input_value(id="수정주가구분", value=1)
        self.set_input_value(id="증가매매분봉", value=0)
        self.comm_rq_data(rqname="opt10080_req", trcode="opt10080", next=0, screen_no="5000")

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
        fid_list = "10;12;20;"
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
        # sScrNo: 화면번호, sRQName: 사용자 구분명, sTrCode: TR이름, sRecordName: 레코드 이름, sPrevNext: 연속조회 유무를 판단하는 값 0: 연속(추가조회) 데이터 없음, 2: 연속(추가조회) 가능
        # 조회요청 응답을 받거나 조회데이터를 수신했을때 호출됩니다.
        # 조회데이터는 이 이벤트에서 GetCommData()함수를 이용해서 얻을 수 있습니다.
        if sRQName == "opt10080_req":
            self._on_opt10080_req(sTrCode, sRQName)

    def _on_opt10080_req(self, trcode, rqname):
        stock_code = self.get_comm_data(trcode, rqname, nIndex=0, strItemName="종목코드").replace("_AL", "")
        data_cnt = self._get_repeat_cnt(trcode, rqname)
        rows = []
        for i in range(data_cnt):
            _time  = self.get_comm_data(trcode, rqname, i, strItemName="체결시간")
            open_  = self.get_comm_data(trcode, rqname, i, strItemName="시가")
            high   = self.get_comm_data(trcode, rqname, i, strItemName="고가")
            low    = self.get_comm_data(trcode, rqname, i, strItemName="저가")
            close  = self.get_comm_data(trcode, rqname, i, strItemName="현재가")
            volume = self.get_comm_data(trcode, rqname, i, strItemName="거래량")
            rows.append([_time, abs(float(open_)), abs(float(high)), abs(float(low)), abs(float(close)), abs(float(volume))])
        minute_data_df = pd.DataFrame(rows, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        minute_data_df = minute_data_df[::-1].reset_index(drop=True)  # 역순으로 해야 일반적 df
        self.stock_code_to_df_dict[stock_code] = minute_data_df

    def get_comm_data(self, strTrCode, strRecordName, nIndex, strItemName):
        ret = self.kiwoom.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            strTrCode, strRecordName, nIndex, strItemName
        )
        return ret.strip()

    def _get_repeat_cnt(self, trcode, rqname):
        ret = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        return ret

    def _receive_realdata(self, sJongmokCode, sRealType, sRealData):
        종목코드 = sJongmokCode.replace("_AL", "")  # 통합시세의 경우 종목코드에 _AL 이 붙어서 나옴
        if sRealType == "주식체결":
            시간   = self._get_comm_realdata(sRealType, nFid=20).zfill(6)
            현재가  = int(self._get_comm_realdata(sRealType, nFid=10).replace('-', ''))  # 현재가
            체결량  = int(self._get_comm_realdata(sRealType, nFid=15))
            df = self.stock_code_to_df_dict.get(종목코드)
            if df is None or len(df) == 0:
                return
            df["Close"].iat[-1] = 현재가
            df["High"].iat[-1]  = max(df["High"].iat[-1], 현재가)
            df["Low"].iat[-1]   = min(df["Low"].iat[-1], 현재가)
            df["Volume"].iat[-1] += abs(체결량)
            체결시간 = datetime.datetime.strptime(df["Date"].iat[-1], "%Y%m%d%H%M%S")
            # 직전 봉분의 기준 시간 (정시 기준)
            last_minute = (체결시간.minute // self.candle_interval) * self.candle_interval
            기준_시간 = 체결시간.replace(minute=last_minute, second=0, microsecond=0)
            # 현재 시간과 기준 시간 차이로 몇 개의 새로운 캔들이 필요한지 계산
            현재체결시간 = datetime.datetime.now().replace(
                hour=int(시간[:2]),
                minute=int(시간[2:4]),
                second=int(시간[4:6]),
            )
            minutes_passed = int((현재체결시간 - 기준_시간).total_seconds() // 60)
            num_new_candles = (minutes_passed // self.candle_interval)
            if num_new_candles > 2:
                self.stock_code_to_df_dict.pop(종목코드)
                logger.info(f"{종목코드} Pop and request again!")
                self.request_opt10080(sJongmokCode, tick_range=f"{self.candle_interval}:{self.candle_interval}분")  # _AL: 통합시세, _NX: NXT 시세
                return
            for i in range(1, num_new_candles + 1):
                new_time = 기준_시간 + datetime.timedelta(minutes=self.candle_interval * i)
                df.loc[len(df)] = {
                    "Date": new_time.strftime("%Y%m%d%H%M%S"),
                    "Close": 현재가,
                    "Open": 현재가,
                    "High": 현재가,
                    "Low": 현재가,
                    "Volume": 0,
                }
                logger.info(f"{종목코드} now candle! df: {df}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
