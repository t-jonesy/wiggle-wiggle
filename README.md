# wiggle-wiggle
find and extract wiggle stereographs from your photos

### huh?

![like this](/media/example.gif)

This script goes through a bunch of pictures, computes a set of perceptual hashes on them, then uses them to try and find runs of images that might be stereo wigglegrams.

It's a little overbuilt in some places and underbuilt in others. Please give it a try and report back.


### usage

Make a venv, install the requirements, etc.

For iCloud pass `-i` and it'll find your photo library automagically. Otherwise pass `-d <directory>` to point it at a folder full of pictures.

Run `hash` to calculate hashes for the images. It'll save a database file so you can rerun this when you take more pictures.

Then you can run `export` to see the wigglegrams.


### things that might not work

* Date detection in directory mode is probably borked. It only really can handle images with valid EXIF dates. I will fix it soon™️
* Some of the option flags are dummied out still.
* Really just a bunch of UX work left.
