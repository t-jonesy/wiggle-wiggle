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

# pip install ...
# python -i wiggler.py
# >>> do_hashes()
# >>> wigs = find_wigglers()
# >>> for w in wigs: make_wiggle(w)
# ...or something like that

# edit these!
TMP_LOCATION = "/Volumes/ramdisk"
OH_DAMN_OVERRIDE = "/Users/lem/Pictures/Photos Library.photoslibrary/originals"

pillow_heif.register_heif_opener()

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
            exp_res = osxphotos.PhotoExporter(img).export(TMP_LOCATION, filename=tmp_name, options=osxphotos.ExportOptions(download_missing=True))
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
            found_ctime = datetime.datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
        else:
            found_ctime = datetime.fromtimestamp(os.path.getmtime(file))

        built = cls(uid=str(uuid.uuid4()), date=found_ctime)
        built.path = file
        built.hashes = HashSet.of_file(file)

        if thumb_file:
            built.thumb_path = thumb_file
            built.thumb_hashes = HashSet.of_file(thumb_file)

        return built

    def best_version(self):
        if self.path: return self.path, self.hashes
        elif self.thumb_path: return self.thumb_path, self.thumb_hashes
        else: raise RuntimeError("No image!")


# -------------
# DATA DIVISION

# All the hashes we know. It's a dict for some reason that I can't remember.
hashdb = {}

def backup_db(root: str):
    with open(f"{root}/_hashes.json", "w") as hf:
        hf.write(json.dumps(list(hashdb.items())))

def restore_db(root: str):
    global hashdb
    with open(f"{root}/_hashes.json", "r") as hf:
        tmp_hashes = json.loads(hf.read())
    for itm in tmp_hashes:
        hashdb[itm.uid] = itm




def do_hashes():
    global hashes

    for i, img in enumerate(target_images):
        print(f"{int(i/len(target_images)*100)}% {img.date.strftime("%Y-%m-%d %H:%M:%S")} {img.uuid}: ", end="")
        actual_path = img.path

        if str(img.uuid) in hashes.keys():
            print(f"hash {str(hashes[img.uuid][2])} (cached)")
            continue

        if img.ismissing:
            exp_res = osxphotos.PhotoExporter(img).export(TMP_LOCATION, options=osxphotos.ExportOptions(download_missing=True))
            actual_path = TMP_LOCATION + "/" + img.original_filename

        if not actual_path:
            try:
                print("trying ", end="")
                actual_path = f"{OH_DAMN_OVERRIDE}/{img.directory}/{img.filename}"
            except Exception as e:
                try:
                    print("nodir ", end="")
                    actual_path = f"{OH_DAMN_OVERRIDE}/{img.filename[0]}/{img.filename}"

                except Exception as f:
                    print(e, f)
                    breakpoint()
                    continue

        if not os.path.exists(actual_path):
            print("not found!!!")
            continue

        if actual_path.endswith(".rw2"):
            print("--raw--")
            continue

        try:
            this_hash = imagehash.phash(Image.open(actual_path))
            hashes[str(img.uuid)] = (str(img.uuid), img.date.isoformat(), str(this_hash))  # HashData(img.uuid, img.date, this_hash)
        except Exception as e:
            print(e)
            # breakpoint()
            continue

        if actual_path.startswith(TMP_LOCATION):
            print(f"hash {str(this_hash)} (downloaded)")
            os.remove(actual_path)
        else:
            print(f"hash {str(this_hash)}")

        if i % 200 == 0:
            print("backup...")
            backup_db()

def find_wigglers(thresh: int):
    all_hashes = sorted(list(hashes.values()), key=lambda x: datetime.fromisoformat(x[1]))
    inflated_hashes = []

    wigglers = []
    this_wiggle = []

    for h in all_hashes:
        inflated_hashes.append({"uuid": h[0], "date": datetime.fromisoformat(h[1]), "hash": imagehash.hex_to_hash(h[2])})

    for i in range(1, len(inflated_hashes)):
        distance = inflated_hashes[i]["hash"] - inflated_hashes[i - 1]["hash"]
        if distance < thresh and distance > 0:
            if len(this_wiggle) == 0:
                print(f"Wiggle on {inflated_hashes[i]['date']}...", end="")
                this_wiggle.append(inflated_hashes[i - 1])
            this_wiggle.append(inflated_hashes[i])
        elif len(this_wiggle) > 0:
            print(f" ran for {len(this_wiggle)} pics")
            wigglers.append(this_wiggle.copy())
            this_wiggle = []

    return wigglers

def make_wiggle(imgs: list):
    # Export all the images first.
    pillows = []
    paths = []

    try_name = f"wiggle_{imgs[0]['date'].strftime('%Y-%m-%d_%H-%M-%S')}.gif",
    if os.path.exists(try_name):
        print("skipping")
        return

    for img in imgs:
        info = _photodb.photos(uuid=[img["uuid"]])[0]
        exp_res = osxphotos.PhotoExporter(info).export(TMP_LOCATION, options=osxphotos.ExportOptions(download_missing=True))
        path = TMP_LOCATION + "/" + info.original_filename
        paths.append(path)
        gottem = Image.open(path)
        gottem.thumbnail((600, 600))
        pillows.append(gottem)



    pillows[0].save(try_name, save_all=True, append_images=pillows[1:] + list(reversed(pillows))[1:], duration=100, loop=0)

    for p in paths:
        os.remove(p)


if __name__ == "__main__":
    # let's fuckin go
    parser = argparse.ArgumentParser(prog="./run_hashes.py", description="", epilog="")

    parser_db = parser.add_mutually_exclusive_group(required=True)
    parser_db.add_argument("--icloud", "-i", action="store_true", help="Scan your iCloud photo library.")
    parser_db.add_argument("--directory", "-d", help="Scan a directory of pictures.")

    parser.add_argument("action", choices=["hash", "list-wiggles", "export-wiggles"])

    args = parser.parse_args()

    if args.icloud:
        # A bunch of things expect this.
        _photodb = osxphotos.PhotosDB()
    else:
        pass

        
    if args.action == "hash":
        # Unfortunately this needs special casing.
        if _photodb is not None:
            # iCloud mode.
            target_dir = "/".join(_photodb.library_path.split("/")[:-1])
            target_images = _photodb.query(osxphotos.QueryOptions(movies=False, hidden=False))
            target_images.sort(key=lambda x: x.date)

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
        else:
            # Directory mode.
            # I promise the repetition here is clearer than trying to break this out into a class.
            target_dir = args.directory
            target_files = []

            for file in glob.glob(f"{target_dir}/**/*", recursive=True):
                if "image/" in magic.from_file(file):
                    target_files.append(file)
            
            hash_added = 0
            hash_error = 0
            hash_found = 0

            oof_db = [h.path for h in hashdb]

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
            entries = sorted(list(hashdb.items()), key=lambda x: x.date)
            hashdb = {}
            for ent in entries:
                hashdb[ent.uid] = ent

        print(f"Found {hash_added + hash_found + hash_error} images - added {hash_added} new, {hash_error} failed")
        backup_db(target_dir)
