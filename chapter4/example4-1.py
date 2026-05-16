# chapter4 / example4-1.py
# 목표: 자동 매수/매도 주문 (feat. 자동 신용 주문)
#
# SendOrder 파라미터:
#   sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo
#   nOrderType: 1=신규매수, 2=신규매도, 11=SOR매수, 12=SOR매도
#   sHogaGb: "00"=지정가, "03"=시장가
#
# SendOrderCredit 파라미터:
#   sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, sOrgOrderNo
#   sCreditGb: "03"=신용매수(자기융자), "99"=신용매도(자기융자 합)
#
# OnReceiveChejanData:
#   sGubun: "0"=접수/체결, "1"=국내주식잔고, "4"=파생잔고
#
# 주문 흐름:
#   SendOrder → OnReceiveTrData(주문번호) → OnReceiveMsg(주문서버메시지) → OnReceiveChejanData(접수/체결)

import sys
import datetime

from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.account_num = None
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.kiwoom.dynamicCall("CommConnect()")

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveChejanData.connect(self.receive_chejandata)
        self.kiwoom.OnReceiveMsg.connect(self.receive_msg)

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공")
        else:
            print("로그인 실패")
        self.after_login()

    def after_login(self):
        self.get_account_info()
        # 하나씩 주석을 풀어서 해보세요!
        self.market_buy_order()
        # self.market_sell_order()
        # self.limit_buy_order()
        # self.limit_sell_order()
        # self.limit_credit_buy_order()   # 실전투자 - 신용 주문 가능 계좌에서만 가능
        # self.limit_credit_sell_order()  # 실전투자 - 신용 주문 가능 계좌에서만 가능

    def get_account_info(self):
        account_nums = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCNO"]).rstrip(';'))
        self.account_num = account_nums.split(';')[0]

    def market_buy_order(self):
        self.send_order("시장가매수주문", "5000", self.account_num,
                        1, "005930", 1, "", "03", "")

    def market_sell_order(self):
        self.send_order("시장가매도주문", "5000", self.account_num,
                        2, "005930", 1, "", "03", "")

    def limit_buy_order(self):
        self.send_order("지정가매수주문", "5000", self.account_num,
                        11, "005930", 1, 100_000, "00", "")  # 11=SOR매수

    def limit_sell_order(self):
        self.send_order("지정가매도주문", "5000", self.account_num,
                        12, "005930", 1, 100_000, "00", "")  # 12=SOR매도

    def limit_credit_buy_order(self):
        self.send_credit_order("지정가신용매수주문", "5000", self.account_num,
                               1, "005930", 1, 100_000, "00", "03", "", "")

    def limit_credit_sell_order(self):
        self.send_credit_order("지정가신용매도주문", "5000", self.account_num,
                               2, "005930", 1, 100_000, "00", "99",
                               datetime.datetime.now().strftime("%Y%m%d"), "")

    def send_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo):
        print("Sending order")
        return self.kiwoom.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo]
        )

    def send_credit_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, sOrgOrderNo):
        print("Sending credit order")
        return self.kiwoom.dynamicCall(
            "SendOrderCredit(QString, QString, QString, int, QString, int, int, QString, QString, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sCreditGb, sLoanDate, sOrgOrderNo]
        )

    def receive_msg(self, sScrNo, sRQName, sTrCode, sMsg):
        print(f"Received MSG! 화면번호: {sScrNo}, 사용자 구분명: {sRQName}, TR이름: {sTrCode}, 메세지: {sMsg}")

    def get_chejandata(self, nFid):
        ret = self.kiwoom.dynamicCall("GetChejanData(int)", nFid)
        return ret

    def receive_chejandata(self, sGubun, nItemCnt, sFIdList):
        # sGubun: '0'=접수/체결, '1'=국내주식잔고, '4'=파생잔고
        if sGubun == "0":
            종목코드    = self.get_chejandata(9001).replace("A", "").strip()
            종목명     = self.get_chejandata(302).strip()
            주문체결시간  = self.get_chejandata(908).strip()
            주문수량    = 0 if len(self.get_chejandata(900)) == 0 else int(self.get_chejandata(900))
            주문가격    = 0 if len(self.get_chejandata(901)) == 0 else int(self.get_chejandata(901))
            체결수량    = 0 if len(self.get_chejandata(911)) == 0 else int(self.get_chejandata(911))
            체결가격    = 0 if len(self.get_chejandata(910)) == 0 else int(self.get_chejandata(910))
            미체결수량   = 0 if len(self.get_chejandata(902)) == 0 else int(self.get_chejandata(902))
            주문구분    = self.get_chejandata(905).replace("+", "").replace("-", "").strip()
            매매구분    = self.get_chejandata(906).strip()
            단위체결가   = 0 if len(self.get_chejandata(914)) == 0 else int(self.get_chejandata(914))
            단위체결량   = 0 if len(self.get_chejandata(915)) == 0 else int(self.get_chejandata(915))
            원주문번호   = self.get_chejandata(904).strip()
            주문번호    = self.get_chejandata(9203).strip()
            거래소구분   = self.get_chejandata(2134)
            거래소구분명  = self.get_chejandata(2135)
            SOR여부   = self.get_chejandata(2136)
            print(
                f"Received chejandata! 주문체결시간: {주문체결시간}, 종목코드: {종목코드}, "
                f"종목명: {종목명}, 주문수량: {주문수량}, 주문가격: {주문가격}, 체결수량: {체결수량}, 체결가격: {체결가격}, "
                f"주문구분: {주문구분}, 미체결수량: {미체결수량}, 매매구분: {매매구분}, 단위체결가: {단위체결가}, "
                f"단위체결량: {단위체결량}, 주문번호: {주문번호}, 원주문번호: {원주문번호}, "
                f"거래소구분: {거래소구분}, 거래소구분명: {거래소구분명}, SOR여부: {SOR여부}"
            )


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
