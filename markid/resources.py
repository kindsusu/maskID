"""Model / asset path resolution.

Single source of truth for locating the bundled OCR model files, working both
in a normal source checkout and inside a PyInstaller one-file bundle
(``sys._MEIPASS``).
"""

import os
import sys


def base_dir():
    """Return the directory that contains the ``models`` folder.

    - Frozen (PyInstaller one-file): ``sys._MEIPASS`` (models added via
      ``--add-data "models;models"``).
    - Source checkout: the project root, i.e. the parent of the ``markid``
      package directory.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return meipass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def models_dir():
    return os.path.join(base_dir(), "models")


def rec_model_path():
    """Path to the Korean recognition ONNX model."""
    return os.path.join(models_dir(), "korean_rec.onnx")


def rec_keys_path():
    """Path to the Korean recognition dictionary (keys) file."""
    return os.path.join(models_dir(), "korean_dict.txt")


def models_available():
    return os.path.isfile(rec_model_path()) and os.path.isfile(rec_keys_path())
