# chapter4 / example4-3.py
# 목표: 자동 정정 주문
#
# 미체결 주문 자동 정정 흐름:
#   SendOrder(지정가매수/매도) → OnReceiveChejanData에서 unfinished_order_num_to_info_dict에 저장
#   → QTimer(250ms)로 check_unfinished_orders() 주기 호출
#   → 주문 후 10초 경과 시 미체결수량만큼 정정 주문 발송
#
# nOrderType: 1=신규매수, 2=신규매도, 3=매수취소, 4=매도취소, 5=매수정정, 6=매도정정
#             11=SOR매수, 12=SOR매도, 13=SOR취소

import sys
import datetime

from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtCore import QTimer
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.account_num = None
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.kiwoom.dynamicCall("CommConnect()")
        self.unfinished_order_num_to_info_dict = dict()

        self.timer1 = QTimer()
        self.timer1.timeout.connect(self.check_unfinished_orders)
        self.timer1.start(250)  # 0.25초마다 한번

    def check_unfinished_orders(self):
        pop_list = []
        for order_num, stock_info_dict in self.unfinished_order_num_to_info_dict.items():
            주문번호    = order_num
            종목코드    = stock_info_dict['종목코드']
            주문체결시간  = stock_info_dict['주문체결시간']
            미체결수량   = stock_info_dict['미체결수량']
            주문구분    = stock_info_dict['주문구분']
            order_time = datetime.datetime.now().replace(
                hour=int(주문체결시간[:-4]),
                minute=int(주문체결시간[-4:-2]),
                second=int(주문체결시간[-2:]),
            )
            if 주문구분 == "매수" and datetime.datetime.now() - order_time >= datetime.timedelta(seconds=10):
                print(f"종목코드: {종목코드}, 주문번호: {주문번호}, 미체결수량: {미체결수량}, 매수 정정 주문!")
                self.send_order(
                    "매수정정주문",   # 사용자 구분명
                    "5000",         # 화면번호
                    self.account_num,  # 계좌 번호
                    5,              # 주문유형, 1:신규매수, 2:신규매도, 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정, 11: SOR매수, 12: SOR매도, 13: SOR취소
                    "005930",       # 종목코드
                    미체결수량,       # 주문 수량
                    140000,         # 주문 가격, 시장가의 경우 공백
                    "00",           # 거래 구분, 00: 지정가, 03: 시장가, 05: 조건부지정가, 06: 최유리지정가, 07: 최우선지정가 등 (KOAStudio 참조)
                    주문번호,         # 주문번호 (정정 주문의 경우 사용, 나머지 공백)
                )
                pop_list.append(주문번호)
            elif 주문구분 == "매도" and datetime.datetime.now() - order_time >= datetime.timedelta(seconds=10):
                print(f"종목코드: {종목코드}, 주문번호: {주문번호}, 미체결수량: {미체결수량}, 매도 정정 주문!")
                self.send_order(
                    "매도정정주문",   # 사용자 구분명
                    "5000",         # 화면번호
                    self.account_num,  # 계좌 번호
                    6,              # 주문유형, 1:신규매수, 2:신규매도, 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정, 11: SOR매수, 12: SOR매도, 13: SOR취소
                    "005930",       # 종목코드
                    미체결수량,       # 주문 수량
                    100000,         # 주문 가격, 시장가의 경우 공백
                    "00",           # 거래 구분, 00: 지정가, 03: 시장가, 05: 조건부지정가, 06: 최유리지정가, 07: 최우선지정가 등 (KOAStudio 참조)
                    주문번호,         # 주문번호 (정정 주문의 경우 사용, 나머지 공백)
                )
                pop_list.append(주문번호)
        for order_num in pop_list:
            self.unfinished_order_num_to_info_dict.pop(order_num)

    def get_account_info(self):
        account_nums = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCNO"]).rstrip(';'))
        # print(f"계좌번호 리스트: {account_nums}")
        self.account_num = account_nums.split(';')[0]
        # print(f"사용 계좌 번호: {self.account_num}")

    def limit_buy_order(self):
        self.send_order(
            "지정가매수주문",   # 사용자 구분명
            "5000",           # 화면번호
            self.account_num, # 계좌 번호
            11,               # 주문유형, 1:신규매수, 2:신규매도, 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정, 11: SOR매수, 12: SOR매도, 13: SOR취소
            "005930",         # 종목코드
            1,                # 주문 수량
            100_000,          # 주문 가격
            "00",             # 거래 구분, 00: 지정가, 03: 시장가, 05: 조건부지정가, 06: 최유리지정가, 07: 최우선지정가 등 (KOAStudio 참조)
            "",               # 주문번호 (정정 주문의 경우 사용, 나머지 공백)
        )

    def limit_sell_order(self):
        self.send_order(
            "지정가매도주문",   # 사용자 구분명
            "5000",           # 화면번호
            self.account_num, # 계좌 번호
            2,                # 주문유형, 1:신규매수, 2:신규매도, 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정, 11: SOR매수, 12: SOR매도, 13: SOR취소
            "005930",         # 종목코드
            1,                # 주문 수량
            130_000,          # 주문 가격
            "00",             # 거래 구분, 00: 지정가, 03: 시장가, 05: 조건부지정가, 06: 최유리지정가, 07: 최우선지정가 등 (KOAStudio 참조)
            "",               # 주문번호 (정정 주문의 경우 사용, 나머지 공백)
        )

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
        self.limit_buy_order()
        # self.limit_sell_order()

    def receive_msg(self, sScrNo, sRQName, sTrCode, sMsg):
        print(f"Received MSG! 화면번호: {sScrNo}, 사용자 구분명: {sRQName}, TR이름: {sTrCode}, 메세지: {sMsg}")

    def send_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo):
        print("Sending order")
        return self.kiwoom.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo]
        )

    def get_chejandata(self, nFid):
        ret = self.kiwoom.dynamicCall("GetChejanData(int)", nFid)
        return ret

    def receive_chejandata(self, sGubun, nItemCnt, sFIdList):
        # sGubun: 체결구분 접수와 체결시 '0'값, 국내주식 잔고전달은 '1'값, 파생잔고 전달은 '4'
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
            self.unfinished_order_num_to_info_dict[주문번호] = dict(
                종목코드=종목코드,
                미체결수량=미체결수량,
                주문체결시간=주문체결시간,
                주문구분=주문구분,
            )
            if 미체결수량 == 0:
                self.unfinished_order_num_to_info_dict.pop(주문번호)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
