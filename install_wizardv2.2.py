#!/usr/bin/env python3
"""
edgetunnel-wizard
-----------------
Deploys local edgetunnel (_worker.js) to Cloudflare Pages or Workers through
the Cloudflare API.

Setup:  pip install requests
Run:    python edgetunnel_wizard.py
"""

import argparse
import json
import os
import re
import secrets
import string
import sys
import tempfile
import time
import uuid
import webbrowser
from getpass import getpass
from pathlib import Path
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is missing. Install it with: pip install requests")

API = "https://api.cloudflare.com/client/v4"
COMPAT_DATE = "2025-11-04"          # same date as upstream wrangler.toml
COMPAT_FLAGS = ["nodejs_compat"]
TIMEOUT = 60                        # seconds, applied to every HTTP call

TOKEN_PERMISSIONS = [
    {"key": "workers_scripts", "type": "edit"},      # Workers scripts, workers.dev on/off
    {"key": "workers_kv_storage", "type": "edit"},   # KV namespace
    {"key": "page", "type": "edit"},                 # Pages projects and deployments
    {"key": "user_details", "type": "read"},         # shows which user the token belongs to
]

STORE_DIR = Path.home() / ".config" / "edgetunnel-wizard"
STORE_FILE = STORE_DIR / "tokens.json"

ALNUM = string.ascii_lowercase + string.digits
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?$")


# ------------------------------------------------------------------ output
def info(msg):
    print(f"[*] {msg}")


def ok(msg):
    print(f"[+] {msg}")


def warn(msg):
    print(f"[!] {msg}")


def fail(msg):
    print(f"[x] {msg}")


# ------------------------------------------------------------------ input
def ask(prompt):
    return input(prompt).strip()


def ask_choice(count, default=1):
    while True:
        raw = ask(f"Choose [1-{count}, Enter = {default}]: ")
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw)
        warn("out of range, try again")


# ------------------------------------------------------------------ random values
def rand_str(alphabet, length):
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_name():
    return "edt-" + rand_str(ALNUM, 12)


def random_subdomain():
    return rand_str(ALNUM, 16)


# ------------------------------------------------------------------ token link
def token_creation_url():
    query = urlencode({
        "permissionGroupKeys": json.dumps(TOKEN_PERMISSIONS, separators=(",", ":")),
        "accountId": "*",
        "zoneId": "all",
        "name": "edgetunnel-wizard",
    })
    return f"https://dash.cloudflare.com/profile/api-tokens?{query}"


def ask_new_token():
    link = token_creation_url()
    print()
    info("1. Open this link. The permissions are already selected:")
    print(f"\n   {link}\n")
    info("2. Click 'Continue to summary', then 'Create Token', and copy the token.")
    info("3. Paste the token below (the input is hidden).")
    try:
        webbrowser.open(link)
    except Exception:
        pass
    try:
        return getpass("Cloudflare API token: ").strip()
    except (EOFError, KeyboardInterrupt):
        # 没有 TTY（管道 / CI）时 getpass 会直接 EOF，不能抛栈
        print()
        return ""


# ------------------------------------------------------------------ saved logins
class TokenStore:
    def load(self):
        try:
            data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [t for t in data.get("tokens", []) if isinstance(t, dict) and t.get("token")]

    def _write(self, items):
        """Atomic write: a crash (or a full disk) must not leave a half-written token
        file behind, because that would wipe every saved API token."""
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(STORE_DIR), prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"tokens": items}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, STORE_FILE)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def save(self, label, token):
        items = [t for t in self.load() if t["token"] != token]
        items.append({"label": label, "token": token})
        self._write(items)

    def forget(self, token):
        self._write([t for t in self.load() if t["token"] != token])


# ------------------------------------------------------------------ progress bar
def human_size(n):
    """Human readable byte count (B / KB / MB)."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ProgressBar:
    """Tiny terminal progress bar (no extra dependency).

    On a real terminal it repaints one line; when the output is piped to a
    file it prints a single plain line instead of a stream of escape codes.
    """

    def __init__(self, label, total, width=26):
        self.label = label
        self.total = max(1, int(total))
        self.width = width
        self.sent = 0
        self.tty = sys.stdout.isatty()
        self._drawn = False

    def start(self):
        if not self.tty:
            info(f"{self.label} ({human_size(self.total)}) ...")
        else:
            self.update(0)

    def update(self, sent):
        self.sent = sent
        if not self.tty:
            return
        ratio = min(1.0, sent / self.total)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        line = f"  {self.label} [{bar}] {ratio * 100:5.1f}%  {human_size(sent)}/{human_size(self.total)}"
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
        self._drawn = True

    def close(self):
        if self.tty and self._drawn:
            # erase the bar, the caller prints the final status line
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()


class UploadBody:
    """File-like view over an already encoded multipart body.

    requests hands the whole payload to urllib3 in one go, so there is nothing
    to report progress from. This wrapper keeps the exact same bytes and the
    exact same Content-Length (requests reads __len__), but lets us count how
    much has actually been written to the socket.
    """

    def __init__(self, data, bar):
        self._data = data
        self._pos = 0
        self._bar = bar

    def __len__(self):
        # requests' super_len() uses this to compute Content-Length
        return len(self._data) - self._pos

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._data[self._pos:]
        else:
            chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        if self._bar and chunk:
            self._bar.update(self._pos)
        return chunk


# ------------------------------------------------------------------ Cloudflare API
class CloudflareError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _describe(status, data):
    items = (data.get("errors") or []) + (data.get("messages") or [])
    parts = []
    for item in items:
        if isinstance(item, dict):
            parts.append(f"{item.get('message', '?')} (code {item.get('code', '?')})")
        else:
            parts.append(str(item))
    text = "; ".join(parts) or "no details"
    hint = ""
    if status in (401, 403):
        hint = " -- the token is wrong or lacks permissions; create a new one with the link."
    return f"HTTP {status}: {text}{hint}"


class Cloudflare:
    def __init__(self, token):
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, path, **kwargs):
        # 只对幂等的 GET 做退避重试：Cloudflare 会对突发请求返回 429/5xx，
        # 而 POST/PUT 重试可能造出重复的 KV / 项目。
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                resp = self.http.request(method, API + path, timeout=TIMEOUT, **kwargs)
            except requests.RequestException as e:
                if attempt + 1 == attempts:
                    raise CloudflareError(
                        f"cannot reach api.cloudflare.com ({type(e).__name__}). "
                        "Check your internet connection or proxy settings."
                    ) from e
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                delay = float(resp.headers.get("Retry-After") or 0) or (2 ** attempt)
                warn(f"Cloudflare answered HTTP {resp.status_code}, retrying in {delay:g}s ...")
                time.sleep(min(delay, 10))
                continue
            break
        try:
            data = resp.json()
        except ValueError:
            raise CloudflareError(
                f"HTTP {resp.status_code}, answer is not JSON: {resp.text[:200]!r}",
                resp.status_code,
            ) from None
        if resp.status_code >= 400 or not data.get("success"):
            raise CloudflareError(_describe(resp.status_code, data), resp.status_code)
        return data

    def upload(self, method, path, files, label):
        """POST/PUT a multipart body and show a progress bar while it uploads.

        The multipart payload is built by requests itself (same bytes, same
        headers as before), only the transfer is streamed so the bar can move.
        """
        try:
            from requests.models import RequestEncodingMixin
            body, content_type = RequestEncodingMixin._encode_files(files, None)
        except Exception:
            # requests internals changed -- fall back to the plain (silent) upload
            return self.call(method, path, files=files)

        bar = ProgressBar(label, len(body))
        bar.start()
        stream = UploadBody(body, bar)
        try:
            return self.call(method, path, data=stream,
                             headers={"Content-Type": content_type})
        finally:
            bar.close()


def list_accounts(cf):
    return cf.call("GET", "/accounts")["result"]


def who_am_i(cf):
    try:
        return cf.call("GET", "/user")["result"].get("email")
    except CloudflareError:
        return None


def workers_dev_domain(cf, acc_id):
    try:
        sub = cf.call("GET", f"/accounts/{acc_id}/workers/subdomain")["result"]["subdomain"]
        return f"{sub}.workers.dev"
    except CloudflareError:
        pass
    info("No workers.dev subdomain on this account yet, creating one ...")
    for _ in range(3):
        candidate = random_subdomain()
        try:
            res = cf.call("PUT", f"/accounts/{acc_id}/workers/subdomain",
                          json={"subdomain": candidate})
            return f"{res['result']['subdomain']}.workers.dev"
        except CloudflareError as e:
            warn(f"'{candidate}' was rejected: {e}")
    raise CloudflareError("could not create a workers.dev subdomain after 3 attempts")


def name_taken(cf, acc_id, kind, name):
    if kind == "pages":
        try:
            cf.call("GET", f"/accounts/{acc_id}/pages/projects/{name}")
            return True
        except CloudflareError as e:
            if e.status == 404:
                return False
            raise
    scripts = cf.call("GET", f"/accounts/{acc_id}/workers/scripts")["result"]
    return any(s.get("id") == name for s in scripts)


def create_kv(cf, acc_id, name):
    res = cf.call("POST", f"/accounts/{acc_id}/storage/kv/namespaces",
                  json={"title": f"{name}-kv-{int(time.time())}"})
    return res["result"]["id"]


def delete_kv(cf, acc_id, ns_id):
    cf.call("DELETE", f"/accounts/{acc_id}/storage/kv/namespaces/{ns_id}")


# ------------------------------------------------------------------ deploy: Pages
def deploy_pages(cf, acc_id, name, script, env, undo):
    ns = create_kv(cf, acc_id, name)
    undo.append(lambda: delete_kv(cf, acc_id, ns))
    ok("KV namespace created")

    env_vars = {k: {"type": "plain_text", "value": v} for k, v in env.items()}
    # production 与 preview 都要绑定 KV 和变量，否则预览域名会读不到配置（Error 1101）
    deployment_env = {
        "compatibility_date": COMPAT_DATE,
        "compatibility_flags": COMPAT_FLAGS,
        "browsers": {},
        "kv_namespaces": {"KV": {"namespace_id": ns}},
        "env_vars": env_vars,
    }
    payload = {
        "name": name,
        "production_branch": "main",
        "deployment_configs": {
            "production": deployment_env,
            "preview": deployment_env,
        },
    }
    res = cf.call("POST", f"/accounts/{acc_id}/pages/projects", json=payload)
    undo.append(lambda: cf.call("DELETE", f"/accounts/{acc_id}/pages/projects/{name}"))
    ok("Pages project created")

    files = {
        "manifest": (None, "{}"),
        "branch": (None, "main"),
        "_worker.js": ("_worker.js", script, "application/javascript"),
    }
    cf.upload("POST", f"/accounts/{acc_id}/pages/projects/{name}/deployments", files,
              "uploading to Pages")
    ok("edgetunnel uploaded to Pages")

    subdomain = res["result"].get("subdomain") or f"{name}.pages.dev"
    return f"https://{subdomain}"


# ------------------------------------------------------------------ deploy: Workers
def deploy_workers(cf, acc_id, name, script, env, undo):
    domain = workers_dev_domain(cf, acc_id)
    ns = create_kv(cf, acc_id, name)
    undo.append(lambda: delete_kv(cf, acc_id, ns))
    ok("KV namespace created")

    bindings = [{"type": "kv_namespace", "name": "KV", "namespace_id": ns}]
    bindings += [{"type": "plain_text", "name": k, "text": v} for k, v in env.items()]
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPAT_DATE,
        "compatibility_flags": COMPAT_FLAGS,
        "bindings": bindings,
    }
    files = {
        "metadata": (None, json.dumps(metadata), "application/json"),
        "worker.js": ("worker.js", script, "application/javascript+module"),
    }
    cf.upload("PUT", f"/accounts/{acc_id}/workers/scripts/{name}", files,
              "uploading to Workers")
    undo.append(lambda: cf.call("DELETE", f"/accounts/{acc_id}/workers/scripts/{name}"))
    ok("edgetunnel uploaded to Workers")

    cf.call("POST", f"/accounts/{acc_id}/workers/scripts/{name}/subdomain", json={"enabled": True})
    ok("workers.dev address enabled")
    return f"https://{name}.{domain}"


# ------------------------------------------------------------------ wizard steps
def ask_name(cf, acc_id, kind):
    default = random_name()
    while True:
        name = (ask(f"Project name [Enter = {default}]: ") or default).lower()
        if not NAME_RE.match(name):
            warn("use 2-58 characters: a-z, 0-9 and '-' (not at the start or the end)")
            continue
        try:
            taken = name_taken(cf, acc_id, kind, name)
        except CloudflareError as e:
            warn(f"could not check the name ({e}); continuing")
            return name
        if taken:
            warn(f"'{name}' already exists in this account, choose another")
            continue
        return name


def clean_file_path(raw):
    """Clean path from drag-and-drop artifacts (quotes, escaped spaces, etc.)"""
    raw = raw.strip()
    # Remove quotes added by drag-and-drop
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        raw = raw[1:-1].strip()
    # Handle escaped spaces on Linux/macOS
    if os.name != 'nt':
        raw = raw.replace(r"\ ", " ")
    return Path(raw).expanduser()


def validate_script(data):
    if data.startswith(b"\xef\xbb\xbf"):          # strip the UTF-8 BOM
        data = data[3:]
    if b"export default" not in data:
        fail("Invalid file: Does not contain 'export default' (not a valid worker script)")
        return None
    return data


def load_script(path_arg):
    # 1. If path was passed via CLI argument (--script)
    if path_arg:
        path = clean_file_path(path_arg)
        if path.is_file():
            data = validate_script(path.read_bytes())
            if data:
                ok(f"Using script: {path.resolve()}")
                return data
        fail(f"Specified script not found or invalid: {path_arg}")

    # 2. Check if a default _worker.js exists in the current directory
    default_file = None
    for cand in [Path("_worker.js"), Path("worker.js")]:
        if cand.is_file():
            default_file = cand
            break

    # 3. Prompt user interactively (Drag & Drop or Enter path)
    print("\n" + "-" * 62)
    info("Please specify your worker file.")
    info("You can Drag & Drop the file into this window, or type/paste its path.")
    if default_file:
        info(f"Press Enter directly to use detected file: [{default_file.name}]")
    print("-" * 62)

    while True:
        prompt_text = f"Worker file path [{default_file.name}]: " if default_file else "Worker file path: "
        user_input = ask(prompt_text)

        if not user_input and default_file:
            path = default_file
        elif not user_input:
            warn("Path cannot be empty. Please drag and drop your file here.")
            continue
        else:
            path = clean_file_path(user_input)

        if not path.is_file():
            fail(f"File not found: {path}")
            continue

        try:
            raw_data = path.read_bytes()
        except Exception as e:
            fail(f"Cannot read file ({e})")
            continue

        data = validate_script(raw_data)
        if data is None:
            continue

        ok(f"Selected worker: {path.resolve()}")
        return data


def rollback(undo):
    if not undo:
        return
    warn("removing what was created before the failure ...")
    for step in reversed(undo):
        try:
            step()
        except Exception as e:
            warn(f"could not remove one item ({e}); delete it in the Cloudflare dashboard")


# ------------------------------------------------------------------ main
def main():
    parser = argparse.ArgumentParser(description="Deploy edgetunnel to Cloudflare Pages or Workers.")
    parser.add_argument("--script", help="path to edgetunnel's _worker.js")
    args = parser.parse_args()

    print("\n  edgetunnel wizard -- deploys cmliu/edgetunnel to your Cloudflare account\n")

    # 1. login: a saved token or a new one
    store = TokenStore()
    saved = store.load()
    token = None
    if saved:
        print("Saved logins:")
        for i, item in enumerate(saved, 1):
            print(f"  {i}. {item.get('label', 'unknown')}")
        choice = ask("Choose a saved login, or press Enter for a new token: ")
        if choice.isdigit() and 1 <= int(choice) <= len(saved):
            token = saved[int(choice) - 1]["token"]

    while True:
        if not token:
            token = ask_new_token()
            if not token:
                fail("no token entered")
                return 1
        cf = Cloudflare(token)
        try:
            accounts = list_accounts(cf)
            if not accounts:
                raise CloudflareError("this token cannot see any Cloudflare account")
            label = who_am_i(cf) or accounts[0]["name"]
            store.save(label, token)
            ok(f"logged in as {label}")
            break
        except CloudflareError as e:
            fail(f"token check failed: {e}")
            store.forget(token)
            token = None

    # 2. account
    if len(accounts) == 1:
        acc = accounts[0]
    else:
        print("\nCloudflare accounts:")
        for i, a in enumerate(accounts, 1):
            print(f"  {i}. {a['name']}")
        acc = accounts[ask_choice(len(accounts)) - 1]
    acc_id = acc["id"]

    # 3. method and name
    print("\nDeployment method:")
    print("  1. Pages    (the edgetunnel README's recommended way)")
    print("  2. Workers")
    kind = "pages" if ask_choice(2) == 1 else "workers"
    name = ask_name(cf, acc_id, kind)

    # 4. script and variables
    script = load_script(args.script)
    env = {
        "ADMIN": rand_str(string.ascii_letters + string.digits, 20),
        "KEY": rand_str(ALNUM, 16),
        "UUID": str(uuid.uuid4()),
        "OFF_LOG": "true",
        "DEBUG": "false",
    }

    # 5. deploy. If anything fails, remove what was already created.
    undo = []
    info(f"deploying '{name}' to {kind} ...")
    try:
        if kind == "pages":
            base = deploy_pages(cf, acc_id, name, script, env, undo)
        else:
            base = deploy_workers(cf, acc_id, name, script, env, undo)
    except CloudflareError as e:
        fail(f"deployment failed: {e}")
        rollback(undo)
        return 1
    except KeyboardInterrupt:
        print()
        fail("interrupted")
        rollback(undo)
        return 1

    # 6. result
    print("\n" + "=" * 62)
    ok("edgetunnel is installed")
    print(f"  Admin panel : {base}/admin")
    print(f"  Password    : {env['ADMIN']}")
    print(f"  Subscription: {base}/{env['KEY']}")
    print(f"  UUID        : {env['UUID']}")
    print(f"  KV Logging  : Disabled (OFF_LOG=true) [Optimized]")
    print(f"  Debug Mode  : Disabled (DEBUG=false) [Optimized]")
    print("=" * 62)
    info("Save these values. They are also visible in the Cloudflare dashboard under Variables.")
    info("Wait about a minute before the first visit. Error 1101 usually means the KV binding "
         "(name KV) or the ADMIN variable is missing.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(1)