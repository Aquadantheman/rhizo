"""
2PC Participant Server for Remote Benchmarking

Deploy this on cloud VMs to measure real geo-distributed 2PC latency.
The coordinator (real_consensus_benchmark.py --remote-2pc) connects to
these participants over the network.

Usage:
    python benchmarks/2pc_participant_server.py --port 9000
    python benchmarks/2pc_participant_server.py --port 9000 --host 0.0.0.0

Protocol:
    Same length-prefixed messages as the localhost 2PC benchmark:
    - PREP (4 bytes) → participant replies VOTE (4 bytes)
    - COMT (4 bytes) → participant replies ACKK (4 bytes)
    - SHUT (4 bytes) → participant shuts down
    - PING (4 bytes) → participant replies PONG (4 bytes)  [for RTT measurement]

The server handles one coordinator connection at a time and automatically
restarts listening after disconnect.
"""

import argparse
import socket
import struct
import sys

MSG_PREPARE = b"PREP"
MSG_VOTE_COMMIT = b"VOTE"
MSG_COMMIT = b"COMT"
MSG_ACK = b"ACKK"
MSG_SHUTDOWN = b"SHUT"
MSG_PING = b"PING"
MSG_PONG = b"PONG"


def _send_msg(sock: socket.socket, msg: bytes) -> None:
    """Send a length-prefixed message."""
    sock.sendall(struct.pack("!I", len(msg)) + msg)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes."""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return b""
        data += chunk
    return data


def _recv_msg(sock: socket.socket) -> bytes:
    """Receive a length-prefixed message."""
    raw_len = _recv_exact(sock, 4)
    if not raw_len:
        return b""
    msg_len = struct.unpack("!I", raw_len)[0]
    return _recv_exact(sock, msg_len)


def run_server(host: str, port: int) -> None:
    """Run the 2PC participant server."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    print(f"2PC participant listening on {host}:{port}")
    print("Waiting for coordinator connection...")

    while True:
        conn, addr = server.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"Coordinator connected from {addr}")

        counter = 0
        transactions = 0
        shutdown = False

        try:
            while True:
                msg = _recv_msg(conn)
                if not msg:
                    print(f"Coordinator disconnected ({transactions} transactions completed)")
                    break
                if msg == MSG_SHUTDOWN:
                    print(f"Shutdown requested ({transactions} transactions completed)")
                    shutdown = True
                    break
                if msg == MSG_PING:
                    _send_msg(conn, MSG_PONG)
                elif msg == MSG_PREPARE:
                    counter += 1
                    _send_msg(conn, MSG_VOTE_COMMIT)
                elif msg == MSG_COMMIT:
                    transactions += 1
                    _send_msg(conn, MSG_ACK)
        except (ConnectionResetError, BrokenPipeError):
            print(f"Connection lost ({transactions} transactions completed)")
        finally:
            conn.close()

        if shutdown:
            break

        print("Waiting for next coordinator connection...")

    server.close()
    print("Server shut down.")


def main():
    parser = argparse.ArgumentParser(
        description="2PC Participant Server for remote benchmarking"
    )
    parser.add_argument(
        "--port", type=int, default=9000,
        help="Port to listen on (default: 9000)"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
