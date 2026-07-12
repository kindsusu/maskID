"""PyInstaller entry script.

Kept at the project root so the bundled executable imports the ``markid``
package normally.  All logic lives in :mod:`markid.cli`.
"""

import sys

from markid.cli import main

if __name__ == "__main__":
    sys.exit(main())
