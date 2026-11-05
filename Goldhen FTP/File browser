import ftplib
import ipaddress
import os
import sys
import time
import threading
from pathlib import Path

# ==========================================
# CONFIGURATION
# ==========================================

PORT = 2121
TIMEOUT = 15
BLOCK_SIZE = 1024 * 1024
PKG_DIRECTORY = "/data/pkg/"
RETRY_DELAY = 5

# Background upload state
upload_thread = None
upload_cancel = threading.Event()
upload_lock = threading.Lock()

upload_status = {
    "active": False,
    "state": "Idle",
    "progress": 0.0,
    "attempts": 0,
    "message": "No active upload"
}


# ==========================================
# IP ADDRESS
# ==========================================

def ask_ip():
    while True:
        address = input("Enter your PS4 IP address: ").strip()

        try:
            return str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            print("Invalid IPv4 address. Try again.")


# ==========================================
# FTP CONNECTION
# ==========================================

def connect(ip, timeout=TIMEOUT):
    ftp = ftplib.FTP()

    try:
        ftp.connect(ip, PORT, timeout=timeout)
        ftp.login()
        ftp.set_pasv(True)
        return ftp

    except (OSError, ftplib.Error):
        try:
            ftp.close()
        except OSError:
            pass
        raise


def connect_with_retry(ip, cancel=None):
    attempt = 0

    while cancel is None or not cancel.is_set():
        attempt += 1

        with upload_lock:
            upload_status["attempts"] = attempt
            upload_status["state"] = "Connecting"
            upload_status["message"] = (
                f"Connecting to {ip}:{PORT} (attempt {attempt})"
            )

        print(f"\n[UPLOAD] Connecting to {ip}:{PORT} "
              f"(attempt {attempt})...")

        try:
            ftp = connect(ip)
            print("[UPLOAD] Connection established.")
            return ftp

        except (OSError, ftplib.Error) as error:
            print(f"[UPLOAD] Connection failed: {error}")
            print(f"[UPLOAD] Retrying in {RETRY_DELAY} seconds...")

            with upload_lock:
                upload_status["state"] = "Waiting"
                upload_status["message"] = str(error)

            if cancel is not None and cancel.wait(RETRY_DELAY):
                break

            if cancel is None:
                time.sleep(RETRY_DELAY)

    return None


# ==========================================
# DIRECTORY NAVIGATION
# ==========================================

def list_directory(ftp):
    print("\nContents of:", ftp.pwd())

    try:
        ftp.retrlines("LIST")
    except ftplib.all_errors as error:
        print("FTP error:", error)


def open_directory(ftp):
    folder = input("Folder name: ").strip()

    if not folder:
        return

    try:
        ftp.cwd(folder)
        print("Current directory:", ftp.pwd())
    except ftplib.all_errors as error:
        print("Cannot open folder:", error)


def parent_directory(ftp):
    try:
        ftp.cwd("..")
        print("Current directory:", ftp.pwd())
    except ftplib.all_errors as error:
        print("FTP error:", error)


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

    try:
        ftp.mkd(folder)
        print("Folder created:", folder)
    except ftplib.all_errors as error:
        print("Cannot create folder:", error)


# ==========================================
# DIRECTORY LISTING FOR DELETION
# ==========================================

def get_directory_entries(ftp):
    original_directory = ftp.pwd()
    entries = []

    try:
        ftp.cwd(PKG_DIRECTORY)

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


# ==========================================
# NUMBERED FILE DELETION
# ==========================================

def delete_pkg_file(ftp):
    print("\n========== DELETE FROM /data/pkg/ ==========")

    try:
        entries = get_directory_entries(ftp)
    except ftplib.all_errors as error:
        print("Cannot read directory:", error)
        return

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
        print("Cancelled.")
        return

    original_directory = ftp.pwd()

    try:
        ftp.cwd(PKG_DIRECTORY)
        ftp.delete(filename)
        print("File deleted:", filename)

    except ftplib.all_errors as error:
        print("Deletion failed:", error)

    finally:
        try:
            ftp.cwd(original_directory)
        except ftplib.all_errors:
            pass


# ==========================================
# BACKGROUND UPLOAD WORKER
# ==========================================

def update_upload(state=None, progress=None, message=None):
    with upload_lock:
        if state is not None:
            upload_status["state"] = state

        if progress is not None:
            upload_status["progress"] = progress

        if message is not None:
            upload_status["message"] = message


def background_upload(ip, local_path, remote_directory):
    file_path = Path(local_path)
    filename = file_path.name

    # Upload to a temporary name so an interrupted transfer
    # does not leave a partially uploaded final file.
    temporary_name = "." + filename + ".uploading"

    file_size = file_path.stat().st_size

    while not upload_cancel.is_set():
        ftp = None

        try:
            ftp = connect_with_retry(ip, upload_cancel)

            if ftp is None:
                break

            ftp.cwd(remote_directory)

            # Check how much of the temporary file already exists.
            offset = 0

            try:
                remote_size = ftp.size(temporary_name)

                if remote_size is not None:
                    if remote_size <= file_size:
                        offset = remote_size
                    else:
                        ftp.delete(temporary_name)

            except ftplib.all_errors:
                # SIZE may not be supported by the FTP server.
                # Restart the temporary upload if its size is unknown.
                try:
                    ftp.delete(temporary_name)
                except ftplib.all_errors:
                    pass

                offset = 0

            if upload_cancel.is_set():
                break

            if offset == file_size:
                print("\n[UPLOAD] Temporary file already complete.")

            else:
                print(
                    f"\n[UPLOAD] Resuming from "
                    f"{offset:,}/{file_size:,} bytes."
                )

                update_upload(
                    state="Uploading",
                    progress=(
                        offset / file_size * 100
                        if file_size else 100
                    )
                )

                transferred = offset

                def progress(block):
                    nonlocal transferred

                    transferred += len(block)

                    percentage = (
                        transferred / file_size * 100
                        if file_size else 100
                    )

                    update_upload(
                        progress=percentage,
                        message=f"Uploading {filename}"
                    )

                with file_path.open("rb") as file:
                    file.seek(offset)

                    ftp.storbinary(
                        f"STOR {temporary_name}",
                        file,
                        blocksize=BLOCK_SIZE,
                        callback=progress,
                        rest=offset if offset else None
                    )

            if upload_cancel.is_set():
                break

            update_upload(
                state="Finalizing",
                progress=100,
                message="Finalizing remote file"
            )

            # Replace the destination only after the temporary
            # upload has completed.
            try:
                ftp.delete(filename)
            except ftplib.all_errors:
                pass

            ftp.rename(temporary_name, filename)

            update_upload(
                state="Completed",
                progress=100,
                message=f"Completed: {filename}"
            )

            print(f"\n[UPLOAD] Completed successfully: {filename}")
            return

        except (OSError, EOFError, ftplib.Error) as error:
            print(f"\n[UPLOAD] Transfer interrupted: {error}")
            print("[UPLOAD] Reconnecting and attempting to resume...")

            update_upload(
                state="Retrying",
                message=str(error)
            )

            if upload_cancel.wait(RETRY_DELAY):
                break

        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except (OSError, ftplib.Error):
                    try:
                        ftp.close()
                    except OSError:
                        pass

    update_upload(
        state="Cancelled",
        message="Upload cancelled."
    )

    print("\n[UPLOAD] Upload stopped.")


# ==========================================
# START BACKGROUND UPLOAD
# ==========================================

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
        print("Cancelled.")
        return

    upload_cancel.clear()

    with upload_lock:
        upload_status.update({
            "active": True,
            "state": "Starting",
            "progress": 0.0,
            "attempts": 0,
            "message": "Starting background upload"
        })

    upload_thread = threading.Thread(
        target=background_upload,
        args=(ip, str(file_path), remote_directory),
        daemon=False
    )

    upload_thread.start()

    print("\nBackground upload started.")
    print("You can continue using the FTP browser.")


# ==========================================
# UPLOAD STATUS
# ==========================================

def show_upload_status():
    with upload_lock:
        state = upload_status["state"]
        progress = upload_status["progress"]
        attempts = upload_status["attempts"]
        message = upload_status["message"]

    print("\n========== UPLOAD STATUS ==========")
    print("State:", state)
    print(f"Progress: {progress:.2f}%")
    print("Connection attempts:", attempts)
    print("Message:", message)
    print("===================================")


# ==========================================
# CANCEL UPLOAD
# ==========================================

def cancel_upload():
    if upload_thread is None or not upload_thread.is_alive():
        print("No active upload.")
        return

    if input("Type CANCEL to stop the upload: ").strip() == "CANCEL":
        upload_cancel.set()
        print("Cancellation requested.")


# ==========================================
# MAIN MENU
# ==========================================

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


# ==========================================
# MAIN PROGRAM
# ==========================================

def main():
    global upload_thread

    ip = ask_ip()

    print("\nPS4 IP:", ip)
    print("FTP port:", PORT)

    ftp = connect_with_retry(ip)

    if ftp is None:
        print("Could not connect.")
        return

    print("\nConnected successfully!")
    print("Current directory:", ftp.pwd())

    try:
        while True:
            show_menu()

            choice = input("> ").strip()

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

            elif choice == "9":
                show_upload_status()

            elif choice == "10":
                cancel_upload()

            elif choice == "11":
                if upload_thread is not None and upload_thread.is_alive():
                    print("An upload is still running.")
                    print("Cancel it before exiting.")
                    continue

                break

            else:
                print("Invalid option.")

    except KeyboardInterrupt:
        print("\nInterrupted.")

    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            ftp.close()

        print("FTP browser disconnected.")
        input("Press Enter to close...")


if __name__ == "__main__":
    main()
