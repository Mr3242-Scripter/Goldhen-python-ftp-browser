import ftplib
import ipaddress
import threading
import time
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

PORT = 2121
TIMEOUT = 15
BLOCK_SIZE = 1024 * 1024
RETRY_DELAY = 5

MONITOR_INTERVAL = 3
MONITOR_RECONNECT_DELAY = 5

PKG_DIRECTORY = "/data/pkg/"

# ============================================================
# GLOBAL STATE
# ============================================================

shutdown_event = threading.Event()
monitor_thread = None
monitor_lock = threading.Lock()

monitor_status = {
    "connected": False,
    "state": "Not started",
    "last_error": "",
    "last_check": "Never",
}

upload_lock = threading.Lock()
upload_cancel = threading.Event()
upload_thread = None

upload_status = {
    "active": False,
    "state": "Idle",
    "progress": 0.0,
    "attempts": 0,
    "message": "No active upload",
}


# ============================================================
# IP ADDRESS
# ============================================================

def ask_ip():
    # No network checks or background threads run before this returns.
    while True:
        address = input("Enter your PS4 IP address: ").strip()
        try:
            return str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            print("Invalid IPv4 address.")


# ============================================================
# FTP CONNECTION HELPERS
# ============================================================

def connect(ip):
    ftp = ftplib.FTP()
    try:
        ftp.connect(ip, PORT, timeout=TIMEOUT)
        ftp.login()
        ftp.set_pasv(True)
        if ftp.sock:
            ftp.sock.settimeout(TIMEOUT)
        return ftp
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise


def close_ftp(ftp):
    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def ensure_browser_connection(ip, ftp):
    if ftp is not None:
        try:
            ftp.voidcmd("NOOP")
            return ftp
        except Exception:
            close_ftp(ftp)

    print(f"\nConnecting to {ip}:{PORT}...")
    try:
        ftp = connect(ip)
        print("Connected to GoldHEN FTP.")
        return ftp
    except Exception as error:
        print("PS4 is currently unreachable:", error)
        return None


# ============================================================
# INDEPENDENT CONNECTION MONITOR
# ============================================================

def set_monitor_status(connected, state, error=""):
    with monitor_lock:
        monitor_status["connected"] = connected
        monitor_status["state"] = state
        monitor_status["last_error"] = error
        monitor_status["last_check"] = time.strftime("%H:%M:%S")


def connection_monitor(ip):
    """
    Own FTP control session. It does not share the browser or upload
    FTP objects, because ftplib connections are not thread-safe.
    """
    ftp = None

    while not shutdown_event.is_set():
        try:
            if ftp is None:
                set_monitor_status(False, "Disconnected - reconnecting")
                ftp = connect(ip)
                ftp.voidcmd("NOOP")
                set_monitor_status(True, "Connected")
            else:
                ftp.voidcmd("NOOP")
                set_monitor_status(True, "Connected")

            shutdown_event.wait(MONITOR_INTERVAL)

        except Exception as error:
            close_ftp(ftp)
            ftp = None
            set_monitor_status(
                False,
                "Disconnected - retrying",
                str(error),
            )
            shutdown_event.wait(MONITOR_RECONNECT_DELAY)

    close_ftp(ftp)
    set_monitor_status(False, "Stopped")


def start_connection_monitor(ip):
    global monitor_thread
    shutdown_event.clear()
    monitor_thread = threading.Thread(
        target=connection_monitor,
        args=(ip,),
        name="GoldHEN-Connection-Monitor",
        daemon=True,
    )
    monitor_thread.start()


def show_connection_status():
    with monitor_lock:
        status = monitor_status.copy()

    print("\n========== PS4 CONNECTION ==========")
    print("Status:", status["state"])
    print("Connected:", status["connected"])
    print("Last check:", status["last_check"])
    if status["last_error"]:
        print("Last error:", status["last_error"])
    print("====================================")


# ============================================================
# UPLOAD STATUS
# ============================================================

def update_status(
    state=None,
    progress=None,
    attempts=None,
    message=None,
    active=None,
):
    with upload_lock:
        if state is not None:
            upload_status["state"] = state
        if progress is not None:
            upload_status["progress"] = progress
        if attempts is not None:
            upload_status["attempts"] = attempts
        if message is not None:
            upload_status["message"] = message
        if active is not None:
            upload_status["active"] = active


def get_upload_state():
    with upload_lock:
        return upload_status.copy()


def show_upload_status():
    status = get_upload_state()
    print("\n========== UPLOAD STATUS ==========")
    print("Active:", status["active"])
    print("State:", status["state"])
    print(f"Progress sent: {status['progress']:.2f}%")
    print("Connection attempts:", status["attempts"])
    print("Message:", status["message"])
    print("===================================")


# ============================================================
# DIRECTORY NAVIGATION
# ============================================================

def list_directory(ftp):
    print("\nContents of:", ftp.pwd())
    ftp.retrlines("LIST")


def open_directory(ftp):
    folder = input("Folder name: ").strip()
    if folder:
        ftp.cwd(folder)
        print("Current directory:", ftp.pwd())


def parent_directory(ftp):
    ftp.cwd("..")
    print("Current directory:", ftp.pwd())


def create_directory(ftp):
    folder = input("New folder name: ").strip()
    if (
        not folder
        or folder in (".", "..")
        or "/" in folder
        or "\\" in folder
    ):
        print("Invalid folder name.")
        return
    ftp.mkd(folder)
    print("Folder created:", folder)


# ============================================================
# DIRECTORY ENTRIES / NUMBERED DELETION
# ============================================================

def get_directory_entries(ftp, directory):
    original_directory = ftp.pwd()
    entries = []

    try:
        ftp.cwd(directory)
        lines = []
        ftp.retrlines("LIST", lines.append)

        for line in lines:
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue

            permissions = parts[0]
            name = parts[8]

            if name in (".", ".."):
                continue

            entries.append({
                "name": name,
                "is_directory": permissions.startswith("d"),
            })
    finally:
        ftp.cwd(original_directory)

    return sorted(entries, key=lambda item: item["name"].lower())


def delete_pkg_file(ftp):
    print("\n========== DELETE FROM /data/pkg/ ==========")

    entries = get_directory_entries(ftp, PKG_DIRECTORY)
    files = [entry for entry in entries if not entry["is_directory"]]

    if not files:
        print("No files found.")
        return

    for index, entry in enumerate(files, start=1):
        print(f"{index}. {entry['name']}")

    print("\n0. Cancel")
    choice = input("\nEnter file number: ").strip()

    if choice == "0":
        return

    try:
        number = int(choice)
    except ValueError:
        print("Invalid number.")
        return

    if not 1 <= number <= len(files):
        print("Number out of range.")
        return

    filename = files[number - 1]["name"]
    print("\nSelected:", filename)

    if input("Type DELETE to confirm: ").strip() != "DELETE":
        print("Deletion cancelled.")
        return

    original_directory = ftp.pwd()
    try:
        ftp.cwd(PKG_DIRECTORY)
        ftp.delete(filename)
        print("Deleted:", filename)
    finally:
        ftp.cwd(original_directory)


# ============================================================
# REMOTE SIZE HELPERS
# ============================================================

def get_remote_size(ftp, filename):
    try:
        return ftp.size(filename)
    except ftplib.error_perm as error:
        response = str(error)
        if response.startswith(("500", "501", "502", "504")):
            return "UNSUPPORTED"
        if response.startswith("550"):
            return None
        raise


def get_size_from_listing(ftp, filename):
    lines = []
    ftp.retrlines("LIST", lines.append)

    for line in lines:
        parts = line.split(maxsplit=8)
        if len(parts) < 9 or parts[8] != filename:
            continue
        try:
            return int(parts[4])
        except (ValueError, IndexError):
            return None

    return None


def remove_remote_file_if_present(ftp, filename):
    try:
        ftp.delete(filename)
    except ftplib.all_errors:
        pass


# ============================================================
# BACKGROUND UPLOAD
# ============================================================

def background_upload_single(ip, local_path, remote_directory):
    file_path = Path(local_path)
    filename = file_path.name
    temporary_name = "." + filename + ".uploading"
    file_size = file_path.stat().st_size

    attempt = 0
    restart_after_rest_error = False
    clean_restart_used = False

    try:
        while not upload_cancel.is_set():
            ftp = None
            attempt += 1

            update_status(
                state="Connecting",
                attempts=attempt,
                message=f"Connecting to {ip}:{PORT}",
            )
            print(f"\n[UPLOAD] Connection attempt {attempt}...")

            try:
                ftp = connect(ip)
                ftp.cwd(remote_directory)

                if upload_cancel.is_set():
                    break

                # GoldHEN's SIZE reply can wrap at 4 GiB. For large files,
                # LIST is the authoritative size source; never use SIZE's
                # modulo-2^32 result as a resume offset.
                offset = 0
                if file_size >= (1 << 32):
                    remote_size = get_size_from_listing(ftp, temporary_name)
                else:
                    remote_size = get_remote_size(ftp, temporary_name)
                    if remote_size == "UNSUPPORTED":
                        remote_size = get_size_from_listing(ftp, temporary_name)

                if remote_size is None:
                    # No matching temporary file was found, or its size
                    # cannot be established. Start with a clean temp file.
                    remove_remote_file_if_present(ftp, temporary_name)

                if remote_size is not None:
                    if 0 <= remote_size <= file_size:
                        offset = remote_size
                    else:
                        print("[UPLOAD] Invalid remote partial size; restarting.")
                        remove_remote_file_if_present(ftp, temporary_name)
                        offset = 0

                if offset == file_size:
                    print("[UPLOAD] Temporary file already has the full size.")
                else:
                    update_status(
                        state="Uploading",
                        progress=(offset / file_size * 100 if file_size else 0),
                        message=f"Uploading {filename}",
                    )

                    transferred = offset

                    def progress(block):
                        nonlocal transferred
                        if upload_cancel.is_set():
                            raise InterruptedError("Upload cancellation requested.")
                        transferred += len(block)
                        percentage = (
                            min(transferred / file_size * 100, 100)
                            if file_size else 100
                        )
                        update_status(progress=percentage)

                    print(
                        f"[UPLOAD] Starting at {offset:,}/{file_size:,} bytes."
                    )

                    with file_path.open("rb") as source:
                        source.seek(offset)
                        try:
                            ftp.storbinary(
                                f"STOR {temporary_name}",
                                source,
                                blocksize=BLOCK_SIZE,
                                callback=progress,
                                rest=offset if offset else None,
                            )
                        except (ftplib.error_perm, ftplib.error_temp) as error:
                            response = str(error)

                            # REST rejected: do not repeat the same offset forever.
                            if offset and response.startswith(
                                ("500", "501", "502", "504")
                            ):
                                raise RuntimeError(
                                    "GoldHEN rejected FTP REST. "
                                    "Resume is unsupported; partial upload retained."
                                ) from error

                            # ftplib raises 4xx replies such as 451 as error_temp,
                            # not error_perm. Catch both classes so this recovery
                            # path actually runs.
                            if response.startswith("451"):
                                if clean_restart_used:
                                    raise RuntimeError(
                                        "GoldHEN returned 451 again after a clean "
                                        "restart. Upload stopped to avoid an "
                                        "infinite retry loop."
                                    ) from error

                                clean_restart_used = True
                                remove_remote_file_if_present(ftp, temporary_name)
                                raise ConnectionError(
                                    "GoldHEN returned 451; restarting from zero "
                                    "once on the next connection."
                                ) from error

                            raise

                if upload_cancel.is_set():
                    break

                update_status(
                    state="Verifying",
                    progress=100,
                    message="Checking remote file size",
                )

                # SIZE may wrap at 4 GiB on GoldHEN. Verify large
                # transfers with the byte count reported by LIST instead.
                if file_size >= (1 << 32):
                    verified_size = get_size_from_listing(ftp, temporary_name)
                else:
                    verified_size = get_remote_size(ftp, temporary_name)
                    if verified_size == "UNSUPPORTED":
                        verified_size = get_size_from_listing(ftp, temporary_name)

                if verified_size is None:
                    raise ConnectionError(
                        "Could not read the remote temporary file size."
                    )

                if verified_size != file_size:
                    raise ConnectionError(
                        f"Size mismatch: local={file_size}, remote={verified_size}"
                    )

                if upload_cancel.is_set():
                    break

                update_status(
                    state="Finalizing",
                    progress=100,
                    message="Renaming temporary file",
                )

                ftp.rename(temporary_name, filename)

                update_status(
                    state="Completed",
                    progress=100,
                    message=f"Completed: {filename}",
                )
                print(f"\n[UPLOAD] Successfully completed: {filename}")
                return

            except InterruptedError:
                print("\n[UPLOAD] Cancellation requested.")
                break

            except RuntimeError as error:
                print("\n[UPLOAD] Stopped:", error)
                update_status(state="Failed", message=str(error))
                return

            except ftplib.error_perm as error:
                print("\n[UPLOAD] FTP server error:", error)
                update_status(state="Failed", message=str(error))
                return

            except (
                OSError,
                EOFError,
                ftplib.Error,
                ConnectionError,
            ) as error:
                print("\n[UPLOAD] Connection/transfer interrupted:", error)
                print(f"[UPLOAD] Retrying in {RETRY_DELAY} seconds...")
                update_status(state="Retrying", message=str(error))
                if upload_cancel.wait(RETRY_DELAY):
                    break

            finally:
                close_ftp(ftp)

    finally:
        if upload_cancel.is_set():
            update_status(state="Cancelled", message="Upload cancelled.")
        else:
            current = get_upload_state()
            if current["state"] not in ("Completed", "Failed"):
                update_status(state="Stopped", message="Upload stopped.")
        update_status(active=False)
        print("\n[UPLOAD] Worker stopped.")


def set_upload_power_request(enabled):
    """Keep Windows and the display awake while uploading; restore normal behavior afterward."""
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    flags = (
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        if enabled else ES_CONTINUOUS
    )
    result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    if result == 0:
        raise ctypes.WinError()


def background_upload(ip, local_paths, remote_directory):
    """Upload selected files while preventing Windows from sleeping."""
    power_request_set = False
    try:
        try:
            set_upload_power_request(True)
            power_request_set = True
            print("[POWER] Sleep prevention enabled for the upload.")
        except Exception as error:
            print("[POWER] Warning: could not prevent system sleep:", error)

        total = len(local_paths)
        failed_files = []

        for index, local_path in enumerate(local_paths, start=1):
            if upload_cancel.is_set():
                break

            file_name = Path(local_path).name
            update_status(
                state="Starting",
                progress=0,
                attempts=0,
                message=f"Uploading file {index}/{total}: {file_name}",
                active=True,
            )
            print(f"\\n[BATCH] File {index}/{total}: {file_name}")

            background_upload_single(ip, local_path, remote_directory)

            result = get_upload_state()
            if result["state"] == "Failed":
                failed_files.append(file_name)

            if upload_cancel.is_set():
                break

        if upload_cancel.is_set():
            update_status(state="Cancelled", message="Upload batch cancelled.", active=False)
        elif failed_files:
            failed_list = ", ".join(failed_files)
            update_status(
                state="Completed with errors",
                progress=100,
                message=f"Processed {total} file(s); failed: {failed_list}",
                active=False,
            )
            print(f"\\n[BATCH] Finished with failed file(s): {failed_list}")
        else:
            update_status(
                state="Completed",
                progress=100,
                message=f"Successfully uploaded {total} selected file(s).",
                active=False,
            )
            print(f"\\n[BATCH] Successfully uploaded {total} selected file(s).")
    finally:
        if power_request_set:
            try:
                set_upload_power_request(False)
                print("[POWER] Sleep prevention released; normal power settings restored.")
            except Exception as error:
                print("[POWER] Warning: could not release sleep prevention:", error)


# ============================================================
# START / CANCEL UPLOAD
# ============================================================

def choose_upload_files():
    """Open the Windows Explorer-style picker with multi-select explicitly enabled."""
    import json
    import subprocess

    powershell_script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Select one or more files to upload'
$dialog.Filter = 'All files (*.*)|*.*'
$dialog.Multiselect = $true
$dialog.CheckFileExists = $true
$dialog.CheckPathExists = $true
$dialog.AutoUpgradeEnabled = $true
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    ConvertTo-Json -InputObject @($dialog.FileNames) -Compress
}
$dialog.Dispose()
"""

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            powershell_script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "Windows multi-select file picker failed: "
            + (details or f"PowerShell exited with code {completed.returncode}.")
        )

    output = completed.stdout.strip()
    if not output:
        return []

    try:
        selected = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Could not decode the selected file paths returned by Windows."
        ) from error

    # PowerShell may serialize a one-item array as a single JSON string.
    if isinstance(selected, str):
        selected = [selected]
    if not isinstance(selected, list):
        raise RuntimeError("Windows returned an unexpected file selection.")

    paths = [Path(item) for item in selected if isinstance(item, str) and item]
    if len(paths) != len(selected):
        raise RuntimeError("Windows returned one or more invalid file paths.")

    return paths


def start_upload(ip, remote_directory, pkg_only=False):
    global upload_thread

    if upload_thread is not None and upload_thread.is_alive():
        print("An upload is already running.")
        return

    try:
        file_paths = choose_upload_files()
    except Exception as error:
        print("Could not open the Explorer file picker:", error)
        return

    if not file_paths:
        print("File selection cancelled.")
        return

    invalid_paths = [path for path in file_paths if not path.is_file()]
    if invalid_paths:
        print("These selections are not files:")
        for path in invalid_paths:
            print(" -", path)
        return

    total_size = sum(path.stat().st_size for path in file_paths)
    print(f"\nSelected {len(file_paths)} file(s):")
    for path in file_paths:
        print(f" - {path.name} ({path.stat().st_size:,} bytes)")
    print("Total size:", f"{total_size:,}", "bytes")
    print("Destination:", remote_directory)

    if input("Start background upload? (y/n): ").strip().lower() != "y":
        print("Upload cancelled.")
        return

    upload_cancel.clear()
    update_status(
        state="Starting",
        progress=0,
        attempts=0,
        message="Starting background upload",
        active=True,
    )

    upload_thread = threading.Thread(
        target=background_upload,
        args=(ip, [str(path) for path in file_paths], remote_directory),
        name="GoldHEN-Background-Upload",
        daemon=False,
    )
    upload_thread.start()
    print("\nBackground upload started.")


def cancel_upload():
    if upload_thread is None or not upload_thread.is_alive():
        print("No active upload.")
        return

    if input("Type CANCEL to stop: ").strip() == "CANCEL":
        upload_cancel.set()
        print("Cancellation requested.")


# ============================================================
# MENU
# ============================================================

def show_menu():
    print("\n========== GOLDHEN FTP BROWSER ==========")
    print("1. List current directory")
    print("2. Open folder")
    print("3. Parent directory")
    print("4. Show current directory")
    print("5. Create folder")
    print("6. Select file(s) to upload to current directory")
    print("7. Select file(s) to upload to /data/pkg/")
    print("   For options 6 and 7: hold CTRL and click each file to select")
    print("   multiple files. Use SHIFT+click for a range, or CTRL+A")
    print("   to select all files. Then click Open.")
    print("8. Delete numbered file from /data/pkg/")
    print("9. Show upload status")
    print("10. Cancel background upload")
    print("11. Show PS4 connection status")
    print("12. Exit")
    print("=========================================")


# ============================================================
# MAIN
# ============================================================

def main():
    # The monitor is deliberately not started until after the IP prompt.
    ip = ask_ip()

    print("\nPS4 IP:", ip)
    print("FTP port:", PORT)

    start_connection_monitor(ip)
    print(
        f"\nConnection monitor started. "
        f"Checking every {MONITOR_INTERVAL} seconds."
    )

    ftp = None

    try:
        while True:
            show_menu()
            choice = input("> ").strip()

            if choice == "9":
                show_upload_status()
                continue

            if choice == "10":
                cancel_upload()
                continue

            if choice == "11":
                show_connection_status()
                continue

            if choice == "12":
                if upload_thread is not None and upload_thread.is_alive():
                    print(
                        "An upload is still running. "
                        "Wait for it to finish or cancel it first."
                    )
                    continue
                break

            if choice not in ("1", "2", "3", "4", "5", "6", "7", "8"):
                print("Invalid option.")
                continue

            ftp = ensure_browser_connection(ip, ftp)
            if ftp is None:
                print("Cannot perform this operation while offline.")
                continue

            try:
                if choice == "1":
                    list_directory(ftp)
                elif choice == "2":
                    open_directory(ftp)
                elif choice == "3":
                    parent_directory(ftp)
                elif choice == "4":
                    print("Current directory:", ftp.pwd())
                elif choice == "5":
                    create_directory(ftp)
                elif choice == "6":
                    start_upload(ip, ftp.pwd())
                elif choice == "7":
                    start_upload(ip, PKG_DIRECTORY, pkg_only=True)
                elif choice == "8":
                    delete_pkg_file(ftp)

            except (OSError, EOFError, ftplib.Error) as error:
                print("\nFTP operation failed:", error)
                print("The browser will reconnect on its next operation.")
                close_ftp(ftp)
                ftp = None

    finally:
        close_ftp(ftp)
        shutdown_event.set()

        if monitor_thread is not None:
            monitor_thread.join(timeout=TIMEOUT + 2)

        print("\nFTP browser closed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProgram interrupted.")
    finally:
        shutdown_event.set()
        print("Program stopped.")
