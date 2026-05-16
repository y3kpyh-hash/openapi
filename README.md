# 키움증권 OpenAPI 자동매매 프로그램

키움증권 OpenAPI+를 활용한 주식 자동매매 프로그램 (Python + PyQt5)
Claude Code (VS Code) 환경 기준으로 작성되었습니다.

---

## 개발 환경 (중요: 32비트 Python 필수)

키움 OpenAPI OCX는 **32비트 전용**입니다. 64비트 Python으로는 실행 불가합니다.

### 1단계 - 32비트 Python 3.10 설치

아래 링크에서 Windows installer (32-bit) 다운로드 및 설치:
https://www.python.org/downloads/release/python-31011/

- 설치 경로 예: C:\Python310_32
- 설치 시 "Add Python to PATH" 체크 해제 권장 (64비트 Python과 충돌 방지)

설치 확인:
```
C:\Python310_32\python.exe -c "import platform; print(platform.architecture())"
# ('32bit', 'WindowsPE') 출력되어야 정상
```

### 2단계 - 의존성 패키지 설치

```
C:\Python310_32\python.exe -m pip install -r requirements.txt
```

### 3단계 - VS Code 인터프리터 설정

VS Code에서 Ctrl+Shift+P -> "Python: Select Interpreter"
-> "Enter interpreter path" -> C:\Python310_32\python.exe 입력

또는 .vscode/settings.json 파일이 자동 생성되어 있으면 바로 사용 가능합니다.

### 4단계 - 키움 OpenAPI+ 설치 확인

C:\OpenAPI\khopenapi.ocx 파일이 있어야 합니다.
영웅문4 HTS 설치 후 OpenAPI+ 신청 및 설치하면 자동으로 등록됩니다.

---

## 챕터 구성

| 챕터 | 주제 | 파일 |
|------|------|------|
| chapter1 | OCX 연결 + 로그인 + 계좌조회 | example1-1.py ~ example1-3.py |
| chapter2 | TR 조회와 실시간 데이터 처리 | example2-1.py ~ |
| chapter3 | 주문과 잔고처리 | (예정) |
| chapter4 | 조건검색 | (예정) |
| chapter5 | 기타함수 + 자동화 완성 | (예정) |

---

## Chapter1 - 로그인 및 기본 구조

### example1-1.py
키움 OCX 연결 + CommConnect() 로그인 + 연결 상태 확인 (after_login)

### example1-2.py
로그인 후 GetLoginInfo() 로 사용자 ID, 이름, 계좌번호 목록 출력

### example1-3.py
KiwoomAPI 클래스 분리 - 실전 구조 기반으로 캡슐화

---

## Chapter2 - TR 조회와 실시간 데이터 처리

### example2-1.py
opt10001(주식기본정보요청) TR 요청 구조 실습
- `KiwoomAPI` 클래스로 리팩토링 (MyWindow → KiwoomAPI)
- `_set_signal_slots()`: OnEventConnect + OnReceiveTrData 연결
- `get_basic_stock_info("005930")`: SetInputValue → CommRqData 요청
- `_receive_tr_data()`: TR 이벤트 수신 후 rqname으로 라우팅
- `get_comm_data()`: GetCommData 래퍼
- `on_opt10001_req()`: 종목코드, 현재가, 기준가, 시가, 고가, 저가, 상/하한가 출력

---

## 핵심 동작 원리

요청(함수 호출) -> 이벤트 발생(On~) -> 데이터 획득

- 모든 동작은 비동기 이벤트 기반
- 스레드 미지원 - 메인 스레드에서만 호출
- 화면번호(스크린번호): 최대 200개, 4자리 숫자 (0000 제외)
- 계좌번호: 10자리 전체 입력 필요

---

## 실행 방법 (VS Code)

1. VS Code 하단 인터프리터가 32비트 Python으로 설정된 것 확인
2. chapter1/example1-1.py 열기
3. 우측 상단 실행 버튼(>) 또는 F5
4. 키움 로그인창에서 ID/PW 입력 후 로그인