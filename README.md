# wiggle-wiggle
find and extract wiggle stereographs from your photos

### huh?

![like this](/media/example.gif)

This script goes through a bunch of pictures, computes a set of perceptual hashes on them, then uses them to try and find runs of images that might be stereo wigglegrams.

It's a little overbuilt in some places and underbuilt in others. Please give it a try and report back.


### usage

Make a venv and install the requirements. With [uv](https://docs.astral.sh/uv/):

```
uv venv
uv pip sync requirements.txt
```

For iCloud pass `-i` and it'll find your photo library automagically. Otherwise pass `-d <directory>` to point it at a folder full of pictures.

Run `hash` to calculate hashes for the images. It saves a database (`_hashes.json`) next to your pictures, so you can rerun it when you take more — already-hashed images are skipped.

Then run `export` to stitch the wigglegrams.

```
./wigglewiggle.py -i hash      # hash your iCloud library
./wigglewiggle.py -i export    # build the gifs
```


### going faster

Hashing is the slow part (six perceptual hashes per image), so it runs in parallel — one image per CPU core — with a progress bar and ETA. Use `-j N` to cap the worker count if you'd rather leave some cores free.

You can also borrow cores from another machine. The hashing only needs the image *bytes*, so a helper box doesn't need your photo library — just the hashing libraries from `requirements-worker.txt`:

```
uv venv && uv pip sync requirements-worker.txt
```

Then either run a worker on it by hand:

```
./wigglewiggle.py serve            # listens on :8765, one slot per core
```

...or let the machine with the photos start one for you over SSH and shut it down when it's done:

```
# start + use a worker over SSH
./wigglewiggle.py -i hash --remote-ssh user@192.168.1.50

# or point at an already-running worker
./wigglewiggle.py -i hash --remote 192.168.1.50:8765
```

Both machines pull from one shared queue, so a faster box just grabs more work. Worker library versions are pinned to match, so the hashes come out byte-identical no matter who computed them. Over a Thunderbolt bridge, adding a 12-core laptop to a 10-core desktop ran ~2.3x faster — basically linear.


### things that might not work

* Date detection in directory mode is probably borked. It only really can handle images with valid EXIF dates. I will fix it soon™️
* Some of the option flags are dummied out still.
* Really just a bunch of UX work left.
