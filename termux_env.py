"""Entorno tipo Termux dentro de la app (solo Android, uso personal, el APK debe llamarse com.termux).
Descarga el bootstrap oficial de Termux, lo instala en /data/data/com.termux/files/usr y abre bash con un PTY real."""
import fcntl, json, os, pty, shutil, struct, termios, threading, time, urllib.request, zipfile

FILES = "/data/data/com.termux/files"
PREFIX, HOME = FILES + "/usr", FILES + "/home"
API = "https://api.github.com/repos/termux/termux-packages/releases?per_page=40"
ARCH = {"aarch64": "aarch64", "arm64": "aarch64", "armv7l": "arm", "armv8l": "arm", "x86_64": "x86_64", "i686": "i686"}


def available():
    return os.path.isdir(FILES) and os.access(FILES, os.W_OK)


def ready():
    return os.path.isfile(PREFIX + "/bin/bash")


def env():
    return {"HOME": HOME, "PREFIX": PREFIX, "PATH": PREFIX + "/bin", "TMPDIR": PREFIX + "/tmp", "TERM": "xterm-256color",
            "COLORTERM": "truecolor", "LANG": "en_US.UTF-8", "SHELL": PREFIX + "/bin/bash", "ANDROID_DATA": "/data",
            "ANDROID_ROOT": "/system", "EXTERNAL_STORAGE": "/sdcard"}


def install(log):
    arch = ARCH.get(os.uname().machine)
    if not arch:
        raise RuntimeError("arquitectura no soportada: " + os.uname().machine)
    log("Buscando el bootstrap oficial de Termux...")
    url = None
    for r in json.load(urllib.request.urlopen(API, timeout=30)):
        if r["tag_name"].startswith("bootstrap-") and "apt-android-7" in r["tag_name"]:
            url = next((a["browser_download_url"] for a in r["assets"] if a["name"] == f"bootstrap-{arch}.zip"), None)
            if url:
                break
    if not url:
        raise RuntimeError("no se encontro el bootstrap para " + arch)
    log("Descargando " + url.split("/")[-2])
    zp = FILES + "/bootstrap.zip"
    with urllib.request.urlopen(url, timeout=60) as r, open(zp, "wb") as f:
        total, n, last = int(r.headers.get("Content-Length", 0)), 0, 0
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            n += len(b)
            if time.time() - last > 3:
                last = time.time()
                log(f"  {n >> 20} / {total >> 20} MB")
    log("Instalando...")
    stage, links = PREFIX + "-staging", []
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    with zipfile.ZipFile(zp) as z:
        for i in z.infolist():
            if i.filename == "SYMLINKS.txt":
                links = [l.split("\u2190", 1) for l in z.read(i).decode().splitlines() if "\u2190" in l]
                continue
            dst = os.path.join(stage, i.filename)
            if i.is_dir():
                os.makedirs(dst, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(i) as s, open(dst, "wb") as f:
                head = s.read(4)
                f.write(head)
                shutil.copyfileobj(s, f)
            os.chmod(dst, 0o700 if head[:2] == b"#!" or head == b"\x7fELF" else 0o600)
    for t, p in links:
        d = os.path.join(stage, p)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if not os.path.lexists(d):
            os.symlink(t, d)
    shutil.rmtree(PREFIX, ignore_errors=True)
    os.rename(stage, PREFIX)
    os.makedirs(HOME, exist_ok=True)
    os.makedirs(PREFIX + "/tmp", exist_ok=True)
    os.remove(zp)
    log("Listo.\r\n")


class Term:
    def __init__(self):
        self.buf, self.base, self.lock = bytearray(), 0, threading.Lock()
        self.fd = self.pid = None
        self.started, self.pending, self.size = False, [], (80, 24)

    def out(self, b):
        with self.lock:
            self.buf += b.encode() if isinstance(b, str) else b
            if len(self.buf) > 400000:
                cut = len(self.buf) - 300000
                del self.buf[:cut]
                self.base += cut

    def read(self, pos):
        with self.lock:
            pos = max(pos, self.base)
            return self.base + len(self.buf), bytes(self.buf[pos - self.base:])

    def start(self):
        if not self.started:
            self.started = True
            self.out('Iniciando entorno...\r\n')
            threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        try:
            if not ready():
                install(lambda s: self.out(s + "\r\n"))
            self._spawn()
        except Exception as e:
            self.out(f"\r\n\x1b[31mError: {e}\x1b[0m\r\n")
            self.started = False

    def _spawn(self):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(HOME)
                os.execve(PREFIX + "/bin/bash", ["bash", "-l"], env())
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        self.resize(*self.size)
        threading.Thread(target=self._pump, daemon=True).start()
        time.sleep(0.3)
        for c in self.pending:
            self.write(c + "\n")
        self.pending = []

    def _pump(self):
        while True:
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                break
            self.out(b)
        try:
            os.waitpid(self.pid, 0)
        except Exception:
            pass
        self.fd = self.pid = None
        self.started = False
        self.out("\r\n[sesion terminada - abre la terminal de nuevo]\r\n")

    def write(self, d):
        if self.fd is not None:
            os.write(self.fd, d.encode() if isinstance(d, str) else d)

    def resize(self, c, r):
        self.size = (c, r)
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))

    def send(self, line):
        if self.fd is not None:
            self.write(line + "\n")
        else:
            self.pending.append(line)
            self.start()
