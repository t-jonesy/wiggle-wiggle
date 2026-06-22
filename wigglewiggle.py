#!/usr/bin/env python
import os
import io
import sys
import uuid
import glob
import json
import time
import struct
import queue
import hashlib
import argparse
import threading
import subprocess
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor

# The hashing stack - required everywhere, including on bare remote workers.
import attrs
import cattrs
import imagehash
from PIL import Image
from PIL.ExifTags import Base
import pillow_heif

# These are only needed on the orchestrator (the machine that owns the Photos
# library and drives the run). A bare hashing worker doesn't need them, so keep
# them optional: that lets wigglewiggle.py run on a remote box that only has the
# hashing libraries installed.
try:
    import magic
except ImportError:
    magic = None

try:
    import osxphotos
except ImportError:
    osxphotos = None

try:
    from rich.progress import (
        Progress, BarColumn, TextColumn, TaskProgressColumn,
        MofNCompleteColumn, TimeElapsedColumn, TimeRemainingColumn, ProgressColumn,
    )
    from rich.text import Text
except ImportError:
    Progress = None
    ProgressColumn = object

# wigglegram maker
# '26 lem
# gpl license (no warranty)

# edit these!
TMP_LOCATION = "/tmp"

pillow_heif.register_heif_opener()

# why isn't this default behavior
def _fuck_dt_structure(obj: str, _) -> datetime:
    return datetime.fromisoformat(obj)

cattrs.global_converter.register_unstructure_hook(datetime, datetime.isoformat)
cattrs.global_converter.register_structure_hook(datetime, _fuck_dt_structure)

# --------------
# IMAGE DIVISION

# Defined here so that images can know how to export themselves.
# If not None, we're in iCloud mode.
_photodb = None

# Better to precompute all the hashes, in case we want them.
@attrs.define
class HashSet:
    perceptual: imagehash.ImageHash = None
    average: imagehash.ImageHash = None
    difference: imagehash.ImageHash = None
    wavelet: imagehash.ImageHash = None
    color: imagehash.ImageHash = None
    crop_resist: imagehash.ImageHash = None
    # This is here as a future option - what *image data* hashes to the above set of perceptual hashes??
    crypto: str = None

    @classmethod
    def of_image(cls, img: Image):
        built = cls()
        built.perceptual = imagehash.phash(img)
        built.average = imagehash.average_hash(img)
        built.difference = imagehash.dhash(img)
        built.wavelet = imagehash.whash(img)
        built.color = imagehash.colorhash(img, binbits=3)
        built.crop_resist = imagehash.crop_resistant_hash(img)
        return built

    @classmethod
    def of_file(cls, img_path: str):
        img = Image.open(img_path)
        built = cls.of_image(img)
        built.crypto = hashlib.md5(open(img_path, 'rb').read()).hexdigest()
        return built

    @classmethod
    def of_bytes(cls, data: bytes):
        # Same as of_file but from an in-memory buffer, so a worker can hash an
        # image that was shipped over the wire. Decoding the same bytes with the
        # same library versions yields identical hashes on any machine.
        built = cls.of_image(Image.open(io.BytesIO(data)))
        built.crypto = hashlib.md5(data).hexdigest()
        return built

    # @converter.register_structure_hook
    def __serialize__(self) -> dict:
        if self is None: return None
        built = {}
        if self.perceptual: built["p"] = str(self.perceptual)
        if self.average: built["a"] = str(self.average)
        if self.difference: built["d"] = str(self.difference)
        if self.wavelet: built["w"] = str(self.wavelet)
        if self.color: built["c"] = str(self.color)
        if self.crop_resist: built["r"] = str(self.crop_resist)
        if self.crypto: built["m"] = self.crypto
        return built

    # @converter.register_unstructure_hook
    def __deserialize__(data: dict, cls):
        built = cls()
        if data is None: return None
        if "p" in data: built.perceptual = imagehash.hex_to_hash(data["p"])
        if "a" in data: built.average = imagehash.hex_to_hash(data["a"])
        if "d" in data: built.difference = imagehash.hex_to_hash(data["d"])
        if "w" in data: built.wavelet = imagehash.hex_to_hash(data["w"])
        if "c" in data: built.color = imagehash.hex_to_flathash(data["c"], hashsize=3)
        if "r" in data: built.crop_resist = imagehash.hex_to_multihash(data["r"])
        if "m" in data: built.crypto = data["m"]
        return built

# why don't decorators work :(
cattrs.global_converter.register_unstructure_hook(HashSet, HashSet.__serialize__)
cattrs.global_converter.register_structure_hook(HashSet, HashSet.__deserialize__)

@attrs.define
class HashedImage:
    uid: str
    date: datetime
    path: str = None
    hashes: HashSet = None
    _ios: bool = False

    # Sometimes it's easier to get a thumbnail.
    thumb_path: str = None
    thumb_hashes: HashSet = None

    def _try_fetch_from_icloud(self, force_download: bool = False) -> None:
        # iCloud photos are ephemeral; what do we have?
        if _photodb is None: raise RuntimeError("not working from icloud today")
        if not self._ios: raise TypeError("what the fuck are you doing")

        pinfo = _photodb.photos(uuid=[self.uid])
        if len(pinfo) == 0: raise KeyError("Photo not found: deleted?")
        if len(pinfo) > 1: raise KeyError("Multiple entries for photo in DB???")
        pinfo = pinfo[0]
        
        if pinfo.ismissing and force_download:
            tmp_name = f"{self.uid}.{pinfo.filename.split('.')[-1]}"
            exp_res = osxphotos.PhotoExporter(pinfo).export(TMP_LOCATION, filename=tmp_name, options=osxphotos.ExportOptions(download_missing=True))
            # Exporting forces a download anyway - better maybe to export to bitbucket and use official path?
            self.path = f"{TMP_LOCATION}/{tmp_name}"
        elif not pinfo.ismissing:
            self.path = pinfo.path

        # Extract thumbnails.
        thumbs = []
        for dp in pinfo.path_derivatives:
            thumbs.append((dp, os.path.getsize(dp)))
        if len(thumbs) > 0:
            self.thumb_path = sorted(thumbs, key=lambda x: x[1])[-1][0]

        # At this point we should have *something*
        if not self.path and not self.thumb_path:
            raise RuntimeError("Nothing present for photo")

    @classmethod
    def from_ios(cls, img: "osxphotos.PhotoInfo", force_download: bool = False):
        # iOS has its own perculiarties
        if _photodb is None: raise RuntimeError("not working from icloud today")

        built = cls(uid=img.uuid, date=img.date)
        built._ios = True

        # Make image data happen.
        built._try_fetch_from_icloud(force_download)
        if built.path: built.hashes = HashSet.of_file(built.path)
        if built.thumb_path: built.thumb_hashes = HashSet.of_file(built.thumb_path)
    
        return built

    @classmethod
    def from_file(cls, file: str, thumb_file: str = None):
        loaded_img = Image.open(file)
        date_str = loaded_img.getexif().get(36867)
        if date_str is not None:
            found_ctime = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
        else:
            found_ctime = datetime.fromtimestamp(os.path.getmtime(file))

        built = cls(uid=str(uuid.uuid4()), date=found_ctime)
        built.path = file
        built.hashes = HashSet.of_file(file)

        if thumb_file:
            built.thumb_path = thumb_file
            built.thumb_hashes = HashSet.of_file(thumb_file)

        return built

    def best_version(self, force_redownload: bool = False):
        if self._ios and force_redownload:
            self._try_fetch_from_icloud(True)

        if self.path: return self.path, self.hashes
        elif self.thumb_path: return self.thumb_path, self.thumb_hashes
        else: raise RuntimeError("No image!")


# -------------
# DATA DIVISION

# All the hashes we know. It's a dict for some reason that I can't remember.
hashdb: dict[HashedImage] = {}

def backup_db(root: str):
    with open(f"{root}/_hashes.json", "w") as hf:
        hf.write(json.dumps([cattrs.unstructure(x) for x in hashdb.values()]))

def restore_db(root: str):
    global hashdb
    with open(f"{root}/_hashes.json", "r") as hf:
        tmp_hashes = json.loads(hf.read())
    for itm in tmp_hashes:
        built = cattrs.structure(itm, HashedImage)
        hashdb[built.uid] = built

# The heavy lifting (opening the image + 6 perceptual hashes) is CPU-bound, so we
# fan it out one image per core. Path resolution needs the _photodb object (which
# isn't worth shipping to a subprocess), so that stays in the parent; the workers
# below only ever touch file paths.

def _resolve_ios(img, force_download=False):
    """Find the local file + best thumbnail for a PhotoInfo. No hashing - keep this cheap."""
    built = HashedImage(uid=img.uuid, date=img.date)
    built._ios = True

    if img.ismissing and force_download:
        tmp_name = f"{img.uuid}.{img.filename.split('.')[-1]}"
        osxphotos.PhotoExporter(img).export(
            TMP_LOCATION, filename=tmp_name,
            options=osxphotos.ExportOptions(download_missing=True),
        )
        built.path = f"{TMP_LOCATION}/{tmp_name}"
    elif not img.ismissing:
        built.path = img.path

    thumbs = [(dp, os.path.getsize(dp)) for dp in img.path_derivatives]
    if thumbs:
        built.thumb_path = sorted(thumbs, key=lambda x: x[1])[-1][0]

    if not built.path and not built.thumb_path:
        raise RuntimeError("Nothing present for photo")
    return built

def _hash_built(built):
    """Subprocess worker: fill in the perceptual hashes for an already-resolved image."""
    try:
        if built.path:
            built.hashes = HashSet.of_file(built.path)
        if built.thumb_path:
            built.thumb_hashes = HashSet.of_file(built.thumb_path)
        return built, None
    except Exception as e:
        return None, f"{built.uid}: {e}"

def _hash_file(file):
    """Subprocess worker: hash a single file from disk."""
    try:
        return HashedImage.from_file(file), None
    except Exception as e:
        return None, f"{file}: {e}"

def _resolve_file(file):
    """Build a HashedImage with its date (EXIF/mtime) but no hashes yet, so the
    hashing itself can be shipped to a worker."""
    loaded = Image.open(file)
    date_str = loaded.getexif().get(36867)
    if date_str is not None:
        ctime = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
    else:
        ctime = datetime.fromtimestamp(os.path.getmtime(file))
    built = HashedImage(uid=str(uuid.uuid4()), date=ctime)
    built.path = file
    return built

class _RateColumn(ProgressColumn):
    """Throughput in images/sec, so you can watch the pool work."""
    def render(self, task):
        speed = task.finished_speed or task.speed
        if not speed:
            return Text("--/s", style="progress.data.speed")
        return Text(f"{speed:.1f}/s", style="progress.data.speed")

def _hash_progress():
    """A progress bar with a count, percentage, throughput, elapsed time and ETA."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        _RateColumn(),
        TextColumn("elapsed"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
    )

# ----------------
# DISTRIBUTED DIVISION
#
# A second machine (e.g. a laptop over a Thunderbolt bridge) can pitch in as extra
# cores. The hashing is location-independent - it only needs the image *bytes* - so
# the orchestrator (this machine, which owns the Photos library) ships bytes to a
# pool of workers and collects back the hashes. Workers run `wigglewiggle.py serve`
# and only need the hashing libraries installed, not osxphotos.
#
# Wire format for one image: a length-prefixed (full_bytes, thumb_bytes) frame in,
# a JSON {hashes, thumb_hashes, error} reply out. Plain bytes + JSON keeps it
# independent of Python/pickle versions across machines.

def _pack_blobs(*blobs):
    out = bytearray()
    for b in blobs:
        b = b or b""
        out += struct.pack(">I", len(b))
        out += b
    return bytes(out)

def _unpack_blobs(data, n):
    out, off = [], 0
    for _ in range(n):
        (ln,) = struct.unpack_from(">I", data, off)
        off += 4
        out.append(bytes(data[off:off + ln]))
        off += ln
    return out

def _read_bytes(path):
    if not path:
        return b""
    with open(path, "rb") as f:
        return f.read()

def _hash_image_blobs(full, thumb):
    """Worker subprocess: hash whichever of (full, thumb) bytes were sent."""
    out = {"hashes": None, "thumb_hashes": None, "error": None}
    try:
        if full:
            out["hashes"] = HashSet.of_bytes(full).__serialize__()
        if thumb:
            out["thumb_hashes"] = HashSet.of_bytes(thumb).__serialize__()
    except Exception as e:
        out["error"] = str(e)
    return out

def serve_worker(host="0.0.0.0", port=8765, workers=None):
    """Run a hashing worker: POST /hash to hash an image frame, GET /info for slot count."""
    workers = workers or os.cpu_count()
    pool = ProcessPoolExecutor(max_workers=workers)
    print(f"wigglewiggle worker: hashing on {workers} cores, listening on {host}:{port}", flush=True)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep connections alive across requests

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/info":
                self._send(200, json.dumps({"workers": workers}).encode())
            else:
                self._send(404, b"{}")

        def do_POST(self):
            if self.path != "/hash":
                self._send(404, b"{}")
                return
            length = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(length)
            try:
                full, thumb = _unpack_blobs(data, 2)
                result = pool.submit(_hash_image_blobs, full, thumb).result()
                self._send(200, json.dumps(result).encode())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode())

        def log_message(self, *a):
            pass  # quiet

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pool.shutdown(cancel_futures=True)

def _probe_endpoint(host, port, timeout=90):
    """Wait for a worker to come up; return how many slots (cores) it offers."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/info")
            info = json.loads(conn.getresponse().read())
            conn.close()
            return int(info.get("workers", 1))
        except Exception as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"worker {host}:{port} never became ready: {last}")

def _run_distributed(builts, endpoints, target_dir, progress, task):
    """Hash `builts` across worker endpoints, finalizing into hashdb with periodic
    backups. Returns (added, errors). Failed jobs are retried (failing over to
    another machine) before being given up on."""
    job_q = queue.Queue()
    for b in builts:
        job_q.put((b, 0))

    lock = threading.Lock()
    stats = {"added": 0, "error": 0}
    max_attempts = max(2, len(endpoints) + 1)

    def finalize(built, payload):
        if payload.get("hashes"):
            built.hashes = HashSet.__deserialize__(payload["hashes"], HashSet)
        if payload.get("thumb_hashes"):
            built.thumb_hashes = HashSet.__deserialize__(payload["thumb_hashes"], HashSet)
        with lock:
            if built.hashes or built.thumb_hashes:
                hashdb[built.uid] = built
                stats["added"] += 1
                if stats["added"] % 200 == 0:
                    backup_db(target_dir)
            else:
                stats["error"] += 1
            progress.advance(task)

    def give_up(built, msg):
        with lock:
            stats["error"] += 1
            progress.console.print(f"img \"{built.uid}\": huh? {msg}")
            progress.advance(task)

    def worker(ep):
        conn = http.client.HTTPConnection(ep["host"], ep["port"], timeout=300)
        while True:
            try:
                built, attempts = job_q.get_nowait()
            except queue.Empty:
                break
            try:
                body = _pack_blobs(_read_bytes(built.path), _read_bytes(built.thumb_path))
                conn.request("POST", "/hash", body=body)
                resp = conn.getresponse()
                payload = json.loads(resp.read())
                if resp.status != 200 or payload.get("error"):
                    raise RuntimeError(payload.get("error") or f"HTTP {resp.status}")
                finalize(built, payload)
            except Exception as e:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = http.client.HTTPConnection(ep["host"], ep["port"], timeout=300)
                if attempts + 1 < max_attempts:
                    job_q.put((built, attempts + 1))  # let another worker try
                else:
                    give_up(built, e)
        conn.close()

    threads = []
    for ep in endpoints:
        for _ in range(ep["slots"]):
            t = threading.Thread(target=worker, args=(ep,), daemon=True)
            t.start()
            threads.append(t)
    for t in threads:
        t.join()

    return stats["added"], stats["error"]

def _start_local_worker(workers, port):
    """Launch a worker on this machine (loopback) using `workers` cores."""
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "serve",
         "--host", "127.0.0.1", "--port", str(port), "--jobs", str(workers)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc

def _start_ssh_worker(ssh_target, port, remote_dir="~/wiggle-wiggle"):
    """Launch a worker on a remote box over SSH. Returns (host, stop_callback)."""
    host = ssh_target.split("@")[-1]
    remote_cmd = (
        f"cd {remote_dir} && "
        f"nohup .venv/bin/python wigglewiggle.py serve --host 0.0.0.0 --port {port} "
        f">/tmp/wiggle_worker.log 2>&1 & echo $!"
    )
    out = subprocess.check_output(
        ["ssh", "-o", "BatchMode=yes", ssh_target, remote_cmd], text=True
    ).strip()
    pid = out.splitlines()[-1].strip()

    def stop():
        subprocess.run(["ssh", "-o", "BatchMode=yes", ssh_target, f"kill {pid} 2>/dev/null"],
                       check=False)

    return host, stop

def run_hashes_on_icloud(workers=None, endpoints=None):
    global hashdb

    # iCloud mode.
    target_dir = "/".join(_photodb.library_path.split("/")[:-1])
    target_images = _photodb.query(osxphotos.QueryOptions(movies=False, hidden=False))
    target_images.sort(key=lambda x: x.date)

    try:
        restore_db(target_dir)
    except Exception as e:
        pass

    hash_added = 0
    hash_error = 0
    hash_found = 0

    with _hash_progress() as progress:
        # Resolve local paths up front (serial - needs _photodb); skip anything we already have.
        resolve = progress.add_task("resolving", total=len(target_images))
        jobs = []
        for img in target_images:
            progress.advance(resolve)
            if img.uuid in hashdb:
                hash_found += 1
                continue
            try:
                jobs.append(_resolve_ios(img))
            except Exception as e:
                progress.console.print(f"img \"{img.uuid}\": huh? {e}")
                hash_error += 1

        hashing = progress.add_task("hashing  ", total=len(jobs))
        if endpoints:
            slots = sum(e["slots"] for e in endpoints)
            progress.console.print(f"{hash_found} cached, hashing {len(jobs)} images across {slots} workers on {len(endpoints)} machines...")
            added, errs = _run_distributed(jobs, endpoints, target_dir, progress, hashing)
            hash_added += added
            hash_error += errs
        else:
            progress.console.print(f"{hash_found} cached, hashing {len(jobs)} images across {workers or os.cpu_count()} cores...")
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for built, err in pool.map(_hash_built, jobs):
                    progress.advance(hashing)
                    if err:
                        progress.console.print(f"img huh? {err}")
                        hash_error += 1
                        continue
                    hashdb[built.uid] = built
                    hash_added += 1
                    if hash_added % 100 == 0:
                        backup_db(target_dir)

    backup_db(target_dir)
    return hash_added, hash_found, hash_error

def run_hashes_on_directory(directory: str, workers=None, endpoints=None):
    global hashdb

    # Directory mode.
    # I promise the repetition here is clearer than trying to break this out into a class.
    target_dir = directory
    target_files = []

    for file in glob.glob(f"{target_dir}/**/*", recursive=True):
        if "image/" in magic.from_file(file, mime=True):
            target_files.append(file)

    try:
        restore_db(target_dir)
    except Exception as e:
        pass

    hash_added = 0
    hash_error = 0
    hash_found = 0

    oof_db = set(h.path for h in hashdb.values())
    jobs = [file for file in target_files if file not in oof_db]
    hash_found = len(target_files) - len(jobs)

    with _hash_progress() as progress:
        hashing = progress.add_task("hashing  ", total=len(jobs))
        if endpoints:
            slots = sum(e["slots"] for e in endpoints)
            progress.console.print(f"{hash_found} cached, hashing {len(jobs)} images across {slots} workers on {len(endpoints)} machines...")
            builts = []
            for file in jobs:
                try:
                    builts.append(_resolve_file(file))
                except Exception as e:
                    progress.console.print(f"img \"{file}\": huh? {e}")
                    hash_error += 1
                    progress.advance(hashing)
            added, errs = _run_distributed(builts, endpoints, target_dir, progress, hashing)
            hash_added += added
            hash_error += errs
        else:
            progress.console.print(f"{hash_found} cached, hashing {len(jobs)} images across {workers or os.cpu_count()} cores...")
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for built, err in pool.map(_hash_file, jobs):
                    progress.advance(hashing)
                    if err:
                        progress.console.print(f"img huh? {err}")
                        hash_error += 1
                        continue
                    hashdb[built.uid] = built
                    hash_added += 1
                    if hash_added % 100 == 0:
                        backup_db(target_dir)

    # Sorry
    entries = sorted(list(hashdb.values()), key=lambda x: x.date)
    hashdb = {}
    for ent in entries:
        hashdb[ent.uid] = ent

    backup_db(target_dir)
    return hash_added, hash_found, hash_error

# ---------------
# WIGGLE DIVISION

def find_wigglegrams(thresh: int) -> list[list[HashedImage]]:
    date_sorted = sorted(list(hashdb.values()), key=lambda x: x.date)
    wigglers = []
    this_wiggler = []

    # Also do some statistics.
    closest_non_zero_dist = None
    furthest_dist = None
    this_average = 0

    for i in range(1, len(date_sorted)):
        # TODO: good way of specifying what hash to use
        # also don't do extra work even if it's not much
        path_current, hash_current = date_sorted[i].best_version()
        path_last, hash_last = date_sorted[i - 1].best_version()

        dist = hash_current.perceptual - hash_last.perceptual
        if closest_non_zero_dist is None or closest_non_zero_dist > dist:
            closest_non_zero_dist = dist
        if furthest_dist is None or furthest_dist < dist:
            furthest_dist = dist

        if dist < thresh and dist > 0:
            # Belongs in a wigglegram
            if len(this_wiggler) == 0:
                print(f"Wiggle on {date_sorted[i - 1].date.isoformat()}... ", end="")
                this_wiggler.append(date_sorted[i - 1])
            this_wiggler.append(date_sorted[i])
            this_average += dist
        elif len(this_wiggler) > 0:
            this_average /= len(this_wiggler)
            print(f" ran for {len(this_wiggler)} pics (avg dist {this_average})")
            wigglers.append(this_wiggler)
            this_wiggler = []
            this_average = 0

    if len(this_wiggler) > 0:
        wigglers.append(this_wiggler)

    # For autothresholding, someday
    # print(f"close {closest_non_zero_dist}, far {furthest_dist}")

    return wigglers

def make_wigglegram(filename: str, imgs: list[HashedImage], frame_duration: int = 100, max_size: int = 600, boomerang: bool = True, force_redownload: bool = False):
    pillows = []

    for img in imgs:
        best_path, _ = img.best_version(force_redownload)
        gottem = Image.open(best_path)
        gottem.thumbnail((max_size, max_size))
        pillows.append(gottem)

    if boomerang:
        pillows = pillows + list(reversed(pillows))[1:]

    pillows[0].save(filename, save_all=True, append_images=pillows[1:], duration=frame_duration, loop=0)

def experiment_smooth_gradient():
    # Attempt to make a smooth transition between all the images in the database.
    # SLOW
    options = list(hashdb.values())
    smoothed = []
    
    smoothed.append(options.pop(0))

    while len(options) > 0:
        current_deltas = []
        path_current, hash_current = smoothed[-1].best_version()
        for i, img in enumerate(options):
            path_last, hash_last = img.best_version()
            dist = hash_current.perceptual - hash_last.perceptual
            current_deltas.append((dist, i, img))

        current_deltas.sort(key=lambda x: x[0])
        best = current_deltas[0]
        smoothed.append(best[2])
        del options[best[1]]

    return smoothed

if __name__ == "__main__":
    # let's fuckin go
    parser = argparse.ArgumentParser(prog="wigglewiggle", description="", epilog="")

    # Not required: `serve` mode needs neither of these.
    parser_db = parser.add_mutually_exclusive_group(required=False)
    parser_db.add_argument("--icloud", "-i", action="store_true", help="Scan your iCloud photo library.")
    parser_db.add_argument("--directory", "-d", help="Scan a directory of pictures.")

    parser.add_argument("action", choices=["hash", "export", "serve"])
    # Force download ain't done yet. Gotta experiment on hash/size relationship first
    parser.add_argument("--force-download", help="Forcibly download high-resolution images from iCloud, if missing locally.")
    parser.add_argument("--output", "-o", help="Output directory for wigglegrams.")
    parser.add_argument("--threshold", "-t", help="How similar an image must be to be considered a wigglegram.", type=int, default=10)
    parser.add_argument("--jobs", "-j", help="Number of parallel worker processes for hashing (default: one per CPU core).", type=int, default=None)
    parser.add_argument("--remote", action="append", metavar="HOST:PORT", help="Use an already-running worker as extra cores. Repeatable.")
    parser.add_argument("--remote-ssh", action="append", metavar="USER@HOST", help="Start a worker on a remote box over SSH and use it. Repeatable.")
    parser.add_argument("--host", default="0.0.0.0", help="serve: interface to bind (default 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8765, help="Worker port (default 8765).")

    args = parser.parse_args()

    if args.action == "serve":
        serve_worker(host=args.host, port=args.port, workers=args.jobs)
        sys.exit(0)

    if not args.icloud and not args.directory:
        parser.error("one of --icloud/-i or --directory/-d is required")

    if args.icloud:
        if osxphotos is None:
            parser.error("osxphotos is not installed - needed for --icloud")
        # A bunch of things expect this.
        _photodb = osxphotos.PhotosDB()
        target_dir = "/".join(_photodb.library_path.split("/")[:-1])
    else:
        target_dir = args.directory

    if args.action == "hash":
        endpoints = []
        teardowns = []
        try:
            if args.remote or args.remote_ssh:
                # Distributed run: a local worker plus any remote ones, sharing the load
                # via a single queue so a faster machine simply pulls more work.
                local_workers = args.jobs or max(1, os.cpu_count() - 1)
                lproc = _start_local_worker(local_workers, args.port)
                teardowns.append(lambda: lproc.terminate())
                slots = _probe_endpoint("127.0.0.1", args.port)
                endpoints.append({"host": "127.0.0.1", "port": args.port, "slots": slots})
                print(f"local worker: {slots} cores")

                for tgt in (args.remote_ssh or []):
                    host, stop = _start_ssh_worker(tgt, args.port)
                    teardowns.append(stop)
                    slots = _probe_endpoint(host, args.port)
                    endpoints.append({"host": host, "port": args.port, "slots": slots})
                    print(f"remote worker {tgt}: {slots} cores")

                for rem in (args.remote or []):
                    host, _, p = rem.rpartition(":")
                    slots = _probe_endpoint(host, int(p))
                    endpoints.append({"host": host, "port": int(p), "slots": slots})
                    print(f"remote worker {rem}: {slots} cores")

            if _photodb is not None:
                hash_added, hash_found, hash_error = run_hashes_on_icloud(args.jobs, endpoints or None)
            else:
                hash_added, hash_found, hash_error = run_hashes_on_directory(args.directory, args.jobs, endpoints or None)

            print(f"Found {hash_added + hash_found + hash_error} images - added {hash_added}, {hash_error} failed")
        finally:
            for stop in teardowns:
                try:
                    stop()
                except Exception:
                    pass

    elif args.action == "export":
        output_dir = target_dir if args.output is None else args.output
        restore_db(target_dir)

        all_found = find_wigglegrams(args.threshold)

        for wig in all_found:
            try_name = f"{output_dir}/wiggle_{wig[0].date.strftime('%Y-%m-%d_%H-%M-%S')}.gif"
            if os.path.exists(try_name):
                # print("already exists")
                continue

            make_wigglegram(try_name, wig)








