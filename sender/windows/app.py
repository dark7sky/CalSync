"""CalSync Windows GUI and system-tray application.

Requires PySide6. No hidden browser automation or direct Internet connection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QFileSystemWatcher, QSettings, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
)

from core import Engine, json_compact


def data_folder() -> Path:
    return Path.home() / "AppData" / "Local" / "CalSync"


class Client(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CalSync - Windows Desktop")
        self.setMinimumSize(920, 640)
        self.config = QSettings("CalSync", "WindowsDesktop")
        self.engine = Engine(data_folder() / "client.db")
        self.monitoring = False
        self.exiting = False
        self.last_batch = None
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self.schedule_scan)
        self.watcher.fileChanged.connect(self.schedule_scan)

        self.tray = QSystemTrayIcon(self)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.setWindowIcon(icon)
        self.tray.setIcon(icon)
        menu = QMenu(self)
        self.tray_show = QAction("창 열기", self)
        self.tray_show.triggered.connect(self.restore)
        self.tray_pause = QAction("로컬 JSON 감시 일시중지", self)
        self.tray_pause.triggered.connect(self.toggle_monitor)
        self.tray_scan = QAction("지금 스캔", self)
        self.tray_scan.triggered.connect(self.scan)
        self.tray_exit = QAction("완전히 종료", self)
        self.tray_exit.triggered.connect(self.quit)
        menu.addActions([self.tray_show, self.tray_pause, self.tray_scan])
        menu.addSeparator()
        menu.addAction(self.tray_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.restore()
            if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                          QSystemTrayIcon.ActivationReason.Trigger) else None
        )
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        host = QWidget()
        self.setCentralWidget(host)
        vertical = QVBoxLayout(host)

        self.status = QLabel("로컬 감시 중지")
        vertical.addWidget(self.status)
        info = QLabel("외부 전송은 Menlo Browser의 사용자 제출이 필요합니다. "
                      "이 프로그램은 직접 외부 네트워크에 연결하지 않습니다.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#64748b")
        vertical.addWidget(info)

        config_group = QGroupBox("설정")
        form = QFormLayout(config_group)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(self.config.value("folder", str(Path.home() / "Documents")))
        browse = QPushButton("폴더 선택")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder)
        folder_row.addWidget(browse)
        form.addRow("JSON 폴더", folder_row)
        self.url = QLineEdit(self.config.value("url", "https://calendar.example.com"))
        form.addRow("CalSync HTTPS URL", self.url)
        self.deletions = QLineEdit(self.config.value("deletion_types", ""))
        self.deletions.setPlaceholderText("예: cancelled,deleted (실제 JSON type 값 확인 후 설정)")
        form.addRow("명시적 삭제 type 값", self.deletions)
        self.interval = QSpinBox()
        self.interval.setRange(10, 3600)
        self.interval.setValue(int(self.config.value("interval", 30)))
        self.interval.setSuffix("초")
        form.addRow("주기적 재스캔", self.interval)
        vertical.addWidget(config_group)

        buttons = QHBoxLayout()
        self.monitor_button = QPushButton("로컬 자동 감시 시작")
        self.monitor_button.clicked.connect(self.toggle_monitor)
        self.scan_button = QPushButton("지금 스캔")
        self.scan_button.clicked.connect(self.scan)
        self.prepare_button = QPushButton("전송 데이터 준비 · 복사")
        self.prepare_button.clicked.connect(self.prepare)
        self.open_button = QPushButton("Menlo 웹페이지 열기")
        self.open_button.clicked.connect(self.open_site)
        for b in (self.monitor_button, self.scan_button, self.prepare_button, self.open_button):
            buttons.addWidget(b)
        vertical.addLayout(buttons)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["번호", "동작", "상태", "원본 식별자", "Batch ID"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        vertical.addWidget(self.table)

        receipt_actions = QHBoxLayout()
        self.submitted_button = QPushButton("웹 제출 버튼을 눌렀음 (확인 전)")
        self.submitted_button.clicked.connect(self.mark_submitted)
        self.import_button = QPushButton("서버 영수증/상태 가져오기")
        self.import_button.clicked.connect(self.import_receipt)
        receipt_actions.addWidget(self.submitted_button)
        receipt_actions.addWidget(self.import_button)
        vertical.addLayout(receipt_actions)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(130)
        vertical.addWidget(self.logs)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.scan)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.scan)
        self.folder.editingFinished.connect(self.folder_changed)
        self.interval.valueChanged.connect(self.reset_timer)
        self.url.editingFinished.connect(self.save_settings)
        self.deletions.editingFinished.connect(self.save_settings)

        self.refresh_table()
        if str(self.config.value("monitor", "false")).lower() == "true":
            self.start_monitor()

    def log(self, message: str):
        from datetime import datetime
        self.logs.append(f"[{datetime.now():%H:%M:%S}] {message}")

    def save_settings(self):
        self.config.setValue("folder", self.folder.text().strip())
        self.config.setValue("url", self.url.text().strip())
        self.config.setValue("deletion_types", self.deletions.text())
        self.config.setValue("interval", self.interval.value())
        self.config.setValue("monitor", "true" if self.monitoring else "false")
        self.config.sync()

    def choose_folder(self):
        choice = QFileDialog.getExistingDirectory(
            self, "JSON 파일 폴더", self.folder.text())
        if choice:
            self.folder.setText(choice)
            self.folder_changed()

    def folder_changed(self):
        self.save_settings()
        self.update_watches()
        if self.monitoring:
            self.scan()

    def update_watches(self):
        all_paths = self.watcher.files() + self.watcher.directories()
        if all_paths:
            self.watcher.removePaths(all_paths)
        target = Path(self.folder.text())
        if target.is_dir():
            self.watcher.addPath(str(target))
            for path in target.glob("*.json"):
                self.watcher.addPath(str(path))

    def reset_timer(self):
        self.save_settings()
        if self.monitoring:
            self.timer.start(self.interval.value() * 1000)

    def start_monitor(self):
        if not Path(self.folder.text().strip()).is_dir():
            QMessageBox.warning(self, "폴더 없음", "유효한 JSON 폴더를 먼저 선택하세요.")
            return
        self.monitoring = True
        self.save_settings()
        self.update_watches()
        self.timer.start(self.interval.value() * 1000)
        self.monitor_button.setText("로컬 자동 감시 일시중지")
        self.tray_pause.setText("로컬 JSON 감시 일시중지")
        self.scan()
        self.status.setText("로컬 JSON 자동 감시 중 (서버 전송은 Menlo에서 확인 필요)")
        self.log("자동 감시 시작")

    def toggle_monitor(self):
        if not self.monitoring:
            self.start_monitor()
        else:
            self.monitoring = False
            self.timer.stop()
            self.debounce.stop()
            self.save_settings()
            self.monitor_button.setText("로컬 자동 감시 시작")
            self.tray_pause.setText("로컬 JSON 감시 재개")
            self.status.setText("로컬 JSON 자동 감시 중지")
            self.log("자동 감시 일시중지")

    def schedule_scan(self):
        if self.monitoring:
            self.debounce.start(750)

    def scan(self):
        target = Path(self.folder.text().strip())
        try:
            result = self.engine.scan(
                target, {x.strip() for x in self.deletions.text().split(",") if x.strip()})
            self.update_watches()
            self.status.setText(
                f"{'감시 중' if self.monitoring else '수동 스캔'} · "
                f"발견 {result['events']}건 · 준비/처리 중 {self.engine.pending_count()}건 · "
                f"수정 감지 {result['changes']}건")
            for error in result["errors"][:15]:
                self.log("확인 필요: " + error)
            if result["changes"]:
                self.log(f"수정된 기존 이벤트 {result['changes']}건은 자동 덮어쓰기하지 않습니다.")
            self.refresh_table()
        except (ValueError, OSError, Exception) as error:
            self.log("스캔 실패: " + str(error))
            self.status.setText("스캔 실패: " + str(error))

    def refresh_table(self):
        rows = self.engine.queue_rows()
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate((
                row["id"], row["action"], row["status"], row["ref"],
                row["batch_id"] or ""
            )):
                item = QTableWidgetItem(str(value))
                if j == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()

    def prepare(self):
        packet = self.engine.prepare()
        if not packet:
            QMessageBox.information(self, "전송할 항목 없음", "준비할 새 요청이 없습니다.")
            return
        self.last_batch = packet["batchId"]
        raw = json.dumps(packet, ensure_ascii=False, indent=2)
        QApplication.clipboard().setText(raw)
        self.refresh_table()
        dialog = QDialog(self)
        dialog.setWindowTitle("Menlo 웹페이지에 붙여넣을 JSON")
        dialog.resize(720, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("클립보드에 복사했습니다. CalSync 웹페이지에 붙여넣어 제출하세요.\n"
                                "만약 Menlo가 복사를 차단한다면 기관 정책을 확인하세요."))
        editor = QTextEdit()
        editor.setPlainText(raw)
        editor.setReadOnly(True)
        layout.addWidget(editor)
        close = QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
        self.log(f"{self.last_batch}: JSON 전송 준비됨 (아직 실제 서버로 보내지 않음)")

    def mark_submitted(self):
        if not self.last_batch:
            QMessageBox.information(self, "Batch 미선택", "전송 데이터 준비를 먼저 수행하세요.")
            return
        count = self.engine.mark_submitted(self.last_batch)
        self.log(f"웹 제출했다고 표시한 요청 {count}건. 서버 수신 영수증 확인 전입니다.")
        self.refresh_table()

    def import_receipt(self):
        raw, ok = QInputDialog.getMultiLineText(
            self, "서버 수신 상태 가져오기",
            "Menlo 웹페이지에서 복사한 JSON 영수증/상태를 붙여넣으세요.")
        if not ok or not raw.strip():
            return
        try:
            result = self.engine.load_receipt(raw)
            self.log(f"서버 응답 갱신 {result['updated']}건")
            for error in result["errors"]:
                self.log("검증 실패: " + error)
            self.refresh_table()
        except (ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, "영수증 오류", str(error))

    def open_site(self):
        parsed = urlsplit(self.url.text().strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            QMessageBox.warning(self, "HTTPS 주소 오류", "https://도메인 형식만 허용됩니다.")
            return
        self.save_settings()
        QDesktopServices.openUrl(QUrl(self.url.text().strip()))
        self.log("기본 브라우저를 열었습니다. Menlo 경유인지 직접 확인하세요.")

    def restore(self):
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def quit(self):
        self.exiting = True
        self.timer.stop()
        self.debounce.stop()
        self.save_settings()
        self.tray.hide()
        self.engine.close()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent):
        if self.exiting:
            event.accept()
            return
        if self.monitoring and QSystemTrayIcon.isSystemTrayAvailable():
            answer = QMessageBox(self)
            answer.setWindowTitle("CalSync 자동 감시 실행 중")
            answer.setText("JSON 자동 감시가 실행 중입니다. 어떻게 하시겠습니까?")
            to_tray = answer.addButton("트레이로", QMessageBox.ButtonRole.AcceptRole)
            leave = answer.addButton("완전히 종료", QMessageBox.ButtonRole.DestructiveRole)
            cancel = answer.addButton("취소", QMessageBox.ButtonRole.RejectRole)
            answer.setDefaultButton(to_tray)
            answer.exec()
            chosen = answer.clickedButton()
            if chosen == to_tray:
                event.ignore()
                self.hide()
                self.tray.showMessage("CalSync", "트레이에서 JSON 감시를 계속합니다.",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)
            elif chosen == leave:
                self.quit()
                event.accept()
            else:
                event.ignore()
        else:
            self.quit()
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CalSync Windows")
    app.setQuitOnLastWindowClosed(False)
    client = Client()
    client.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
