# chapter1 / example1-2.py
# 목표: 로그인 후 GetLoginInfo로 사용자/계좌 정보 조회
#
# GetLoginInfo 인자값:
#   ACCOUNT_CNT    - 보유계좌 갯수
#   ACCLIST(ACCNO) - ';'로 연결된 보유계좌 목록
#   USER_ID        - 사용자 ID
#   USER_NAME      - 사용자 이름
#   GetServerGubun - 접속서버 구분 (1: 모의투자, 나머지: 실거래)
#   KEY_BSECGB     - 키보드 보안 해지여부 (0: 정상, 1: 해지)
#   FIREW_SECGB    - 방화벽 설정여부 (0: 미설정, 1: 설정, 2: 해지)

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
        account_cnt  = self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCOUNT_CNT"])
        account_list = str(self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["ACCLIST"]).rstrip(';'))
        user_id      = self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["USER_ID"])
        user_name    = self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["USER_NAME"])
        server_gubun = self.kiwoom.dynamicCall("GetLoginInfo(QString)", ["GetServerGubun"])

        server = "모의투자" if server_gubun == "1" else "실거래"

        print(f"사용자 ID  : {user_id}")
        print(f"사용자 이름: {user_name}")
        print(f"보유계좌 수: {account_cnt}")
        print(f"계좌번호   : {account_list}")
        print(f"접속서버   : {server}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = MyWindow()
    sys.exit(app.exec_())
