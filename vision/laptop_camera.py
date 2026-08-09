#!/usr/bin/env python3
"""
Laptop camera server — streams webcam to Jetson over TCP.

Run this on the laptop, then on the Jetson:
  python3 face_track.py --network <this-laptop-ip>

Requirements (laptop):
  pip install opencv-python

Usage:
  python laptop_camera.py                  # default port 8485, camera 0
  python laptop_camera.py --port 8485
  python laptop_camera.py --camera 1      # if laptop has multiple cameras
"""

import argparse
import socket
import struct
import sys

import cv2

# NOTE: this script is meant to be copied to and run standalone on a laptop (see module
# docstring), so it deliberately keeps its own constants rather than importing config/ —
# the laptop doesn't have the rest of the repo. Keep NETWORK_PORT here in sync with
# config/camera.py's NETWORK_PORT on the Jetson side.
HOST = "0.0.0.0"
PORT = 8485
WIDTH = 640
HEIGHT = 480
JPEG_QUALITY = 70


def serve(port: int, camera_index: int) -> None:
    cap = cv2.VideoCapture(camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    if not cap.isOpened():
        print(f"ERROR: Cannot open camera {camera_index}", file=sys.stderr)
        sys.exit(1)

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, port))
    server.listen(1)

    # Print this laptop's IPs so the user knows what to pass to --network
    import socket as _s
    try:
        hostname = _s.gethostname()
        ips = _s.getaddrinfo(hostname, None, _s.AF_INET)
        ip_list = list({r[4][0] for r in ips if not r[4][0].startswith("127.")})
    except Exception:
        ip_list = ["<your-laptop-ip>"]

    print(f"Laptop camera server ready on port {port}")
    print(f"Your IPs: {', '.join(ip_list)}")
    print(f"On Jetson run: python3 face_track.py --network {ip_list[0] if ip_list else '<ip>'}")
    print("Waiting for Jetson to connect...")

    while True:
        conn, addr = server.accept()
        print(f"Jetson connected from {addr}")
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("Camera read failed")
                    break
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                data = buf.tobytes()
                header = struct.pack(">L", len(data))
                try:
                    conn.sendall(header + data)
                except OSError as exc:
                    print(f"Connection lost: {exc}")
                    break
                cv2.imshow("Laptop Camera → Jetson", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    cap.release()
                    cv2.destroyAllWindows()
                    conn.close()
                    server.close()
                    return
        finally:
            conn.close()
            print("Jetson disconnected — waiting for reconnect...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream laptop webcam to Jetson via TCP")
    parser.add_argument("--port", type=int, default=PORT, help=f"TCP port (default: {PORT})")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index (default: 0)")
    args = parser.parse_args()
    serve(args.port, args.camera)


if __name__ == "__main__":
    main()
