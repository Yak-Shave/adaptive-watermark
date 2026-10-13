# Adaptive Watermark

Stamps your logo onto a batch of images and picks light or dark for each one by looking at the background under it.

A thin, script-style logo is easy to lose against a busy or mid-toned photo, and a hard outline turns a thin line into a blob. So every image is measured on its own, only where the logo will sit. The brightness under the logo's shape decides the stamp colour, and where contrast is marginal a soft glow in the opposite colour goes behind the stroke. The stroke itself is never outlined or thickened.

## Requirements

- Python 3.10 or newer
- `pillow` (required)
- `PySide6` (optional, only for the preview window; `--batch` works without it)

Runs on Windows, macOS and Linux.

## Setup

```bash
pip install pillow PySide6
```

Leave out `PySide6` if you only plan to use `--batch`.

Put your logo next to `watermark.py` and name it `logo.png` (`.webp`, `.jpg` and `.bmp` also work). A PNG with a transparent background gives the best result.

## Usage

```bash
python watermark.py ./my-images             # preview window: pick corner, size, opacity
python watermark.py ./my-images --batch     # stamp the whole folder, no window
python watermark.py ./my-images --dry-run   # list what would be stamped, write nothing
python watermark.py ./my-images --restore   # put the original files back
```

| Argument | What it does |
|---|---|
| `folder` | Folder to work on |
| `--subfolders` | Include subfolders |
| `--batch` | Stamp everything and exit, without opening a window |
| `--restore` | Copy the backed-up originals back, and exit |
| `--logo PATH` | Logo image (default: `logo.png` next to the script) |
| `--anchor ...` | `top-left` to `bottom-right`, nine positions (default: `bottom-left`) |
| `--size N` | Logo width in percent of the image width (default: 12) |
| `--margin N` | Distance from the edge in percent of the image width (default: 2) |
| `--opacity N` | Stamp opacity in percent (default: 85) |
| `--color ...` | `auto` (measure the background), `light`, `dark`, or a grey value 0-255 |
| `--dry-run` | List what would be stamped without writing anything |

## Notes

- Images are stamped in place. Before the first stamp, every original is copied into `yakshave_data/` next to the script, keeping its subfolder structure, so `--restore` puts each file back where it came from. An existing backup is never overwritten.
- The tool does not notice an image that is already stamped. Run it twice and the logo goes on twice. Restore first, then stamp again with new settings.
- PNGs with transparency come back without it. JPEGs and WebPs are re-saved after stamping (JPEG at quality 95), so expect the usual small generation loss.
- Batch mode prints one line per image as it goes, so you can watch progress live.

## Yak Shave Launcher

Drop `watermark.py` into a folder the [Yak Shave Launcher](https://github.com/Yak-Shave/yakshave-launcher) scans. The header at the top of the file is read automatically and the tool shows up as a card in the Images category.

## About Yak Shave

Lightweight, no-bullshit CLI tools and Python scripts for power users. Every tool is a single file. New tools appear on [Patreon](https://www.patreon.com/cw/YakShave) first and reach GitHub a little later.

## License

MIT
