"""Shared presentation for generated audio; model callbacks stay with each workbench."""
from pathlib import Path
from PySide6.QtCore import Qt,QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton,QSizePolicy,QStylePainter,QStyleOptionButton,QStyle
from . import icons

EMPTY_TEXT = '生成后可试听，音频会自动保存到音频库。'

class OutputLocationButton(QPushButton):
    """Elide only the path's tail, reserving the complete icon and button padding."""
    def set_path(self,path):
        path=Path(path).expanduser()
        try:display='~/'+str(path.relative_to(Path.home()))
        except ValueError:display=str(path)
        self.setText(display);self.setToolTip(str(path))
        self.setAccessibleName('保存位置');self.setAccessibleDescription(str(path))

    def display_text(self):
        option=QStyleOptionButton();self.initStyleOption(option)
        area=self.style().subElementRect(QStyle.SE_PushButtonContents,option,self)
        available=max(0,area.width()-self.iconSize().width()-32)
        return self.fontMetrics().elidedText(self.text(),Qt.ElideRight,available)

    def paintEvent(self,event):
        option=QStyleOptionButton();self.initStyleOption(option)
        option.text=self.display_text()
        painter=QStylePainter(self);painter.drawControl(QStyle.CE_PushButton,option)

    def sizeHint(self):return QSize(220,38)
    def minimumSizeHint(self):return QSize(124,38)

def card_style(card,layout,c):
    card.setObjectName('unifiedVoiceResult')
    card.setStyleSheet(f'QFrame#unifiedVoiceResult {{background:{c.surface};border:1px solid {c.line};border-radius:13px;}}')
    layout.setContentsMargins(16,16,16,16);layout.setSpacing(14)
    card.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)

def action_style(button,c,*,download=False,location=False):
    button.setFixedHeight(38);button.setMinimumWidth(104 if download else 80)
    button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
    button.setCursor(Qt.PointingHandCursor);button.setAutoDefault(False)
    button.setStyleSheet(f'''QPushButton {{font-size:13px;font-weight:500;color:{c.ink};background:{c.accent_wash if download else c.surface};border:1px solid {c.line};border-radius:8px;padding:0 12px;}}
        QPushButton:hover {{background:{c.accent_wash};border-color:{c.accent};}}
        QPushButton:pressed {{background:{c.surface_alt};border-color:{c.accent};}}
        QPushButton:focus {{border:2px solid {c.accent};padding:0 11px;}}
        QPushButton:disabled {{color:{c.ink_muted};background:{c.surface_alt};}}''')
    if download:
        button.setText('下载音频');button.setAccessibleName('下载音频')
        button.setIcon(QIcon(icons.pixmap('download',c.accent,16)))
    if location:
        button.setMinimumWidth(124);button.setMaximumWidth(220)
        button.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        button.setIcon(QIcon(icons.pixmap('folder',c.accent,16)))
    button.setIconSize(QSize(16,16))

def player_style(play,wave,time,c):
    play.setFixedSize(44,44);play.setText('');play.setIconSize(QSize(18,18))
    play.setIcon(QIcon(icons.pixmap('play','#ffffff',18)))
    play.setAccessibleName('播放本次结果')
    play.setStyleSheet(f'''QPushButton {{padding:0;border:0;border-radius:22px;background:{c.accent};color:white;}}
        QPushButton:hover {{border:2px solid {c.line};}} QPushButton:focus {{border:2px solid {c.ink};}}''')
    wave.setMinimumWidth(60);wave.setFixedHeight(48);wave.setAccessibleName('本次结果播放进度')
    time.setStyleSheet(f'QLabel {{color:{c.ink_muted};font-size:11px;background:transparent;border:0;}}')
    time.setMinimumWidth(80)
