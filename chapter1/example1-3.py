# chapter1 / example1-3.py
# 목표: 로그인 + 계좌 정보 조회를 클래스로 분리 (실전 구조)
#       KiwoomAPI 클래스에 kiwoom 기능을 모두 캡슐화

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI:
    def __init__(self):
        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.kiwoom.OnEventConnect.connect(self._event_connect)

    def comm_connect(self):
        self.kiwoom.dynamicCall("CommConnect()")

    def get_connect_state(self):
        return self.kiwoom.dynamicCall("GetConnectState()")

    def get_login_info(self, tag):
        return self.kiwoom.dynamicCall("GetLoginInfo(QString)", tag)

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공!")
        else:
            print(f"로그인 실패! 오류코드: {err_code}")
        self._after_login()

    def _after_login(self):
        if self.get_connect_state() == 0:
            print("서버와 연결이 끊겼습니다!")
            return
        print("서버와 연결 중입니다!")

        accounts_raw = self.get_login_info("ACCNO")
        accounts = [a for a in accounts_raw.split(";") if a.strip()]

        print("=" * 40)
        print(f"사용자 ID   : {self.get_login_info('USER_ID')}")
        print(f"사용자 이름 : {self.get_login_info('USER_NAME')}")
        print(f"보유 계좌 수: {self.get_login_info('ACCOUNT_CNT')}")
        for i, acc in enumerate(accounts):
            print(f"계좌 {i+1}     : {acc}")
        print("=" * 40)


class MyWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.api = KiwoomAPI()
        self.api.comm_connect()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = MyWindow()
    sys.exit(app.exec_())
