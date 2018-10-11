![ocrmypdf-auto logo](https://raw.githubusercontent.com/cmccambridge/ocrmypdf-auto/master/media/logo.png)

# cmccambridge/ocrmypdf-auto
[![Docker Repository on Quay](https://quay.io/repository/cmccambridge/ocrmypdf-auto/status "Docker Repository on Quay")](https://quay.io/repository/cmccambridge/ocrmypdf-auto)

This container automates one stage in a "paperless" document processing pipeline: Take all the PDFs in a folder, run OCR on them, and save the output to another folder. It combines the excellent tools [OCRmyPDF][ocrmypdf] and [tesseract-ocr][tesseract] with `inotify`-based file monitoring and some new configurability.

For example, you could configure a wireless document scanner to save all images to one volume, and use this container to monitor all new incoming files, OCR them, and write the finished (searchable!) PDFs to another volume.

![ocrmypdf-auto workflow. Scan to input volume, ocrmypdf-auto runs, collect results in output volume](https://raw.githubusercontent.com/cmccambridge/ocrmypdf-auto/master/media/flow.png)

* [Usage](#usage)
* [Volumes](#volumes)
* [Environment Variables](#environment-variables-and-global-configuration)
* [`OCRmyPDF` Configuration Files](#ocrmypdf-configuration-files)
* [unRAID Integration](#unraid-integration)
* [Future Work](#future-work)

[ocrmypdf]: https://github.com/jbarlow83/OCRmyPDF
[tesseract]: https://github.com/tesseract-ocr/tesseract

## Usage

Quick & Easy:
```
docker create \
  -v <input directory>:/input \
  -v <output directory>:/output \
  -v <appdata/config directory>:/config \
  quay.io/cmccambridge/ocrmypdf-auto
```

Full Custom:
```
docker create \
  -v <input directory>:/input \
  -v <output directory>:/output \
  -v <appdata/config directory>:/config \
  -v <temp directory>:/ocrtemp \
  -v <archvie directory>:/archive \
  -e OCR_LANGUAGES="deu chi-sim ita" \
  -e OCR_OUTPUT_MODE=MIRROR_TREE \
  -e OCR_PROCESS_EXISTING_ON_START=1 \
  -e OCR_ACTION_ON_SUCCESS=ARCHIVE_INPUT_FILES \
  quay.io/cmccambridge/ocrmypdf-auto
```

## Volumes

### Required Volumes

A few volumes are required for normal operation of `ocrmypdf-auto`:

|Volume|Description|
|---|---|
|`/input`|Directory to monitor for input files|
|`/output`|Directory to write OCR'ed PDF output to|
|`/config`|Directory to store the master `ocr.config` OCRmyPDF configuration file|

## Optional Volumes

A few additional volumes may be mounted for advanced configuration:

|Volume|Description|
|---|---|
|`/ocrtemp`|Directory to use for all OCRmyPDF temporary files, e.g. to select a ramdisk or a local cache|
|`/archive`|Directory to use for archiving input files after successful processing, in `ARCHIVE_INPUT_FILES` output mode|

## Environment Variables and Global Configuration

`ocrmypdf-auto` can be configured with a few top-level / global parameters that affect the overall operations of the container:

|Environment Variable|Description|
|---|---|
|`OCR_LANGUAGES` | Additional languages (besides English) to install. Given as a space separated list. All possible packages can be found on the [Ubuntu site][ocr-languages]. Example: `deu chi-sim ita` |
|`OCR_OUTPUT_MODE` | Controls the output directory layout: <br /> `MIRROR_TREE` - (Default) Mirror the directory structure of the input directory, i.e. for an input file `/input/foo/bar.pdf` create an output file `/output/foo/bar.pdf`. <br /> `SINGLE_FOLDER` - Collect all output files in a single flat folder, i.e. for an input file `/input/foo/bar.pdf` create an output file `/output/bar.pdf`.|
|`OCR_PROCESS_EXISTING_ON_START` | Set to `1` to enable processing of any files in the input directory when the container is launched. <br/> Set to `0` (Default) or unset to ignore existing files until they are modified.|
|`OCR_ACTION_ON_SUCCESS` | Controls the action (if any) to perform after successful OCR processing: <br /> `NOTHING` - (Default) Do nothing. Input files remain in place where they were found. <br /> `ARCHIVE_INPUT_FILES` - Archive input files by **moving** them (overwriting existing files!) to the `/archive` Volume <br /> `DELETE_INPUT_FILES` - Delete the input file after successful processing.|
|`OCR_VERBOSITY` | Control the verbosity of debug logging. Accepts python `logging` levels, e.g. `warn` (Default), `info`, `debug`, etc.|
|`USERMAP_UID` | Set the UID that the OCR tools will run as.|
|`USERMAP_GID` | Set the GID that the OCR tools will run as.|

[ocr-languages]: https://packages.ubuntu.com/search?keywords=tesseract-ocr-&searchon=names&suite=bionic&section=all

## OCRmyPDF Configuration Files

`ocrmypdf-auto` supports flexible configuration of the `ocrmypdf` binary itself, by allowing you to specify command line options in text files, one option per line.

Example:
```
# ocrmypdf-auto Config File
#
# The contents of this file are exactly one command-line option per line,
# including the "value" following the option, if any.
#
# Any blank lines or lines BEGINNING with a '#' are ignored

# Common OCRmyPDF options (see ocrmypdf --help for full current list!)

# Deskew the input file before OCR (and rebuild the output file with the skew correction)
--deskew

# Enable automatic page rotation based on tesseract orientation and script detection
--rotate-pages

# Configure automatic rotation threshold (in arbitrary units defined by tesseract)
--rotate-pages-threshold 4
```

Notes:
* The syntax is very simplistic, as described in the default `ocr.config` file that is created when the container is started with a new or empty `/config` Volume.
* It is recommended for consistency of behavior that you specify the `--skip-text` option, which will cause `OCRmyPDF` NOT to consider it an error when an input page already contains text.

### `ocr.config` File Selection

To support flexible configuration of OCRmyPDF from a single container, `ocrmypdf-auto` supports a "hierarchy" of configuration files. For a given input file, `ocrmypdf-auto` will first examine the file's own directory for an `ocr.config` to read. If found, that file (and that file _alone_) will be used to configure OCRmyPDF. If no `ocr.config` is present in the input directory, the parent directory will be searched next, and then the parent's parent, and so on up to the base `/input` Volume. If no `ocr.config` is found at any level of the directory hierarchy, then a final location is examined at `/config/ocr.config`.

Note that only a _single_ `ocr.config` file is used for a given OCRmyPDF invocation, so all desired command-line options must be specified in the corresponding configuration file. (There is no mechanism to "override" configuration from parent directories.)

For example, in this `/input` Volume hierarchy:

```
/input
├── foo
│   ├── bar
│   │   ├── bar.pdf
│   │   └── ocr.config
│   ├── baz
│   │   └── baz.pdf
│   ├── foo.pdf
│   └── ocr.config
├── qux.pdf
└── quux
    └── quuux
        └── quuuux.pdf
```

The following `ocr.config` file selections would be made:

|PDF file|`ocr.config` file|Notes|
|---|---|---|
|`/input/foo/bar/bar.pdf`|`/input/foo/bar/ocr.config`|Same directory as `bar.pdf`|
|`/input/foo/baz/baz.pdf`|`/input/foo/ocr.config`|No config file in `baz`, so parent's is selected|
|`/input/foo/foo.pdf`|`/input/foo/ocr.config`|Same directory as `foo.pdf`|
|`/input/qux.pdf`|`/config/ocr.config`|No config file exists in this directory, which is the `/input` Volume root|
|`/input/quux/quuux/quuuux.pdf`|`/config/ocr.config`|No config file in the same directory nor any parent all the way to the `/input` Volume root|

## unRAID Integration

If you're an [unRAID][unraid] user, you may want to install ocrmypdf-auto through [Community Applications][ca] instead of directly installing this Docker image. The unRAID template will set some default settings that integrate well with unRAID (see below), as well as the latest updates if the container template itself changes over time.

### unRAID-specific Recommended Settings

Notes:
* _I'm using unRAID terminology_ `path` _and_ `variable` _here, for clarity, in place of Docker terminology_ `volume` _and_ `environment variable`.
* _You can also review these settings in the [ocrmypdf-auto template][template] itself_

|Type|Setting|Value|Notes|
|----|-------|-----|-----|
|Path|`/config`|`/mnt/user/appdata/ocrmypdf-auto`|Store configuration files (ocr.config) in standard unRAID appdata location|
|Variable|`OCR_LANGUAGES`|``|Additional languages (besides English) to install. Given as a space separated list. Example: `deu chi-sim ita` |
|Variable|`OCR_OUTPUT_MODE`|`MIRROR_TREE`|Organizes a tree of output files that mirrors the input files. This is a good, non-surprising default for running as a NAS service.|
|Variable|`OCR_ACTION_ON_SUCCESS`|`NOTHING`|By default, don't touch the input files after processing. This is a **safe** default for running as a NAS service, but **Note:** you may wish to customize this behavior once you're comfortable with ocrmypdf-auto operations!|
|Variable|`OCR_PROCESS_EXISTING_ON_START`|`0`|Corresponding with the `OCR_ACTION_ON_SUCCESS`, we explicitly disable processing existing files on startup to avoid reprocessing all files every time the container is updated or unRAID is rebooted.|
|Variable|`USERMAP_UID`|`99`|This is the UID of unRAID's `nobody` user. You should use this value unless you really know what you're doing!|
|Variable|`USERMAP_GID`|`100`|This is the GID of unRAID's `users` group. You should use this value unless you really know what you're doing!|

[unraid]: https://lime-technology.com/
[ca]: https://lime-technology.com/forums/topic/38582-plug-in-community-applications/
[template]: https://github.com/cmccambridge/unraid-templates/blob/master/cmccambridge/ocrmypdf-auto.xml

## Future Work

Some specific future work items I have planned:
* [#2][i2] Add automatic OCR language pack installation (via tesseract language packages) 
* [#3][i3] Build an Alpine-based image for size reduction (requires tesseract v4 to be supported in Alpine for best quality results)

Please also see the [GitHub Issues][issues], where you can report any problems or make any feature requests as well!

[i2]: https://github.com/cmccambridge/ocrmypdf-auto/issues/2
[i3]: https://github.com/cmccambridge/ocrmypdf-auto/issues/3
[issues]: https://github.com/cmccambridge/ocrmypdf-auto/issues/

## Credits

Additional credits for icons in the flow chart:
* Icon made by [Freepik][cr-freepik] from [www.flaticon.com][cr-flaticon]
* Icon made by [Smashicons][cr-smashicons] from [www.flaticon.com][cr-flaticon]

[cr-freepik]: https://www.freepik.com
[cr-smashicons]: https://smashicons.com
[cr-flaticon]: https://www.flaticon.com
