# chapter3 / example3-2.py
# 목표: 영웅문 HTS 조건식 결과 API 연동 (feat. 관심종목 연동)
#
# 조건검색 흐름:
#   GetConditionLoad() → OnReceiveConditionVer → GetConditionNameList()
#   → SendCondition() → OnReceiveTrCondition (일반조회) / OnReceiveRealCondition (실시간)
#
# 조건검색 제한:
#   - 1초당 5회 조회 제한
#   - 조건별 1분당 1회 제한 (실시간 조건검색 수신에는 영향없음)
#   - 조건결과 100종목 초과 시 실시간 조건검색 신호 수신 불가
#   - 실시간 조건검색 최대 10개
#
# using_condition_name: HTS(영웅문4)에 저장된 조건식 이름과 정확히 일치해야 함

import sys

from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QAxContainer import QAxWidget


class KiwoomAPI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.realtime_data_scrnum = 5000
        self.using_condition_name = "자동매매 매수 테스트"  # 등록할 조건명 (HTS에서 저장한 조건식명)

        self.kiwoom = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._set_signal_slots()  # 키움증권 API와 내부 메소드를 연동
        self._login()

    def _set_signal_slots(self):
        self.kiwoom.OnEventConnect.connect(self._event_connect)
        self.kiwoom.OnReceiveConditionVer.connect(self._receive_condition)
        self.kiwoom.OnReceiveRealCondition.connect(self._receive_real_condition)
        self.kiwoom.OnReceiveTrCondition.connect(self._receive_tr_condition)

    def _login(self):
        ret = self.kiwoom.dynamicCall("CommConnect()")
        if ret == 0:
            print("로그인 창 열기 성공!")

    def _event_connect(self, err_code):
        if err_code == 0:
            print("로그인 성공!")
            self._after_login()
        else:
            raise Exception("로그인 실패!")

    def _after_login(self):
        print("조건 검색 정보 요청")
        self.kiwoom.dynamicCall("GetConditionLoad()")  # 조건 검색 정보 요청

    def _set_input_value(self, id, value):
        self.kiwoom.dynamicCall("SetInputValue(QString, QString)", id, value)

    def _comm_rq_data(self, rqname, trcode, next, screen_no):
        self.kiwoom.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, trcode, next, screen_no)

    def _comm_get_data(self, code, real_type, field_name, index, item_name):
        ret = self.kiwoom.dynamicCall(
            "CommGetData(QString, QString, QString, int, QString)",
            code, real_type, field_name, index, item_name
        )
        return ret.strip()

    def _receive_real_condition(self, strCode, strType, strConditionName, strConditionIndex):
        # strType: 이벤트 종류, "I":종목편입, "D":종목이탈
        # strConditionName: 조건식 이름
        # strConditionIndex: 조건명 인덱스
        print(f"조건식 실시간 편입/이탈 이벤트 수신! Received real condition, {strCode}, {strType}, {strConditionName}, {strConditionIndex}")

    def _receive_tr_condition(self, scrNum, strCodeList, strConditionName, nIndex, nNext):
        print(f"조건식 일반 검색 조회 완료! Received TR Condition, strCodeList: {strCodeList}, strConditionName: {strConditionName}, "
              f"nIndex: {nIndex}, nNext: {nNext}, scrNum: {scrNum}")
        for stock_code in strCodeList.split(';'):
            print(stock_code)

    def _get_realtime_data_screen_num(self):
        self.realtime_data_scrnum += 1
        if self.realtime_data_scrnum > 5150:
            self.realtime_data_scrnum = 5000
        return str(self.realtime_data_scrnum)

    def _receive_condition(self):
        condition_info = self.kiwoom.dynamicCall("GetConditionNameList()").split(';')
        print(f"조건명, 조건index 수신! {condition_info}")
        for condition_name_idx_str in condition_info:  # 모든 조건식 리스트를 순회
            if len(condition_name_idx_str) == 0:
                continue
            condition_idx, condition_name = condition_name_idx_str.split('^')
            if condition_name == self.using_condition_name:
                self.send_condition(self._get_realtime_data_screen_num(), condition_name, condition_idx, nsearch=0)  # 0: 조건식 일반 조회

    def send_condition(self, scrNum, condition_name, nidx, nsearch):
        # nSearch: 조회구분, 0:조건검색, 1:실시간 조건검색
        result = self.kiwoom.dynamicCall("SendCondition(QString, QString, int, int)", scrNum, condition_name, nidx, nsearch)
        if result == 1:
            print(f"{condition_name} 조건검색 등록")

    def set_real(self, scrNum, strCodeList, strFidList, strRealType):
        self.kiwoom.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            scrNum, strCodeList, strFidList, strRealType
        )

    def _get_comm_realdata(self, strCode, nFid):
        return self.kiwoom.dynamicCall("GetCommRealData(QString, int)", strCode, nFid)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom_api = KiwoomAPI()
    sys.exit(app.exec_())
