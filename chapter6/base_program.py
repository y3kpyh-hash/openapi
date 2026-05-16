import os
import datetime

import pandas as pd
from loguru import logger
from PyQt5.QtCore import Qt

from utils import log_exceptions


class AutoTrader:
    """
    자동매매 베이스 클래스.
    common_api.KiwoomAPI 와 trading_product.CustomAutoTrader 사이의 중간 계층.
    주문 큐(orders_queue) 처리, 미체결 관리, 계좌 정보 관련 공통 로직을 담당.
    """

    # ========================
    # 주문 헬퍼 메소드들
    # ========================

    def get_trading_account_num(self) -> str:
        return self.customAccountNumComboBox.currentText().replace('****', '')

    def get_account_key(self, account_num: str, stock_code: str) -> tuple:
        return (account_num, stock_code)

    # ========================
    # 매수 취소/정정 주문 큐
    # ========================

    def enqueue_cancel_order(
        self,
        stock_code,
        account_num='',
        order_type='매수취소',
        order_quantity=0,
        order_num='',
        exchange='KRX',
        is_credit_order=False,
    ):
        if order_type == "매수취소":
            exchange_num = 3
            if exchange == 'NXT':
                exchange_num = 23
            elif exchange == 'SOR':
                exchange_num = 13
            self.orders_queue.put(
                [
                    "매수취소주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    "",
                    "00",
                    "",
                    "",
                    order_num,
                ]
            )
        elif order_type == "매도취소":
            exchange_num = 4
            if exchange == 'NXT':
                exchange_num = 24
            elif exchange == 'SOR':
                exchange_num = 14
            self.orders_queue.put(
                [
                    "매도취소주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    "",
                    "00",
                    "",
                    "",
                    order_num,
                ]
            )

    def enqueue_amend_order(
        self,
        stock_code,
        account_num='',
        order_type='매수정정',
        order_quantity=0,
        order_price=0,
        order_num='',
        exchange='KRX',
        is_credit_order=False,
    ):
        if order_type == "매수정정":
            exchange_num = 5
            if exchange == 'NXT':
                exchange_num = 25
            elif exchange == 'SOR':
                exchange_num = 15
            self.orders_queue.put(
                [
                    "매수정정주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "03",
                    "",
                    "",
                    order_num,
                ]
            )
        elif order_type == "매도정정":
            exchange_num = 6
            if exchange == 'NXT':
                exchange_num = 26
            elif exchange == 'SOR':
                exchange_num = 16
            self.orders_queue.put(
                [
                    "매도정정주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "03",
                    "",
                    "",
                    order_num,
                ]
            )

    # ========================
    # 매수 주문 큐
    # ========================

    def enqueue_buy_order(
        self,
        stock_code,
        account_num='',
        is_market_order=False,
        order_quantity=0,
        order_price=0,
        exchange='KRX',
        is_credit_order=False,
    ):
        exchange_num = 1
        if exchange == 'NXT':
            exchange_num = 21
        elif exchange == 'SOR':
            exchange_num = 11

        if is_market_order and not is_credit_order and order_price == 0:
            self.orders_queue.put(
                [
                    "시장가매수주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    "",
                    "03",
                    "",
                    "",
                    "",
                ],
            )
        elif not is_market_order and not is_credit_order:
            self.orders_queue.put(
                [
                    "지정가매수주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "03",
                    "",
                    "",
                ],
            )
        elif not is_market_order and is_credit_order:
            self.orders_queue.put(
                [
                    "지정가신용매수주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "03",
                    "",
                    "",
                    "99",
                    "99991231",
                    "",
                ],
            )

    # ========================
    # 매도 주문 큐
    # ========================

    def enqueue_sell_order(
        self,
        stock_code,
        account_num='',
        is_market_order=False,
        order_quantity=0,
        order_price=0,
        exchange='KRX',
        is_credit_order=False,
    ):
        exchange_num = 2
        if exchange == 'NXT':
            exchange_num = 22
        elif exchange == 'SOR':
            exchange_num = 12

        if is_market_order and not is_credit_order and order_price == 0:
            self.orders_queue.put(
                [
                    "시장가매도주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    "",
                    "03",
                    "",
                    "",
                    "",
                ],
            )
        elif not is_market_order and not is_credit_order:
            self.orders_queue.put(
                [
                    "지정가매도주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "03",
                    "",
                    "",
                ],
            )
        elif not is_market_order and is_credit_order:
            self.orders_queue.put(
                [
                    "지정가신용매도주문",
                    self._get_screen_num(),
                    account_num,
                    exchange_num,
                    stock_code,
                    int(order_quantity),
                    int(order_price),
                    "00",
                    "99",
                    "99991231",
                    "",
                ],
            )

    # ========================
    # 실시간 틱 데이터 수신 콜백
    # ========================

    @log_exceptions
    def on_receive_realtime_tick_data(self, data):
        self.now_time = datetime.datetime.now()
        종목코드   = data['종목코드']
        현재가    = data['현재가']
        전일대비   = data['전일대비']
        account_info_index       = self.account_info_df.index
        credit_account_info_index = self.credit_account_info_df.index
        for 계좌번호 in self.account_list:
            key = self.get_account_key(계좌번호, 종목코드)
            if key in account_info_index:
                self.account_info_df.at[key, "현재가"] = 현재가
                self.account_info_df.at[key, "전일대비(%)"] = round(전일대비, 2)
                매수가격 = self.account_info_df.at[key, "평균단가"]
                수익률 = round((현재가 - 매수가격) / 매수가격 * 100 - self.transaction_cost, 2)
                self.account_info_df.at[key, "수익률(%)"] = 수익률

    # ========================
    # 체결 데이터 수신 콜백
    # ========================

    @log_exceptions
    def on_receive_order_data(self, data):
        계좌번호   = data['계좌번호']
        주문구분   = data['주문구분']
        종목코드   = data['종목코드']
        종목명    = data['종목명']
        미체결수량  = data['미체결수량']
        체결수량   = data['체결수량']
        체결가격   = data['체결가격']
        단위체결수량 = data['단위체결수량']
        단위체결가격 = data['단위체결가격']

        key = self.get_account_key(계좌번호, 종목코드)
        account_info_index        = self.account_info_df.index
        credit_account_info_index = self.credit_account_info_df.index

        if 주문구분 in ("매수신용", "매수신용정정") and 단위체결수량 > 0:
            try:
                보유수량 = self.credit_account_info_df.at[key, "보유수량"]
                평균단가 = self.credit_account_info_df.at[key, "평균단가"]
                self.credit_account_info_df.at[key, "평균단가"] = round(
                    (평균단가 * 보유수량 + 단위체결가격 * 단위체결수량) / (보유수량 + 단위체결수량)
                )
                self.credit_account_info_df.at[key, "보유수량"]    += 단위체결수량
                self.credit_account_info_df.at[key, "매매가능수량"] += 단위체결수량
                self.update_credit_cell(key, column_name="평균단가")
                self.update_credit_cell(key, column_name="보유수량")
                self.update_credit_cell(key, column_name="매매가능수량")
            except KeyError:
                self.credit_account_info_df.loc[key] = {
                    "계좌번호":  계좌번호,
                    "종목코드":  종목코드,
                    "종목명":   종목명,
                    "보유수량":  단위체결수량,
                    "매매가능수량": 단위체결수량,
                    "평균단가":  단위체결가격,
                    "현재가":   단위체결가격,
                    "전일대비(%)": None,
                    "수익률(%)": -self.transaction_cost,
                }
            self.update_pandas_models()

        elif key in credit_account_info_index and 주문구분 in ("매도신용", "매도신용정정") and 단위체결수량 > 0:
            try:
                self.credit_account_info_df.at[key, "보유수량"]    -= 단위체결수량
                self.credit_account_info_df.at[key, "매매가능수량"] -= 단위체결수량
                if self.credit_account_info_df.at[key, "보유수량"] <= 0:
                    self.credit_account_info_df.drop(key, inplace=True)
                    self.clear_auto_sell_df_by_stock_code(종목코드, target_account="신용", account_num=계좌번호)
            except KeyError:
                pass
            self.update_pandas_models()

        elif 주문구분 in ("매수", "매수정정") and 단위체결수량 > 0:
            try:
                보유수량 = self.account_info_df.at[key, "보유수량"]
                평균단가 = self.account_info_df.at[key, "평균단가"]
                self.account_info_df.at[key, "평균단가"] = round(
                    (평균단가 * 보유수량 + 단위체결가격 * 단위체결수량) / (보유수량 + 단위체결수량)
                )
                self.account_info_df.at[key, "보유수량"]    += 단위체결수량
                self.account_info_df.at[key, "매매가능수량"] += 단위체결수량
                self.update_cell(key, column_name="평균단가")
                self.update_cell(key, column_name="보유수량")
                self.update_cell(key, column_name="매매가능수량")
            except KeyError:
                self.account_info_df.loc[key] = {
                    "계좌번호":  계좌번호,
                    "종목코드":  종목코드,
                    "종목명":   종목명,
                    "보유수량":  단위체결수량,
                    "매매가능수량": 단위체결수량,
                    "평균단가":  단위체결가격,
                    "현재가":   단위체결가격,
                    "전일대비(%)": None,
                    "수익률(%)": -self.transaction_cost,
                }
            self.update_pandas_models()

        elif key in account_info_index and 주문구분 in ("매도", "매도정정") and 단위체결수량 > 0:
            try:
                self.account_info_df.at[key, "보유수량"]    -= 단위체결수량
                self.account_info_df.at[key, "매매가능수량"] -= 단위체결수량
                if self.account_info_df.at[key, "보유수량"] <= 0:
                    self.account_info_df.drop(key, inplace=True)
            except KeyError:
                pass
            self.update_pandas_models()

    # ========================
    # 비밀번호 설정 완료 콜백 (override in subclass)
    # ========================

    @log_exceptions
    def on_finished_password_settings(self):
        logger.debug(f"계좌번호: {self.using_account_num} 정보 조회를 시작합니다.")
        self.request_get_account_balance()

    # ========================
    # 자동매매 현황 테이블 셀 업데이트 헬퍼
    # ========================

    def update_cell(self, key, column_name):
        pass

    def update_credit_cell(self, key, column_name):
        pass

    def update_pandas_models(self):
        pass

    def clear_auto_sell_df_by_stock_code(self, stock_code, target_account='현금', account_num=''):
        pass
