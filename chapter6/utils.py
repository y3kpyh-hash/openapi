import os
import sys
import functools
import traceback

from loguru import logger
from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QVariant
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QStyledItemDelegate, QComboBox, QLineEdit,
)
from PyQt5.QtGui import QColor


set_pw_msg = (
    "키움증권 계좌 비밀번호 자동입력 설정을 진행합니다.\n"
    "키움증권 공인인증 로그인 후 [계좌관리 > 계좌비밀번호 저장] 메뉴에서\n"
    "사용할 계좌의 비밀번호를 등록해주세요.\n"
    "등록 후 확인 버튼을 눌러주세요."
)

reset_pw_msg = (
    "비밀번호 재설정을 진행합니다.\n"
    "키움증권 공인인증 로그인 후 [계좌관리 > 계좌비밀번호 저장] 메뉴에서\n"
    "비밀번호를 재등록해주세요.\n"
    "등록 후 확인 버튼을 눌러주세요."
)


def log_exceptions(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.exception(f"Exception in {func.__name__}: {e}")
    return wrapper


def resource_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def format_number(line_edit: QLineEdit):
    text = line_edit.text().replace(',', '')
    if text.isdigit():
        line_edit.blockSignals(True)
        line_edit.setText(f"{int(text):,}")
        line_edit.blockSignals(False)


def mask_account_number(account_num: str) -> str:
    if len(account_num) >= 8:
        return account_num[:2] + '****' + account_num[-4:]
    return account_num


class PandasModel(QAbstractTableModel):
    def __init__(self, data):
        super().__init__()
        self._data = data

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._data.columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return QVariant()
        value = self._data.iloc[index.row(), index.column()]
        if role == Qt.DisplayRole:
            return str(value) if value is not None else ""
        if role == Qt.ForegroundRole:
            try:
                fval = float(value)
                if fval > 0:
                    return QColor(Qt.red)
                elif fval < 0:
                    return QColor(Qt.blue)
            except (ValueError, TypeError):
                pass
        return QVariant()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return str(self._data.columns[section])
            if orientation == Qt.Vertical:
                return str(self._data.index[section])
        return QVariant()

    def refresh(self):
        self.layoutChanged.emit()


class ConfirmDialog(QDialog):
    def __init__(self, title='확인', message='', parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout()
        layout.addWidget(QLabel(message))
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton('확인')
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('취소')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        self.setLayout(layout)


class MaskedComboBoxDelegate(QStyledItemDelegate):
    def displayText(self, value, locale):
        return mask_account_number(str(value))
