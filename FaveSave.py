from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import html
import json
from json import load as json_load
from os import listdir, makedirs, path
import os
import re
import sys
import threading
import time

from PyQt6.QtCore import QCoreApplication, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
import yt_dlp
from yt_dlp.utils import DownloadCancelled


URL_PATTERN = re.compile(r"(https?://[^\s<>\"]+)")
TIKTOK_PROFILE_RE = re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@([^/?#\s]+)", re.IGNORECASE)


def make_links_clickable(message):
    message = str(message)
    parts = []
    last_index = 0
    for match in URL_PATTERN.finditer(message):
        parts.append(html.escape(message[last_index:match.start()]))
        url = match.group(0)
        if 'tiktok' in url.lower():
            safe_url = html.escape(url, quote=True)
            parts.append(f"<a href=\"{safe_url}\">{safe_url}</a>")
        else:
            parts.append(html.escape(url))
        last_index = match.end()
    parts.append(html.escape(message[last_index:]))
    return ''.join(parts)


# Determine the path to the logo based on whether the app is bundled
if hasattr(sys, '_MEIPASS'):
    logo_path = path.join(sys._MEIPASS, 'img', 'logo.png')
else:
    logo_path = path.join(path.dirname(__file__), 'img', 'logo.png')


def normalize_profile_url(profile_input):
    """Accept @username, username, tiktok.com/@username, or a full profile URL."""
    value = (profile_input or '').strip()
    if not value:
        raise ValueError("Enter a TikTok username or profile URL.")

    if value.startswith('@'):
        username = value[1:].strip()
        if not username:
            raise ValueError("Enter a TikTok username after @.")
        return f"https://www.tiktok.com/@{username}", username

    match = TIKTOK_PROFILE_RE.search(value)
    if match:
        username = match.group(1)
        return f"https://www.tiktok.com/@{username}", username

    if value.startswith('http://') or value.startswith('https://'):
        # Let yt-dlp try unusual TikTok URLs, but use a generic filename prefix.
        return value, "profile"

    username = value.lstrip('@')
    return f"https://www.tiktok.com/@{username}", username


def safe_prefix_part(value):
    value = (value or 'profile').lstrip('@')
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('_') or 'profile'


def extract_video_id(video_url):
    clean = video_url.split('?')[0].rstrip('/')
    parts = [part for part in clean.split('/') if part]
    if 'video' in parts:
        index = parts.index('video')
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else video_url


def get_downloaded_videos(download_folder):
    downloaded_videos = set()
    makedirs(download_folder, exist_ok=True)
    for file_name in listdir(download_folder):
        downloaded_videos.add(file_name)
    return downloaded_videos


def is_video_downloaded(video_url, downloaded_videos):
    video_id = extract_video_id(video_url)
    return any(
        file.endswith(f"{video_id}.mp4")
        or file.endswith(f"{video_id}.m4a")
        or file.endswith(f"{video_id}.mp3")
        or file.endswith(f"{video_id}.webm")
        for file in downloaded_videos
    )


def extract_profile_video_links(profile_url, log_callback, stop_event=None):
    """Use yt-dlp to enumerate videos from a TikTok profile page."""
    links = []
    seen = set()

    ydl_opts = {
        'quiet': True,
        'ignoreerrors': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
    }

    log_callback(f"🔎 Fetching profile video list from: {profile_url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(profile_url, download=False)

    if not info:
        return links

    entries = info.get('entries')
    if not entries:
        webpage_url = info.get('webpage_url') or info.get('original_url') or profile_url
        return [webpage_url]

    for entry in entries:
        if stop_event and stop_event.is_set():
            break
        if not entry:
            continue

        candidate = entry.get('webpage_url') or entry.get('url')
        if not candidate:
            continue

        if not str(candidate).startswith('http'):
            video_id = str(candidate).strip('/ ')
            if video_id:
                candidate = f"{profile_url.rstrip('/')}/video/{video_id}"

        if candidate and candidate not in seen:
            seen.add(candidate)
            links.append(candidate)

    return links


def download_video(video_url, download_folder, prefix, stop_event=None):
    if stop_event and stop_event.is_set():
        raise DownloadCancelled('Download cancelled before start')

    def _progress_hook(_):
        if stop_event and stop_event.is_set():
            raise DownloadCancelled('Download cancelled by user')

    ydl_opts = {
        'outtmpl': path.join(download_folder, f"{prefix}%(id)s.%(ext)s"),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'merge_output_format': 'mp4',
        'ignoreerrors': False,
        'progress_hooks': [_progress_hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


def process_profile_videos(profile_input, download_folder, log_callback, progress_callback,
                           detailed_progress_callback, stop_event=None, max_concurrent_downloads=3):
    stop_event = stop_event or threading.Event()

    try:
        profile_url, username = normalize_profile_url(profile_input)
    except ValueError as exc:
        log_callback(f"❌ {exc}")
        return 0, 0, 0, []

    prefix = f"profile_{safe_prefix_part(username)}_"

    try:
        video_links = extract_profile_video_links(profile_url, log_callback, stop_event=stop_event)
    except Exception as exc:
        log_callback(f"❌ Could not fetch TikTok profile videos: {exc}")
        log_callback("💡 Try updating yt-dlp, checking the username, or using a logged-in browser cookies workflow outside this app if TikTok blocks access.")
        return 0, 0, 0, []

    total_videos = len(video_links)
    if total_videos == 0:
        log_callback("No public videos found for this profile, or TikTok blocked the profile listing request.")
        return 0, 0, 0, []

    try:
        downloaded_videos = get_downloaded_videos(download_folder)
        log_callback(f"📁 Download folder: {download_folder}")
        log_callback(f"📊 Found {total_videos:,} profile videos and {len(downloaded_videos):,} existing files")
    except Exception as exc:
        log_callback(f"❌ Error accessing download folder: {exc}")
        return total_videos, 0, total_videos, video_links

    downloaded_count = 0
    failed_count = 0
    processed_count = 0
    active_futures = {}
    pending_tasks = []
    start_time = time.time()
    stall_reported = False
    STALL_THRESHOLD = 60

    def emit_progress(context):
        elapsed_time = time.time() - start_time
        detailed_progress_callback({
            'current_video': min(context['index'], total_videos),
            'total_videos': total_videos,
            'current_url': context['url'],
            'video_id': extract_video_id(context['url']),
            'elapsed_time': elapsed_time,
            'downloaded_count': downloaded_count,
            'failed_count': failed_count,
        })

    def update_progress_bar():
        progress_callback(int((processed_count / total_videos) * 100) if total_videos else 0)

    def check_for_stall():
        nonlocal stall_reported
        if not active_futures:
            stall_reported = False
            return
        current_time = time.time()
        stalled = any(
            info.get('start_time') is not None and current_time - info['start_time'] > STALL_THRESHOLD
            for info in active_futures.values()
        )
        if stalled and not stall_reported:
            stall_reported = True
            log_callback(f"⚠️ Download appears stalled (>{STALL_THRESHOLD}s)")
        elif not stalled and stall_reported:
            stall_reported = False
            log_callback("✅ Download resumed...")

    def download_task(url):
        try:
            download_video(url, download_folder, prefix, stop_event=stop_event)
            return {'status': 'downloaded'}
        except DownloadCancelled:
            return {'status': 'cancelled'}
        except Exception as exc:
            return {'status': 'error', 'error': str(exc)}

    def harvest_futures(block):
        nonlocal downloaded_count, failed_count, processed_count
        if not active_futures:
            return False
        done, _ = wait(
            list(active_futures.keys()),
            timeout=None if block else 0,
            return_when=FIRST_COMPLETED,
        )
        if not done:
            return False

        for future in done:
            context = active_futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                result = {'status': 'error', 'error': str(exc)}

            status = result.get('status')
            if status == 'downloaded':
                downloaded_count += 1
                log_callback(f"✅ Downloaded: {context['url']}")
            elif status == 'cancelled':
                log_callback(f"🛑 Cancelled: {context['url']}")
            else:
                failed_count += 1
                log_callback(f"❌ Failed to download {context['url']} : {result.get('error', 'Unknown error')}")

            processed_count += 1
            emit_progress(context)
            update_progress_bar()
            check_for_stall()
        return True

    for index, url in enumerate(video_links, start=1):
        if stop_event.is_set():
            break
        context = {'index': index, 'url': url}
        if is_video_downloaded(url, downloaded_videos):
            log_callback(f"🎥 Processing Video {index} of {total_videos}")
            log_callback(f"Already downloaded: {url}")
            downloaded_count += 1
            processed_count += 1
            emit_progress(context)
            update_progress_bar()
            QCoreApplication.processEvents()
        else:
            pending_tasks.append(context)

    with ThreadPoolExecutor(max_workers=max_concurrent_downloads) as executor:
        for context in pending_tasks:
            if stop_event.is_set():
                log_callback("Cancellation requested - stopping new downloads")
                break

            emit_progress(context)
            log_callback(f"🎥 Processing Video {context['index']} of {total_videos}")
            log_callback(f"Downloading: {context['url']}")

            while len(active_futures) >= max_concurrent_downloads and not stop_event.is_set():
                harvest_futures(block=True)
                check_for_stall()

            if stop_event.is_set():
                break

            future = executor.submit(download_task, context['url'])
            context['start_time'] = time.time()
            active_futures[future] = context

            while harvest_futures(block=False):
                check_for_stall()

        if stop_event.is_set():
            for future in list(active_futures.keys()):
                future.cancel()

        while active_futures:
            harvest_futures(block=True)
            check_for_stall()

    update_progress_bar()
    return total_videos, downloaded_count, failed_count, video_links


class ProfileDownloadWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)
    detailed_progress_signal = pyqtSignal(dict)

    def __init__(self, profile_input, download_folder):
        super().__init__()
        self.profile_input = profile_input
        self.download_folder = download_folder
        self.total_videos = 0
        self.downloaded_videos = 0
        self.failed_videos_count = 0
        self.video_links = []
        self.stop_event = threading.Event()
        self.max_concurrent_downloads = 3

    def run(self):
        self.stop_event.clear()
        results = process_profile_videos(
            self.profile_input,
            self.download_folder,
            self.log_signal.emit,
            self.progress_signal.emit,
            self.detailed_progress_signal.emit,
            stop_event=self.stop_event,
            max_concurrent_downloads=self.max_concurrent_downloads,
        )
        self.total_videos, self.downloaded_videos, self.failed_videos_count, self.video_links = results

    def request_cancel(self):
        self.stop_event.set()


class VideoDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FaveSave - TikTok Profile Video Downloader")
        self.setGeometry(100, 100, 650, 720)

        self.download_folder = ""
        self.worker = None
        self.is_downloading = False
        self.was_cancelled = False
        self.watchdog_timer = None
        self.last_heartbeat = 0
        self.watchdog_timeout = 30
        self.max_hang_duration = 120

        self.init_ui()
        self.init_watchdog()
        self.load_settings()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        if path.exists(logo_path):
            logo_label = QLabel()
            logo_label.setPixmap(QPixmap(logo_path))
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(logo_label)

        title_label = QLabel("Download TikTok Videos From a Specific User")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(title_label)

        profile_label = QLabel("👤 TikTok username or profile URL:")
        layout.addWidget(profile_label)

        self.profile_input = QLineEdit()
        self.profile_input.setPlaceholderText("@username or https://www.tiktok.com/@username")
        self.profile_input.textChanged.connect(self.save_settings)
        layout.addWidget(self.profile_input)

        help_label = QLabel("💡 This uses yt-dlp to read the public profile page. Private videos, login-only videos, or TikTok anti-bot blocks may not be accessible.")
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(help_label)

        self.output_folder_label = QLabel("📂 Set Download Folder (where you want to save videos):")
        layout.addWidget(self.output_folder_label)

        self.output_folder_button = QPushButton("Click here to choose a download folder")
        self.output_folder_button.clicked.connect(self.set_output_folder)
        self.output_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.output_folder_button)

        advanced_label = QLabel("⚙️ Advanced Settings:")
        advanced_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(advanced_label)

        concurrent_layout = QHBoxLayout()
        concurrent_label = QLabel("🔄 Max Concurrent Downloads:")
        concurrent_label.setStyleSheet("font-size: 12px;")
        concurrent_layout.addWidget(concurrent_label)

        self.concurrent_downloads_spinner = QSpinBox()
        self.concurrent_downloads_spinner.setMinimum(1)
        self.concurrent_downloads_spinner.setMaximum(10)
        self.concurrent_downloads_spinner.setValue(1)
        self.concurrent_downloads_spinner.setToolTip("Number of videos to download simultaneously (1-10)")
        self.concurrent_downloads_spinner.valueChanged.connect(self.save_settings)
        concurrent_layout.addWidget(self.concurrent_downloads_spinner)
        concurrent_layout.addStretch()
        layout.addLayout(concurrent_layout)

        self.description = QTextBrowser()
        self.description.setPlaceholderText("Logs will appear here...")
        self.description.setReadOnly(True)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.description.setMinimumHeight(180)
        layout.addWidget(self.description)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid #ddd;
                border-radius: 8px;
                text-align: center;
                font-weight: bold;
                font-size: 14px;
                height: 25px;
                background-color: #f0f0f0;
                color: #333;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4CAF50, stop:1 #45a049);
                border-radius: 6px;
            }
        """)
        layout.addWidget(self.progress_bar)

        self.progress_info_label = QLabel("Enter a TikTok profile and choose a download folder")
        self.progress_info_label.setStyleSheet("""
            color: #333;
            font-size: 14px;
            font-weight: bold;
            padding: 8px;
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 6px;
        """)
        self.progress_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress_info_label)

        spacer = QSpacerItem(0, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        layout.addItem(spacer)

        self.start_button = QPushButton("Start Download")
        self.start_button.clicked.connect(self.start_download)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #45a049; }
        """)
        layout.addWidget(self.start_button)

        self.cancel_button = QPushButton("Cancel Download")
        self.cancel_button.clicked.connect(self.cancel_download)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setVisible(False)
        self.cancel_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover { background-color: #da190b; }
        """)
        layout.addWidget(self.cancel_button)

    def log_message(self, message):
        text = str(message)
        lines = text.splitlines() or ['']
        for line in lines:
            if 'http' in line.lower():
                formatted_message = make_links_clickable(line)
            else:
                formatted_message = html.escape(line)
            self.description.append(formatted_message if formatted_message else '&nbsp;')
        cursor = self.description.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.description.setTextCursor(cursor)
        self.description.ensureCursorVisible()
        self.update_heartbeat()

    def set_output_folder(self):
        default_dir = self.download_folder if self.download_folder and path.exists(self.download_folder) else path.abspath(".")
        folder = QFileDialog.getExistingDirectory(self, "Select Download Folder", default_dir)
        if folder:
            self.download_folder = folder
            self.output_folder_button.setText(folder)
            self.log_message(f"Download folder set to: {folder}")
            self.save_settings()

    def start_download(self):
        profile_input = self.profile_input.text().strip()
        if not profile_input:
            QMessageBox.warning(self, "Warning", "Please enter a TikTok username or profile URL first.")
            return

        if self.is_downloading:
            QMessageBox.warning(self, "Warning", "Download is already in progress. Please cancel the current download first.")
            return

        if not self.download_folder:
            self.download_folder = path.join(path.abspath("."), "downloaded_videos")
            self.output_folder_button.setText(self.download_folder)
            self.log_message(f"🔧 Auto-set download folder to: {self.download_folder}")
            self.save_settings()

        try:
            makedirs(self.download_folder, exist_ok=True)
            from os import access, W_OK
            if not access(self.download_folder, W_OK):
                QMessageBox.warning(self, "Warning", f"Cannot write to download folder: {self.download_folder}\nPlease choose a different folder.")
                return
        except Exception as exc:
            QMessageBox.warning(self, "Warning", f"Error validating download folder: {exc}\nPlease choose a different folder.")
            return

        self.log_message(f"Selected TikTok Profile: {profile_input}")
        self.log_message(f"Selected Download Folder: {self.download_folder}")
        self.progress_bar.setValue(0)
        self.progress_info_label.setText("🚀 Fetching profile videos...")
        self.was_cancelled = False
        self.update_download_ui_state(True)

        self.worker = ProfileDownloadWorker(profile_input, self.download_folder)
        self.worker.max_concurrent_downloads = self.concurrent_downloads_spinner.value()
        self.worker.log_signal.connect(self.log_message)
        self.worker.progress_signal.connect(self.update_progress_bar)
        self.worker.detailed_progress_signal.connect(self.update_detailed_progress)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def on_worker_finished(self):
        if not self.worker:
            self.update_download_ui_state(False)
            return

        self.is_downloading = False
        cancel_requested = self.worker.stop_event.is_set() or self.was_cancelled

        if cancel_requested:
            self.log_message("❌ Download cancelled by user")
            self.progress_info_label.setText("⏸️ Download cancelled - click Resume to continue")
        else:
            total_processed = self.worker.downloaded_videos + self.worker.failed_videos_count
            success_rate = (self.worker.downloaded_videos / total_processed * 100) if total_processed > 0 else 0
            success_rate = min(success_rate, 100.0)

            self.log_message(f"🎉 Download completed! {self.worker.total_videos:,} total videos processed")
            self.log_message(f"✅ Successfully downloaded/skipped existing: {self.worker.downloaded_videos:,} videos")
            if self.worker.failed_videos_count > 0:
                self.log_message(f"❌ Failed Videos: {self.worker.failed_videos_count:,}")

            self.progress_info_label.setText(
                f"🎉 Download Complete! {self.worker.downloaded_videos:,} videos ready "
                f"({success_rate:.1f}% success rate)"
            )
            self.start_button.setText("🎉 Done! (Click to start new download)")
            self.was_cancelled = False

        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel Download")
        self.update_heartbeat()
        self.update_download_ui_state(False)
        self.worker = None

    def update_progress_bar(self, value):
        self.progress_bar.setValue(value)
        self.update_heartbeat()

    def update_download_ui_state(self, is_downloading):
        self.is_downloading = is_downloading
        if is_downloading:
            self.start_button.setVisible(False)
            self.cancel_button.setVisible(True)
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("Cancel Download")
            self.profile_input.setEnabled(False)
            self.output_folder_button.setEnabled(False)
            self.concurrent_downloads_spinner.setEnabled(False)
        else:
            self.start_button.setText("Resume Download" if self.was_cancelled else "Start Download")
            self.start_button.setVisible(True)
            self.cancel_button.setVisible(False)
            self.profile_input.setEnabled(True)
            self.output_folder_button.setEnabled(True)
            self.concurrent_downloads_spinner.setEnabled(True)

    def cancel_download(self):
        if self.worker and self.worker.isRunning():
            self.log_message("🛑 Cancelling download...")
            self.was_cancelled = True
            self.worker.request_cancel()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling...")
            self.progress_info_label.setText("⏸️ Cancelling downloads... Please wait...")
            self.update_heartbeat()

    def update_detailed_progress(self, progress_info):
        current = progress_info['current_video']
        total = progress_info['total_videos']
        downloaded = progress_info['downloaded_count']
        failed = progress_info['failed_count']
        elapsed = progress_info['elapsed_time']
        elapsed_minutes = int(elapsed // 60)
        elapsed_seconds = int(elapsed % 60)
        elapsed_str = f"{elapsed_minutes:02d}:{elapsed_seconds:02d}"
        self.progress_info_label.setText(
            f"📊 Progress: {current:,}/{total:,} videos | "
            f"✅ Ready: {downloaded:,} | ❌ Failed: {failed:,} | "
            f"⏱️ Elapsed: {elapsed_str}"
        )
        self.update_heartbeat()

    def init_watchdog(self):
        self.watchdog_timer = QTimer()
        self.watchdog_timer.timeout.connect(self.watchdog_check)
        self.watchdog_timer.start(5000)
        self.update_heartbeat()

    def update_heartbeat(self):
        self.last_heartbeat = time.time()

    def watchdog_check(self):
        if not self.is_downloading or self.was_cancelled:
            return
        if time.time() - self.last_heartbeat > self.watchdog_timeout:
            self.handle_unresponsive_app()

    def handle_unresponsive_app(self):
        hang_duration = time.time() - self.last_heartbeat
        if hang_duration > self.max_hang_duration:
            self.log_message("⚠️ App may be waiting on TikTok or yt-dlp. You can cancel if it does not recover.")
            self.update_heartbeat()

    def get_settings_file_path(self):
        settings_dir = os.path.expanduser("~/.favesave")
        os.makedirs(settings_dir, exist_ok=True)
        return os.path.join(settings_dir, "settings.json")

    def load_settings(self):
        try:
            settings_file = self.get_settings_file_path()
            if os.path.exists(settings_file):
                with open(settings_file, 'r', encoding='utf-8') as f:
                    settings = json_load(f)
                profile_input = settings.get('profile_input') or ''
                if profile_input:
                    self.profile_input.setText(profile_input)
                download_folder = settings.get('download_folder') or ''
                if download_folder:
                    self.download_folder = download_folder
                    self.output_folder_button.setText(download_folder)
                if 'concurrent_downloads' in settings:
                    self.concurrent_downloads_spinner.setValue(settings['concurrent_downloads'])
                self.log_message("⚙️ Settings restored from previous session")
        except Exception as exc:
            self.log_message(f"⚠️ Could not load settings: {exc}")

    def save_settings(self):
        try:
            settings = {
                'profile_input': self.profile_input.text().strip() if hasattr(self, 'profile_input') else '',
                'download_folder': self.download_folder if self.download_folder else '',
                'concurrent_downloads': self.concurrent_downloads_spinner.value() if hasattr(self, 'concurrent_downloads_spinner') else 1,
            }
            settings_file = self.get_settings_file_path()
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            if hasattr(self, 'description'):
                self.log_message(f"⚠️ Could not save settings: {exc}")

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.log_message("👋 Exiting - cancelling active downloads...")
            self.worker.request_cancel()
            self.worker.wait()
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoDownloaderApp()
    window.show()
    sys.exit(app.exec())
