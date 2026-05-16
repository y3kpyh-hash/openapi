# chapter2 / example2-1.py
# 목표: TR 데이터 요청 - opt10001 주식기본정보요청
#
# 요청 흐름:
#   SetInputValue() -> CommRqData() -> OnReceiveTrData 이벤트 -> GetCommData()
#
# opt10001 입력값:
#   종목코드 - 조회할 종목코드 (예: "005930" 삼성전자)
#
# opt10001 출력값 (주요):
#   종목코드, 종목명, 현재가, 기준가, 시가, 고가, 저가, 상한가, 하한가

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.kiwoom.dynamicCall("CommConnect()")

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self.event_connect)
        self.kiwoom.OnReceiveTrData.connect(self._receive_tr_data)

    def event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공")
        else:
            print("로그인 실패")
        self.after_login()

    def after_login(self):
        self.get_basic_stock_info("005930")

    def get_basic_stock_info(self, stock_code):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", ["종목코드", stock_code])
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            ["opt10001_req", "opt10001", 0, "5000"]
        )

    def _receive_tr_data(self, screen_no, rqname, trcode, record_name, prev_next,
                         data_len, err_code, msg1, msg2):
        if rqname == "opt10001_req":
            self.on_opt10001_req(trcode, record_name)

    def get_comm_data(self, trcode, record_name, index, item_name):
        ret = self.kiwoom.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            [trcode, record_name, index, item_name]
        )
        return ret.strip()

    def on_opt10001_req(self, trcode, record_name):
        stock_code  = self.get_comm_data(trcode, record_name, 0, "종목코드")
        current     = self.get_comm_data(trcode, record_name, 0, "현재가")
        base_price  = self.get_comm_data(trcode, record_name, 0, "기준가")
        open_price  = self.get_comm_data(trcode, record_name, 0, "시가")
        high_price  = self.get_comm_data(trcode, record_name, 0, "고가")
        low_price   = self.get_comm_data(trcode, record_name, 0, "저가")
        upper_limit = self.get_comm_data(trcode, record_name, 0, "상한가")
        lower_limit = self.get_comm_data(trcode, record_name, 0, "하한가")

        print(f"종목코드: {stock_code}")
        print(f"현재가  : {current}")
        print(f"기준가  : {base_price}")
        print(f"시가    : {open_price}")
        print(f"고가    : {high_price}")
        print(f"저가    : {low_price}")
        print(f"상한가  : {upper_limit}")
        print(f"하한가  : {lower_limit}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = KiwoomAPI()
    sys.exit(app.exec_())
