🪒 Adaptive Watermark

Stamps a logo onto a batch of images and automatically picks light or dark colors for each image by measuring the background directly underneath it.
Features

    Smart Sampling: Measures background brightness only where the logo actually sits (not the whole corner).

    Dynamic Glow: Automatically adds a soft shadow on mid-toned or busy backgrounds so thin logos never vanish.

    In-Place Safety Net: Automatically backs up original images into yakshave_data/ before any changes are made. Full --restore support included.

    GUI & Batch Mode: Interactive PySide6 preview window or lightweight headless --batch execution without Qt dependencies.

Requirements

    Python 3.10+

    pillow (required)

    PySide6 (optional, for GUI preview)

Quick Start

    Place your logo.png next to the script.

    Run live preview or batch process:

python watermark.py ./my-images             # Preview window
python watermark.py ./my-images --batch     # Batch mode (no GUI)
python watermark.py ./my-images --restore   # Undo and restore original images

🧡 Support Yak Shave on Patreon

If you find these tools helpful and want to streamline your workflows, consider supporting the project!

    Tier 1 (Scripts & Snippets): Standalone Python tools and updates ("as-is").

    Tier 2 (Applications & Suites): Complete application suites, toolchains, and deep-dive walkthroughs.

👉 Join Patreon: https://www.patreon.com/cw/YakShave
