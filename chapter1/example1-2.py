# chapter1 / example1-2.py
# 목표: 로그인 후 사용자/계좌 정보 조회 (GetLoginInfo)
#
# GetLoginInfo 태그:
#   ACCOUNT_CNT  - 보유 계좌 수
#   ACCNO        - 전체 계좌번호 목록 (세미콜론 구분)
#   USER_ID      - 사용자 ID
#   USER_NAME    - 사용자 이름
#   KEY_BSECGB   - 키보드 보안 해지 여부 (0:정상, 1:해지)
#   FIREW_SECGB  - 방화벽 설정 여부 (0:미설정, 1:설정, 2:해지)

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class MyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.dynamicCall("CommConnect()")

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공!")
        else:
            print("로그인 실패!")
        self.after_login()

    def after_login(self):
        if self.kiwoom.dynamicCall("GetConnectState()") == 0:
            print("서버와 연결이 끊겼습니다!")
            return
        print("서버와 연결 중입니다!")
        self.get_login_info()

    def get_login_info(self):
        account_cnt = self.kiwoom.dynamicCall("GetLoginInfo(QString)", "ACCOUNT_CNT")
        accounts_raw = self.kiwoom.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        user_id = self.kiwoom.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        user_name = self.kiwoom.dynamicCall("GetLoginInfo(QString)", "USER_NAME")

        accounts = [a for a in accounts_raw.split(";") if a.strip()]

        print("=" * 40)
        print(f"사용자 ID   : {user_id}")
        print(f"사용자 이름 : {user_name}")
        print(f"보유 계좌 수: {account_cnt}")
        for i, acc in enumerate(accounts):
            print(f"계좌 {i+1}     : {acc}")
        print("=" * 40)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = MyWindow()
    sys.exit(app.exec_())
