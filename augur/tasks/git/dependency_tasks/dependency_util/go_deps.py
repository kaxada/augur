import sys, re
from pathlib import Path

def get_files(path):
    dir = path
    p = Path(dir)
    return list(p.glob('**/*.go'))

def get_deps_for_file(path):
    with open(path, 'r') as f:
        if not (matches := re.findall('import\s+\(([\s\S]*?)\)', f.read())):
            return matches if (matches := re.findall('import\s+"(\w+)"', f.read())) else []
        imports = []
        for m in matches:
            imports += re.findall('(\w+)', m)
        return imports

