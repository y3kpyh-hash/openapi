# chapter1 / example1-3.py
# 목표: 로그인 후 시장별 종목코드 리스트 조회
#
# GetCodeListByMarket(시장구분값) - ';'로 구분된 종목코드 리스트 반환
# [시장구분값]
#   0   : 코스피
#   10  : 코스닥
#   3   : ELW
#   8   : ETF
#   50  : KONEX
#   4   : 뮤추얼펀드
#   5   : 신주인수권
#   6   : 리츠
#   9   : 하이얼펀드
#   30  : K-OTC
#   NXT : NXT종목

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class MyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.kiwoom.OnEventConnect.connect(self.event_connect)
        self.kiwoom.dynamicCall("CommConnect()")

    def event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공")
        else:
            print("로그인 실패")
        self.after_login()

    def after_login(self):
        # KOSPI 종목코드 + 종목명
        ret = self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", ["0"])
        kospi_code_list = ret.split(';')
        for stock_code in kospi_code_list:
            name = self.kiwoom.dynamicCall("GetMasterCodeName(QString)", [stock_code])
            print(f"KOSPI 종목코드: {stock_code}, 종목명: {name}")

        # KOSDAQ 종목코드 리스트
        ret = self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", ["10"])
        kosdaq_code_list = ret.split(';')
        print(f"KOSDAQ 종목코드 리스트 : {kosdaq_code_list}")

        # NXT 종목코드 리스트
        ret = self.kiwoom.dynamicCall("GetCodeListByMarket(QString)", ["NXT"])
        nxt_code_list = ret.split(';')
        print(f"NXT상장 종목코드 리스트 : {nxt_code_list}")

        # 지수선물 종목코드 리스트
        ret = self.kiwoom.dynamicCall("GetFutureList()")
        futures_list = ret.split(";")
        print(f"지수선물 종목코드 리스트 : {futures_list}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = MyWindow()
    sys.exit(app.exec_())
