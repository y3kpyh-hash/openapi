# chapter5 / example5-1.py
# 목표: 스탑로스 구현
#
# 스탑로스 흐름:
#   매수 체결 감지 (OnReceiveChejanData) → 실시간 현재가 등록 (SetRealReg)
#   → 현재가 수신 (OnReceiveRealData) → 수익률 계산 → 임계값 이하 시 시장가 매도
#
# stop_loss_threshold: 평단가 대비 -1.5% 이하로 떨어질 경우 시장가 매도 주문

import sys

import pandas as pd
from loguru import logger
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.account_num = None

        self._screen_num = 5000
        self.realtime_registered_codes_set = set()
        self.realtime_watchlist_df = pd.DataFrame(
            columns=[
                "보유수량",
                "매입가",
                "현재가",
            ]
        )
        self.stop_loss_threshold = -1.5  # 평단가 대비 -1.5% 이하로 떨어질 경우 시장가 매도 주문

        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()  # 키움증권 API와 내부 메소드를 연동
        self._login()

    def get_account_info(self):
        account_nums = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCNO"]).rstrip(';'))
        print(f"계좌번호 리스트: {account_nums}")
        self.account_num = account_nums.split(';')[-1]
        print(f"사용 계좌 번호: {self.account_num}")

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveRealData.connect(self._receive_realdata)
        self.kiwoom.OnReceiveChejanData.connect(self.receive_chejandata)
        self.kiwoom.OnReceiveMsg.connect(self.receive_msg)

    def _login(self):
        ret = self.kiwoom.dynamicCall("CommConnect()")
        if ret == 0:
            logger.info("로그인 창 열기 성공!")

    def _event_connect(self, err_code):
        if err_code == 0:
            logger.info("로그인 성공!")
            self._after_login()
        else:
            raise Exception("로그인 실패!")

    def _after_login(self):
        self.get_account_info()

    def send_order(self, sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo):
        print("Sending order")
        return self.kiwoom.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [sRQName, sScreenNo, sAccNo, nOrderType, sCode, nQty, nPrice, sHogaGb, sOrgOrderNo]
        )

    def _get_repeat_cnt(self, trcode, rqname):
        ret = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        return ret

    def _get_screen_num(self):
        self._screen_num += 1
        if self._screen_num > 5150:
            self._screen_num = 5000
        return str(self._screen_num)

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
            self.set_real(self._get_screen_num(), code, fid_list, strRealType="1")
            logger.info(f"{code}, 실시간 등록 완료!")
            self.realtime_registered_codes_set.add(code)

    def _get_comm_realdata(self, strCode, nFid):
        return self.kiwoom.dynamicCall("GetCommRealData(QString, int)", strCode, nFid)

    def _receive_realdata(self, sJongmokCode, sRealType, sRealData):
        sJongmokCode = sJongmokCode.replace("_AL", "")  # 통합시세의 경우 종목코드에 _AL 이 붙어서 나옴
        if sRealType == "주식체결":
            현재가    = int(self._get_comm_realdata(sRealType, nFid=10).replace('-', ''))  # 현재가
            등락률    = float(self._get_comm_realdata(sRealType, nFid=12))
            체결시간   = self._get_comm_realdata(sRealType, nFid=20)
            거래소구분  = self._get_comm_realdata(sRealType, nFid=9081)
            if sJongmokCode not in self.realtime_watchlist_df.index:
                return
            self.realtime_watchlist_df.at[sJongmokCode, "현재가"] = 현재가
            매입가 = self.realtime_watchlist_df.at[sJongmokCode, "매입가"]
            if 매입가 is None:
                return
            수익률 = (현재가 - 매입가) / 매입가 * 100
            print(f"종목코드: {sJongmokCode}, 체결시간: {체결시간}, 현재가: {현재가}, 등락률: {등락률}, 거래소구분: {거래소구분}, 수익률(%): {수익률}")
            if 수익률 <= self.stop_loss_threshold:
                print(f"종목코드: {sJongmokCode}, 시장가 매도 진행!")
                self.send_order(
                    "시장가매도주문",                                                      # 사용자 구분명
                    self._get_screen_num(),                                              # 화면번호
                    self.account_num,                                                    # 계좌번호
                    2,                                                                   # 주문유형, 1:신규매수, 2:신규매도, 3:매수취소, 4:매도취소, 5:매수정정, 6:매도정정, 11: SOR매수, 12: SOR매도, 13: SOR취소
                    sJongmokCode,                                                        # 종목코드
                    int(self.realtime_watchlist_df.at[sJongmokCode, "보유수량"]),         # 주문 수량
                    "",                                                                  # 주문 가격, 시장가의 경우 공백
                    "03",                                                                # 주문 유형, 00: 지정가, 03: 시장가, 05: 조건부지정가, 06: 최유리지정가, 07: 최우선지정가 등 (KOAStudio 참조)
                    "",                                                                  # 주문번호 (정정 주문의 경우 사용, 나머지 공백)
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
            if 매매구분 == "매수" and 체결수량 > 0 and 미체결수량 == 0:
                print(f"종목코드: {종목코드} 스탑로스 감시 편입!")
                self.register_code_to_realtime_list(종목코드)
                self.realtime_watchlist_df.loc[종목코드] = {
                    "보유수량": 체결수량,
                    "매입가": 체결가격,
                    "현재가": None,
                }

    def receive_msg(self, sScrNo, sRQName, sTrCode, sMsg):
        print(f"Received MSG! 화면번호: {sScrNo}, 사용자 구분명: {sRQName}, TR이름: {sTrCode}, 메세지: {sMsg}")

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
