# chapter1 / example1-3.py
# 목표: 로그인 완성 + 로그인 후 계좌/사용자 정보 조회 (GetLoginInfo)
#
# GetLoginInfo 조회 태그:
#   ACCOUNT_CNT  - 보유 계좌 수
#   ACCNO        - 전체 계좌번호 목록 (세미콜론 구분)
#   USER_ID      - 사용자 ID
#   USER_NAME    - 사용자 이름
#   KEY_BSECGB   - 키보드 보안 해지 여부 (0:정상, 1:해지)
#   FIREW_SECGB  - 방화벽 설정 여부 (0:미설정, 1:설정, 2:해지)

import sys
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton,
                             QLabel, QVBoxLayout, QWidget, QTextEdit)
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        self.OnEventConnect.connect(self._on_event_connect)

    def login(self):
        self.dynamicCall("CommConnect()")

    def get_login_info(self, tag):
        return self.dynamicCall("GetLoginInfo(QString)", tag)

    def _on_event_connect(self, err_code):
        if err_code == 0:
            self._fetch_user_info()
        else:
            self.log_widget.append(f"[로그인 실패] 오류코드: {err_code}")

    def _fetch_user_info(self):
        account_cnt = self.get_login_info("ACCOUNT_CNT")
        accounts_raw = self.get_login_info("ACCNO")
        user_id = self.get_login_info("USER_ID")
        user_name = self.get_login_info("USER_NAME")

        accounts = [a for a in accounts_raw.split(";") if a.strip()]

        self.log_widget.append("=" * 40)
        self.log_widget.append("[로그인 성공] 사용자 정보")
        self.log_widget.append(f"  사용자 ID   : {user_id}")
        self.log_widget.append(f"  사용자 이름 : {user_name}")
        self.log_widget.append(f"  보유 계좌 수: {account_cnt}")
        for i, acc in enumerate(accounts):
            self.log_widget.append(f"  계좌 {i+1}     : {acc}")
        self.log_widget.append("=" * 40)

        print(f"[로그인 성공] ID={user_id}, 이름={user_name}, 계좌수={account_cnt}")
        print(f"[계좌 목록] {accounts}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("키움 OpenAPI - Chapter1 Example1-3 (로그인 + 계좌조회)")
        self.setGeometry(100, 100, 500, 350)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.kiwoom = KiwoomAPI()

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.append("[앱 시작] 로그인 버튼을 눌러 키움 로그인창을 실행하세요.")
        self.kiwoom.log_widget = self.log

        login_btn = QPushButton("로그인")
        login_btn.clicked.connect(self.kiwoom.login)

        layout.addWidget(QLabel("로그"))
        layout.addWidget(self.log)
        layout.addWidget(login_btn)
        layout.addWidget(self.kiwoom)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
