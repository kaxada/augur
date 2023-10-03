import sys
import re
from pathlib import Path

def get_files(path):
    #copied from example on https://docs.python.org/3/library/pathlib.html
    dir = path
    p = Path(dir)
    return list(p.glob('**/*.rs'))

def get_deps_for_file(path):
    #gets imports in specified file path.
    with open(path, 'r') as f:
        content = f.read()
        matches = re.findall(r'use\s+([\w:]+)(\s+as\s+([\w:]+))?(\s*\*\s*)?(;|\n)', content)
        return [m[0] for m in matches]