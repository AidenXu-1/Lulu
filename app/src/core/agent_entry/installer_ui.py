"""One distribution install action. Runs from a copied, signed staging App."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

from .delivery_package import _read


def independent_dmg_hash(dmg):
    dmg=Path(dmg);sidecar=dmg.with_name(dmg.name+'.sha256')
    try:
        raw,_meta=_read(sidecar)
    except FileNotFoundError as exc:
        raise RuntimeError('PACKAGE_INDEPENDENT_HASH_REQUIRED') from exc
    expected=re.fullmatch(rb'([0-9a-f]{64})  '+re.escape(os.fsencode(dmg.name))+rb'\n',raw)
    if expected is None:raise RuntimeError('PACKAGE_INDEPENDENT_HASH_REQUIRED')
    return expected.group(1).decode(),{'path':str(sidecar),'sha256':hashlib.sha256(raw).hexdigest()}


def source_volume_dmg(volume):
    volume=Path(volume).resolve(strict=True)
    result=subprocess.run(['/usr/bin/hdiutil','info','-plist'],capture_output=True,check=True)
    images=plistlib.loads(result.stdout).get('images',[])
    matches=[]
    for info in images:
        for entity in info.get('system-entities',[]):
            mount=entity.get('mount-point')
            if mount and Path(mount).resolve()==volume:
                matches.append(Path(info['image-path']))
    if len(matches)!=1 or not matches[0].is_absolute():raise RuntimeError('PACKAGE_SOURCE_VOLUME_REQUIRED')
    return matches[0]


def create_install_dialog(worker):
    from PySide6.QtWidgets import QDialog
    class InstallDialog(QDialog):
        def reject(self):
            if not worker.isRunning():super().reject()
        def closeEvent(self,event):
            if worker.isRunning():event.ignore()
            else:super().closeEvent(event)
    return InstallDialog()


def run_installer(source_volume):
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QLabel,QPushButton
    from .product_install import build_default_product_installer
    from .transaction import _replace_json
    import sys

    class Worker(QThread):
        finished_install=Signal(bool,str)
        def run(self):
            try:
                app=Path(sys.executable).parent.parent.parent
                dmg=source_volume_dmg(source_volume)
                expected,provenance=independent_dmg_hash(dmg)
                # This process runs from the copied App, so normal source-volume
                # detach cannot be held busy by its executable/runtime mappings.
                detached=subprocess.run(['/usr/bin/hdiutil','detach',str(Path(source_volume).resolve())],capture_output=True)
                if detached.returncode:raise RuntimeError('PACKAGE_DETACH_FAILED')
                installer=build_default_product_installer()
                state=installer.paths.state_root
                installer.recover_pending()
                parent=Path(tempfile.mkdtemp(prefix='lulu-product-install-')).resolve()
                transaction_id='product-'+parent.name+'-'+os.urandom(4).hex()
                installer.install_from_dmg(dmg,source_app=app,expected_sha256=expected,
                    stage_root=parent/'verified',transaction_id=transaction_id)
                _replace_json(state/'product-transactions'/transaction_id/'independent-hash-source.json',provenance)
                self.finished_install.emit(True,'安装完成，正在打开 Lulu。')
            except Exception as exc:
                messages={
                    'PACKAGE_INDEPENDENT_HASH_REQUIRED':'缺少或无法匹配配套校验文件。请将原名 DMG 和同名 .dmg.sha256 文件放在同一文件夹，再重新打开安装包。',
                    'PACKAGE_HASH_MISMATCH':'安装包校验未通过，请重新取得完整安装包。',
                    'PACKAGE_DETACH_FAILED':'安装盘暂时无法退出，请关闭安装盘中其他正在使用的文件后重试。',
                    'PRODUCT_CLIENTS_STILL_RUNNING':'Lulu 尚未退出，请退出后重新安装。',
                    'PRODUCT_UNFINISHED_TASKS':'还有未结束的转录任务，请先完成或取消，再安装新版。',
                    'PRODUCT_TASKS_CHANGED':'任务在交接期间发生了变化，本次升级已停止，原资料已保留。',
                    'PRODUCT_OLD_TASK_SERVICE_UNAVAILABLE':'暂时无法读取旧任务。请打开原版 Lulu，确认任务显示正常后再安装。',
                    'PRODUCT_TASK_HANDOVER_INVALID':'旧任务交接检查未通过，原资料已保留。',
                    'AGENT_ENTRY_OWNERSHIP_CONFLICT':'现有入口与安装记录不一致，已保留原文件并停止安装。',
                }
                self.finished_install.emit(False,messages.get(str(exc),'安装未完成，原文件与恢复资料已保留。错误：'+str(exc)))

    application=QApplication.instance() or QApplication([])
    worker=Worker()
    window=create_install_dialog(worker);window.setWindowTitle('安装 Lulu');window.resize(430,200)
    layout=QVBoxLayout(window)
    label=QLabel('将安装 Lulu，并保留现有内容和旧版备份。\n安装过程中会退出正在运行的 Lulu。');label.setWordWrap(True)
    button=QPushButton('安装 Lulu');layout.addWidget(label);layout.addWidget(button)
    worker.setParent(window)
    def begin():
        button.setEnabled(False);label.setText('正在校验并安装，请稍候…');worker.start()
    def finished(ok,message):
        label.setText(message)
        if ok:
            subprocess.Popen(['/usr/bin/open','/Applications/Lulu.app'],start_new_session=True)
            button.setText('完成');button.setEnabled(True)
            button.clicked.disconnect();button.clicked.connect(window.accept)
        else:
            button.setText('关闭');button.setEnabled(True)
            button.clicked.disconnect();button.clicked.connect(window.reject)
    # A live installation worker must outlive the dialog.
    button.clicked.connect(begin);worker.finished_install.connect(finished)
    window.exec();worker.wait()
    return 0
