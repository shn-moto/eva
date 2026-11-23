"""Минимальный telnet-клиент к root-шеллу Станции 1 (порт 2323).
Использование: py -3 rootsh.py "команда" [таймаут_сек]
"""
import socket, sys, time

HOST, PORT = "192.168.1.20", 2323

def negotiate(sock):
    # отвечаем DONT/WONT на telnet-negotiation (IAC=255)
    data = b""
    sock.settimeout(2)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(chunk) < 4096:
                break
    except socket.timeout:
        pass
    return data

def run(cmd, timeout=20):
    s = socket.create_connection((HOST, PORT), timeout=10)
    negotiate(s)
    s.sendall(cmd.encode() + b"\n")
    out = b""
    s.settimeout(timeout)
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out.decode(errors="replace")

if __name__ == "__main__":
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    print(run(cmd, timeout))
