#!/usr/bin/env python3
"""Run smolnugget straight from a checkout: `python3 snt.py --help`.

After `pip install .` the same CLI is available as `snt`.
"""

import sys

from smolnugget.cli import main

if __name__ == "__main__":
    sys.exit(main())
