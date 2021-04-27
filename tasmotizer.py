#!/usr/bin/env python
import re
import sys
from time import sleep

import serial

import tasmotizer_esptool as esptool
import json

from datetime import datetime

from PyQt5.QtCore import QUrl, Qt, QThread, QObject, pyqtSignal, pyqtSlot, QSettings, QTimer, QSize, QIODevice
from PyQt5.QtGui import QPixmap
from PyQt5.QtNetwork import QNetworkRequest, QNetworkAccessManager, QNetworkReply
from PyQt5.QtSerialPort import QSerialPortInfo, QSerialPort
from PyQt5.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton, QComboBox, QWidget, QCheckBox, QRadioButton, \
    QButtonGroup, QFileDialog, QProgressBar, QLabel, QMessageBox, QDialogButtonBox, QGroupBox, QFormLayout, QStatusBar

import banner
import firmwareURL

from gui import HLayout, VLayout, GroupBoxH, GroupBoxV, SpinBox, dark_palette
from utils import MODULES, NoBinFile, NetworkError

__version__ = '0.1'

class ESPWorker(QObject):
    error = pyqtSignal(Exception)
    waiting = pyqtSignal()
    done = pyqtSignal()

    def __init__(self, port, actions, **params):
        super().__init__()
        self.command = [
                      '--chip', 'esp32',
                      '--port', port,
                      '--baud', '921600'
            ]

        self._actions = actions
        self._params = params
        self._continue = False

    @pyqtSlot()
    def run(self):
        esptool.sw.setContinueFlag(True)

        try:
            if 'backup' in self._actions:
                command_backup = ['read_flash', '0x00000', self._params['backup_size'],
                                  'backup_{}.bin'.format(datetime.now().strftime('%Y%m%d_%H%M%S'))]
                esptool.main(self.command + command_backup)

                auto_reset = self._params['auto_reset']
                if not auto_reset:
                    self.wait_for_user()

            if esptool.sw.continueFlag() and 'write' in self._actions:
                file_path = self._params['file_path']
                # command_write = ['write_flash', '0x10000', file_path]
                print(file_path)
                command_write = ['--before','default_reset','--after','hard_reset','write_flash','-z','--flash_mode','dio','--flash_freq','80m','--flash_size','detect','0xe000','boot_app0.bin','0x1000','bootloader_dio_80m.bin','0x10000',file_path ,'0x8000','cansat-firmware.ino.partitions.bin']
                if 'erase' in self._actions:
                    command_write.append('--erase-all')
                # print(self.command + command_write)    
                esptool.main(self.command + command_write)

        except (esptool.FatalError, serial.SerialException) as e:
            self.error.emit(e)
        self.done.emit()

    def wait_for_user(self):
        self._continue = False
        self.waiting.emit()
        while not self._continue:
            sleep(.1)

    def continue_ok(self):
        self._continue = True

    def abort(self):
        esptool.sw.setContinueFlag(False)

class ProcessDialog(QDialog):
    def __init__(self, port, **kwargs):
        super().__init__()

        self.setWindowTitle('Preparando seu Satélite Educacional...')
        self.setFixedWidth(600)

        self.exception = None

        esptool.sw.progress.connect(self.update_progress)

        self.nam = QNetworkAccessManager()
        self.nrBinFile = QNetworkRequest()
        self.bin_data = b''

        self.setLayout(VLayout(5, 5))
        self.actions_layout = QFormLayout()
        self.actions_layout.setSpacing(5)

        self.layout().addLayout(self.actions_layout)

        self._actions = []
        self._action_widgets = {}

        self.port = port

        self.auto_reset = kwargs.get('auto_reset', False)

        self.file_path = kwargs.get('file_path')
        if self.file_path and self.file_path.startswith('http'):
            self._actions.append('download')

        self.backup = kwargs.get('backup')
        if self.backup:
            self._actions.append('backup')
            self.backup_size = kwargs.get('backup_size')

        self.erase = kwargs.get('erase')
        if self.erase:
            self._actions.append('erase')

        if self.file_path:
            self._actions.append('write')

        self.create_ui()
        self.start_process()

    def create_ui(self):
        for action in self._actions:
            pb = QProgressBar()
            pb.setFixedHeight(35)
            self._action_widgets[action] = pb
            self.actions_layout.addRow(action.capitalize(), pb)

        self.btns = QDialogButtonBox(QDialogButtonBox.Abort)
        self.btns.rejected.connect(self.abort)
        self.layout().addWidget(self.btns)

        self.sb = QStatusBar()
        self.layout().addWidget(self.sb)

    def appendBinFile(self):
        self.bin_data += self.bin_reply.readAll()

    def saveBinFile(self):
        if self.bin_reply.error() == QNetworkReply.NoError:
            self.file_path = self.file_path.split('/')[-1]
            with open(self.file_path, 'wb') as f:
                f.write(self.bin_data)
            self.run_esp() 
        else:
            raise NetworkError

    def updateBinProgress(self, recv, total):
        self._action_widgets['download'].setValue(recv//total*100)

    def download_bin(self):
        self.nrBinFile.setAttribute(QNetworkRequest.FollowRedirectsAttribute, True)
        self.nrBinFile.setUrl(QUrl(self.file_path))
        self.bin_reply = self.nam.get(self.nrBinFile)
        self.bin_reply.readyRead.connect(self.appendBinFile)
        self.bin_reply.downloadProgress.connect(self.updateBinProgress)
        self.bin_reply.finished.connect(self.saveBinFile)

    def show_connection_state(self, state):
        self.sb.showMessage(state, 0)

    def run_esp(self):
        params = {
            'file_path': self.file_path,
            'auto_reset': self.auto_reset,
            'erase': self.erase
        }

        if self.backup:
            backup_size = f'0x{2 ** self.backup_size}00000'
            params['backup_size'] = backup_size

        self.esp_thread = QThread()
        self.esp = ESPWorker(
            self.port,
            self._actions,
            **params
        )
        esptool.sw.connection_state.connect(self.show_connection_state)
        self.esp.waiting.connect(self.wait_for_user)
        self.esp.done.connect(self.accept)
        self.esp.error.connect(self.error)
        self.esp.moveToThread(self.esp_thread)
        self.esp_thread.started.connect(self.esp.run)
        self.esp_thread.start()

    def start_process(self):
        if 'download' in self._actions:
            self.download_bin()
            self._actions = self._actions[1:]
        else:
            self.run_esp()

    def update_progress(self, action, value):
        self._action_widgets[action].setValue(value)

    @pyqtSlot()
    def wait_for_user(self):
        dlg = QMessageBox.information(self,
                                      'User action required',
                                      'Please power cycle the device, wait a moment and press OK',
                                      QMessageBox.Ok | QMessageBox.Cancel)
        if dlg == QMessageBox.Ok:
            self.esp.continue_ok()
        elif dlg == QMessageBox.Cancel:
            self.esp.abort()
            self.esp.continue_ok()
            self.abort()

    def stop_thread(self):
        self.esp_thread.wait(2000)
        self.esp_thread.exit()

    def accept(self):
        self.stop_thread()
        self.done(QDialog.Accepted)

    def abort(self):
        self.sb.showMessage('Aborting...', 0)
        QApplication.processEvents()
        self.esp.abort()
        self.stop_thread()
        self.reject()

    def error(self, e):
        self.exception = e
        self.abort()

    def closeEvent(self, e):
        self.stop_thread()

class Tasmotizer(QDialog):

    def __init__(self):
        super().__init__()
        self.settings = QSettings('tasmotizer.cfg', QSettings.IniFormat)

        self.port = ''

        self.nam = QNetworkAccessManager()

        self.esp_thread = None

        self.setWindowTitle(f'PION Kits Educacionais {__version__}')
        self.setMinimumWidth(480)

        self.mode = 0  # BIN file
        self.file_path = ''

        self.release_data = b''
        self.development_data = b''

        self.create_ui()

        self.refreshPorts()
        # self.getFeeds()

    def create_ui(self):
        vl = VLayout(5)
        self.setLayout(vl)

        # Banner
        banner = QLabel()
        banner.setPixmap(QPixmap(':/banner.png'))
        vl.addWidget(banner)

        # Port groupbox
        gbPort = GroupBoxH('Selecionar porta', 3)
        self.cbxPort = QComboBox()
        pbRefreshPorts = QPushButton('Atualizar')
        gbPort.addWidget(self.cbxPort)
        gbPort.addWidget(pbRefreshPorts)
        gbPort.layout().setStretch(0, 4)
        gbPort.layout().setStretch(1, 1)

        # Firmware groupbox
        gbFW = GroupBoxV('Select image', 3)

        hl_rb = HLayout(0)
        rbFile = QRadioButton('BIN file')
        self.rbRelease = QRadioButton('Release')
        self.rbRelease.setEnabled(False)
        self.rbDev = QRadioButton('Development')
        self.rbDev.setEnabled(False)

        self.rbgFW = QButtonGroup(gbFW)
        self.rbgFW.addButton(rbFile, 0)
        self.rbgFW.addButton(self.rbRelease, 1)
        self.rbgFW.addButton(self.rbDev, 2)

        hl_rb.addWidgets([rbFile, self.rbRelease, self.rbDev])
        gbFW.addLayout(hl_rb)

        self.wFile = QWidget()
        hl_file = HLayout(0)
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        self.file.setPlaceholderText('Click "Open" to select the image')
        pbFile = QPushButton('Open')
        hl_file.addWidgets([self.file, pbFile])
        self.wFile.setLayout(hl_file)

        self.cbHackboxBin = QComboBox()
        self.cbHackboxBin.setVisible(False)
        self.cbHackboxBin.setEnabled(False)

        self.cbSelfReset = QCheckBox('Self-resetting device (NodeMCU, Wemos)')
        self.cbSelfReset.setToolTip('Check if your device has self-resetting capabilities supported by esptool')

        gbBackup = GroupBoxV('Backup')
        self.cbBackup = QCheckBox('Save original firmware')
        self.cbBackup.setToolTip('Firmware backup is ESPECIALLY recommended when you flash a Sonoff, Tuya, Shelly etc. for the first time.\nWithout a backup you will not be able to restore the original functionality.')

        self.cbxBackupSize = QComboBox()
        self.cbxBackupSize.addItems([f'{2 ** s}MB' for s in range(5)])
        self.cbxBackupSize.setEnabled(False)

        hl_backup_size = HLayout(0)
        hl_backup_size.addWidgets([QLabel('Flash size:'), self.cbxBackupSize])
        hl_backup_size.setStretch(0, 3)
        hl_backup_size.setStretch(1, 1)

        gbBackup.addWidget(self.cbBackup)
        gbBackup.addLayout(hl_backup_size)

        self.cbErase = QCheckBox('Erase before flashing')
        self.cbErase.setToolTip('Erasing previous firmware ensures all flash regions are clean for Tasmota, which prevents many unexpected issues.\nIf unsure, leave enabled.')
        self.cbErase.setChecked(True)

        gbFW.addWidgets([self.wFile, self.cbHackboxBin, self.cbSelfReset, self.cbErase])

        # Buttons
        self.pbTasmotize = QPushButton('Gravar firmware!')
        self.pbTasmotize.setFixedHeight(50)
        self.pbTasmotize.setStyleSheet('background-color: #0D2556;')

        hl_btns = HLayout([50, 3, 50, 3])
        hl_btns.addWidgets([self.pbTasmotize])

        vl.addWidgets([gbPort])
        vl.addLayout(hl_btns)

        pbRefreshPorts.clicked.connect(self.refreshPorts)
        self.rbgFW.buttonClicked[int].connect(self.setBinMode)
        rbFile.setChecked(True)
        # pbFile.clicked.connect(self.openBinFile)

        self.cbBackup.toggled.connect(self.cbxBackupSize.setEnabled)

        self.pbTasmotize.clicked.connect(self.start_process)

    def refreshPorts(self):
        self.cbxPort.clear()
        ports = reversed(sorted(port.portName() for port in QSerialPortInfo.availablePorts()))
        for p in ports:
            port = QSerialPortInfo(p)
            self.cbxPort.addItem(port.portName(), port.systemLocation())

    def setBinMode(self, radio):
        self.mode = radio
        self.wFile.setVisible(self.mode == 0)
        self.cbHackboxBin.setVisible(self.mode > 0)

        if self.mode == 1:
            self.processReleaseInfo()
        elif self.mode == 2:
            self.processDevelopmentInfo()

    # def getFeeds(self):
        # self.release_reply = self.nam.get(self.nrRelease)
        # self.release_reply.readyRead.connect(self.appendReleaseInfo)
        #self.release_reply.finished.connect(lambda: self.rbRelease.setEnabled(True))

        # self.development_reply = self.nam.get(self.nrDevelopment)
        # self.development_reply.readyRead.connect(self.appendDevelopmentInfo)
        #self.development_reply.finished.connect(lambda: self.rbDev.setEnabled(True))

    def appendReleaseInfo(self):
        self.release_data += self.release_reply.readAll()

    def appendDevelopmentInfo(self):
        self.development_data += self.development_reply.readAll()

    def processReleaseInfo(self):
        self.fill_bin_combo(self.release_data, self.rbRelease)

    def processDevelopmentInfo(self):
        self.fill_bin_combo(self.development_data, self.rbDev)

    def fill_bin_combo(self, data, rb):
        try:
            reply = json.loads(str(data, 'utf8'))
            version, bins = list(reply.items())[0]
            version = version.replace('-', ' ').title()

            rb.setText(version)
            if len(bins) > 0:
                self.cbHackboxBin.clear()
                for img in bins:
                    img['filesize'] //= 1024
                    self.cbHackboxBin.addItem('{binary} [{filesize}kB]'.format(**img), '{otaurl}'.format(**img))
                self.cbHackboxBin.setEnabled(True)
        except json.JSONDecodeError as e:
            self.setBinMode(0)
            self.rbgFW.button(0).setChecked(True)
            QMessageBox.critical(self, 'Error', f'Cannot load bin data:\n{e.msg}')

    def start_process(self):
        try:
            if self.mode == 0:
                self.file_path = firmwareURL.URL
                #if len(self.file.text()) > 0:
                #    self.file_path = self.file.text()
                #    self.settings.setValue('bin_file', self.file_path)
                #else:
                #    raise NoBinFile

            elif self.mode in (1, 2):
                self.file_path = self.cbHackboxBin.currentData()

            process_dlg = ProcessDialog(
                self.cbxPort.currentData(),
                file_path=self.file_path,
                #backup=self.cbBackup.isChecked(),
                backup=False,
                backup_size=0,#self.cbxBackupSize.currentIndex(),
                erase=True,#self.cbErase.isChecked(),
                auto_reset=True#self.cbSelfReset.isChecked()
            )
            result = process_dlg.exec_()
            if result == QDialog.Accepted:
                message = 'Programado com Sucesso! \n\nSeu kit está sendo reiniciado, isso pode levar algum tempo.'
                QMessageBox.information(self, 'OK', message)
            elif result == QDialog.Rejected:
                if process_dlg.exception:
                    QMessageBox.critical(self, 'Error', str(process_dlg.exception))
                else:
                    QMessageBox.critical(self, 'Processo Cancelado', 'O processo foi cancelado pelo usuário')
            
        except NoBinFile:
            QMessageBox.critical(self, 'Image path missing', 'Select a binary to write, or select a different mode.')
        except NetworkError as e:
            QMessageBox.critical(self, 'Network error', e.message)


def main():
    app = QApplication(sys.argv)
    app.setAttribute(Qt.AA_DisableWindowContextHelpButton)
    app.setQuitOnLastWindowClosed(True)
    app.setStyle('Fusion')

    app.setPalette(dark_palette)
    app.setStyleSheet('QToolTip { color: #ffffff; background-color: #2a82da; border: 1px solid white; }')
    app.setStyle('Fusion')

    mw = Tasmotizer()
    mw.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
