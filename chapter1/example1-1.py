# chapter1 / example1-1.py
# 목표: PyQt5 기본 윈도우 생성 + 키움 OpenAPI OCX 컨트롤 연결 확인

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget
from PyQt5.QAxContainer import QAxWidget


class KiwoomOCX(QAxWidget):
    def __init__(self):
        super().__init__()
        # khopenapi.ocx 의 ProgID 로 OCX 컨트롤 로드
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("키움 OpenAPI - Chapter1 Example1-1")
        self.setGeometry(100, 100, 400, 200)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # OCX 컨트롤 생성
        self.kiwoom = KiwoomOCX()

        # 연결 상태 확인
        state = self.kiwoom.dynamicCall("GetConnectState()")
        status = "연결됨" if state == 1 else "미연결 (로그인 필요)"

        label = QLabel(f"OpenAPI OCX 로드 상태: 성공\n연결 상태: {status}")
        layout.addWidget(label)
        layout.addWidget(self.kiwoom)

        print(f"[OCX 로드] 성공")
        print(f"[연결 상태] {status} (GetConnectState={state})")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
