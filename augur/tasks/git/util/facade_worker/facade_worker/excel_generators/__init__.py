#!/usr/bin/python3

import os
import glob

files = glob.glob(f'{os.path.dirname(__file__)}/generate*.py')

__all__ = [os.path.basename(f)[:-3] for f in files if os.path.isfile(f)]

