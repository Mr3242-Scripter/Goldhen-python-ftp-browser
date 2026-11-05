import ftplib
import ipaddress
import os
import sys
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

PKG_DIRECTORY = "/data/pkg/"

# ============================================================
# GLOBAL UPLOAD STATE
# ============================================================

upload_lock = threading.Lock()
upload_cancel = threading.Event()
upload_thread = None

upload_status = {
    "active": False,
    "state": "Idle",
    "progress": 0.0,
    "attempts": 0,
    "message": "No active upload"
}


# ============================================================
# IP ADDRESS
# ============================================================

def ask_ip():
    while True:
        address = input("Enter your PS4 IP address: ").strip()

        try:
            return str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            print("Invalid IPv4 address.")


# ============================================================
# FTP CONNECTION
# ============================================================

def connect(ip):
    ftp = ftplib.FTP()

    try:
        ftp.connect(ip, PORT, timeout=TIMEOUT)
        ftp.login()
        ftp.set_pasv(True)
        return ftp

    except (OSError, ftplib.Error):
        try:
            ftp.close()
        except OSError:
            pass
        raise


def close_ftp(ftp):
    if ftp is None:
        return

    try:
        ftp.quit()
    except (OSError, ftplib.Error):
        try:
            ftp.close()
        except OSError:
            pass


def ensure_browser_connection(ip, ftp):
    if ftp is not None:
        try:
            ftp.voidcmd("NOOP")
            return ftp
        except (OSError, EOFError, ftplib.Error):
            close_ftp(ftp)

    print(f"\nConnecting to {ip}:{PORT}...")

    try:
        ftp = connect(ip)
        print("Connected to GoldHEN FTP.")
        return ftp

    except (OSError, ftplib.Error) as error:
        print("PS4 is currently unreachable:", error)
        print("The browser will remain available.")
        return None


# ============================================================
# UPLOAD STATUS
# ============================================================

def update_status(
    state=None,
    progress=None,
    attempts=None,
    message=None,
    active=None
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


def show_upload_status():
    with upload_lock:
        status = upload_status.copy()

    print("\n========== UPLOAD STATUS ==========")
    print("Active:", status["active"])
    print("State:", status["state"])
    print(f"Progress: {status['progress']:.2f}%")
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

    if not folder:
        return

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
# REMOTE DIRECTORY LISTING
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
                "is_directory": permissions.startswith("d")
            })

    finally:
        ftp.cwd(original_directory)

    return sorted(
        entries,
        key=lambda item: item["name"].lower()
    )


# ============================================================
# NUMBERED FILE DELETION
# ============================================================

def delete_pkg_file(ftp):
    print("\n========== DELETE FROM /data/pkg/ ==========")

    entries = get_directory_entries(ftp, PKG_DIRECTORY)

    files = [
        entry for entry in entries
        if not entry["is_directory"]
    ]

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
# REMOTE FILE SIZE
# ============================================================

def get_remote_size(ftp, filename):
    """
    Returns:
        integer: remote file size
        None: file does not exist
        "UNSUPPORTED": SIZE is unsupported
    """

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
    """
    Fallback for FTP servers where SIZE is unavailable.
    Uses the UNIX-style LIST format returned by GoldHEN.
    """

    lines = []
    ftp.retrlines("LIST", lines.append)

    for line in lines:
        parts = line.split(maxsplit=8)

        if len(parts) < 9:
            continue

        if parts[8] == filename:
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
# BACKGROUND UPLOAD WORKER
# ============================================================

def background_upload(ip, local_path, remote_directory):
    file_path = Path(local_path)
    filename = file_path.name

    temporary_name = "." + filename + ".uploading"
    file_size = file_path.stat().st_size

    rest_supported = True
    size_supported = True
    attempt = 0

    try:
        while not upload_cancel.is_set():
            ftp = None

            try:
                attempt += 1

                update_status(
                    state="Connecting",
                    attempts=attempt,
                    message=f"Connecting to {ip}:{PORT}"
                )

                print(
                    f"\n[UPLOAD] Connection attempt {attempt}..."
                )

                ftp = connect(ip)

                print("[UPLOAD] Connected.")

                ftp.cwd(remote_directory)

                if upload_cancel.is_set():
                    break

                # --------------------------------------------
                # DETERMINE EXISTING TEMPORARY FILE SIZE
                # --------------------------------------------

                offset = 0

                if size_supported:
                    try:
                        remote_size = get_remote_size(
                            ftp,
                            temporary_name
                        )

                        if remote_size == "UNSUPPORTED":
                            size_supported = False
                            rest_supported = False

                            print(
                                "[UPLOAD] SIZE unsupported. "
                                "Restarting transfers from zero."
                            )

                            remove_remote_file_if_present(
                                ftp,
                                temporary_name
                            )

                        elif remote_size is not None:
                            if remote_size <= file_size:
                                offset = remote_size
                            else:
                                print(
                                    "[UPLOAD] Temporary file is "
                                    "larger than the source. "
                                    "Restarting."
                                )

                                remove_remote_file_if_present(
                                    ftp,
                                    temporary_name
                                )

                    except ftplib.all_errors as error:
                        print(
                            "[UPLOAD] Could not determine "
                            "remote size:",
                            error
                        )

                        size_supported = False
                        rest_supported = False

                        remove_remote_file_if_present(
                            ftp,
                            temporary_name
                        )

                else:
                    remove_remote_file_if_present(
                        ftp,
                        temporary_name
                    )

                # --------------------------------------------
                # UPLOAD
                # --------------------------------------------

                if offset != file_size:
                    update_status(
                        state="Uploading",
                        progress=(
                            offset / file_size * 100
                            if file_size else 0
                        ),
                        message=f"Uploading {filename}"
                    )

                    transferred = offset

                    def progress(block):
                        nonlocal transferred

                        if upload_cancel.is_set():
                            raise InterruptedError(
                                "Upload cancellation requested."
                            )

                        transferred += len(block)

                        percentage = (
                            transferred / file_size * 100
                            if file_size else 100
                        )

                        update_status(
                            progress=percentage
                        )

                    print(
                        f"[UPLOAD] Starting at "
                        f"{offset:,}/{file_size:,} bytes."
                    )

                    with file_path.open("rb") as file:
                        file.seek(offset)

                        try:
                            ftp.storbinary(
                                f"STOR {temporary_name}",
                                file,
                                blocksize=BLOCK_SIZE,
                                callback=progress,
                                rest=(
                                    offset
                                    if offset and rest_supported
                                    else None
                                )
                            )

                        except ftplib.error_perm as error:
                            response = str(error)

                            # REST unsupported: restart safely
                            # instead of retrying the same offset.
                            if (
                                offset
                                and rest_supported
                                and response.startswith(
                                    ("500", "501", "502", "504")
                                )
                            ):
                                print(
                                    "[UPLOAD] REST unsupported. "
                                    "Restarting from zero."
                                )

                                rest_supported = False

                                remove_remote_file_if_present(
                                    ftp,
                                    temporary_name
                                )

                                raise ConnectionError(
                                    "Restart required because REST "
                                    "is unsupported."
                                )

                            raise

                if upload_cancel.is_set():
                    break

                # --------------------------------------------
                # VERIFY TEMPORARY FILE
                # --------------------------------------------

                update_status(
                    state="Verifying",
                    progress=100,
                    message="Checking uploaded file size"
                )

                remote_size = None

                if size_supported:
                    try:
                        remote_size = get_remote_size(
                            ftp,
                            temporary_name
                        )

                        if remote_size == "UNSUPPORTED":
                            size_supported = False

                    except ftplib.all_errors:
                        size_supported = False

                if not size_supported or remote_size is None:
                    remote_size = get_size_from_listing(
                        ftp,
                        temporary_name
                    )

                if remote_size is None:
                    raise RuntimeError(
                        "Unable to verify the uploaded file size."
                    )

                if remote_size != file_size:
                    raise ConnectionError(
                        f"Size mismatch: local={file_size}, "
                        f"remote={remote_size}"
                    )

                print("[UPLOAD] File size verified.")

                if upload_cancel.is_set():
                    break

                # --------------------------------------------
                # FINALIZE
                # --------------------------------------------

                update_status(
                    state="Finalizing",
                    progress=100,
                    message="Renaming temporary file"
                )

                # Never delete an existing destination here.
                # If the server refuses to replace it, report
                # the error rather than risking data loss.
                ftp.rename(temporary_name, filename)

                update_status(
                    state="Completed",
                    progress=100,
                    message=f"Completed: {filename}"
                )

                print(
                    f"\n[UPLOAD] Successfully completed: {filename}"
                )

                return

            except InterruptedError:
                print("\n[UPLOAD] Cancellation requested.")
                break

            except RuntimeError as error:
                print("\n[UPLOAD] Cannot safely finalize:", error)
                update_status(
                    state="Failed",
                    message=str(error)
                )
                return

            except ftplib.error_perm as error:
                print("\n[UPLOAD] FTP permission/server error:", error)

                update_status(
                    state="Failed",
                    message=str(error)
                )
                return

            except (
                OSError,
                EOFError,
                ftplib.Error,
                ConnectionError
            ) as error:
                print("\n[UPLOAD] Connection/transfer interrupted:", error)
                print(
                    f"[UPLOAD] Retrying in {RETRY_DELAY} seconds..."
                )

                update_status(
                    state="Retrying",
                    message=str(error)
                )

                if upload_cancel.wait(RETRY_DELAY):
                    break

            finally:
                close_ftp(ftp)

    finally:
        if upload_cancel.is_set():
            update_status(
                state="Cancelled",
                message="Upload cancelled."
            )
        elif upload_status["state"] not in (
            "Completed",
            "Failed"
        ):
            update_status(
                state="Stopped",
                message="Upload stopped."
            )

        update_status(active=False)

        print("\n[UPLOAD] Worker stopped.")


# ============================================================
# START BACKGROUND UPLOAD
# ============================================================

def start_upload(ip, remote_directory, pkg_only=False):
    global upload_thread

    if upload_thread is not None and upload_thread.is_alive():
        print("An upload is already running.")
        return

    local_path = input("Full local file path: ").strip().strip('"')

    if not local_path:
        return

    file_path = Path(local_path).expanduser()

    if not file_path.is_file():
        print("File not found.")
        return

    if pkg_only and file_path.suffix.lower() != ".pkg":
        print("Please select a .pkg file.")
        return

    print("\nFile:", file_path.name)
    print("Size:", f"{file_path.stat().st_size:,}", "bytes")
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
        active=True
    )

    upload_thread = threading.Thread(
        target=background_upload,
        args=(ip, str(file_path), remote_directory),
        daemon=False
    )

    upload_thread.start()

    print("\nBackground upload started.")
    print("The FTP browser remains available.")


# ============================================================
# CANCEL UPLOAD
# ============================================================

def cancel_upload():
    if upload_thread is None or not upload_thread.is_alive():
        print("No active upload.")
        return

    if input("Type CANCEL to stop: ").strip() == "CANCEL":
        upload_cancel.set()
        print("Cancellation requested.")


# ============================================================
# MAIN MENU
# ============================================================

def show_menu():
    print("\n========== GOLDHEN FTP BROWSER ==========")
    print("1. List current directory")
    print("2. Open folder")
    print("3. Parent directory")
    print("4. Show current directory")
    print("5. Create folder")
    print("6. Upload file to current directory")
    print("7. Upload PKG to /data/pkg/")
    print("8. Delete numbered file from /data/pkg/")
    print("9. Show upload status")
    print("10. Cancel background upload")
    print("11. Exit")
    print("=========================================")


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():
    ip = ask_ip()

    print("\nPS4 IP:", ip)
    print("FTP port:", PORT)

    ftp = None

    print("\nThe browser will start even if the PS4 is offline.")

    while True:
        show_menu()

        choice = input("> ").strip()

        # These options do not need the browser FTP connection.
        if choice == "9":
            show_upload_status()
            continue

        if choice == "10":
            cancel_upload()
            continue

        if choice == "11":
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
                start_upload(
                    ip,
                    PKG_DIRECTORY,
                    pkg_only=True
                )

            elif choice == "8":
                delete_pkg_file(ftp)

        except (
            OSError,
            EOFError,
            ftplib.Error
        ) as error:
            print("\nFTP operation failed:", error)
            print("The browser will reconnect on its next operation.")

            close_ftp(ftp)
            ftp = None

    close_ftp(ftp)

    print("\nFTP browser closed.")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nProgram interrupted.")

    finally:
        print("Program stopped.")
