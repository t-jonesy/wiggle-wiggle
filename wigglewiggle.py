#!/usr/bin/env python
import os
import sys
import uuid
import glob
import json
import hashlib
import argparse
from datetime import datetime

import magic
import attrs
import cattrs
import osxphotos
import imagehash
from PIL import Image
from PIL.ExifTags import Base
import pillow_heif

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
    def from_ios(cls, img: osxphotos.PhotoInfo, force_download: bool = False):
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

def run_hashes_on_icloud():
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

    for img in target_images:
        print(f"img \"{img.uuid}\": ", end="")

        if img.uuid in hashdb:
            if hashdb[img.uuid].hashes is None and False:
                # download and hash full images if that is missing
                pass
            else:
                print("cached!")
                hash_found += 1
        else:
            try:
                built = HashedImage.from_ios(img, False)
                hashdb[built.uid] = built
                hash_added += 1
                if built.hashes:
                    print(f"added, phash {built.hashes.perceptual}")
                else:
                    print(f"added, phash {built.thumb_hashes.perceptual} (thumb)")

                if hash_added % 100 == 0:
                    backup_db(target_dir)

            except Exception as e:
                print(f"huh? {e}")
                hash_error += 1

    backup_db(target_dir)
    return hash_added, hash_found, hash_error

def run_hashes_on_directory(directory: str):
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

    oof_db = [h.path for h in hashdb.values()]

    for file in target_files:
        print(f"img \"{file}\": ", end="")

        if file in oof_db:
            print("cached!")
            hash_found += 1
        else:
            try:
                built = HashedImage.from_file(file, False)
                hashdb[built.uid] = built
                oof_db.append(built.path)
                hash_added += 1
                print(f"added, phash {built.hashes.perceptual}")

                if hash_added % 100 == 0:
                    backup_db(target_dir)

            except Exception as e:
                print(f"huh? {e}")
                hash_error += 1
    
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

    parser_db = parser.add_mutually_exclusive_group(required=True)
    parser_db.add_argument("--icloud", "-i", action="store_true", help="Scan your iCloud photo library.")
    parser_db.add_argument("--directory", "-d", help="Scan a directory of pictures.")

    parser.add_argument("action", choices=["hash", "export"])
    # Force download ain't done yet. Gotta experiment on hash/size relationship first
    parser.add_argument("--force-download", help="Forcibly download high-resolution images from iCloud, if missing locally.")
    parser.add_argument("--output", "-o", help="Output directory for wigglegrams.")
    parser.add_argument("--threshold", "-t", help="How similar an image must be to be considered a wigglegram.", type=int, default=10)

    args = parser.parse_args()

    if args.icloud:
        # A bunch of things expect this.
        _photodb = osxphotos.PhotosDB()
        target_dir = "/".join(_photodb.library_path.split("/")[:-1])
    else:
        target_dir = args.directory
        pass

    if args.action == "hash":
        # Unfortunately this needs special casing.
        if _photodb is not None:
            hash_added, hash_found, hash_error = run_hashes_on_icloud()
        else:
            hash_added, hash_found, hash_error = run_hashes_on_directory(args.directory)

        print(f"Found {hash_added + hash_found + hash_error} images - added {hash_added}, {hash_error} failed")

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








