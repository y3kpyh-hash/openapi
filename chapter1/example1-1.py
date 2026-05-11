# chapter1 / example1-1.py
# 목표: 키움 OCX 연결 + 로그인 + 연결 상태 확인

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
        else:
            print("서버와 연결 중입니다!")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    myWindow = MyWindow()
    sys.exit(app.exec_())
