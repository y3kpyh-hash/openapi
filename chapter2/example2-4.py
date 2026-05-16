# chapter2 / example2-4.py
# 목표: opw00018 계좌평가잔고내역요청 - 보유 종목 정보 DataFrame 출력
#
# opw00018 입력값:
#   계좌번호          - 전분 조회할 보유계좌번호 10자리
#   비밀번호          - 사용안함 (공백)
#   비밀번호입력매체구분 - 00 (공백 불가)
#   조회구분          - 1:합산, 2:개별
#   거래소구분         - KRX:한국거래소, NXT:대체거래소, 공백:한국거래소
#
# opw00018 출력값:
#   싱글 [계좌평가결과] - 추정예탁자산 등
#   멀티 [계좌평가잔고개별합산] - 종목번호, 종목명, 보유수량, 매매가능수량, 현재가, 매입가 등

import sys
import pandas as pd
from loguru import logger
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()
        self.using_account_num = ""
        self.account_info_df = pd.DataFrame(
            columns=[
                "계좌번호",
                "종목코드",
                "종목명",
                "보유수량",
                "매매가능수량",
                "평균단가",
                "현재가",
            ],
            index=pd.Index(data=[], name="종목코드"),
        )
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
        account_nums = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCNO"]).rstrip(';'))
        account_list = account_nums.split(';')
        self.using_account_num = account_list[-1]  # 계좌번호들 중 가장 마지막 것으로 조회
        self.get_account_balance()

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )

    def set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def get_account_balance(self, next=0):
        if len(self.using_account_num) > 0:
            self.set_input_value(id="계좌번호", value=self.using_account_num)
            self.set_input_value(id="비밀번호", value="")
            self.set_input_value(id="비밀번호입력매체구분", value="00")
            self.set_input_value(id="조회구분", value="1")
            self.set_input_value(id="거래소구분", value="")  # 공백: KRX시세, NXT: NXT 시세
            self.comm_rq_data(rqname="opw00018_req", trcode="opw00018", next=next, screen_no="5000")

    def _receive_tr_data(self, sScrnNo, sRQName, sTrCode, sRecordName, sPrevNext,
                         nDataLength, sErrorCode, sMessage, sSplmMsg):
        if sRQName == "opw00018_req":
            self.on_opw00018_req(sTrCode, sRQName)

    def on_opw00018_req(self, sTrCode, sRQName):
        try:
            현재평가잔고 = int(self.get_comm_data(sTrCode, sRQName, nIndex=0, strItemName="추정예탁자산"))
        except:
            현재평가잔고 = 0
        logger.info(f"현재 평가 잔고: {현재평가잔고}")
        cnt = self._get_repeat_cnt(sTrCode, sRQName)
        for i in range(cnt):
            try:
                종목코드     = self.get_comm_data(sTrCode, sRQName, i, strItemName="종목번호").replace("A", "").strip()
                종목명      = self.get_comm_data(sTrCode, sRQName, i, strItemName="종목명")
                매매가능수량  = int(self.get_comm_data(sTrCode, sRQName, i, strItemName="매매가능수량"))
                보유수량     = int(self.get_comm_data(sTrCode, sRQName, i, strItemName="보유수량"))
                현재가      = int(self.get_comm_data(sTrCode, sRQName, i, strItemName="현재가"))
                매입가      = int(self.get_comm_data(sTrCode, sRQName, i, strItemName="매입가"))
                self.account_info_df.loc[종목코드] = {
                    "계좌번호":    self.using_account_num,
                    "종목코드":    종목코드,
                    "종목명":     종목명,
                    "보유수량":    보유수량,
                    "매매가능수량": 매매가능수량,
                    "평균단가":    매입가,
                    "현재가":     현재가,
                }
            except Exception as e:
                logger.exception(e)
        print(self.account_info_df)

    def get_comm_data(self, strTrCode, strRecordName, nIndex, strItemName):
        ret = self.kiwoom.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            strTrCode, strRecordName, nIndex, strItemName
        )
        return ret.strip()

    def _get_repeat_cnt(self, trcode, rqname):
        ret = self.kiwoom.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        return ret


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
