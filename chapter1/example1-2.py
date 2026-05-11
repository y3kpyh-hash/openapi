# chapter1 / example1-2.py
# 목표: 키움 OpenAPI 로그인 구현
#       CommConnect() 호출 -> 로그인창 -> OnEventConnect 이벤트 수신

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QPushButton, QLabel, QVBoxLayout, QWidget
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        # 로그인 완료 이벤트 연결
        self.OnEventConnect.connect(self._on_event_connect)

    def login(self):
        # 키움 로그인창 실행
        self.dynamicCall("CommConnect()")

    def _on_event_connect(self, err_code):
        if err_code == 0:
            print("[로그인] 성공")
            self.login_label.setText("로그인 상태: 성공")
        else:
            print(f"[로그인] 실패 - 오류코드: {err_code}")
            self.login_label.setText(f"로그인 상태: 실패 (오류코드 {err_code})")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("키움 OpenAPI - Chapter1 Example1-2 (로그인)")
        self.setGeometry(100, 100, 400, 200)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.kiwoom = KiwoomAPI()

        self.status_label = QLabel("로그인 상태: 미연결")
        # label을 OCX에서 접근할 수 있도록 참조 전달
        self.kiwoom.login_label = self.status_label

        login_btn = QPushButton("로그인")
        login_btn.clicked.connect(self.kiwoom.login)

        layout.addWidget(self.status_label)
        layout.addWidget(login_btn)
        layout.addWidget(self.kiwoom)

        print("[앱 시작] '로그인' 버튼을 눌러 키움 로그인창을 실행하세요.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
